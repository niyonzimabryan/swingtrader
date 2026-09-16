#!/usr/bin/env python3
"""
Swing Trader — Main Entry Point
Starts the trading runtime: the scheduled pipeline, the monitors, and — unless
`TELEGRAM_ENABLED=false` — the Telegram bot.

**Headless mode.** With `TELEGRAM_ENABLED=false` this is a plain asyncio program
with no Telegram in it at all: no token is required, no `Application`,
`MessageQueue` or `SwingTraderBot` is built, and `bot.telegram_bot` is not even
imported. Everything else runs unchanged — the pipeline and its startup
reconciliation, the scheduler (scans behind `SCHEDULER_ENABLED`) with the same
daily pre-market self-restart, the order and position monitors and the watchdog,
the daily digest and the weekly report, Phase 6's execution services, and the
owner approval poller. Human-facing messages go out through `notify/` (email);
the owner's controls are the MCP owner tools rather than `/live_kill`, which is
why `OWNER_ID` stops being optional there (Spec K §10).
"""

import asyncio
import os
import signal
import sys

# The workspace service is built from this same repository and image, and
# Railway applies the repo's `railway.toml` start command (`python main.py`) to
# every service it builds from it. The workspace service sets
# SERVICE_ROLE=workspace; it must never start the bot — Telegram allows one
# polling connection — so hand off here, before a single bot module is imported.
# It replaces the process rather than importing the workspace: Spec K §4 keeps
# the bot's import closure free of `workspace` (tests/test_no_execute_scope.py),
# so a workspace change can never restart or alter the trading monitor.
if os.environ.get("SERVICE_ROLE", "").strip().lower() == "workspace":
    os.execv(sys.executable, [sys.executable, "-m", "workspace.server", *sys.argv[1:]])

from config.settings import Settings
from database.db import init_db
from database.schema import SchemaMismatch
from orchestrator.pipeline import TradingPipeline
from orchestrator.runtime import RuntimeContainer
from orchestrator.scheduler import PipelineScheduler
from orchestrator.universe import seed_universe
from bot.notifications import NotificationManager
from execution.order_monitor import OrderMonitor
from execution.position_monitor import PositionMonitor
from bot.daily_digest import DailyDigest
from bot.weekly_report import WeeklyReport
from tracking.position_reconciliation import reconcile_broker_positions
from portfolio import paging
from research_workspace import paging as research_paging
from utils import billing_alerts
from utils.async_call import call_with_timeout
from utils.lifecycle import MonitorWatchdog
from utils.logger import setup_logging, get_logger


def _init_langfuse(settings):
    """Initialize Langfuse OTEL auto-instrumentation if keys are configured."""
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return None
    try:
        import os
        import base64

        # Langfuse SDK client env vars
        os.environ["LANGFUSE_PUBLIC_KEY"] = settings.langfuse_public_key
        os.environ["LANGFUSE_SECRET_KEY"] = settings.langfuse_secret_key
        os.environ["LANGFUSE_BASE_URL"] = settings.langfuse_base_url

        # OTEL exporter env vars (required separately for span export)
        base_url = settings.langfuse_base_url.rstrip("/")
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"{base_url}/api/public/otel"
        auth_string = base64.b64encode(
            f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
        ).decode()
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Basic {auth_string}"

        # CRITICAL: get_client() MUST be called BEFORE instrument()
        # get_client() sets up the TracerProvider with the correct HTTP exporter.
        # If instrument() runs first, spans go to the default gRPC exporter
        # which Langfuse rejects with 401.
        from langfuse import get_client
        client = get_client()
        client.auth_check()  # Fail fast if creds are wrong

        from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor
        AnthropicInstrumentor().instrument()
        return client
    except ImportError as e:
        print(f"[langfuse] ImportError: {e}")
        return None
    except Exception as e:
        print(f"[langfuse] Init failed: {type(e).__name__}: {e}")
        return None


