"""
Macro Regime Agent — classifies market environment as risk-on / neutral / risk-off.
Pure rules-based scoring. No Claude API calls needed.
Runs once daily pre-market.
"""

from datetime import date
from agents.base_agent import BaseAgent, AgentOutput
from data.macro_data import MacroDataAdapter
from data.market_data import MarketDataAdapter
from database.db import get_session
from database.models import MacroRegime
from utils.logger import get_logger
import json

log = get_logger("macro_agent")

# Regime thresholds
RISK_ON_THRESHOLD = 3
RISK_OFF_THRESHOLD = -3

REGIME_PARAMS = {
    "risk-on": {"multiplier": 1.25, "max_positions": 7, "max_exposure": 0.80},
    "neutral": {"multiplier": 0.875, "max_positions": 5, "max_exposure": 0.60},
    "risk-off": {"multiplier": 0.625, "max_positions": 3, "max_exposure": 0.40},
}


class MacroRegimeAgent(BaseAgent):
    agent_type = "macro"

    def __init__(self, settings, anthropic_client=None):
        super().__init__(settings, anthropic_client)
        self.macro_data = MacroDataAdapter(settings.fred_api_key)
        self.market_data = MarketDataAdapter()

    def analyze(self, ticker: str = None, **kwargs) -> AgentOutput:
        """Classify current macro regime using 6 indicators."""
        log.info("macro_regime_analysis_start")

        scores = {}
        details = {}
        total_score = 0

        # 1. VIX level and trend
        vix = self.market_data.get_vix()
        details["vix"] = vix
        if vix.get("level", 20) < 16 and vix.get("trend") == "declining":
            scores["vix"] = 1
        elif vix.get("level", 20) > 25 and vix.get("trend") == "rising":
            scores["vix"] = -1
        elif vix.get("level", 20) > 30:
            scores["vix"] = -2
        else:
            scores["vix"] = 0
        total_score += scores["vix"]

        # 2. Yield curve (2Y/10Y spread)
        yc = self.macro_data.get_yield_curve()
        details["yield_curve"] = yc
        if yc.get("inverted"):
            if yc.get("steepening"):
                scores["yield_curve"] = 0  # Steepening from inversion — transitional
            else:
                scores["yield_curve"] = -1
        else:
            if yc.get("spread", 0) > 0.5:
                scores["yield_curve"] = 1
            else:
                scores["yield_curve"] = 0
        total_score += scores["yield_curve"]

        # 3. Credit spreads
        cs = self.macro_data.get_credit_spreads()
        details["credit_spreads"] = cs
        if cs.get("widening") and cs.get("elevated"):
            scores["credit_spreads"] = -1
        elif not cs.get("widening") and not cs.get("elevated"):
            scores["credit_spreads"] = 1
        else:
            scores["credit_spreads"] = 0
        total_score += scores["credit_spreads"]

        # 4. S&P 500 vs 200-day MA
        sp500 = self.market_data.get_sp500_data()
        details["sp500"] = sp500
        if sp500.get("above_200"):
            scores["sp500_trend"] = 1
        else:
            scores["sp500_trend"] = -1
        total_score += scores["sp500_trend"]

        # 5. Russell 2000 vs 200-day MA (breadth confirmation)
        rut = self.market_data.get_russell2000_data()
        details["russell_2000"] = rut
        if rut.get("above_200"):
            scores["breadth"] = 1
        else:
            scores["breadth"] = -1
        total_score += scores["breadth"]

        # 6. Sector momentum dispersion
        sector_momentum = self.market_data.get_sector_etf_momentum()
        details["sector_momentum"] = sector_momentum
        if sector_momentum:
            momentums = [v["momentum_30d"] for v in sector_momentum.values()]
            avg_momentum = sum(momentums) / len(momentums) if momentums else 0
            positive_count = sum(1 for m in momentums if m > 0)
            if avg_momentum > 2 and positive_count >= 8:
                scores["sector_momentum"] = 1
            elif avg_momentum < -2 and positive_count <= 3:
                scores["sector_momentum"] = -1
            else:
                scores["sector_momentum"] = 0
        else:
            scores["sector_momentum"] = 0
        total_score += scores["sector_momentum"]

        # 7. Fed funds rate direction
        ff = self.macro_data.get_fed_funds_rate()
        details["fed_funds"] = ff
        if ff.get("direction") == "falling":
            scores["fed_funds"] = 1
        elif ff.get("direction") == "rising":
            scores["fed_funds"] = -1
        else:
            scores["fed_funds"] = 0
        total_score += scores["fed_funds"]

        # Classify regime
        if total_score >= RISK_ON_THRESHOLD:
            regime = "risk-on"
        elif total_score <= RISK_OFF_THRESHOLD:
            regime = "risk-off"
        else:
            regime = "neutral"

        params = REGIME_PARAMS[regime]
        confidence = min(1.0, abs(total_score) / 7.0)

        # Build reasoning
        signal_summary = ", ".join(f"{k}={v:+d}" for k, v in scores.items())
        reasoning = (
            f"Regime: {regime.upper()} (score {total_score:+d}/7). "
            f"Signals: {signal_summary}. "
            f"VIX at {vix.get('level', '?')} ({vix.get('trend', '?')}). "
            f"Yield curve {'inverted' if yc.get('inverted') else 'normal'} "
            f"(spread {yc.get('spread', '?')}bps). "
            f"S&P 500 {'above' if sp500.get('above_200') else 'below'} 200-day MA."
        )

        # Save to database
        self._save_regime(regime, confidence, params, reasoning, details, scores)

        log.info(
            "macro_regime_result",
            regime=regime, score=total_score, confidence=confidence,
            multiplier=params["multiplier"],
        )

        return AgentOutput(
            agent_type=self.agent_type,
            ticker=None,
            score=confidence,
            confidence=confidence,
            direction="bullish" if regime == "risk-on" else "bearish" if regime == "risk-off" else "neutral",
            reasoning=reasoning,
            raw_data={
                "regime": regime,
                "total_score": total_score,
                "scores": scores,
                "position_size_multiplier": params["multiplier"],
                "max_positions": params["max_positions"],
                "max_exposure": params["max_exposure"],
            },
            run_id=self.run_id,
        )

    def get_latest_regime(self) -> dict:
        """Get the most recent regime from the database."""
        with get_session() as session:
            regime = session.query(MacroRegime).order_by(MacroRegime.date.desc()).first()
            if regime:
                return {
                    "regime": regime.regime,
                    "confidence": regime.confidence,
                    "position_size_multiplier": regime.position_size_multiplier,
                    "max_positions": regime.max_positions,
                    "reasoning": regime.reasoning,
                    "date": str(regime.date),
                }
        # Default to neutral if no data
        return {
            "regime": "neutral",
            "confidence": 0.5,
            "position_size_multiplier": 0.875,
            "max_positions": 5,
            "reasoning": "No regime data available yet.",
            "date": str(date.today()),
        }

    def _save_regime(self, regime, confidence, params, reasoning, details, scores):
        """Persist regime to database."""
        try:
            with get_session() as session:
                today = date.today()
                existing = session.query(MacroRegime).filter_by(date=today).first()
                if existing:
                    existing.regime = regime
                    existing.confidence = confidence
                    existing.position_size_multiplier = params["multiplier"]
                    existing.max_positions = params["max_positions"]
                    existing.reasoning = reasoning
                    existing.raw_inputs = json.dumps({"details": str(details), "scores": scores})
                else:
                    entry = MacroRegime(
                        date=today,
                        regime=regime,
                        confidence=confidence,
                        position_size_multiplier=params["multiplier"],
                        max_positions=params["max_positions"],
                        reasoning=reasoning,
                        raw_inputs=json.dumps({"details": str(details), "scores": scores}),
                    )
                    session.add(entry)
        except Exception as e:
            log.error("save_regime_failed", error=str(e))
