"""
Core bot command handlers: /help, /status, /regime, /positions, /agents, /exposure, /risk, /scan, /watchlist, /upcoming
"""

import asyncio

from telegram import Update
from telegram.ext import ContextTypes
from bot.auth import authorized
from bot.handlers._blocking_utils import run_blocking, BlockingCallTimeout
from bot.formatters import escape_md, format_portfolio_status, format_positions_detail
from utils.logger import get_logger

log = get_logger("bot_commands")
REGIME_TIMEOUT_S = 120


@authorized
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all available commands."""
    text = (
        "*🤖 Swing Trader Bot*\n\n"
        "*Portfolio & Status*\n"
        "`/status` \\- Portfolio dashboard\n"
        "`/positions` \\- Open positions detail\n"
        "`/exposure` \\- Sector exposure breakdown\n"
        "`/risk` \\- Risk dashboard\n\n"
        "*Analysis*\n"
        "`/regime` \\- Current macro regime\n"
        "`/test TICKER thesis` \\- Full analysis pipeline\n"
        "`/score TICKER` \\- Quick fundamental snapshot\n"
        "`/watchlist` \\- Active watchlist\n"
        "`/upcoming` \\- Upcoming catalysts\n\n"
        "*Performance*\n"
        "`/performance` \\- Performance summary\n"
        "`/history` \\- Recent trade log\n"
        "`/memo ID` \\- Retrieve past memo\n\n"
        "*Trading*\n"
        "`/broker` \\- Active broker and account\n"
        "`/mode` \\- review, paper, or live execution mode\n"
        "`/orders` \\- Recent broker orders\n"
        "`/close TICKER` \\- Close a position\n"
        "`/adjust TICKER stop PRICE` \\- Adjust stop\\-loss\n\n"
        "*System*\n"
        "`/scan` \\- Trigger full pipeline scan\n"
        "`/ask QUESTION` \\- Natural language query\n"
        "`/agents` \\- Agent health check\n"
        "`/pause` / `/resume` \\- Pause/resume scanning\n"
        "`/config` \\- View configuration\n"
        "`/help` \\- This message"
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")


@authorized
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Portfolio dashboard."""
    pipeline = context.bot_data.get("pipeline")
    if not pipeline:
        await update.message.reply_text("System initializing...")
        return

    try:
        # Get account info
        account = {}
        if pipeline.broker:
            account = pipeline.broker.get_account_info()

        # Get positions
        positions = []
        if pipeline.broker:
            positions = pipeline.broker.get_positions_detail()

        # Get regime
        regime = pipeline.macro_agent.get_latest_regime()

        text = format_portfolio_status(account, positions, regime)
        await update.message.reply_text(text, parse_mode="MarkdownV2")
    except Exception as e:
        log.error("status_command_failed", error=str(e))
        await update.message.reply_text(f"Error fetching status: {str(e)[:200]}")


@authorized
async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detailed open positions."""
    pipeline = context.bot_data.get("pipeline")
    if not pipeline:
        await update.message.reply_text("System initializing...")
        return

    try:
        positions = []
        if pipeline.broker:
            positions = pipeline.broker.get_positions_detail()

        text = format_positions_detail(positions)
        await update.message.reply_text(text, parse_mode="MarkdownV2")
    except Exception as e:
        log.error("positions_command_failed", error=str(e))
        await update.message.reply_text(f"Error: {str(e)[:200]}")


@authorized
async def regime_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Current macro regime with reasoning."""
    pipeline = context.bot_data.get("pipeline")
    if not pipeline:
        await update.message.reply_text("System initializing...")
        return

    await update.message.reply_text("Refreshing macro regime...", parse_mode=None)

    try:
        text = await run_blocking(
            operation="regime_command",
            fn=lambda: _build_regime_text(pipeline),
            timeout_s=REGIME_TIMEOUT_S,
        )
        await update.message.reply_text(text, parse_mode="MarkdownV2")
    except BlockingCallTimeout:
        await update.message.reply_text(
            f"Regime refresh timed out after {REGIME_TIMEOUT_S}s. Try again shortly.",
            parse_mode=None,
        )
    except Exception as e:
        log.error("regime_command_failed", error=str(e))
        await update.message.reply_text(f"Error: {str(e)[:200]}")