async def _reconcile_startup_positions(pipeline, settings, log, *, pager=None):
    """Backfill DB trade rows for any broker positions already open at startup.

    **Async, and the broker call goes off-loop.** `get_positions_detail()` is
    synchronous for every adapter, and the Robinhood one reaches
    `RobinhoodMCPBroker._call_tool_sync`, which calls `asyncio.run()`. Called
    directly from `main()`'s running loop that is illegal, so every boot since
    `BROKER_PRIMARY=robinhood` raised "asyncio.run() cannot be called from a
    running event loop" and the process started with no view of the account.
    `call_with_timeout` is the idiom both monitors already use for exactly this
    (`utils/async_call.py`), and it adds the bound the bare call never had: a
    broker that hangs here would otherwise hang the boot before the monitors
    ever start.

    **The failure is loud, not fatal.** Refusing to boot would take the monitors
    and the approval poller down with it — the two things that watch an open
    live position and act on the owner's decisions — and Railway's ON_FAILURE
    policy would turn one unreachable broker into a crash loop. So this pages
    instead: `portfolio.paging.STARTUP_RECONCILIATION_FAILED` on whatever
    channel the deployment configured, an `error` log line rather than a
    `warning`, and a banner on stdout. The propose path is what actually keeps
    an unseen book from sizing a trade — `check_ledger_fresh` refuses on a
    never-synced ledger (`portfolio/freshness.py`) — and that guard does not
    need this function to have succeeded.
    """
    if pager is None:
        from portfolio.paging import pager_for

        pager = pager_for(settings)

    timeout_s = float(getattr(settings, "monitor_broker_call_timeout_s", 30) or 30)
    execution_mode_setting = str(getattr(settings, "execution_mode", "paper")).lower()

    brokers = []
    paper = getattr(pipeline, "paper_broker", None)
    active = getattr(getattr(pipeline, "broker", None), "active", None)
    for broker, execution_mode in (
        (paper, "paper"),
        (active, execution_mode_setting),
    ):
        if broker and all(id(broker) != id(existing[0]) for existing in brokers):
            brokers.append((broker, execution_mode))

    for broker, execution_mode in brokers:
        broker_name = getattr(broker, "name", "alpaca")
        broker_account_id = getattr(broker, "account_number", "") or None
        try:
            positions = await call_with_timeout(
                broker.get_positions_detail, timeout_s=timeout_s
            )
            result = reconcile_broker_positions(
                positions,
                broker_name=broker_name,
                broker_account_id=broker_account_id,
                execution_mode=execution_mode,
                source="startup",
            )
            if result["created"] or result["updated"]:
                log.info("startup_positions_reconciled", broker=broker_name, **result)
        except Exception as e:
            error = (
                f"timed out after {timeout_s:.0f}s"
                if isinstance(e, asyncio.TimeoutError)
                else f"{type(e).__name__}: {e}"
            )
            detail = {
                "broker": broker_name,
                "execution_mode": execution_mode,
                "error": error,
                "detail": (
                    "the process started with no view of this broker's open "
                    "positions. Unseen exposure reads as no exposure to anything "
                    "that does not check ledger freshness."
                ),
                "recovery": (
                    "Run `python -m scripts.portfolio_sync --dry-run` to see the "
                    "underlying broker error, then restart the service. Until a "
                    "sync completes, treat the ledger's exposure figures as "
                    "unknown rather than as zero."
                ),
            }
            log.error(paging.STARTUP_RECONCILIATION_FAILED, **detail)
            try:
                pager(paging.STARTUP_RECONCILIATION_FAILED, detail)
            except Exception as page_error:  # pragma: no cover - channel-specific
                log.error(
                    "startup_reconciliation_page_failed",
                    broker=broker_name,
                    error=str(page_error),
                )
            # stdout as well as the structured log: a deploy is watched in the
            # Railway log pane, and "live, and blind to the account" is not a
            # line that should have to be grepped for.
            print(
                f"\n⚠️  startup position reconciliation FAILED for {broker_name} "
                f"(execution_mode={execution_mode}): {error}"
                f"\n   {detail['detail']}"
                f"\n   {detail['recovery']}\n"
            )


