"""
Trading Pipeline — the central orchestration logic.
Chains: data fetch → agent analysis → scoring → memo generation → delivery.

V2: Source-aware routing (Discovery → Watchlist → Universe),
    Discovery Agent integration, watchlist auto-add on Opus recommendation.
"""

import asyncio
import json
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime

from agents.base_agent import AgentOutput
from config.tickers import UNIVERSE
from agents.macro_agent import MacroRegimeAgent
from agents.catalyst_agent import CatalystAgent
from agents.fundamental_agent import FundamentalAgent
from agents.pattern_agent import PatternAgent
from agents.web_research_agent import WebResearchAgent
from agents.discovery_agent import DiscoveryAgent, DiscoveryOutput
from agents.deep_research_agent import DeepResearchAgent
from scoring.engine import ScoringEngine
from memo.generator import MemoGenerator
from execution.alpaca_client import AlpacaClient
from execution.risk_manager import RiskManager
from execution.position_manager import PositionManager
from execution.order_manager import OrderManager
from orchestrator.universe import seed_universe, get_active_universe, get_watchlist, add_to_watchlist
from utils.anthropic_client import AnthropicClient
from utils.web_search_client import WebSearchClient
from utils.deep_research_client import DeepResearchClient
from utils.logger import get_logger

log = get_logger("pipeline")


def _langfuse_context(session_id: str = None, tags: list = None):
    """Return a Langfuse propagate_attributes context manager, or a no-op if unavailable."""
    try:
        from langfuse import propagate_attributes
        kwargs = {}
        if session_id:
            kwargs["session_id"] = session_id
        if tags:
            kwargs["tags"] = tags
        return propagate_attributes(**kwargs)
    except ImportError:
        from contextlib import nullcontext
        return nullcontext()


@dataclass
class ScanTickerItem:
    """A ticker in the scan list with source-aware routing metadata."""
    ticker: str
    sector: str
    source: str              # "discovery" | "watchlist" | "universe"
    haiku_threshold: int     # 0 = skip Haiku, 2 = low, 3 = normal
    discovery_context: str = ""  # Pre-validated catalyst context (discovery only)
    direction_hint: str = ""     # From discovery