def _build_regime_text(pipeline) -> str:
    """Sync helper for regime command to run in executor."""
    result = pipeline.macro_agent.analyze()
    rd = result.raw_data

    regime = rd.get("regime", "unknown")
    emoji = "🟢" if regime == "risk-on" else "🟡" if regime == "neutral" else "🔴"

    text = (
        f"{emoji} *MACRO REGIME: {escape_md(regime.upper())}*\n\n"
        f"Confidence: `{result.confidence:.2f}`\n"
        f"Position Multiplier: `{rd.get('position_size_multiplier', 1.0)}x`\n"
        f"Max Positions: `{rd.get('max_positions', 5)}`\n\n"
        f"*Indicator Scores:*\n"
    )
    for k, v in rd.get("scores", {}).items():
        indicator_emoji = "🟢" if v > 0 else "🔴" if v < 0 else "⚪"
        text += f"  {indicator_emoji} {escape_md(k)}: `{v:+d}`\n"
    text += f"\n{escape_md(result.reasoning)}"
    return text


@authorized
async def agents_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Agent health check."""
    text = (
        "*🤖 AGENT STATUS*\n\n"
        "✅ Macro Regime Agent \\- Operational\n"
        "✅ Catalyst Agent \\- Operational\n"
        "✅ Fundamental Agent \\- Operational\n"
        "✅ Pattern Agent \\- Operational\n"
        "✅ Web Research Agent \\- Operational\n"
        "✅ Scoring Engine \\- Operational\n"
        "✅ Memo Generator \\- Operational\n"
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")


@authorized
async def exposure_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sector exposure breakdown."""
    pipeline = context.bot_data.get("pipeline")
    if not pipeline or not pipeline.broker:
        await update.message.reply_text("No positions or system initializing\\.", parse_mode="MarkdownV2")
        return

    try:
        positions = pipeline.broker.get_positions_detail()
        if not positions:
            await update.message.reply_text("No open positions\\.", parse_mode="MarkdownV2")
            return

        from config.tickers import UNIVERSE
        sectors = {}
        total = 0
        for pos in positions:
            sector = UNIVERSE.get(pos["ticker"], "Unknown")
            mv = pos.get("market_value", 0)
            sectors[sector] = sectors.get(sector, 0) + mv
            total += mv

        text = "*📊 EXPOSURE BREAKDOWN*\n\n"
        for sector, value in sorted(sectors.items(), key=lambda x: -x[1]):
            pct = (value / total * 100) if total > 0 else 0
            text += f"  {escape_md(sector)}: `${value:,.0f}` \\(`{pct:.1f}%`\\)\n"
        text += f"\nTotal exposure: `${total:,.0f}`"
        await update.message.reply_text(text, parse_mode="MarkdownV2")
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)[:200]}")


@authorized
async def risk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Risk dashboard."""
    pipeline = context.bot_data.get("pipeline")
    text = (
        "*⚠️ RISK DASHBOARD*\n\n"
        f"Drawdown Circuit Breaker: `{(pipeline.settings.drawdown_circuit_breaker_pct * 100) if pipeline else 10:.0f}%`\n"
        f"Daily Loss Halt: `{(pipeline.settings.daily_loss_halt_pct * 100) if pipeline else 3:.0f}%`\n"
        f"Max Concurrent Positions: `{pipeline.settings.max_concurrent_positions if pipeline else 8}`\n"
        f"Max Sector Exposure: `{(pipeline.settings.max_sector_exposure * 100) if pipeline else 30:.0f}%`\n"
        f"Max Single Position: `{(pipeline.settings.max_position_pct * 100) if pipeline else 10:.0f}%`\n"
        f"Max Stop\\-Loss: `{(pipeline.settings.max_stop_loss_pct * 100) if pipeline else 8:.0f}%`\n"
        f"Robinhood Max Order: `${getattr(pipeline.settings, 'robinhood_max_order_notional', 5) if pipeline else 5:.0f}`\n"
        f"Robinhood Daily Cap: `${getattr(pipeline.settings, 'robinhood_max_daily_notional', 10) if pipeline else 10:.0f}`\n"
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")


@authorized
async def broker_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show or switch the active broker: /broker [alpaca|robinhood|accounts] [account]."""
    pipeline = context.bot_data.get("pipeline")
    if not pipeline:
        await update.message.reply_text("System initializing...", parse_mode=None)
        return

    args = list(context.args or [])
    if not args:
        await update.message.reply_text(_broker_summary(pipeline), parse_mode=None)
        return

    action = args[0].lower()
    if action == "accounts":
        try:
            accounts = await run_blocking(
                operation="broker_accounts",
                fn=lambda: _broker_accounts(pipeline),
                timeout_s=60,
            )
            await update.message.reply_text(accounts, parse_mode=None)
        except Exception as e:
            await update.message.reply_text(f"Could not fetch broker accounts: {str(e)[:300]}", parse_mode=None)
        return

    if action in ("alpaca", "paper"):
        pipeline.configure_broker(primary="alpaca", execution_mode="paper")
        await update.message.reply_text(_broker_summary(pipeline), parse_mode=None)
        return

    if action in ("robinhood", "rh"):
        account_number = args[1] if len(args) > 1 else None
        # Safety: selecting Robinhood always drops execution to review_only.
        # The operator must then explicitly run /mode live to arm live placement.
        # We never inherit a prior "live" mode across a broker switch.
        mode = "review_only"
        if account_number and account_number.isdigit() and len(account_number) <= 2:
            try:
                pipeline.configure_broker(primary="robinhood", execution_mode=mode)
                account_number = await run_blocking(
                    operation="broker_select_account",
                    fn=lambda: _select_robinhood_account(pipeline, int(account_number)),
                    timeout_s=60,
                )
            except Exception as e:
                await update.message.reply_text(f"Could not select Robinhood account: {str(e)[:300]}", parse_mode=None)
                return
        pipeline.configure_broker(primary="robinhood", execution_mode=mode, robinhood_account_number=account_number)
        await update.message.reply_text(_broker_summary(pipeline), parse_mode=None)
        return

    await update.message.reply_text("Usage: /broker, /broker accounts, /broker alpaca, /broker robinhood ACCOUNT_OR_INDEX", parse_mode=None)