def _channel_names(settings, telegram_enabled: bool) -> list[str]:
    """Every channel a human-facing message can travel on, in this mode.

    Telegram is reported from the *flag*, not from whether its credentials are
    present: the switch-over leaves `TELEGRAM_BOT_TOKEN` set on purpose, so it
    stays reversible, and a startup line that named a channel the process will
    never call would be worse than no line at all.
    """
    try:
        from notify.registry import configured_channels

        names = [
            str(getattr(channel, "name", "") or "")
            for channel in configured_channels(settings)
        ]
    except Exception:  # pragma: no cover - a channel that cannot even be built
        names = []
    if telegram_enabled:
        # The bot delivers through its own in-process queue, which is not a
        # `notify` channel; it is a channel to a human all the same.
        return ["telegram"] + [name for name in names if name and name != "telegram"]
    return [name for name in names if name and name != "telegram"]


async def main():
    # Setup logging
    setup_logging("INFO")
    log = get_logger("main")
    log.info("swing_trader_starting")

    # Load settings
    settings = Settings()

    # E6: log which Gemini model each stage resolved to, so per-stage skew
    # (e.g. a stale preview pin on one stage) is visible in Railway logs.
    log.info(
        "gemini_models_resolved",
        web_search_provider=settings.web_search_provider,
        search=settings.gemini_search_model,
        discovery=settings.gemini_discovery_model,
        web_research=settings.gemini_web_research_model,
        flash_screen=settings.gemini_flash_model,
    )

    # Initialize Langfuse observability (no-op if keys not set)
    langfuse_client = _init_langfuse(settings)
    if langfuse_client:
        log.info("langfuse_initialized", host=settings.langfuse_base_url)

    # Which runtime this is. Default true, so a deployment that sets nothing
    # gets exactly what it got before this flag existed.
    telegram_enabled = bool(getattr(settings, "telegram_enabled", True))

    # Validate critical keys. The Telegram pair is required only when Telegram
    # is the channel: headless there is no polling connection to open, and
    # demanding a token for a process that will never call the API is how a
    # deployment ends up carrying a credential it does not use.
    missing = []
    if not settings.anthropic_api_key:
        missing.append("ANTHROPIC_API_KEY")
    if telegram_enabled:
        if not settings.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not settings.telegram_chat_id:
            missing.append("TELEGRAM_CHAT_ID")
    if missing:
        log.error("missing_api_keys", keys=missing, telegram_enabled=telegram_enabled)
        print(f"\n❌ Missing required API keys: {', '.join(missing)}")
        print("Copy .env.example to .env and fill in your keys.")
        print("See .env.example for registration links.\n")
        sys.exit(1)

    # Headless + Phase 6 needs an explicit owner. `resolve_owner_id` falls back
    # to TELEGRAM_CHAT_ID (Spec K §10), so with Telegram off and OWNER_ID unset
    # every approval would be minted against the empty string and every one of
    # them would be bound to nobody. Refuse to start rather than run a lifecycle
    # whose owner binding is vacuous.
    if not telegram_enabled and getattr(settings, "phase6_execution_enabled", False):
        from portfolio.approvals import resolve_owner_id as _check_owner_id

        if not _check_owner_id(settings):
            log.error(
                "owner_id_required_headless",
                detail=(
                    "TELEGRAM_ENABLED=false with PHASE6_EXECUTION_ENABLED=true, but no "
                    "owner id resolves: OWNER_ID is unset and its fallback, "
                    "TELEGRAM_CHAT_ID, is unset too. Every approval is bound to an "
                    "owner, so set OWNER_ID. See docs/ENV_SETUP.md 'Headless'."
                ),
            )
            print(
                "\n❌ OWNER_ID is required when TELEGRAM_ENABLED=false and Phase 6 is on."
                "\n   Approvals are owner-bound and OWNER_ID's only fallback is "
                "TELEGRAM_CHAT_ID.\n   See docs/ENV_SETUP.md 'Headless'.\n"
            )
            sys.exit(1)

    # One line naming the mode and every channel a human-facing message can
    # travel on. "Running, but nobody is being told anything" is the failure
    # this exists to make visible on the first line of a deploy log.
    channel_names = _channel_names(settings, telegram_enabled)
    log.info(
        "runtime_mode",
        telegram=telegram_enabled,
        mode="telegram" if telegram_enabled else "headless",
        channels=",".join(channel_names) or "none",
    )
    if not channel_names:
        log.warning(
            "no_human_channel",
            detail=(
                "Telegram is off and no notify/ channel is configured, so nothing "
                "reaches a human: every notification falls back to the structured "
                "log (portfolio.paging.log_pager). Set NOTIFY_EMAIL_ENABLED=true "
                "with RESEND_API_KEY, PAGER_EMAIL_FROM and PAGER_EMAIL_TO."
            ),
        )

    # Initialize database. Migrations run here, before any ORM session exists;
    # a schema Alembic cannot safely adopt stops the process with instructions
    # rather than crash-looping on a stack trace under the restart policy.
    try:
        init_db(settings.database_url)
    except SchemaMismatch as exc:
        log.error("schema_mismatch", detail=str(exc))
        print(f"\n❌ Database schema cannot be adopted automatically.\n\n{exc}\n")
        sys.exit(1)
    log.info("database_initialized")

    # Seed ticker universe
    seed_universe()

    # Initialize pipeline
    pipeline = TradingPipeline(settings)
    log.info("pipeline_ready")
    await _reconcile_startup_positions(pipeline, settings, log)

    # The process-level shelf for the services more than one caller needs.
    # `app.bot_data` was that shelf; it is a view of this now (orchestrator/runtime.py).
    runtime = RuntimeContainer()

    # Initialize Telegram bot — or not. Headless, nothing under `bot.telegram_bot`
    # is even imported, so no `Application`, no `Bot`, and no polling connection.
    bot = None
    app = None
    mq = None
    if telegram_enabled:
        from bot.telegram_bot import SwingTraderBot
        from bot.message_queue import MessageQueue

        bot = SwingTraderBot(settings, pipeline)
        app = bot.build()

        # Initialize message queue and notifications
        mq = MessageQueue(app.bot)
        notifications = NotificationManager(mq, settings.telegram_chat_id, settings)
    else:
        # Same manager, a `notify/` sink instead of the queue. Everything that
        # holds a NotificationManager — the pipeline, the monitors, the digest,
        # the watchdog, billing_alerts, research_paging — is unchanged.
        notifications = NotificationManager(settings=settings)
    pipeline.notification_manager = notifications
    pipeline.bot_loop = asyncio.get_running_loop()  # For deep research async scheduling
    billing_alerts.register(notifications, pipeline.bot_loop)
    # Spec M §4: a triggered thesis invalidator pages through the same
    # channel. Registering is harmless with RESEARCH_WORKSPACE_ENABLED
    # false — nothing calls the pager until the check job runs.
    research_paging.register(notifications, pipeline.bot_loop)

    # Phase 6 (Spec L §6): the proposal -> approval -> execution lifecycle.
    # Wired only when PHASE6_EXECUTION_ENABLED; with the flag off the proposal
    # tool is not even registered on the workspace and no callback is accepted.
    # The card sender posts through the existing message queue, or by email when
    # Telegram is off; the execution service is the ONLY path from an approval to
    # a placement either way, and it lives in execution/ where the workspace can
    # never import it.
    approval_poller = None
    if getattr(settings, "phase6_execution_enabled", False):
        from execution.lifecycle import ExecutionService
        from portfolio.approvals import resolve_owner_id as _resolve_owner_id

        from database.db import get_session as _get_session

        if telegram_enabled:
            from bot.handlers.proposals import register_bot_card_sender

            register_bot_card_sender(mq, settings.telegram_chat_id, pipeline.bot_loop, settings)
        else:
            # The email card is the only proposal channel headless, and it says
            # so: the closing note names the `approve_order` MCP owner tool and
            # prints the proposal uid it takes. The card still cannot approve
            # anything — it never could — and the owner tool only *records* the
            # decision for the poller below to act on.
            from notify.approval import register_email_card_sender

            register_email_card_sender(settings, approval_route="mcp")

        # Headless, a page is a page card on the configured channels — the same
        # renderer `portfolio.paging` already uses, with the recovery text
        # verbatim, rather than a system_message routed through a queue that
        # does not exist. `pager_for` returns the log-only default when nothing
        # is configured, which is the correct behaviour and not a failure.
        _headless_pager = None
        if not telegram_enabled:
            from notify.registry import non_telegram_channels
            from portfolio.paging import pager_for as _pager_for

            _headless_pager = _pager_for(
                settings, channels=non_telegram_channels(settings)
            )

        def _pager(event, detail):
            # A protection failure or an unknown placement must reach the owner
            # on the same channel everything else pages on. The recovery text is
            # in `detail`; system_message carries it verbatim.
            detail = detail or {}
            if _headless_pager is not None:
                try:
                    _headless_pager(event, detail)
                except Exception as exc:  # pragma: no cover - channel-specific
                    log.warning("phase6_page_failed", event=event, error=str(exc))
                return
            recovery = detail.get("recovery", "")
            # A Strategy Lab page names its execution, a Phase 6 one its
            # proposal. Same channel, same recovery text, and the line says which
            # row to go and look at rather than printing "?" for the other kind.
            subject = (
                f"execution {str(detail.get('execution_id'))[:12]}"
                if detail.get("execution_id")
                else f"proposal {detail.get('proposal_id', '?')}"
            )
            message = f"⚠️ {event}: {subject} {detail.get('ticker', '')}\n{recovery}"
            try:
                asyncio.run_coroutine_threadsafe(
                    notifications.system_message(message),
                    pipeline.bot_loop,
                )
            except Exception as exc:  # pragma: no cover - loop-specific
                log.warning("phase6_page_failed", event=event, error=str(exc))

        runtime.execution_service = ExecutionService(
            session_factory=_get_session,
            broker=pipeline.broker,
            settings=settings,
            pager=_pager,
        )
        log.info("phase6_execution_wired", execution_mode=getattr(settings, "execution_mode", "paper"))

        # Strategy Lab arms (Spec Q §12 invariant 11, PR 6). A SECOND service,
        # deliberately, and the difference is the whole invariant: the one above
        # is bound to `pipeline.broker`, the router that follows the global
        # EXECUTION_MODE, while this one is given a venue -> adapter MAP and the
        # arm's own immutable mode selects from it. A paper arm therefore reaches
        # Alpaca paper when EXECUTION_MODE=live and BROKER_PRIMARY=robinhood, and
        # `bind_adapter` refuses any other pairing before broker review.
        #
        # The live venue is registered only when the primary broker declares
        # itself to be that venue. Registering the router would reintroduce the
        # global-mode inference this service exists to remove.
        from strategy_lab.execution import LIVE_VENUE, PAPER_VENUE

        _adapters = {PAPER_VENUE: pipeline.paper_broker}
        if str(getattr(pipeline.primary_broker, "venue", "") or "").lower() == LIVE_VENUE:
            _adapters[LIVE_VENUE] = pipeline.primary_broker

        from execution.strategy_lifecycle import StrategyExecutionService

        runtime.strategy_lab_adapters = _adapters
        runtime.strategy_execution_service = StrategyExecutionService(
            session_factory=_get_session,
            settings=settings,
            adapters=_adapters,
            pager=_pager,
            owner_id=_resolve_owner_id(settings),
        )
        log.info(
            "strategy_lab_execution_wired",
            venues=sorted(_adapters),
            paper_enabled=bool(getattr(settings, "strategy_lab_paper_enabled", False)),
            live_enabled=bool(getattr(settings, "strategy_lab_live_enabled", False)),
        )

        # The owner control surface's runtime half (Spec K §10). The MCP tools
        # RECORD a decision into `owner_actions`; this is the only thing that
        # acts on one, and it acts by calling the same services the Telegram
        # callback calls, with the same arguments. It is behind its own flag as
        # well as Phase 6's: PHASE6_EXECUTION_ENABLED is already on in
        # production, and turning a second path to placement on as a side effect
        # of a deploy is exactly what "every new capability ships behind a flag
        # defaulting off" exists to prevent.
        if getattr(settings, "owner_action_poller_enabled", False):
            from orchestrator.approval_poller import ApprovalPoller

            def _memo_executor(memo_id: int) -> dict:
                # `execute_approved_trade` is a coroutine on the bot loop; the
                # poller runs its pass on a worker thread. Bridge it the same way
                # the card sender bridges the message queue, and wait: the
                # poller's claim is what makes waiting safe.
                future = asyncio.run_coroutine_threadsafe(
                    pipeline.order_manager.execute_approved_trade(memo_id),
                    pipeline.bot_loop,
                )
                return future.result(timeout=120)

            approval_poller = ApprovalPoller(
                session_factory=_get_session,
                settings=settings,
                execution_service=runtime.execution_service,
                strategy_execution_service=runtime.strategy_execution_service,
                memo_executor=_memo_executor,
                adapters=_adapters,
                notify=_pager,
                instance_id=os.environ.get("RAILWAY_REPLICA_ID", "") or "",
            )
            log.info(
                "approval_poller_wired",
                interval_seconds=getattr(settings, "owner_action_poll_seconds", 20),
            )

    # Telegram's handlers still read `context.bot_data["execution_service"]` and
    # friends by name, so the container is copied in here — which is why not one
    # file under `bot/handlers/` changed for this. The container itself goes in
    # too, under "runtime", for anything that ever wants the live object.
    if app is not None:
        app.bot_data.update(runtime.as_bot_data())

    # Initialize order monitor — always bound to the Alpaca broker, never the
    # mode-sensitive router. These monitors manage Alpaca order lifecycles only;
    # Robinhood live trades are managed via callbacks/manual close. Binding to
    # the router would let a /mode switch re-point an in-flight position's
    # monitor at the wrong broker.
    order_monitor = OrderMonitor(pipeline.paper_broker, notifications, settings)

    # Initialize position monitor (60-sec live price checks during market hours)
    position_monitor = PositionMonitor(pipeline.paper_broker, notifications, settings)

    # Reliability watchdog: exits(1) if a monitor loop stalls during market hours
    # so Railway's ON_FAILURE policy restarts a fresh container (Spec B1).
    watchdog = MonitorWatchdog([order_monitor, position_monitor], notifications, settings)

    # Initialize daily digest (5 PM ET, math only — no AI)
    daily_digest = DailyDigest(pipeline.broker, notifications, settings)

    # Initialize weekly report (Sunday 6 PM ET, Sonnet narrative — ~$0.03/week)
    weekly_report = WeeklyReport(pipeline.broker, notifications, settings)

    # Initialize scheduler (skip scans if SCHEDULER_ENABLED=false to save API
    # credits — but the daily pre-market self-restart still runs regardless).
    #
    # `os` is imported at module scope. A second `import os` used to sit on this
    # line, and because a function-level import binds the name *locally for the
    # whole function*, it made every earlier `os.environ` read in `main()` an
    # UnboundLocalError — including the one that gives the approval poller its
    # RAILWAY_REPLICA_ID, so OWNER_ACTION_POLLER_ENABLED=true crashed the
    # process at startup. `tests/test_headless_runtime.py` is what caught it.
    scheduler_enabled = os.getenv("SCHEDULER_ENABLED", "true").lower() not in ("false", "0", "no")
    scheduler = PipelineScheduler(pipeline, settings)

    async def _graceful_restart_cleanup():
        """Best-effort cleanup before the daily self-restart releases the container."""
        log.info("daily_restart_cleanup_start")
        if approval_poller is not None:
            try:
                await approval_poller.stop()
            except Exception as e:
                log.warning("daily_restart_cleanup_poller_failed", error=str(e))
        for name, monitor in (("position_monitor", position_monitor), ("order_monitor", order_monitor)):
            try:
                await monitor.stop()
            except Exception as e:
                log.warning("daily_restart_cleanup_monitor_failed", monitor=name, error=str(e))
        try:
            await watchdog.stop()
        except Exception as e:
            log.warning("daily_restart_cleanup_watchdog_failed", error=str(e))
        if bot is not None:
            try:
                await bot.stop()
            except Exception as e:
                log.warning("daily_restart_cleanup_bot_failed", error=str(e))
        if langfuse_client:
            try:
                langfuse_client.flush()
            except Exception as e:
                log.warning("daily_restart_cleanup_langfuse_failed", error=str(e))

    scheduler.set_restart_callback(_graceful_restart_cleanup)
    if scheduler_enabled:
        scheduler.set_daily_digest(daily_digest)
        scheduler.set_weekly_report(weekly_report)
    scheduler.start(enable_scans=scheduler_enabled)
    log.info("scheduler_ready", scans_enabled=scheduler_enabled)

    # Start bot
    if telegram_enabled:
        log.info("starting_telegram_bot")
    else:
        log.info("starting_headless_runtime", channels=",".join(channel_names) or "none")
    print("\n✅ Swing Trader is running!")
    if telegram_enabled:
        print(f"   Telegram bot active — send /help to your bot")
    else:
        print(f"   Headless (TELEGRAM_ENABLED=false) — no Telegram connection")
        print(f"   Notifications: {', '.join(channel_names) or 'none (log only)'}")
        print(f"   Owner controls: the MCP owner tools, not /live_kill")
    if scheduler_enabled:
        print(f"   Scheduler: 3 daily scans at {settings.pre_market_hour}:00, {settings.midday_hour}:00, {settings.post_market_hour}:00 ET")
        print(f"   Daily digest: 5:00 PM ET (weekdays)")
        print(f"   Weekly report: Sunday 6:00 PM ET (Sonnet)")
    else:
        print(f"   ⏸ Scheduler PAUSED (set SCHEDULER_ENABLED=true to resume)")
    print(f"   Order monitor: polling every 30s")
    print(f"   Position monitor: polling every 60s (market hours only)")
    from config.tickers import UNIVERSE
    print(f"   Universe: {len(UNIVERSE)} tickers")
    print(f"   Press Ctrl+C to stop\n")

    try:
        if bot is not None:
            await bot.start()

        # Start order monitor (runs as async background task)
        await order_monitor.start()
        log.info("order_monitor_started")

        # Start position monitor (60-sec price checks during market hours)
        await position_monitor.start()
        log.info("position_monitor_started")

        # Start reliability watchdog (restarts the process on a stuck monitor)
        await watchdog.start()
        log.info("watchdog_ready")

        # Start the owner-decision poller (no-op unless both flags are on)
        if approval_poller is not None:
            await approval_poller.start()
            log.info("approval_poller_ready")

        # Keep running
        stop_event = asyncio.Event()

        def handle_signal(sig, frame):
            stop_event.set()

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("shutting_down")
        if langfuse_client:
            langfuse_client.flush()
        if approval_poller is not None:
            await approval_poller.stop()
        await watchdog.stop()
        await position_monitor.stop()
        await order_monitor.stop()
        scheduler.stop()
        if bot is not None:
            await bot.stop()
        log.info("swing_trader_stopped")


if __name__ == "__main__":
    asyncio.run(main())
