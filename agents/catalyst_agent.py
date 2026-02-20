"""
Catalyst Agent — primary trade idea generator.
Scans for actionable catalysts using Haiku→Sonnet escalation.
Detects: earnings surprises, insider buying, analyst revisions, M&A, product/regulatory, etc.
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
        1. Fetch news + filings
        2. Keyword pre-filter
        3. Haiku pre-screen (relevance 1-5)
        4. Sonnet deep analysis for score >= threshold
        """
        if not ticker:
            return AgentOutput(agent_type=self.agent_type, reasoning="No ticker provided")

        log.info("catalyst_scan_start", ticker=ticker)

        # Allow passing a thesis directly (for /test command)
        provided_thesis = kwargs.get("thesis", "")

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

        for candidate in candidates[:10]:  # Cap at 10 to manage API costs
            if not self.escalation:
                continue
            haiku_result = self.escalation.haiku_prescreen(
                candidate["text"], ticker, company_context
            )
            score = haiku_result.get("score", 1)

            # Save every catalyst to DB for analysis
            self._save_catalyst(ticker, candidate, haiku_result, escalated=score >= self.settings.catalyst_escalation_threshold)

            if score > best_haiku_score:
                best_haiku_score = score
                best_catalyst = {**candidate, "haiku_result": haiku_result}

        # If best candidate doesn't meet threshold, return low score
        if not best_catalyst or best_haiku_score < self.settings.catalyst_escalation_threshold:
            return AgentOutput(
                agent_type=self.agent_type, ticker=ticker,
                score=best_haiku_score / 5.0 * 0.5,  # Scale to 0-0.5 range
                confidence=0.4,
                direction=best_catalyst["haiku_result"].get("direction", "neutral") if best_catalyst else "neutral",
                reasoning=f"Haiku pre-screen: best score {best_haiku_score}/5, below escalation threshold.",
                raw_data={"haiku_score": best_haiku_score, "candidates_found": len(candidates)},
                run_id=self.run_id,
            )

        # Escalate to Sonnet for deep analysis
        sonnet_result = self.escalation.sonnet_analyze(
            ticker, best_catalyst["text"], best_catalyst["haiku_result"], company_context
        )

        # Map Sonnet's output to agent score
        magnitude = sonnet_result.get("magnitude", 1)
        confidence = sonnet_result.get("confidence", 0.5)
        catalyst_score = (magnitude / 5.0) * confidence  # 0-1 range

        direction = sonnet_result.get("direction", "neutral")

        return AgentOutput(
            agent_type=self.agent_type,
            ticker=ticker,
            score=catalyst_score,
            confidence=confidence,
            direction=direction,
            reasoning=sonnet_result.get("reasoning", ""),
            raw_data={
                "catalyst_type": sonnet_result.get("catalyst_type", ""),
                "catalyst_summary": sonnet_result.get("catalyst_summary", ""),
                "magnitude": magnitude,
                "expected_impact_pct": sonnet_result.get("expected_impact_pct", {}),
                "time_horizon_days": sonnet_result.get("time_horizon_days", 10),
                "counter_arguments": sonnet_result.get("counter_arguments", ""),
                "haiku_score": best_haiku_score,
                "source": best_catalyst.get("source", ""),
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

        magnitude = sonnet_result.get("magnitude", 3)
        confidence = sonnet_result.get("confidence", 0.6)
        catalyst_score = (magnitude / 5.0) * confidence

        return AgentOutput(
            agent_type=self.agent_type,
            ticker=ticker,
            score=catalyst_score,
            confidence=confidence,
            direction=sonnet_result.get("direction", "bullish"),
            reasoning=sonnet_result.get("reasoning", ""),
            raw_data={
                "catalyst_type": sonnet_result.get("catalyst_type", "operator_provided"),
                "catalyst_summary": sonnet_result.get("catalyst_summary", thesis[:200]),
                "magnitude": magnitude,
                "expected_impact_pct": sonnet_result.get("expected_impact_pct", {}),
                "time_horizon_days": sonnet_result.get("time_horizon_days", 10),
                "counter_arguments": sonnet_result.get("counter_arguments", ""),
                "haiku_score": 5,
                "source": "operator",
                "provided_thesis": thesis,
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