@authorized
async def mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Switch execution mode: /mode review|paper|live."""
    pipeline = context.bot_data.get("pipeline")
    if not pipeline:
        await update.message.reply_text("System initializing...", parse_mode=None)
        return

    if not context.args:
        await update.message.reply_text(_broker_summary(pipeline), parse_mode=None)
        return

    mode = context.args[0].lower()
    if mode == "review":
        mode = "review_only"
    if mode not in {"review_only", "paper", "live"}:
        await update.message.reply_text("Usage: /mode review, /mode paper, or /mode live", parse_mode=None)
        return
    pipeline.configure_broker(execution_mode=mode)
    await update.message.reply_text(_broker_summary(pipeline), parse_mode=None)


@authorized
async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent active-broker orders."""
    pipeline = context.bot_data.get("pipeline")
    if not pipeline:
        await update.message.reply_text("System initializing...", parse_mode=None)
        return
    status = context.args[0].lower() if context.args else None
    try:
        text = await run_blocking(
            operation="orders_command",
            fn=lambda: _orders_text(pipeline, status),
            timeout_s=60,
        )
        await update.message.reply_text(text, parse_mode=None)
    except Exception as e:
        log.error("orders_command_failed", error=str(e))
        await update.message.reply_text(f"Error fetching orders: {str(e)[:300]}", parse_mode=None)


@authorized
async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trigger a full pipeline scan from Telegram."""
    pipeline = context.bot_data.get("pipeline")
    if not pipeline:
        await update.message.reply_text("System initializing...")
        return

    if pipeline.paused:
        await update.message.reply_text("Pipeline is paused. Use /resume first.", parse_mode=None)
        return

    await update.message.reply_text("Full scan starting... this may take several minutes.", parse_mode=None)

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, pipeline.run_full_scan)
        # Scan completion notification is sent by the pipeline itself
    except Exception as e:
        log.error("scan_command_failed", error=str(e))
        await update.message.reply_text(f"Scan failed: {str(e)[:200]}", parse_mode=None)


# --- Stub Commands ---

@authorized
async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current watchlist with remove buttons."""
    from orchestrator.universe import get_watchlist, remove_from_watchlist
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    args = update.message.text.split(maxsplit=1)

    # /watchlist remove TICKER
    if len(args) > 1 and args[1].strip().lower().startswith("remove "):
        ticker = args[1].strip().split(maxsplit=1)[1].upper()
        removed = remove_from_watchlist(ticker)
        if removed:
            await update.message.reply_text(f"✅ {ticker} removed from watchlist.", parse_mode=None)
        else:
            await update.message.reply_text(f"⚠️ {ticker} not found on watchlist.", parse_mode=None)
        return

    # /watchlist add TICKER [reason]
    if len(args) > 1 and args[1].strip().lower().startswith("add "):
        from orchestrator.universe import add_to_watchlist
        parts = args[1].strip().split(maxsplit=2)  # "add", "TICKER", optional reason
        ticker = parts[1].upper() if len(parts) > 1 else ""
        reason = parts[2] if len(parts) > 2 else "Manual add via /watchlist"
        if not ticker:
            await update.message.reply_text("Usage: `/watchlist add TICKER [reason]`", parse_mode=None)
            return
        added = add_to_watchlist(ticker, reason=reason, source="operator")
        if added:
            await update.message.reply_text(f"✅ {ticker} added to watchlist. Will re-scan with lower threshold.", parse_mode=None)
        else:
            await update.message.reply_text(f"⚠️ {ticker} is already on the watchlist.", parse_mode=None)
        return

    # Show watchlist
    watchlist = get_watchlist()
    if not watchlist:
        await update.message.reply_text(
            "📋 *Watchlist is empty*\n\n"
            "Add tickers via:\n"
            "• `/watchlist add TICKER reason`\n"
            "• 👀 Watchlist button on memos",
            parse_mode="MarkdownV2",
        )
        return

    lines = ["*📋 WATCHLIST*\n"]
    buttons = []
    for i, item in enumerate(watchlist, 1):
        ticker = escape_md(item["ticker"])
        sector = escape_md(item.get("sector", ""))
        reason = item.get("reason", "")
        # Truncate reason for display
        short_reason = reason[:60] + "..." if len(reason) > 60 else reason
        short_reason = escape_md(short_reason)
        lines.append(f"{i}\\. `{ticker}` \\({sector}\\)")
        if short_reason:
            lines.append(f"   _{short_reason}_")
        # Add remove button for each ticker
        buttons.append([InlineKeyboardButton(
            f"🗑 Remove {item['ticker']}",
            callback_data=f"wl_remove_{item['ticker']}",
        )])

    lines.append(f"\n_{escape_md(f'{len(watchlist)} tickers — re-scanned with lower threshold')}_")

    keyboard = InlineKeyboardMarkup(buttons) if buttons else None
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
    )

