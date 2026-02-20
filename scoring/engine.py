"""
Scoring Engine — aggregates signals, applies weights, gets Opus evaluation.
"""

import json
from agents.base_agent import AgentOutput
from scoring.weights import SIGNAL_WEIGHTS, DIRECTION_PENALTY_PARTIAL, DIRECTION_PENALTY_MAJOR, SCORE_THRESHOLDS
from utils.escalation_manager import EscalationManager
from utils.logger import get_logger

log = get_logger("scoring_engine")


class ScoringEngine:
    def __init__(self, settings, anthropic_client=None):
        self.settings = settings
        self.client = anthropic_client
        self.escalation = EscalationManager(anthropic_client, settings) if anthropic_client else None

    def score_opportunity(
        self,
        ticker: str,
        catalyst: AgentOutput,
        fundamental: AgentOutput,
        pattern: AgentOutput,
        sentiment: AgentOutput,
        regime: dict,
        portfolio_context: str = "",
    ) -> dict:
        """
        Compute composite score and get Opus evaluation.
        Returns full scoring result dict.
        """
        log.info("scoring_start", ticker=ticker)

        # 1. Raw weighted score
        raw_score = (
            catalyst.score * SIGNAL_WEIGHTS["catalyst"]
            + fundamental.score * SIGNAL_WEIGHTS["fundamental"]
            + pattern.score * SIGNAL_WEIGHTS["pattern"]
            + sentiment.score * SIGNAL_WEIGHTS["sentiment"]
        )

        # 2. Handle contrarian flag (flip sentiment contribution)
        contrarian = sentiment.raw_data.get("contrarian_flag", False)
        if contrarian:
            # Recalculate with flipped sentiment
            sentiment_contribution = (1 - sentiment.score) * SIGNAL_WEIGHTS["sentiment"]
            raw_score = (
                catalyst.score * SIGNAL_WEIGHTS["catalyst"]
                + fundamental.score * SIGNAL_WEIGHTS["fundamental"]
                + pattern.score * SIGNAL_WEIGHTS["pattern"]
                + sentiment_contribution
            )

        # 3. Direction alignment check
        # Normalize: treat "ambiguous" as "neutral" everywhere
        def _normalize_dir(d):
            return "neutral" if d in ("neutral", "ambiguous") else d

        directions = {
            "catalyst": _normalize_dir(catalyst.direction),
            "fundamental": _normalize_dir(fundamental.direction),
            "pattern": _normalize_dir(pattern.direction),
            "sentiment": _normalize_dir(sentiment.direction),
        }

        # Derive primary_direction from highest-confidence non-neutral signal
        # Priority order: catalyst > fundamental > pattern > sentiment
        primary_direction = "neutral"
        for agent_name in ("catalyst", "fundamental", "pattern", "sentiment"):
            if directions[agent_name] not in ("neutral",):
                primary_direction = directions[agent_name]
                break
        # Phase 1 is long-only: default to bullish when all signals are neutral
        if primary_direction == "neutral":
            primary_direction = "bullish"

        disagreements = sum(
            1 for agent, d in directions.items()
            if d not in ("neutral",) and d != primary_direction
        )

        if disagreements >= 2:
            alignment_modifier = DIRECTION_PENALTY_MAJOR
            signal_agreement = "conflicting"
        elif disagreements == 1:
            alignment_modifier = DIRECTION_PENALTY_PARTIAL
            signal_agreement = "mostly_aligned"
        else:
            alignment_modifier = 1.0
            signal_agreement = "all_aligned"

        adjusted_score = raw_score * alignment_modifier

        # 4. Opus evaluation (final score + stress test)
        opus_result = {}
        final_score = adjusted_score

        if self.escalation:
            all_signals = {
                "catalyst": {
                    "score": catalyst.score,
                    "confidence": catalyst.confidence,
                    "direction": catalyst.direction,
                    "reasoning": catalyst.reasoning,
                    **{k: v for k, v in catalyst.raw_data.items() if k != "provided_thesis"},
                },
                "fundamental": {
                    "score": fundamental.score,
                    "confidence": fundamental.confidence,
                    "direction": fundamental.direction,
                    "reasoning": fundamental.reasoning,
                    **fundamental.raw_data,
                },
                "pattern": {
                    "score": pattern.score,
                    "confidence": pattern.confidence,
                    "direction": pattern.direction,
                    "reasoning": pattern.reasoning,
                },
                "sentiment": {
                    "score": sentiment.score,
                    "confidence": sentiment.confidence,
                    "direction": sentiment.direction,
                    "reasoning": sentiment.reasoning,
                    "contrarian_flag": contrarian,
                },
                "macro": regime,
            }
            opus_result = self.escalation.opus_evaluate(ticker, all_signals, portfolio_context)
            # Opus can override the score
            if "final_score" in opus_result and not opus_result.get("error"):
                final_score = opus_result["final_score"]

        # 5. Classify
        classification = self._classify(final_score)

        result = {
            "ticker": ticker,
            "raw_score": round(raw_score, 4),
            "adjusted_score": round(adjusted_score, 4),
            "final_score": round(final_score, 4),
            "classification": classification,
            "signal_agreement": signal_agreement,
            "direction": primary_direction,
            "directions": directions,
            "contrarian_flag": contrarian,
            "signal_breakdown": {
                "catalyst": {"score": round(catalyst.score, 3), "weight": SIGNAL_WEIGHTS["catalyst"], "direction": catalyst.direction, "confidence": catalyst.confidence},
                "fundamental": {"score": round(fundamental.score, 3), "weight": SIGNAL_WEIGHTS["fundamental"], "direction": fundamental.direction, "confidence": fundamental.confidence},
                "pattern": {"score": round(pattern.score, 3), "weight": SIGNAL_WEIGHTS["pattern"], "direction": pattern.direction, "confidence": pattern.confidence},
                "sentiment": {"score": round(sentiment.score, 3), "weight": SIGNAL_WEIGHTS["sentiment"], "direction": sentiment.direction, "confidence": sentiment.confidence},
            },
            "regime": regime,
            "opus_evaluation": opus_result,
            "meets_memo_threshold": final_score >= self.settings.memo_threshold,
        }

        log.info(
            "scoring_result", ticker=ticker,
            raw=raw_score, final=final_score,
            classification=classification,
            agreement=signal_agreement,
        )

        return result

    def _classify(self, score: float) -> str:
        """Classify score into conviction levels."""
        if score >= SCORE_THRESHOLDS["high_conviction"]:
            return "high_conviction"
        elif score >= SCORE_THRESHOLDS["moderate"]:
            return "moderate"
        elif score >= SCORE_THRESHOLDS["low"]:
            return "low"
        return "no_action"
