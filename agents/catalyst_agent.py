"""
Catalyst Agent — primary trade idea generator.
Scans for actionable catalysts using Haiku→Sonnet escalation.
Detects: earnings surprises, insider buying, analyst revisions, M&A, product/regulatory, etc.

V2 changes:
- Materiality/direction_confidence split (replaces single 'confidence')
- skip_haiku support for Discovery-sourced tickers
- haiku_threshold_override for Watchlist tickers
"""

import re
import json
from datetime import datetime
from agents.base_agent import BaseAgent, AgentOutput
from data.news_data import NewsDataAdapter
from data.sec_data import SECDataAdapter
from data.market_data import MarketDataAdapter
from utils.escalation_manager import EscalationManager
from database.db import get_session
from database.models import Catalyst, Ticker
from utils.logger import get_logger

log = get_logger("catalyst_agent")

# Keywords for pre-filtering before sending to Haiku
CATALYST_KEYWORDS = re.compile(
    r"(?i)(earnings|revenue|beat|miss|guidance|raise|lower|upgrade|downgrade|"
    r"price target|insider|buy(?:ing|s)?|purchase|acquisition|acquire|merger|"
    r"activist|FDA|approv|launch|contract|award|buyback|repurchase|dividend|"
    r"restructur|layoff|CEO|CFO|appoint|resign|analyst|rating|outperform|"
    r"overweight|underweight|sell|strong buy|initiat)",
)


def _compute_catalyst_score(sonnet_result: dict) -> tuple:
    """
    V2 scoring: materiality * 0.7 + direction_confidence * 0.3
    Returns (score, materiality, direction_confidence)
    If Sonnet returned an error response, returns low scores to signal failure.
    """
    # Guard: failed Sonnet response should not look like a valid neutral signal
    if "error" in sonnet_result:
        return (0.1, 0.1, 0.1)

    materiality = sonnet_result.get("materiality", 0.5)
    direction_confidence = sonnet_result.get("direction_confidence", 0.5)

    # Backwards compat: if model returns old 'confidence' field
    if "confidence" in sonnet_result and "materiality" not in sonnet_result:
        materiality = sonnet_result["confidence"]
        direction_confidence = sonnet_result["confidence"]

    catalyst_score = materiality * 0.7 + direction_confidence * 0.3
    return catalyst_score, materiality, direction_confidence


