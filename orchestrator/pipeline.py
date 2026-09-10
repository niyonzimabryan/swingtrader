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
from utils.timeutils import utcnow_naive

from agents.base_agent import AgentOutput
from config.tickers import UNIVERSE
from agents.macro_agent import MacroRegimeAgent
from agents.catalyst_agent import CatalystAgent
from agents.fundamental_agent import FundamentalAgent
from agents.pattern_agent import PatternAgent
from agents.web_research_agent import WebResearchAgent
from agents.discovery_agent import DiscoveryAgent, DiscoveryOutput
from agents.deep_research_agent import DeepResearchAgent
from scanning.structured_scanner import StructuredScanner, ScanResult as StructuredScanResult
from screening.gemini_screener import GeminiScreener, GeminiBatchResult
from scoring.engine import ScoringEngine
from memo.generator import MemoGenerator
from execution.brokers import create_brokers, rebuild_primary_broker
from execution.risk_manager import RiskManager
from execution.position_manager import PositionManager
from execution.order_manager import OrderManager
from execution.auto_approver import AutoApprover
from tracking.shadow_ledger import record_scored_candidate, classify_cohort
from orchestrator.universe import seed_universe, get_active_universe, get_watchlist, add_to_watchlist
from utils.anthropic_client import AnthropicClient
from utils.web_search_client import WebSearchClient
from utils.deep_research_client import DeepResearchClient
from utils.firecrawl_client import FirecrawlClient
from utils.article_fetcher import ArticleFetcher
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


@dataclass
class ScanItemOutcome:
    """Per-ticker scan result used for funnel telemetry (I0), the shadow ledger
    (I1), and the post-scan paper auto-approve pass (I2)."""
    ticker: str
    source: str
    sector: str
    catalyst_gate_passed: bool = False
    scored: bool = False
    final_score: float = 0.0
    direction: str = "neutral"
    cohort: str = "below"
    memo_id: int | None = None
    ledger_id: int | None = None
    scoring_result: dict | None = None
    regime: dict | None = None
    memo_data: dict | None = None
    auto_executed: bool = False