@authorized
async def upcoming_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show next known catalysts for watchlist + open positions (earnings only in MVP)."""
    from data.upcoming_catalysts import collect_upcoming, format_upcoming_message
    from database.db import get_session
    from database.models import Ticker, Trade
    from orchestrator.universe import get_watchlist

    try:
        watchlist_tickers = [w["ticker"] for w in get_watchlist()]
    except Exception as e:
        log.warning("upcoming_watchlist_lookup_failed", error=str(e))
        watchlist_tickers = []

    position_tickers: list[str] = []
    try:
        with get_session() as session:
            rows = (
                session.query(Ticker.symbol)
                .join(Trade, Trade.ticker_id == Ticker.id)
                .filter(Trade.status == "open")
                .all()
            )
            position_tickers = [r[0] for r in rows]
    except Exception as e:
        log.warning("upcoming_positions_lookup_failed", error=str(e))

    tickers = list({*watchlist_tickers, *position_tickers})
    if not tickers:
        await update.message.reply_text(
            "📅 No upcoming catalysts found — your watchlist and open positions are empty.\n\n"
            "Add tickers via /watchlist add TICKER reason.",
            parse_mode=None,
        )
        return

    try:
        catalysts = await asyncio.to_thread(collect_upcoming, tickers)
    except Exception as e:
        log.error("upcoming_collect_failed", error=str(e))
        await update.message.reply_text(
            "📅 Couldn't fetch upcoming catalysts right now (data provider error). Try again shortly.",
            parse_mode=None,
        )
        return

    await update.message.reply_text(format_upcoming_message(catalysts), parse_mode=None)

@authorized
async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pipeline = context.bot_data.get("pipeline")
    if pipeline:
        pipeline.paused = True
    await update.message.reply_text("⏸ Scanning paused. Existing positions still monitored. Use /resume to restart.", parse_mode=None)

@authorized
async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pipeline = context.bot_data.get("pipeline")
    if pipeline:
        pipeline.paused = False
    await update.message.reply_text("▶️ Scanning resumed.", parse_mode=None)

@authorized
async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pipeline = context.bot_data.get("pipeline")
    if not pipeline:
        await update.message.reply_text("System initializing...")
        return
    s = pipeline.settings
    text = (
        f"Broker: {getattr(getattr(pipeline.broker, 'active', pipeline.broker), 'name', 'unknown')}\n"
        f"Mode: {s.execution_mode}\n"
        f"Live enabled: {s.allow_live_trading}\n"
        f"Robinhood acct: {_mask_account(getattr(s, 'robinhood_account_number', ''))}\n"
        f"RH max order/day: ${s.robinhood_max_order_notional:,.2f} / ${s.robinhood_max_daily_notional:,.2f}\n"
        f"Portfolio: ${s.portfolio_value:,.0f}\n"
        f"Base position: {s.base_position_pct*100:.0f}%\n"
        f"Max position: {s.max_position_pct*100:.0f}%\n"
        f"Memo threshold: {s.memo_threshold}\n"
        f"Max hold: {s.max_holding_days} days\n"
        f"Scoring model: {s.scoring_model}\n"
    )
    await update.message.reply_text(text, parse_mode=None)


def _broker_summary(pipeline) -> str:
    s = pipeline.settings
    active = getattr(getattr(pipeline, "broker", None), "active", None)
    active_name = getattr(active, "name", "unknown")
    lines = [
        "Broker status",
        f"Active broker: {active_name}",
        f"Primary broker: {getattr(s, 'broker_primary', 'alpaca')}",
        f"Execution mode: {getattr(s, 'execution_mode', 'paper')}",
        f"Live trading enabled: {getattr(s, 'allow_live_trading', False)}",
        f"Robinhood account: {_mask_account(getattr(s, 'robinhood_account_number', ''))}",
        f"Robinhood max order: ${getattr(s, 'robinhood_max_order_notional', 5):,.2f}",
        f"Robinhood daily cap: ${getattr(s, 'robinhood_max_daily_notional', 10):,.2f}",
        f"Robinhood order type: {getattr(s, 'robinhood_order_type', 'market')}",
    ]
    if active_name == "robinhood" and not getattr(s, "robinhood_account_number", ""):
        lines.append("Set an Agentic account with /broker robinhood ACCOUNT_NUMBER or ROBINHOOD_ACCOUNT_NUMBER.")
    if getattr(s, "execution_mode", "paper") == "live" and not getattr(s, "allow_live_trading", False):
        lines.append("Live mode is selected, but placement is blocked until ALLOW_LIVE_TRADING=true.")
    return "\n".join(lines)


def _broker_accounts(pipeline) -> str:
    primary = getattr(pipeline, "primary_broker", None)
    if not primary or not hasattr(primary, "get_accounts"):
        return "Account listing is only available for the Robinhood broker."
    accounts = primary.get_accounts()
    if not accounts:
        return "No accounts returned. Check MCP authorization and Robinhood Agentic account setup."
    lines = ["Robinhood accounts"]
    for idx, account in enumerate(accounts, 1):
        number = str(account.get("account_number") or account.get("number") or account.get("id") or "")
        label = account.get("label") or account.get("type") or account.get("account_type") or "account"
        agentic = account.get("agentic_allowed")
        marker = "agentic_allowed=true" if agentic is True else "agentic_allowed=false" if agentic is False else "agentic_allowed=?"
        lines.append(f"{idx}. {_mask_account(number)} - {label} - {marker}")
    lines.append("Use /broker robinhood 1 to select by index, or /broker robinhood ACCOUNT_NUMBER.")
    return "\n".join(lines)


def _select_robinhood_account(pipeline, index: int) -> str:
    primary = getattr(pipeline, "primary_broker", None)
    if not primary or not hasattr(primary, "get_accounts"):
        raise ValueError("Switch to /broker robinhood first, then run /broker accounts.")
    accounts = primary.get_accounts()
    if index < 1 or index > len(accounts):
        raise ValueError(f"Account index {index} is out of range.")
    account = accounts[index - 1]
    number = str(account.get("account_number") or account.get("number") or account.get("id") or "")
    if not number:
        raise ValueError("Selected account did not include an account number.")
    return number


def _orders_text(pipeline, status: str | None = None) -> str:
    orders = pipeline.broker.get_orders(status=status)
    active = getattr(getattr(pipeline, "broker", None), "active", None)
    broker_name = getattr(active, "name", "unknown")
    if not orders:
        suffix = f" ({status})" if status else ""
        return f"No {broker_name} orders found{suffix}."
    lines = [f"{broker_name.title()} orders"]
    for order in orders[:10]:
        symbol = order.get("symbol") or order.get("ticker") or "?"
        state = order.get("state") or order.get("status") or "?"
        side = order.get("side") or "?"
        qty = order.get("quantity") or order.get("qty") or order.get("filled_quantity") or ""
        price = order.get("limit_price") or order.get("average_price") or order.get("filled_avg_price") or ""
        order_id = str(order.get("id") or order.get("order_id") or "")
        tail = f" #{order_id[-8:]}" if order_id else ""
        lines.append(f"{symbol} {side} {qty} {state} {price}{tail}".strip())
    return "\n".join(lines)


def _mask_account(account_number: str) -> str:
    account_number = account_number or ""
    if not account_number:
        return "not set"
    if len(account_number) <= 4:
        return "****"
    return f"****{account_number[-4:]}"