class CatalystAgent(BaseAgent):
    agent_type = "catalyst"

    def __init__(self, settings, anthropic_client=None):
        super().__init__(settings, anthropic_client)
        self.news_data = NewsDataAdapter(settings.finnhub_api_key)
        self.sec_data = SECDataAdapter()
        self.market_data = MarketDataAdapter()
        self.escalation = EscalationManager(anthropic_client, settings) if anthropic_client else None

    def analyze(self, ticker: str = None, **kwargs) -> AgentOutput:
        """
        Scan for catalysts for a single ticker.

        V2 kwargs:
        - skip_haiku (bool): Skip Haiku pre-screen (Discovery-sourced tickers)
        - discovery_context (str): Pre-validated catalyst context from Discovery
        - direction_hint (str): Direction hint from Discovery
        - haiku_threshold_override (int): Override Haiku threshold (Watchlist tickers)
        - thesis (str): Operator-provided thesis (ad-hoc /test)
        """
        if not ticker:
            return AgentOutput(agent_type=self.agent_type, reasoning="No ticker provided")

        log.info("catalyst_scan_start", ticker=ticker)

        # Allow passing a thesis directly (for /test command)
        provided_thesis = kwargs.get("thesis", "")

        # V2: Discovery-sourced tickers skip Haiku
        skip_haiku = kwargs.get("skip_haiku", False)
        discovery_context = kwargs.get("discovery_context", "")
        direction_hint = kwargs.get("direction_hint", "bullish")
        haiku_threshold_override = kwargs.get("haiku_threshold_override", None)

        # Gather raw data
        news = self.news_data.get_company_news(ticker, days=2)
        filings = self.sec_data.get_recent_filings(ticker)
        price_data = self.market_data.get_current_price(ticker)

        company_context = (
            f"Ticker: {ticker}, "
            f"Current price: ${price_data.get('price', '?')}, "
            f"Change: {price_data.get('change_pct', '?')}%, "
            f"Sector: {kwargs.get('sector', 'Unknown')}"
        )

        # If a thesis was provided (ad-hoc /test), skip Haiku and go straight to Sonnet
        if provided_thesis and self.escalation:
            return self._analyze_provided_thesis(ticker, provided_thesis, company_context, price_data)

        # V2: If skipping Haiku (Discovery-sourced), go straight to Sonnet with discovery context
        if skip_haiku and discovery_context and self.escalation:
            return self._analyze_discovery_context(
                ticker, discovery_context, direction_hint, company_context
            )

        # Collect candidate items
        candidates = []

        # News items — keyword pre-filter
        for item in news:
            text = f"{item.get('headline', '')} {item.get('summary', '')}"
            if CATALYST_KEYWORDS.search(text):
                candidates.append({
                    "type": "news",
                    "text": text,
                    "source": item.get("url", ""),
                    "datetime": item.get("datetime", ""),
                })

        # SEC filings
        for filing in filings:
            candidates.append({
                "type": "filing",
                "text": f"{filing.get('form_type', '')} filing: {filing.get('description', '')} {filing.get('company_name', '')}",
                "source": filing.get("filing_url", ""),
                "datetime": filing.get("date_filed", ""),
            })

        if not candidates:
            log.info("no_candidates", ticker=ticker)
            return AgentOutput(
                agent_type=self.agent_type, ticker=ticker,
                score=0.0, confidence=0.3, direction="neutral",
                reasoning=f"No catalyst candidates found for {ticker} in the last 48 hours.",
                run_id=self.run_id,
            )

        # Haiku pre-screen each candidate
        best_catalyst = None
        best_haiku_score = 0

        # V2: Use overridden threshold for watchlist tickers
        effective_threshold = haiku_threshold_override or self.settings.catalyst_escalation_threshold

        for candidate in candidates[:10]:  # Cap at 10 to manage API costs
            if not self.escalation:
                continue
            haiku_result = self.escalation.haiku_prescreen(
                candidate["text"], ticker, company_context
            )
            score = haiku_result.get("score", 1)

            # Save every catalyst to DB for analysis
            self._save_catalyst(ticker, candidate, haiku_result, escalated=score >= effective_threshold)

            if score > best_haiku_score:
                best_haiku_score = score
                best_catalyst = {**candidate, "haiku_result": haiku_result}

        # If best candidate doesn't meet threshold, return low score
        if not best_catalyst or best_haiku_score < effective_threshold:
            return AgentOutput(
                agent_type=self.agent_type, ticker=ticker,
                score=best_haiku_score / 5.0 * 0.5,  # Scale to 0-0.5 range
                confidence=0.4,
                direction=best_catalyst["haiku_result"].get("direction", "neutral") if best_catalyst else "neutral",
                reasoning=f"Haiku pre-screen: best score {best_haiku_score}/5, below escalation threshold ({effective_threshold}).",
                raw_data={"haiku_score": best_haiku_score, "candidates_found": len(candidates)},
                run_id=self.run_id,
            )

        # Escalate to Sonnet for deep analysis
        sonnet_result = self.escalation.sonnet_analyze(
            ticker, best_catalyst["text"], best_catalyst["haiku_result"], company_context
        )

        # V2: Materiality/direction confidence split
        catalyst_score, materiality, direction_confidence = _compute_catalyst_score(sonnet_result)

        # If Sonnet failed, propagate error clearly
        if "error" in sonnet_result:
            log.warning("sonnet_analyze_failed", ticker=ticker, error=sonnet_result.get("error", "")[:200])

        direction = sonnet_result.get("direction", "neutral")

        return AgentOutput(
            agent_type=self.agent_type,
            ticker=ticker,
            score=catalyst_score,
            confidence=(materiality + direction_confidence) / 2,
            direction=direction,
            reasoning=sonnet_result.get("reasoning", sonnet_result.get("error", "")),
            raw_data={
                "catalyst_type": sonnet_result.get("catalyst_type", ""),
                "catalyst_summary": sonnet_result.get("catalyst_summary", ""),
                "magnitude": sonnet_result.get("magnitude", 1),
                "materiality": materiality,
                "direction_confidence": direction_confidence,
                "expected_impact_pct": sonnet_result.get("expected_impact_pct", {}),
                "time_horizon_days": sonnet_result.get("time_horizon_days", 10),
                "counter_arguments": sonnet_result.get("counter_arguments", ""),
                "haiku_score": best_haiku_score,
                "source": best_catalyst.get("source", ""),
                "sonnet_error": sonnet_result.get("error"),
            },
            run_id=self.run_id,
        )

    def _analyze_discovery_context(
        self, ticker: str, discovery_context: str, direction_hint: str, company_context: str
    ) -> AgentOutput:
        """
        V2: Handle Discovery-sourced tickers — skip Haiku, go straight to Sonnet.
        Discovery Agent has already validated this as a quality idea.
        """
        haiku_result = {
            "score": 5,
            "category": "discovery_validated",
            "summary": discovery_context[:200],
            "direction": direction_hint,
            "relevant": True,
        }
        sonnet_result = self.escalation.sonnet_analyze(
            ticker, discovery_context, haiku_result, company_context
        )

        catalyst_score, materiality, direction_confidence = _compute_catalyst_score(sonnet_result)

        if "error" in sonnet_result:
            log.warning("sonnet_analyze_failed_discovery", ticker=ticker, error=sonnet_result.get("error", "")[:200])

        return AgentOutput(
            agent_type=self.agent_type,
            ticker=ticker,
            score=catalyst_score,
            confidence=(materiality + direction_confidence) / 2,
            direction=sonnet_result.get("direction", direction_hint),
            reasoning=sonnet_result.get("reasoning", sonnet_result.get("error", "")),
            raw_data={
                "catalyst_type": sonnet_result.get("catalyst_type", "discovery"),
                "catalyst_summary": sonnet_result.get("catalyst_summary", ""),
                "magnitude": sonnet_result.get("magnitude", 3),
                "materiality": materiality,
                "direction_confidence": direction_confidence,
                "expected_impact_pct": sonnet_result.get("expected_impact_pct", {}),
                "time_horizon_days": sonnet_result.get("time_horizon_days", 10),
                "counter_arguments": sonnet_result.get("counter_arguments", ""),
                "haiku_score": 5,
                "source": "discovery",
                "discovery_context": discovery_context,
                "sonnet_error": sonnet_result.get("error"),
            },
            run_id=self.run_id,
        )

    def _analyze_provided_thesis(self, ticker: str, thesis: str, company_context: str, price_data: dict) -> AgentOutput:
        """Handle ad-hoc thesis from /test command — skip Haiku, go to Sonnet."""
        haiku_result = {
            "score": 5,
            "category": "operator_provided",
            "summary": thesis[:200],
            "direction": "bullish",
            "relevant": True,
        }
        sonnet_result = self.escalation.sonnet_analyze(ticker, thesis, haiku_result, company_context)

        catalyst_score, materiality, direction_confidence = _compute_catalyst_score(sonnet_result)

        if "error" in sonnet_result:
            log.warning("sonnet_analyze_failed_thesis", ticker=ticker, error=sonnet_result.get("error", "")[:200])

        return AgentOutput(
            agent_type=self.agent_type,
            ticker=ticker,
            score=catalyst_score,
            confidence=(materiality + direction_confidence) / 2,
            direction=sonnet_result.get("direction", "bullish"),
            reasoning=sonnet_result.get("reasoning", sonnet_result.get("error", "")),
            raw_data={
                "catalyst_type": sonnet_result.get("catalyst_type", "operator_provided"),
                "catalyst_summary": sonnet_result.get("catalyst_summary", thesis[:200]),
                "magnitude": sonnet_result.get("magnitude", 3),
                "materiality": materiality,
                "direction_confidence": direction_confidence,
                "expected_impact_pct": sonnet_result.get("expected_impact_pct", {}),
                "time_horizon_days": sonnet_result.get("time_horizon_days", 10),
                "counter_arguments": sonnet_result.get("counter_arguments", ""),
                "haiku_score": 5,
                "source": "operator",
                "provided_thesis": thesis,
                "sonnet_error": sonnet_result.get("error"),
            },
            run_id=self.run_id,
        )

    def _save_catalyst(self, ticker: str, candidate: dict, haiku_result: dict, escalated: bool):
        """Persist catalyst detection to database."""
        try:
            with get_session() as session:
                ticker_obj = session.query(Ticker).filter_by(symbol=ticker).first()
                if not ticker_obj:
                    return
                cat = Catalyst(
                    ticker_id=ticker_obj.id,
                    catalyst_type=haiku_result.get("category", "unknown"),
                    summary=haiku_result.get("summary", ""),
                    magnitude=haiku_result.get("score", 1),
                    direction=haiku_result.get("direction", "neutral"),
                    confidence=0.0,
                    raw_source=candidate.get("source", ""),
                    haiku_score=haiku_result.get("score", 1),
                    escalated=escalated,
                    run_id=self.run_id,
                )
                session.add(cat)
        except Exception as e:
            log.error("save_catalyst_failed", ticker=ticker, error=str(e))