class TradingPipeline:
    def __init__(self, settings):
        self.settings = settings
        self.paused = False

        # Initialize Claude client
        self.anthropic_client = None
        if settings.anthropic_api_key:
            self.anthropic_client = AnthropicClient(settings.anthropic_api_key)

        # Initialize agents
        self.macro_agent = MacroRegimeAgent(settings, self.anthropic_client)
        self.catalyst_agent = CatalystAgent(settings, self.anthropic_client)
        self.fundamental_agent = FundamentalAgent(settings, self.anthropic_client)
        self.pattern_agent = PatternAgent(settings, self.anthropic_client)

        # V2: Web Search Client + Discovery Agent + Web Research Agent
        self.web_search_client = None
        self.discovery_agent = None
        self.web_research_agent = None
        if settings.anthropic_api_key:
            self.web_search_client = WebSearchClient(
                settings.web_search_provider, self.anthropic_client, settings
            )
            self.discovery_agent = DiscoveryAgent(
                settings, self.anthropic_client, self.web_search_client
            )
            self.web_research_agent = WebResearchAgent(
                settings, self.anthropic_client, self.web_search_client
            )
        else:
            self.web_research_agent = WebResearchAgent(settings, self.anthropic_client)

        # Initialize scoring and memo
        self.scoring_engine = ScoringEngine(settings, self.anthropic_client)
        self.memo_generator = MemoGenerator(settings, self.anthropic_client)

        # V2: Deep Research Agent (Gemini async, high-conviction only)
        self.deep_research_agent = None
        dr_api_key = getattr(settings, "gemini_api_key", "") if settings.deep_research_provider == "gemini" else getattr(settings, "openai_api_key", "")
        if dr_api_key:
            from utils.escalation_manager import EscalationManager
            dr_client = DeepResearchClient(
                provider=settings.deep_research_provider,
                api_key=dr_api_key,
                settings=settings,
            )
            escalation = EscalationManager(self.anthropic_client, settings) if self.anthropic_client else None
            self.deep_research_agent = DeepResearchAgent(settings, dr_client, escalation)
            log.info("deep_research_agent_initialized", provider=settings.deep_research_provider)

        # Initialize execution
        self.alpaca = AlpacaClient(settings.alpaca_api_key, settings.alpaca_secret_key)
        self.risk_manager = RiskManager(settings)
        self.position_manager = PositionManager(settings)
        self.order_manager = OrderManager(settings, self.alpaca, self.risk_manager, self.position_manager)

        # Telegram notification manager (set after bot starts)
        self.notification_manager = None
        self.bot_loop = None  # asyncio event loop for scheduling deep research from sync context

        # Parallel stage execution stability state
        self._parallel_health = {
            "mode": "normal",  # normal | degraded
            "history": deque(maxlen=max(1, int(self.settings.parallel_bad_run_window))),
            "runs_since_degrade": 0,
            "consecutive_good": 0,
        }

        log.info("pipeline_initialized")

    def get_sector(self, ticker: str) -> str:
        """Get sector for a ticker."""
        return UNIVERSE.get(ticker, "Unknown")

    def run_full_scan(self):
        """
        V2: Full scan with source-aware routing.
        1. Macro regime
        2. Discovery Agent (web search for new ideas)
        3. Build merged scan list (Discovery + Watchlist + Universe)
        4. Process each ticker through pipeline with source-aware routing
        Called 3x daily by the scheduler.
        """
        if self.paused:
            log.info("pipeline_paused, skipping scan")
            return

        log.info("full_scan_start")
        run_start = datetime.utcnow()
        scan_session_id = f"scan-{run_start.strftime('%Y%m%d-%H%M%S')}"

        with _langfuse_context(session_id=scan_session_id, tags=["scheduled_scan"]):
            self._run_full_scan_inner(run_start, scan_session_id)

    def _run_full_scan_inner(self, run_start, scan_session_id):
        """Inner scan logic wrapped by Langfuse session context."""
        # 1. Update macro regime
        regime_output = self.macro_agent.analyze()
        regime = regime_output.raw_data

        # 2. Discovery Agent — find new ideas via web search
        discovery_output = DiscoveryOutput()
        if self.discovery_agent:
            try:
                with _langfuse_context(tags=["discovery"]):
                    discovery_output = self.discovery_agent.discover(regime=regime)
                log.info("discovery_complete", found=len(discovery_output.tickers))
            except Exception as e:
                log.error("discovery_failed", error=str(e))

        # 3. Build merged scan list (priority: discovery > watchlist > universe)
        scan_list = self._build_scan_list(discovery_output)
        log.info(
            "scan_list_built",
            total=len(scan_list),
            discovery=sum(1 for s in scan_list if s.source == "discovery"),
            watchlist=sum(1 for s in scan_list if s.source == "watchlist"),
            universe=sum(1 for s in scan_list if s.source == "universe"),
        )

        # 4. Process each ticker
        memos_generated = 0
        escalated_count = 0
        memo_details = []
        for item in scan_list:
            try:
                memo_data = self._process_scan_item(item, regime)
                if memo_data:
                    memos_generated += 1
                    opus_eval = memo_data.get("opus_evaluation", {})
                    opus_rec = opus_eval.get("recommendation", "")
                    memo_details.append({
                        "ticker": item.ticker,
                        "score": memo_data.get("composite_score", 0),
                        "classification": memo_data.get("classification", ""),
                        "memo_id": memo_data.get("memo_id", 0),
                        "opus_recommendation": opus_rec,
                    })

                    # If Opus recommends watchlist, add it
                    if opus_rec == "watchlist" and item.source != "watchlist":
                        final_score = memo_data.get("composite_score", 0)
                        add_to_watchlist(
                            item.ticker,
                            reason=f"Opus watchlist rec (score: {final_score:.2f})",
                            source="opus_recommendation",
                            sector=item.sector,
                        )

            except Exception as e:
                log.error("ticker_scan_failed", ticker=item.ticker, error=str(e))
                continue

        duration = (datetime.utcnow() - run_start).total_seconds()
        log.info("full_scan_complete", duration_s=duration, memos=memos_generated)

        # Send scan completion notification
        self._send_scan_notification(duration, len(scan_list), escalated_count, memos_generated, memo_details)

    def _send_scan_notification(self, duration_s, total_scanned, escalated, memos_generated, memo_details):
        """Send scan completion notification via Telegram."""
        if not self.notification_manager or not self.bot_loop or self.bot_loop.is_closed():
            return

        # Determine scan type based on time of day
        try:
            from zoneinfo import ZoneInfo
            et_hour = datetime.now(ZoneInfo("America/New_York")).hour
        except Exception:
            et_hour = datetime.utcnow().hour - 5  # rough fallback

        if et_hour < 10:
            scan_type = "Pre-Market"
        elif et_hour < 14:
            scan_type = "Midday"
        else:
            scan_type = "Post-Market"

        try:
            asyncio.run_coroutine_threadsafe(
                self.notification_manager.scan_complete(
                    scan_type=scan_type,
                    duration_s=duration_s,
                    total_scanned=total_scanned,
                    escalated=escalated,
                    memos_generated=memos_generated,
                    memo_details=memo_details,
                ),
                self.bot_loop,
            )
        except Exception as e:
            log.error("scan_notification_failed", error=str(e))

    def _build_scan_list(self, discovery_output: DiscoveryOutput) -> list:
        """
        Merge discovery + watchlist + universe into a deduplicated scan list.
        Priority: discovery > watchlist > universe (first seen wins).
        """
        seen = set()
        scan_list = []

        # Priority 1: Discovery (skip Haiku — already validated)
        for disc in discovery_output.tickers:
            if disc.ticker not in seen:
                seen.add(disc.ticker)
                sector = UNIVERSE.get(disc.ticker, "Unknown")
                scan_list.append(ScanTickerItem(
                    ticker=disc.ticker,
                    sector=sector,
                    source="discovery",
                    haiku_threshold=0,  # Skip Haiku
                    discovery_context=disc.discovery_context,
                    direction_hint=disc.direction_hint,
                ))

        # Priority 2: Watchlist (lower Haiku threshold)
        for w in get_watchlist():
            if w["ticker"] not in seen:
                seen.add(w["ticker"])
                scan_list.append(ScanTickerItem(
                    ticker=w["ticker"],
                    sector=w.get("sector", "Unknown"),
                    source="watchlist",
                    haiku_threshold=self.settings.watchlist_haiku_threshold,
                ))

        # Priority 3: Universe (normal Haiku threshold)
        for ticker, sector in UNIVERSE.items():
            if ticker not in seen:
                seen.add(ticker)
                scan_list.append(ScanTickerItem(
                    ticker=ticker,
                    sector=sector,
                    source="universe",
                    haiku_threshold=self.settings.catalyst_escalation_threshold,
                ))

        return scan_list

    def _process_scan_item(self, item: ScanTickerItem, regime: dict) -> dict:
        """Process a single ticker through the full pipeline with source-aware routing."""
        # Ensure ticker is in DB
        self._ensure_ticker(item.ticker)

        # Route catalyst scan based on source
        catalyst_kwargs = {"sector": item.sector}

        if item.source == "discovery":
            # Skip Haiku — already validated by Discovery Agent
            catalyst_kwargs["skip_haiku"] = True
            catalyst_kwargs["discovery_context"] = item.discovery_context
            catalyst_kwargs["direction_hint"] = item.direction_hint
        elif item.source == "watchlist":
            # Lower Haiku threshold for watchlist tickers
            catalyst_kwargs["haiku_threshold_override"] = item.haiku_threshold

        with _langfuse_context(tags=["catalyst", item.ticker]):
            catalyst = self.catalyst_agent.analyze(ticker=item.ticker, **catalyst_kwargs)

        # Only proceed if catalyst is meaningful
        if catalyst.score < 0.3:
            return None

        # Run remaining agents (parallelized with stability controller)
        fundamental, pattern, web_research, _, _ = self._run_post_catalyst_agents(
            ticker=item.ticker,
            sector=item.sector,
            catalyst=catalyst,
            run_context="scan",
        )

        # Score opportunity
        portfolio_context = self._get_portfolio_context()
        with _langfuse_context(tags=["scoring", item.ticker]):
            result = self.scoring_engine.score_opportunity(
                item.ticker, catalyst, fundamental, pattern, web_research,
                regime, portfolio_context,
            )

        # Generate memo if above threshold
        if result.get("meets_memo_threshold"):
            with _langfuse_context(tags=["memo", item.ticker]):
                memo_data = self.memo_generator.generate(
                    item.ticker, result, catalyst, fundamental, pattern, web_research, regime,
                )
            if memo_data:
                memo_data["source"] = item.source
                log.info(
                    "memo_created",
                    ticker=item.ticker,
                    score=result["final_score"],
                    source=item.source,
                )

                # V2: Trigger deep research for high-conviction ideas (scheduled scans only)
                self._maybe_trigger_deep_research(
                    ticker=item.ticker,
                    memo_data=memo_data,
                    scoring_result=result,
                    catalyst_reasoning=catalyst.reasoning,
                    web_research_reasoning=web_research.reasoning,
                )

                return memo_data

        return None

    def _run_post_catalyst_agents(
        self,
        ticker: str,
        sector: str,
        catalyst: AgentOutput,
        run_context: str,
        progress_cb=None,
    ) -> tuple[AgentOutput, AgentOutput, AgentOutput, dict, int]:
        """
        Run fundamental + pattern + web_research stages with optional parallelism.
        Returns (fundamental, pattern, web_research, stage_statuses, workers_used).
        """
        def _tagged_fundamental():
            with _langfuse_context(tags=["fundamental", ticker]):
                return self.fundamental_agent.analyze(ticker=ticker, sector=sector)

        def _tagged_pattern():
            with _langfuse_context(tags=["pattern", ticker]):
                return self.pattern_agent.analyze(
                    ticker=ticker,
                    catalyst_data=catalyst.raw_data,
                    catalyst_reasoning=catalyst.reasoning,
                )

        def _tagged_web_research():
            with _langfuse_context(tags=["web_research", ticker]):
                return self.web_research_agent.analyze(
                    ticker=ticker,
                    sector=sector,
                    catalyst_data=catalyst.raw_data,
                    catalyst_reasoning=catalyst.reasoning,
                    direction_hint=catalyst.direction,
                )

        stage_fns = {
            "fundamental": _tagged_fundamental,
            "pattern": _tagged_pattern,
            "web_research": _tagged_web_research,
        }
        stage_timeouts = {
            "fundamental": max(1, int(self.settings.parallel_timeout_fundamental_s)),
            "pattern": max(1, int(self.settings.parallel_timeout_pattern_s)),
            "web_research": max(1, int(self.settings.parallel_timeout_web_research_s)),
        }
        stage_order = ("fundamental", "pattern", "web_research")

        use_parallel = self._parallel_scope_enabled(run_context)
        workers = self._get_parallel_workers() if use_parallel else 1
        workers = max(1, min(3, workers))

        if progress_cb:
            if workers > 1:
                progress_cb(
                    f"Running fundamental, pattern, and web research in parallel ({workers} workers)..."
                )
            else:
                progress_cb("Running fundamental, pattern, and web research...")

        futures = {}
        submitted_at = {}
        outputs = {}
        statuses = {}
        status_details = {}
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="post_catalyst")

        try:
            for stage in stage_order:
                submitted_at[stage] = time.perf_counter()
                futures[stage] = executor.submit(stage_fns[stage])

            for stage in stage_order:
                if progress_cb and workers == 1:
                    progress_cb(f"Running {stage.replace('_', ' ')}...")

                timeout_s = stage_timeouts[stage]
                try:
                    outputs[stage] = futures[stage].result(timeout=timeout_s)
                    statuses[stage] = "ok"
                    status_details[stage] = ""
                except FutureTimeoutError:
                    futures[stage].cancel()
                    statuses[stage] = "timeout"
                    detail = f"Stage timed out after {timeout_s}s"
                    status_details[stage] = detail
                    outputs[stage] = self._fallback_stage_output(
                        stage=stage,
                        ticker=ticker,
                        status="timeout",
                        reason=detail,
                    )
                except Exception as exc:
                    statuses[stage] = "error"
                    detail = str(exc)[:300]
                    status_details[stage] = detail
                    outputs[stage] = self._fallback_stage_output(
                        stage=stage,
                        ticker=ticker,
                        status="error",
                        reason=detail,
                    )

                elapsed_s = round(time.perf_counter() - submitted_at[stage], 2)
                log.info(
                    "post_catalyst_stage_result",
                    run_context=run_context,
                    ticker=ticker,
                    stage=stage,
                    status=statuses[stage],
                    elapsed_s=elapsed_s,
                    timeout_s=timeout_s,
                    workers=workers,
                    parallel=workers > 1,
                    status_detail=status_details[stage],
                )
        finally:
            # Do not block the pipeline waiting for timed-out threads to finish.
            executor.shutdown(wait=False, cancel_futures=True)

        bad_stage_count = sum(1 for s in statuses.values() if s in ("timeout", "error"))
        self._update_parallel_health(
            run_context=run_context,
            ticker=ticker,
            workers=workers,
            bad_stage_count=bad_stage_count,
        )

        return (
            outputs["fundamental"],
            outputs["pattern"],
            outputs["web_research"],
            statuses,
            workers,
        )

    def _fallback_stage_output(self, stage: str, ticker: str, status: str, reason: str) -> AgentOutput:
        """Build a controlled fallback output when an agent stage fails."""
        trimmed = (reason or "unknown error")[:300]
        return AgentOutput(
            agent_type=stage,
            ticker=ticker,
            score=0.5,
            confidence=0.1,
            direction="neutral",
            reasoning=f"{stage} stage fallback ({status}): {trimmed}",
            raw_data={
                "status": status,
                "fallback": True,
                "error": trimmed,
            },
        )

    def _parallel_scope_enabled(self, run_context: str) -> bool:
        """Check if parallel agent execution is enabled for this context."""
        if not bool(getattr(self.settings, "parallel_agents_enabled", True)):
            return False
        scope = str(getattr(self.settings, "parallel_agents_scope", "both")).strip().lower()
        return scope == "both" or scope == run_context

    def _get_parallel_workers(self) -> int:
        """Return current worker limit (normal vs degraded mode)."""
        default_workers = max(1, int(getattr(self.settings, "parallel_workers_default", 3)))
        degraded_workers = max(1, int(getattr(self.settings, "parallel_workers_degraded", 2)))
        if self._parallel_health["mode"] == "degraded":
            return min(default_workers, degraded_workers)
        return default_workers

    def _update_parallel_health(self, run_context: str, ticker: str, workers: int, bad_stage_count: int):
        """
        Update rolling health and auto-adjust worker mode.
        Bad run: 2+ stage failures/timeouts.
        """
        if not self._parallel_scope_enabled(run_context):
            return
        if workers <= 1:
            return
        if not bool(getattr(self.settings, "parallel_auto_degrade_enabled", True)):
            return

        is_bad_run = bad_stage_count >= 2
        state = self._parallel_health
        history = state["history"]
        history.append(1 if is_bad_run else 0)

        cooldown_runs = max(1, int(getattr(self.settings, "parallel_cooldown_runs", 20)))
        recovery_good_runs = max(1, int(getattr(self.settings, "parallel_recovery_good_runs", 8)))
        trigger_bad_runs = max(1, int(getattr(self.settings, "parallel_bad_run_count_trigger", 3)))

        if state["mode"] == "degraded":
            state["runs_since_degrade"] += 1
            state["consecutive_good"] = 0 if is_bad_run else state["consecutive_good"] + 1

            if (
                state["runs_since_degrade"] >= cooldown_runs
                and state["consecutive_good"] >= recovery_good_runs
            ):
                state["mode"] = "normal"
                state["runs_since_degrade"] = 0
                state["consecutive_good"] = 0
                reason = (
                    f"Recovered after {cooldown_runs}+ runs with "
                    f"{recovery_good_runs} consecutive healthy runs."
                )
                self._announce_parallel_mode_change("normal", reason)
            return

        # Normal mode
        state["consecutive_good"] = 0 if is_bad_run else state["consecutive_good"] + 1
        bad_in_window = sum(history)

        if bad_in_window >= trigger_bad_runs and len(history) >= trigger_bad_runs:
            state["mode"] = "degraded"
            state["runs_since_degrade"] = 0
            state["consecutive_good"] = 0
            reason = (
                f"{bad_in_window} bad runs in last {len(history)} runs "
                f"(latest ticker: {ticker})"
            )
            self._announce_parallel_mode_change("degraded", reason)

    def _announce_parallel_mode_change(self, mode: str, reason: str):
        """Log + optionally notify operator when parallel worker mode changes."""
        workers = self._get_parallel_workers()
        log.warning("parallel_mode_changed", mode=mode, workers=workers, reason=reason)

        if not bool(getattr(self.settings, "parallel_alert_on_state_change", True)):
            return
        if not self.notification_manager or not self.bot_loop or self.bot_loop.is_closed():
            return

        mode_label = "DEGRADED" if mode == "degraded" else "NORMAL"
        msg = (
            f"Parallel stability mode changed: {mode_label} "
            f"({workers} workers). Reason: {reason}"
        )
        try:
            asyncio.run_coroutine_threadsafe(
                self.notification_manager.system_message(msg),
                self.bot_loop,
            )
        except Exception as e:
            log.error("parallel_mode_alert_failed", error=str(e))

    def _maybe_trigger_deep_research(
        self,
        ticker: str,
        memo_data: dict,
        scoring_result: dict,
        catalyst_reasoning: str,
        web_research_reasoning: str,
    ):
        """
        Check if deep research should fire, and if so, schedule it as an async task.
        Only triggers on scheduled scans (not ad-hoc). Runs in background — pipeline doesn't wait.
        """
        if not self.deep_research_agent:
            return
        if not self.deep_research_agent.should_trigger(scoring_result, is_ad_hoc=False):
            return

        memo_id = memo_data.get("memo_id", 0)
        if not memo_id:
            log.warning("deep_research_skip_no_memo_id", ticker=ticker)
            return

        # Build a notification callback that uses our NotificationManager
        async def _notify(msg: str):
            if self.notification_manager:
                await self.notification_manager.deep_research_update(ticker, msg)

        # Schedule as async task on the bot's event loop — don't block the pipeline
        if not self.bot_loop or self.bot_loop.is_closed():
            log.warning("deep_research_no_bot_loop", ticker=ticker)
            return

        try:
            asyncio.run_coroutine_threadsafe(
                self._run_deep_research_async(
                    ticker=ticker,
                    memo_id=memo_id,
                    scoring_result=scoring_result,
                    catalyst_reasoning=catalyst_reasoning,
                    web_research_reasoning=web_research_reasoning,
                    notify=_notify,
                ),
                self.bot_loop,
            )
            log.info("deep_research_triggered", ticker=ticker, score=scoring_result.get("final_score", 0))
        except Exception as e:
            log.error("deep_research_schedule_failed", ticker=ticker, error=str(e))

    async def _run_deep_research_async(
        self,
        ticker: str,
        memo_id: int,
        scoring_result: dict,
        catalyst_reasoning: str,
        web_research_reasoning: str,
        notify=None,
    ):
        """Background coroutine: run deep research + PDF + send via Telegram."""
        try:
            result = await self.deep_research_agent.run(
                ticker=ticker,
                memo_id=memo_id,
                scoring_result=scoring_result,
                catalyst_reasoning=catalyst_reasoning,
                web_research_reasoning=web_research_reasoning,
                notification_callback=notify,
            )

            # Generate and send PDF if research completed
            if result.get("status") == "completed" and result.get("research_report"):
                from utils.pdf_generator import generate_deep_research_pdf
                pdf_path = generate_deep_research_pdf(
                    ticker=ticker,
                    research_report=result["research_report"],
                    scoring_result=scoring_result,
                    reevaluation=result.get("reevaluation"),
                )
                if pdf_path and self.notification_manager:
                    await self.notification_manager.send_deep_research_pdf(ticker, pdf_path)

                # Update DB with PDF path
                if pdf_path and result.get("dr_request_id"):
                    self.deep_research_agent._update_request(
                        result["dr_request_id"], pdf_path=pdf_path
                    )

        except Exception as e:
            log.error("deep_research_async_failed", ticker=ticker, error=str(e))

    def run_ad_hoc(self, ticker: str, thesis: str = "", progress_cb=None) -> dict:
        """
        Run full pipeline for a single ticker (triggered by /test command).
        Skips Haiku pre-screening if thesis is provided.
        Returns memo data dict.
        V2: Uses web_research_agent instead of reddit stub.

        progress_cb: optional callable(stage_text) for live progress updates.
        """
        log.info("ad_hoc_start", ticker=ticker, has_thesis=bool(thesis))
        _progress = progress_cb or (lambda s: None)

        # Wrap in Langfuse session for observability
        session_id = f"adhoc-{ticker}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
        with _langfuse_context(session_id=session_id, tags=["ad_hoc", ticker]):
            return self._run_ad_hoc_inner(ticker, thesis, _progress)

    def _run_ad_hoc_inner(self, ticker: str, thesis: str, _progress) -> dict:
        """Inner ad-hoc logic wrapped by Langfuse session context."""
        # Ensure ticker is in DB
        self._ensure_ticker(ticker)

        sector = self.get_sector(ticker)

        # 1. Get regime
        _progress("Checking macro regime...")
        regime = self.macro_agent.get_latest_regime()

        # 2. Run all agents
        _progress("Running catalyst analysis (Haiku + Sonnet)...")
        catalyst = self.catalyst_agent.analyze(ticker=ticker, sector=sector, thesis=thesis)

        fundamental, pattern, web_research, _, workers = self._run_post_catalyst_agents(
            ticker=ticker,
            sector=sector,
            catalyst=catalyst,
            run_context="ad_hoc",
            progress_cb=_progress,
        )
        if workers > 1:
            _progress(f"Parallel stage complete ({workers} workers)")

        # 3. Score
        _progress("Scoring with Opus evaluation...")
        portfolio_context = self._get_portfolio_context()
        result = self.scoring_engine.score_opportunity(
            ticker, catalyst, fundamental, pattern, web_research,
            regime, portfolio_context,
        )

        # 4. Generate memo (always for ad-hoc, regardless of threshold)
        _progress("Generating IC memo...")
        memo_data = self.memo_generator.generate(
            ticker, result, catalyst, fundamental, pattern, web_research, regime,
        )

        return memo_data

    async def run_ad_hoc_async(self, ticker: str, thesis: str = "", progress_cb=None) -> dict:
        """Async wrapper for ad-hoc analysis (called from Telegram handler)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self.run_ad_hoc(ticker, thesis, progress_cb=progress_cb)
        )

    def _get_portfolio_context(self) -> str:
        """Build portfolio context string for Opus evaluation."""
        try:
            account = self.alpaca.get_account_info()
            positions = self.alpaca.get_positions_detail()
            parts = [
                f"Portfolio: ${account.get('equity', 0):,.2f}, Cash: ${account.get('cash', 0):,.2f}",
                f"Open positions: {len(positions)}",
            ]
            for p in positions:
                parts.append(f"  {p['ticker']}: {p.get('qty', 0)} shares, P&L {p.get('pnl_pct', 0):+.2f}%")
            return "\n".join(parts)
        except Exception:
            return f"Portfolio: ${self.settings.portfolio_value:,.2f} (initial), 0 positions"

    def _ensure_ticker(self, ticker: str):
        """Make sure a ticker exists in the DB (for ad-hoc analysis)."""
        from database.db import get_session
        from database.models import Ticker
        with get_session() as session:
            existing = session.query(Ticker).filter_by(symbol=ticker).first()
            if not existing:
                sector = UNIVERSE.get(ticker, "Unknown")
                session.add(Ticker(symbol=ticker, sector=sector, in_universe=ticker in UNIVERSE))
