"""
Discovery Agent — dynamic idea sourcing via web search.
Finds actionable catalysts happening NOW, beyond the static S&P 500 universe.
Discovered tickers skip Haiku pre-screen (already validated by Discovery).
Runs on scheduled scans only (NOT on /test).
"""

from dataclasses import dataclass, field
from typing import List
from datetime import datetime

from agents.base_agent import BaseAgent, AgentOutput
from utils.web_search_client import WebSearchClient
from database.db import get_session
from database.models import DiscoveredTicker as DiscoveredTickerModel
from utils.logger import get_logger

log = get_logger("discovery_agent")


@dataclass
class DiscoveredTicker:
    """A single ticker discovered via web search."""
    ticker: str
    catalyst_summary: str
    catalyst_type: str  # earnings_surprise, analyst_revision, m_and_a, etc.
    relevance_score: float  # 0-1
    direction_hint: str  # bullish, bearish, neutral
    discovery_context: str  # Full paragraph of context for Sonnet catalyst


@dataclass
class DiscoveryOutput:
    """Output from a discovery scan."""
    tickers: List[DiscoveredTicker] = field(default_factory=list)
    model_used: str = ""
    search_summary: str = ""
    run_id: str = ""


class DiscoveryAgent(BaseAgent):
    """
    Finds swing trade ideas via web search.
    Uses Sonnet + web_search tool to identify actionable catalysts.
    Returns 8-12 tickers with pre-validated catalyst context.
    """
    agent_type = "discovery"

    def __init__(self, settings, anthropic_client=None, web_search_client: WebSearchClient = None):
        super().__init__(settings, anthropic_client)
        self.web_search = web_search_client

    def analyze(self, ticker: str = None, **kwargs) -> AgentOutput:
        """Not used — Discovery uses discover() instead."""
        return AgentOutput(agent_type=self.agent_type, reasoning="Use discover() method")

    def discover(self, regime: dict = None) -> DiscoveryOutput:
        """
        Web search for actionable catalysts happening NOW.
        Returns 8-12 quality tickers with catalyst summaries.
        """
        if not self.web_search:
            log.warning("no_web_search_client")
            return DiscoveryOutput(run_id=self.run_id)

        log.info("discovery_start", run_id=self.run_id)

        regime_context = self._build_regime_context(regime)
        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(regime_context)

        model = self.settings.discovery_model
        thinking_budget = getattr(self.settings, "discovery_thinking_budget", 0)

        if thinking_budget > 0:
            log.info("discovery_using_thinking", budget=thinking_budget)
            result = self.web_search.search_and_analyze_json_with_thinking(
                system_prompt, user_prompt, model=model, max_searches=10,
                budget_tokens=thinking_budget, max_tokens=max(16000, thinking_budget + 8192),
            )
        else:
            result = self.web_search.search_and_analyze_json(
                system_prompt, user_prompt, model=model, max_searches=10, max_tokens=8192
            )

        if result.get("error"):
            log.error("discovery_failed", error=result["error"])
            return DiscoveryOutput(run_id=self.run_id)

        # Parse results
        output = self._parse_results(result, model)

        # Validate tickers
        output.tickers = self._validate_tickers(output.tickers)

        # Persist to DB
        self._save_discoveries(output)

        log.info("discovery_complete", found=len(output.tickers), run_id=self.run_id)
        return output

    def _build_regime_context(self, regime: dict) -> str:
        """Format macro regime context for the prompt."""
        if not regime:
            return ""
        return (
            f"Current macro regime: {regime.get('regime', 'neutral')} "
            f"(VIX: {regime.get('vix', 'N/A')}, "
            f"S&P distance from 200MA: {regime.get('sp500_distance_200ma', 'N/A')}%)"
        )

    def _build_system_prompt(self) -> str:
        """Build the Discovery Agent system prompt."""
        return (
            "You are an equity research discovery agent for a systematic swing trading system "
            "(1-20 day holding period). Your job is to search the web for actionable catalysts "
            "happening RIGHT NOW that could create swing trade opportunities.\n\n"
            "REQUIREMENTS:\n"
            "- Focus on US-listed equities with market cap > $500M\n"
            "- Only include tickers with SPECIFIC, ACTIONABLE catalysts (not general market commentary)\n"
            "- Each catalyst must have happened in the last 24-48 hours\n"
            "- Prefer quality over quantity — 8-12 high-quality ideas, not 20 weak ones\n"
            "- Include BOTH well-known and under-followed names\n"
            "- Look across ALL sectors, not just tech\n\n"
            "Search for:\n"
            "1. Earnings surprises (beats/misses with significant magnitude) and guidance changes\n"
            "2. Analyst upgrades/downgrades/price target changes (especially cluster revisions)\n"
            "3. FDA approvals, drug trial results, medical device clearances\n"
            "4. M&A activity (rumors, announcements, deal updates)\n"
            "5. Insider buying (especially cluster buys or large open-market purchases)\n"
            "6. Unusual volume/options activity suggesting informed positioning\n"
            "7. Management changes, activist investor activity\n"
            "8. Sector-specific catalysts (tariff changes, regulatory shifts, commodity moves)"
        )

    def _build_user_prompt(self, regime_context: str) -> str:
        """Build the user prompt for discovery."""
        max_tickers = self.settings.discovery_max_tickers
        return (
            f"Today is {datetime.utcnow().strftime('%B %d, %Y')}.\n"
            f"{regime_context}\n\n"
            "Search for the most actionable swing trade catalysts happening right now. "
            "Look at financial news, earnings reports, analyst actions, SEC filings, "
            "and unusual market activity from the last 24-48 hours.\n\n"
            "For EACH ticker you find, provide:\n"
            "- ticker: The stock symbol (US-listed)\n"
            "- catalyst_summary: 1-2 sentence summary of the catalyst\n"
            "- catalyst_type: One of: earnings_surprise, analyst_revision, m_and_a, "
            "product_regulatory, insider_activity, management_change, capital_allocation, "
            "sector_catalyst, unusual_activity, other\n"
            "- relevance_score: 0.0-1.0 (how actionable is this for a 1-20 day swing trade?)\n"
            "- direction_hint: bullish, bearish, or neutral\n"
            "- discovery_context: Full paragraph explaining the catalyst, key numbers, "
            "and why this is actionable (this will be passed to a deeper analysis agent)\n\n"
            f"Return 8-{max_tickers} tickers as JSON:\n"
            '{"tickers": [{"ticker": "AAPL", "catalyst_summary": "...", "catalyst_type": "...", '
            '"relevance_score": 0.85, "direction_hint": "bullish", "discovery_context": "..."}], '
            '"search_summary": "Brief summary of market conditions and what you found"}'
        )

    def _parse_results(self, result: dict, model: str) -> DiscoveryOutput:
        """Parse raw JSON results into DiscoveryOutput."""
        output = DiscoveryOutput(
            model_used=model,
            search_summary=result.get("search_summary", ""),
            run_id=self.run_id,
        )

        raw_tickers = result.get("tickers", [])
        for item in raw_tickers:
            ticker_sym = item.get("ticker", "").upper().strip()
            if not ticker_sym or len(ticker_sym) > 10:
                continue

            # Clamp relevance_score to 0-1
            try:
                rel_score = min(max(float(item.get("relevance_score", 0.5)), 0.0), 1.0)
            except (ValueError, TypeError):
                rel_score = 0.5

            output.tickers.append(DiscoveredTicker(
                ticker=ticker_sym,
                catalyst_summary=item.get("catalyst_summary", ""),
                catalyst_type=item.get("catalyst_type", "other"),
                relevance_score=rel_score,
                direction_hint=item.get("direction_hint", "neutral"),
                discovery_context=item.get("discovery_context", ""),
            ))

        return output

    def _validate_tickers(self, tickers: List[DiscoveredTicker]) -> List[DiscoveredTicker]:
        """
        Basic validation of discovered tickers.
        Full yfinance check happens later in pipeline.
        """
        validated = []
        seen = set()

        for t in tickers:
            # Deduplicate
            if t.ticker in seen:
                continue
            seen.add(t.ticker)

            # Basic format check — allow standard tickers and BRK.B style
            if (t.ticker.replace(".", "").replace("-", "").isalpha()
                    and 1 <= len(t.ticker) <= 6):
                validated.append(t)
            else:
                log.debug("ticker_validation_failed", ticker=t.ticker)

        return validated[:self.settings.discovery_max_tickers]

    def _save_discoveries(self, output: DiscoveryOutput):
        """Persist discovered tickers to database."""
        try:
            with get_session() as session:
                for t in output.tickers:
                    session.add(DiscoveredTickerModel(
                        ticker=t.ticker,
                        catalyst_summary=t.catalyst_summary,
                        catalyst_type=t.catalyst_type,
                        relevance_score=t.relevance_score,
                        direction_hint=t.direction_hint,
                        discovery_context=t.discovery_context,
                        model_used=output.model_used,
                        run_id=output.run_id,
                    ))
        except Exception as e:
            log.error("save_discoveries_failed", error=str(e))