class TradingPipeline:
    def __init__(self, settings):
        self.settings = settings
        self.paused = False

        # Initialize Claude client
        self.anthropic_client = None
        if settings.anthropic_api_key:
            self.anthropic_client = AnthropicClient(settings.anthropic_api_key)

        # Firecrawl + archive.is fallback for paywalled narrative sources.
        # Both are optional — agents fall back gracefully when not configured.
        self.firecrawl_client = FirecrawlClient(settings)
        self.article_fetcher = ArticleFetcher(
            firecrawl_client=self.firecrawl_client,
            archive_enabled=getattr(settings, "archive_is_enabled", True),
        )

        # Initialize agents
        self.macro_agent = MacroRegimeAgent(settings, self.anthropic_client)
        self.catalyst_agent = CatalystAgent(
            settings, self.anthropic_client, article_fetcher=self.article_fetcher
        )
        self.fundamental_agent = FundamentalAgent(settings, self.anthropic_client)
        self.pattern_agent = PatternAgent(settings, self.anthropic_client)

        # V2: Web Search Client + Discovery Agent + Web Research Agent
        self.web_search_client = None
        self.discovery_agent = None
        self.web_research_agent = None
        web_provider = getattr(settings, "web_search_provider", "gemini")
        if web_provider == "gemini" and not getattr(settings, "gemini_api_key", "") and settings.anthropic_api_key:
            log.warning("gemini_search_missing_key_fallback_to_anthropic")
            web_provider = "anthropic"
        web_search_available = (
            (web_provider == "anthropic" and settings.anthropic_api_key)
            or (web_provider == "gemini" and getattr(settings, "gemini_api_key", ""))
        )
        if web_search_available:
            self.web_search_client = WebSearchClient(
                web_provider, self.anthropic_client, settings
            )
            self.discovery_agent = DiscoveryAgent(
                settings, self.anthropic_client, self.web_search_client
            )
            self.web_research_agent = WebResearchAgent(
                settings, self.anthropic_client, self.web_search_client,
                firecrawl_client=self.firecrawl_client,
            )
        else:
            self.web_research_agent = WebResearchAgent(
                settings, self.anthropic_client, firecrawl_client=self.firecrawl_client
            )

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

        # Initialize Tier 1 structured scanner (free, rules-based)
        news_data = None
        if settings.finnhub_api_key:
            from data.news_data import NewsDataAdapter
            news_data = NewsDataAdapter(settings.finnhub_api_key)
        self.structured_scanner = StructuredScanner(settings, news_data)

        # Initialize Tier 2 Gemini Flash screener (web research + preliminary scoring)
        self.gemini_screener = GeminiScreener(settings)
        if self.gemini_screener.is_available:
            log.info("gemini_screener_initialized", model=settings.gemini_flash_model)

        # Initialize execution
        self.alpaca, self.paper_broker, self.primary_broker, self.broker = create_brokers(settings)
        self.risk_manager = RiskManager(settings)
        self.position_manager = PositionManager(settings)
        self.order_manager = OrderManager(settings, self.alpaca, self.risk_manager, self.position_manager, broker=self.broker)

        # Spec I1: shared price cache for shadow-ledger entry prices.
        from data.event_outcomes import PriceHistoryCache
        self._price_cache = PriceHistoryCache(settings)

        # Spec I2: paper autonomy sandbox (structurally paper-only; see AutoApprover).
        self.auto_approver = AutoApprover(
            settings, self.order_manager, self.memo_generator, self.broker,
            notifier=self._auto_approve_notify,
        )

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

    def configure_broker(
        self,
        primary: str | None = None,
        execution_mode: str | None = None,
        robinhood_account_number: str | None = None,
    ) -> None:
        """Update broker settings at runtime for Telegram controls."""
        if primary:
            self.settings.broker_primary = primary
        if execution_mode:
            mode = execution_mode.lower()
            if mode == "review":
                mode = "review_only"
            self.settings.execution_mode = mode
        if robinhood_account_number:
            self.settings.robinhood_account_number = robinhood_account_number
        self.primary_broker = rebuild_primary_broker(self.settings, self.paper_broker)
        self.broker.set_primary(self.primary_broker)

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
        run_start = utcnow_naive()
        scan_session_id = f"scan-{run_start.strftime('%Y%m%d-%H%M%S')}"
        self._start_pipeline_run(scan_session_id, "scheduled_scan")

        try:
            with _langfuse_context(session_id=scan_session_id, tags=["scheduled_scan"]):
                self._run_full_scan_inner(run_start, scan_session_id)
        except Exception as e:
            self._finish_pipeline_run(scan_session_id, status="failed", errors=[str(e)])
            raise

    def _run_full_scan_inner(self, run_start, scan_session_id):
        """Inner scan logic wrapped by Langfuse session context."""
        # 1. Tier 1: Structured scan (free, rules-based) — flags tickers with catalysts
        structured_result = StructuredScanResult()
        try:
            structured_result = self.structured_scanner.scan(UNIVERSE)
            log.info(
                "tier1_scan_complete",
                flagged=len(structured_result.flagged),
                duration_s=structured_result.scan_duration_s,
            )
        except Exception as e:
            log.error("tier1_scan_failed", error=str(e))

        # 2. Tier 2: Gemini Flash screening of flagged tickers (web research + scoring)
        gemini_result = GeminiBatchResult()
        if structured_result.flagged and self.gemini_screener.is_available:
            try:
                # Build batch input from Tier 1 flagged tickers
                batch_input = []
                for flagged in structured_result.flagged:
                    catalyst_context = ", ".join(flagged.catalysts)
                    if flagged.change_pct:
                        catalyst_context += f" | Price change: {flagged.change_pct:+.1f}%"
                    if flagged.volume_ratio:
                        catalyst_context += f" | Volume: {flagged.volume_ratio:.1f}x avg"
                    if flagged.earnings_date:
                        catalyst_context += f" | Earnings: {flagged.earnings_date}"
                    batch_input.append({
                        "symbol": flagged.symbol,
                        "catalyst_context": catalyst_context,
                    })

                gemini_result = self.gemini_screener.screen_batch(batch_input)
                log.info(
                    "tier2_screen_complete",
                    screened=gemini_result.total_screened,
                    escalated=len(gemini_result.escalated),
                    duration_s=gemini_result.duration_s,
                    parsed=gemini_result.parsed,
                    parse_failed=gemini_result.parse_failed,
                    truncated=gemini_result.truncated,
                    parse_fail_rate=gemini_result.parse_fail_rate,
                    degraded=gemini_result.degraded,
                )
            except Exception as e:
                log.error("tier2_screen_failed", error=str(e))

        # I0: cap tier-2 escalations to the top-N by Gemini score before catalyst.
        tier2_escalated_precap = len(getattr(gemini_result, "escalated", []) or [])
        tier2_kept = tier2_escalated_precap
        if tier2_escalated_precap:
            kept, dropped = self._cap_tier2_escalations(
                gemini_result, int(getattr(self.settings, "tier2_max_escalations", 25))
            )
            if dropped:
                gemini_result.escalated = kept
                tier2_kept = len(kept)
                log.info("tier2_escalation_capped", kept=tier2_kept, dropped=dropped)

        # 3. Update macro regime
        regime_output = self.macro_agent.analyze()
        regime = regime_output.raw_data

        # 4. Discovery Agent — find new ideas via web search
        discovery_output = DiscoveryOutput()
        if self.discovery_agent:
            try:
                with _langfuse_context(tags=["discovery"]):
                    discovery_output = self.discovery_agent.discover(regime=regime)
                log.info("discovery_complete", found=len(discovery_output.tickers))
            except Exception as e:
                log.error("discovery_failed", error=str(e))

        # 5. Build merged scan list (priority: tier2_escalated > discovery > watchlist > universe)
        scan_list = self._build_scan_list(discovery_output, structured_result, gemini_result)
        log.info(
            "scan_list_built",
            total=len(scan_list),
            tier2=sum(1 for s in scan_list if s.source == "tier2_gemini"),
            tier1=sum(1 for s in scan_list if s.source == "tier1_scan"),
            discovery=sum(1 for s in scan_list if s.source == "discovery"),
            watchlist=sum(1 for s in scan_list if s.source == "watchlist"),
            universe=sum(1 for s in scan_list if s.source == "universe"),
        )

        # 4. Process each ticker
        memos_generated = 0
        escalated_count = 0
        memo_details = []
        outcomes = []
        catalyst_failures = 0
        for item in scan_list:
            try:
                outcome = self._process_scan_item(item, regime, scan_session_id)
            except Exception as e:
                catalyst_failures += 1
                log.error("ticker_scan_failed", ticker=item.ticker, error=str(e))
                continue

            outcomes.append(outcome)
            memo_data = outcome.memo_data
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
                    "auto_executed": False,
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

        # I2: paper autonomy sandbox — auto-place orders for eligible cohorts.
        auto_summary = self._run_auto_approve(outcomes)
        # Mark auto-taken memos so the scan summary appends "🤖 auto-executed (paper)".
        auto_memo_ids = {o.memo_id for o in outcomes if o.auto_executed and o.cohort == "memo"}
        for md in memo_details:
            md["auto_executed"] = md.get("memo_id") in auto_memo_ids

        duration = (utcnow_naive() - run_start).total_seconds()
        log.info("full_scan_complete", duration_s=duration, memos=memos_generated)

        # I0: one funnel-summary line per scan — the single line Bryan reads.
        log.info(
            "scan_funnel_summary",
            universe=len(UNIVERSE),
            tier1_flagged=len(getattr(structured_result, "flagged", []) or []),
            tier2_screened=getattr(gemini_result, "total_screened", 0),
            tier2_escalated=tier2_escalated_precap,
            tier2_capped=tier2_kept,
            catalyst_analyzed=len(outcomes),
            catalyst_failed=catalyst_failures,
            catalyst_gate_passed=sum(1 for o in outcomes if o.catalyst_gate_passed),
            scored=sum(1 for o in outcomes if o.scored),
            memo_threshold_passed=memos_generated,
            memos_sent=memos_generated,
            shadow_recorded=sum(1 for o in outcomes if o.ledger_id),
            paper_orders=auto_summary.get("placed", 0),
        )

        self._finish_pipeline_run(
            scan_session_id,
            status="success",
            scanned_count=len(scan_list),
            screened_count=getattr(gemini_result, "total_screened", 0),
            researched_count=len(scan_list),
            memos_generated=memos_generated,
            duration_s=duration,
            metadata={"memo_details": memo_details},
        )

        # Send scan completion notification
        self._send_scan_notification(duration, len(scan_list), escalated_count, memos_generated, memo_details)
        self._maybe_alert_high_failure_rate(len(scan_list), catalyst_failures)

        # Strategy Lab shadow pass (Spec Q §14, PR 4). Last, deliberately: every
        # memo has been generated and delivered and every notification sent
        # before a single experiment row is written, so the worst a Strategy Lab
        # defect can cost is the experiment's own evidence. Default-off, and a
        # no-op with either flag false.
        self._run_strategy_lab_shadow(outcomes, run_id=scan_session_id)

    def _run_strategy_lab_shadow(self, outcomes: list, *, run_id: str) -> None:
        """Run the registered shadow arms over the names this scan scored.

        The whole integration lives in `orchestrator/strategy_lab_shadow.py`;
        this is the hook. Two guards, on purpose: the module already catches its
        own exceptions and returns a summary, and this catches anything its
        error handling missed — including an ImportError, which is what a
        half-deployed container looks like. A scan that has already delivered
        its memos must not die of an experiment (Spec Q §14).
        """
        if not bool(getattr(self.settings, "strategy_lab_enabled", False)):
            return
        try:
            from orchestrator import strategy_lab_shadow

            strategy_lab_shadow.run_shadow_for_scan(
                self.settings,
                tickers=[o.ticker for o in outcomes if o.scored],
                run_id=run_id,
            )
        except Exception as e:
            log.error("strategy_lab_shadow_hook_failed", error=str(e)[:300])

    def _maybe_alert_high_failure_rate(self, total_scanned: int, catalyst_failures: int) -> None:
        """
        BRY-301: per-ticker exceptions (billing exhaustion, LLM provider outages)
        are caught so one bad ticker can't kill the whole scan — but that means a
        scan can finish with status="success" and zero memos while almost every
        ticker silently failed, and the scan-failure Telegram path (which only
        fires when run_full_scan() itself raises) never sees it. Page the
        operator once per scan when that happens instead.
        """
        if total_scanned == 0:
            return
        threshold = float(getattr(self.settings, "catalyst_failure_rate_alert_threshold", 0.9))
        failure_rate = catalyst_failures / total_scanned
        if failure_rate <= threshold:
            return

        log.warning(
            "catalyst_error_rate_high",
            failed=catalyst_failures,
            scanned=total_scanned,
            failure_rate=round(failure_rate, 3),
        )
        if not self.notification_manager or not self.bot_loop or self.bot_loop.is_closed():
            return

        msg = (
            f"⚠️ Scan completed but nearly all LLM calls failed — "
            f"{catalyst_failures}/{total_scanned} tickers errored ({failure_rate:.0%}). "
            f"Check provider credit balance / API status."
        )
        try:
            asyncio.run_coroutine_threadsafe(
                self.notification_manager.system_message(msg),
                self.bot_loop,
            )
        except Exception as e:
            log.error("catalyst_error_rate_alert_failed", error=str(e))

    def _send_scan_notification(self, duration_s, total_scanned, escalated, memos_generated, memo_details):
        """Send scan completion notification via Telegram."""
        if not self.notification_manager or not self.bot_loop or self.bot_loop.is_closed():
            return

        # Determine scan type based on time of day
        try:
            from zoneinfo import ZoneInfo
            et_hour = datetime.now(ZoneInfo("America/New_York")).hour
        except Exception:
            et_hour = utcnow_naive().hour - 5  # rough fallback

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

    def _build_scan_list(
        self,
        discovery_output: DiscoveryOutput,
        structured_result: StructuredScanResult = None,
        gemini_result: GeminiBatchResult = None,
        allow_universe_fallback: bool = True,
    ) -> list:
        """
        Merge tier2_escalated + tier1_flagged + discovery + watchlist + remaining universe.
        Priority: tier2_gemini > tier1_scan > discovery > watchlist > universe (first seen wins).
        Tier 2 escalated tickers skip Haiku and carry Gemini's research brief.
        Tier 1 tickers that weren't screened by Gemini still skip Haiku (fallback).
        """
        seen = set()
        scan_list = []

        # Build lookup of Gemini results by ticker
        gemini_lookup = {}
        gemini_escalated_set = set()
        gemini_screened_set = set()
        if gemini_result:
            for gr in gemini_result.results:
                gemini_lookup[gr.ticker] = gr
                gemini_screened_set.add(gr.ticker)
            gemini_escalated_set = set(gemini_result.escalated)

        # Priority 0: Tier 2 Gemini-escalated tickers (skip Haiku — already researched by Gemini)
        if gemini_escalated_set:
            for ticker in gemini_result.escalated:
                if ticker not in seen:
                    seen.add(ticker)
                    gr = gemini_lookup[ticker]
                    context = f"Gemini Flash ({gr.score:.2f}): {gr.summary}"
                    if gr.catalysts:
                        context += f" | Catalysts: {', '.join(gr.catalysts[:3])}"
                    direction_hint = gr.direction if gr.direction != "neutral" else ""
                    scan_list.append(ScanTickerItem(
                        ticker=ticker,
                        sector=UNIVERSE.get(ticker, "Unknown"),
                        source="tier2_gemini",
                        haiku_threshold=0,  # Skip Haiku
                        discovery_context=context,
                        direction_hint=direction_hint,
                    ))

        # Priority 0.5: Tier 1 flagged tickers NOT screened by Gemini (fallback if Gemini unavailable)
        if structured_result:
            for flagged in structured_result.flagged:
                if flagged.symbol in gemini_screened_set:
                    continue
                if flagged.symbol not in seen:
                    seen.add(flagged.symbol)
                    catalyst_context = f"Tier 1 flags: {', '.join(flagged.catalysts)}"
                    if flagged.change_pct:
                        catalyst_context += f" | Change: {flagged.change_pct:+.1f}%"
                    if flagged.volume_ratio:
                        catalyst_context += f" | Vol ratio: {flagged.volume_ratio:.1f}x"
                    if flagged.earnings_date:
                        catalyst_context += f" | Earnings: {flagged.earnings_date}"
                    scan_list.append(ScanTickerItem(
                        ticker=flagged.symbol,
                        sector=flagged.sector or UNIVERSE.get(flagged.symbol, "Unknown"),
                        source="tier1_scan",
                        haiku_threshold=0,  # Skip Haiku
                        discovery_context=catalyst_context,
                    ))

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

        # Priority 3: Remaining universe tickers (normal Haiku threshold)
        # Only include non-flagged universe tickers if no structured scan ran
        # (to avoid processing 500 tickers when Tier 1/2 already filtered)
        has_tier_results = (gemini_escalated_set) or (structured_result and structured_result.flagged)
        if has_tier_results or not allow_universe_fallback:
            # Tier 1/2 ran successfully — only process escalated + discovery + watchlist
            pass
        else:
            # No Tier 1 results — fall back to full universe scan
            for ticker, sector in UNIVERSE.items():
                if ticker not in seen:
                    seen.add(ticker)
                    scan_list.append(ScanTickerItem(
                        ticker=ticker,
                        sector=sector,
                        source="universe",
                        haiku_threshold=self.settings.catalyst_escalation_threshold,
                    ))

        # I0: hard cap on the merged list entering catalyst. The list is already
        # in priority order (tier2 > tier1 > discovery > watchlist > universe), so
        # a head-slice never evicts a higher-priority source for a lower one.
        cap = int(getattr(self.settings, "scan_max_catalyst_tickers", 40) or 0)
        if cap > 0 and len(scan_list) > cap:
            dropped = [s.ticker for s in scan_list[cap:]]
            scan_list = scan_list[:cap]
            log.info("scan_catalyst_capped", kept=cap, dropped=len(dropped), dropped_tickers=dropped[:20])

        return scan_list

    @staticmethod
    def _cap_tier2_escalations(gemini_result, cap: int):
        """Keep the top-N tier-2 escalations by Gemini score (stable: score desc,
        ties by symbol). Returns (kept_tickers, dropped_count)."""
        escalated = list(getattr(gemini_result, "escalated", []) or [])
        if cap <= 0 or len(escalated) <= cap:
            return escalated, 0
        scores = {
            getattr(r, "ticker", ""): getattr(r, "score", 0.0)
            for r in getattr(gemini_result, "results", []) or []
        }
        ranked = sorted(escalated, key=lambda t: (-float(scores.get(t, 0.0)), t))
        kept = ranked[:cap]
        return kept, len(escalated) - len(kept)

    def _run_auto_approve(self, outcomes: list) -> dict:
        """Run the paper auto-approve pass (I2) on the bot loop. Sync wrapper for
        the scan thread; returns the AutoApprover summary."""
        candidates = [o for o in outcomes if o.scored and o.scoring_result]
        if not candidates:
            return {"placed": 0}
        if not bool(getattr(self.settings, "auto_approve_paper", False)):
            return {"placed": 0, "enabled": False}
        if not self.bot_loop or self.bot_loop.is_closed():
            log.warning("auto_approve_skipped_no_bot_loop")
            return {"placed": 0}
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.auto_approver.run(candidates), self.bot_loop
            )
            return future.result(timeout=180)
        except Exception as e:
            log.error("auto_approve_run_failed", error=str(e))
            return {"placed": 0}

    async def _auto_approve_notify(self, message: str) -> None:
        """One-time operator alert used by the AutoApprover safety guard."""
        if self.notification_manager:
            await self.notification_manager.system_message(message)

    def _process_scan_item(self, item: ScanTickerItem, regime: dict, run_id: str = "") -> ScanItemOutcome:
        """Process a single ticker through the full pipeline with source-aware routing.

        Returns a ScanItemOutcome carrying funnel telemetry (I0), the shadow
        ledger row id (I1), and everything the paper auto-approve pass needs (I2).
        """
        outcome = ScanItemOutcome(ticker=item.ticker, source=item.source, sector=item.sector)

        # Ensure ticker is in DB
        self._ensure_ticker(item.ticker)

        # Route catalyst scan based on source
        catalyst_kwargs = {"sector": item.sector}

        if item.source in ("discovery", "tier1_scan", "tier2_gemini"):
            # Skip Haiku — already validated by Discovery Agent or Tier 1 scan
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
            return outcome
        outcome.catalyst_gate_passed = True

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
        outcome.scored = True
        outcome.scoring_result = result
        outcome.regime = regime
        outcome.final_score = float(result.get("final_score", 0) or 0)
        outcome.direction = str(result.get("direction", "neutral") or "neutral")
        outcome.cohort = classify_cohort(outcome.final_score, self.settings)

        # Generate memo if above threshold
        memo_data = None
        if result.get("meets_memo_threshold"):
            with _langfuse_context(tags=["memo", item.ticker]):
                memo_data = self.memo_generator.generate(
                    item.ticker, result, catalyst, fundamental, pattern, web_research, regime,
                )
            if memo_data:
                memo_data["source"] = item.source
                outcome.memo_data = memo_data
                outcome.memo_id = memo_data.get("memo_id") or None
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

        # I1: shadow ledger — record EVERY scored candidate, never affecting the
        # pipeline on failure (record_scored_candidate swallows its own errors).
        trade_params = (memo_data or {}).get("trade_params", {})
        outcome.ledger_id = record_scored_candidate(
            self.settings,
            run_id=run_id,
            ticker=item.ticker,
            source=item.source,
            scoring_result=result,
            regime=regime,
            memo_generated=bool(memo_data),
            cohort=outcome.cohort,
            entry_price=trade_params.get("entry_price"),
            suggested_stop=trade_params.get("stop_loss"),
            target_1=trade_params.get("target_1"),
            price_cache=getattr(self, "_price_cache", None),
        )

        return outcome

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
        session_id = f"adhoc-{ticker}-{utcnow_naive().strftime('%Y%m%d-%H%M%S')}"
        self._start_pipeline_run(session_id, "ad_hoc", metadata={"ticker": ticker})
        try:
            with _langfuse_context(session_id=session_id, tags=["ad_hoc", ticker]):
                memo_data = self._run_ad_hoc_inner(ticker, thesis, _progress, session_id)
            self._finish_pipeline_run(
                session_id,
                status="success",
                scanned_count=1,
                researched_count=1,
                memos_generated=1 if memo_data else 0,
                metadata={"ticker": ticker, "memo_id": memo_data.get("memo_id") if memo_data else None},
            )
            return memo_data
        except Exception as e:
            self._finish_pipeline_run(session_id, status="failed", errors=[str(e)])
            raise

    def _run_ad_hoc_inner(self, ticker: str, thesis: str, _progress, run_id: str = "") -> dict:
        """Inner ad-hoc logic wrapped by Langfuse session context."""
        # Ensure ticker is in DB
        self._ensure_ticker(ticker)

        sector = self.get_sector(ticker)

        # 1. Get regime
        _progress("Checking macro regime...")
        regime = self.macro_agent.get_latest_regime()

        # 2. Run all agents
        # Per-stage Langfuse tags mirror the scheduled path (_process_scan_item) so
        # ad-hoc scoring calls carry the `scoring` tag and land in the BRY-243 corpus
        # (audit spec G1 / P2-LF-1). Nested contexts merge tags onto the outer
        # `ad_hoc` session. fundamental/pattern/web_research are tagged inside the
        # shared _run_post_catalyst_agents.
        _progress("Running catalyst analysis (Haiku + Sonnet)...")
        with _langfuse_context(tags=["catalyst", ticker]):
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
        with _langfuse_context(tags=["scoring", ticker]):
            result = self.scoring_engine.score_opportunity(
                ticker, catalyst, fundamental, pattern, web_research,
                regime, portfolio_context,
            )

        # 4. Generate memo (always for ad-hoc, regardless of threshold)
        _progress("Generating IC memo...")
        with _langfuse_context(tags=["memo", ticker]):
            memo_data = self.memo_generator.generate(
                ticker, result, catalyst, fundamental, pattern, web_research, regime,
            )

        # I1: shadow ledger — ad-hoc scored candidates are recorded too. Failure
        # isolated; never affects the returned memo.
        trade_params = (memo_data or {}).get("trade_params", {})
        record_scored_candidate(
            self.settings,
            run_id=run_id,
            ticker=ticker,
            source="ad_hoc",
            scoring_result=result,
            regime=regime,
            memo_generated=bool(memo_data),
            entry_price=trade_params.get("entry_price"),
            suggested_stop=trade_params.get("stop_loss"),
            target_1=trade_params.get("target_1"),
            price_cache=getattr(self, "_price_cache", None),
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
            account = self.broker.get_account_info()
            positions = self.broker.get_positions_detail()
            parts = [
                f"Portfolio: ${account.get('equity', 0):,.2f}, Cash: ${account.get('cash', 0):,.2f}",
                f"Open positions: {len(positions)}",
            ]
            for p in positions:
                parts.append(f"  {p['ticker']}: {p.get('qty', 0)} shares, P&L {p.get('pnl_pct', 0):+.2f}%")
            return "\n".join(parts)
        except Exception:
            return f"Portfolio: ${self.settings.portfolio_value:,.2f} (initial), 0 positions"

    def _start_pipeline_run(self, run_id: str, trigger_source: str, metadata: dict | None = None) -> None:
        try:
            from database.db import get_session
            from database.models import PipelineRun

            with get_session() as session:
                session.add(
                    PipelineRun(
                        run_id=run_id,
                        trigger_source=trigger_source,
                        status="running",
                        metadata_json=json.dumps(metadata or {}),
                    )
                )
        except Exception as e:
            log.warning("pipeline_run_start_failed", run_id=run_id, error=str(e))

    def _finish_pipeline_run(
        self,
        run_id: str,
        status: str,
        scanned_count: int = 0,
        screened_count: int = 0,
        researched_count: int = 0,
        memos_generated: int = 0,
        duration_s: float | None = None,
        errors: list[str] | None = None,
        metadata: dict | None = None,
    ) -> None:
        try:
            from database.db import get_session
            from database.models import PipelineRun

            with get_session() as session:
                row = session.query(PipelineRun).filter_by(run_id=run_id).first()
                if not row:
                    return
                row.status = status
                row.ended_at = utcnow_naive()
                row.scanned_count = scanned_count
                row.screened_count = screened_count
                row.researched_count = researched_count
                row.memos_generated = memos_generated
                row.duration_s = duration_s
                row.errors_json = json.dumps(errors or [])
                if metadata is not None:
                    row.metadata_json = json.dumps(metadata)
        except Exception as e:
            log.warning("pipeline_run_finish_failed", run_id=run_id, error=str(e))

    def _ensure_ticker(self, ticker: str):
        """Make sure a ticker exists in the DB (for ad-hoc analysis)."""
        from database.db import get_session
        from database.models import Ticker
        with get_session() as session:
            existing = session.query(Ticker).filter_by(symbol=ticker).first()
            if not existing:
                sector = UNIVERSE.get(ticker, "Unknown")
                session.add(Ticker(symbol=ticker, sector=sector, in_universe=ticker in UNIVERSE))
