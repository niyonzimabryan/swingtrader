"""
Scoring Engine — aggregates signals, applies weights, gets Opus evaluation.
V2: Web research replaces sentiment. Opus clamping. Confidence-weighted alignment.
"""

import json
from agents.base_agent import AgentOutput
from scoring.weights import SIGNAL_WEIGHTS, OPUS_MAX_DELTA, DIRECTION_PENALTY_PARTIAL, DIRECTION_PENALTY_MAJOR, SCORE_THRESHOLDS
from utils.escalation_manager import EscalationManager
from utils.logger import get_logger

log = get_logger("scoring_engine")

ACTIVE_OPINION_STATUSES = {"active", "decomposed"}
NO_OPINION_STATUSES = {
    "disabled",
    "error",
    "insufficient_forward_returns",
    "low_confidence_peers",
    "no_data",
    "no_matches",
    "provider_error",
    "stub",
    "unsupported",
}


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
        web_research: AgentOutput,
        regime: dict,
        portfolio_context: str = "",
    ) -> dict:
        """
        Compute composite score and get Opus evaluation.
        V2: web_research replaces sentiment. Opus delta clamped. Confidence-weighted alignment.
        """
        log.info("scoring_start", ticker=ticker)

        agents = {
            "catalyst": (catalyst, SIGNAL_WEIGHTS["catalyst"]),
            "fundamental": (fundamental, SIGNAL_WEIGHTS["fundamental"]),
            "pattern": (pattern, SIGNAL_WEIGHTS["pattern"]),
            "web_research": (web_research, SIGNAL_WEIGHTS["web_research"]),
        }
        counted_agents, effective_weights = self._effective_signal_weights(agents)

        # 1. Raw weighted score
        raw_score = (
            sum(agent.score * effective_weights[name] for name, (agent, _) in counted_agents.items())
            if counted_agents
            else 0.5
        )

        # 2. Confidence-weighted direction alignment
        def _normalize_dir(d):
            return "neutral" if d in ("neutral", "ambiguous") else d

        directions = {name: _normalize_dir(agent.direction) for name, (agent, _) in agents.items()}

        # Derive primary_direction from highest-weight non-neutral signal
        primary_direction = "neutral"
        for agent_name in ("catalyst", "fundamental", "pattern", "web_research"):
            if agent_name not in counted_agents:
                continue
            if directions[agent_name] not in ("neutral",):
                primary_direction = directions[agent_name]
                break
        # If all signals are neutral, leave as neutral (no forced direction)
        # This lets bearish and bullish signals both flow through naturally

        # V2: Confidence-weighted disagreement penalty
        # Instead of binary counting, weight disagreements by confidence * weight
        disagreement_penalty = 0.0
        total_non_neutral_weight = 0.0

        for agent_name, (agent, _) in counted_agents.items():
            weight = effective_weights[agent_name]
            d = directions[agent_name]
            if d == "neutral":
                continue
            total_non_neutral_weight += weight
            if d != primary_direction:
                # Penalty proportional to confidence and weight
                disagreement_penalty += agent.confidence * weight

        # Convert to alignment modifier
        if total_non_neutral_weight > 0 and disagreement_penalty > 0:
            disagreement_ratio = disagreement_penalty / total_non_neutral_weight
            if disagreement_ratio > 0.4:
                alignment_modifier = DIRECTION_PENALTY_MAJOR
                signal_agreement = "conflicting"
            elif disagreement_ratio > 0.15:
                alignment_modifier = DIRECTION_PENALTY_PARTIAL
                signal_agreement = "mostly_aligned"
            else:
                alignment_modifier = 1.0
                signal_agreement = "all_aligned"
        else:
            alignment_modifier = 1.0
            signal_agreement = "all_aligned"

        adjusted_score = raw_score * alignment_modifier

        # 3. Opus evaluation (final score + stress test)
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
                    "status": pattern.raw_data.get("status"),
                    "reasoning": pattern.reasoning,
                    # V2: similarity-weighted stats for Opus evaluation
                    "total_instances": pattern.raw_data.get("total_instances"),
                    "weighted_win_rate_t10": pattern.raw_data.get("weighted_win_rate_t10"),
                    "hs_count": pattern.raw_data.get("hs_count"),
                    "hs_win_rate_t10": pattern.raw_data.get("hs_win_rate_t10"),
                    "hs_median_return_t10": pattern.raw_data.get("hs_median_return_t10"),
                    "most_similar_instance": pattern.raw_data.get("most_similar_instance"),
                },
                "web_research": {
                    "score": web_research.score,
                    "confidence": web_research.confidence,
                    "direction": web_research.direction,
                    "reasoning": web_research.reasoning,
                    "key_finding": web_research.raw_data.get("key_finding", ""),
                },
                "macro": regime,
            }
            opus_result = self.escalation.opus_evaluate(ticker, all_signals, portfolio_context)

            # V2: Opus delta clamping
            if "final_score" in opus_result and not opus_result.get("error"):
                opus_score = opus_result["final_score"]
                opus_delta = opus_score - adjusted_score
                clamped = False

                if abs(opus_delta) > OPUS_MAX_DELTA:
                    clamped_delta = max(-OPUS_MAX_DELTA, min(OPUS_MAX_DELTA, opus_delta))
                    final_score = adjusted_score + clamped_delta
                    log.warning(
                        "opus_delta_clamped",
                        ticker=ticker,
                        original_delta=round(opus_delta, 4),
                        clamped_delta=round(clamped_delta, 4),
                        adjusted=round(adjusted_score, 4),
                        final=round(final_score, 4),
                    )
                    clamped = True
                    opus_result["delta_clamped"] = True
                    opus_result["original_opus_score"] = opus_score
                    opus_result["clamped_delta"] = round(clamped_delta, 4)
                else:
                    final_score = opus_score

        # Ensure final_score is clamped to [0, 1]
        final_score = max(0.0, min(1.0, final_score))

        # 4. Classify
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
            "signal_breakdown": {
                "catalyst": self._breakdown_entry("catalyst", catalyst, effective_weights),
                "fundamental": self._breakdown_entry("fundamental", fundamental, effective_weights),
                "pattern": self._breakdown_entry("pattern", pattern, effective_weights),
                "web_research": self._breakdown_entry("web_research", web_research, effective_weights),
            },
            "regime": regime,
            "opus_evaluation": opus_result,
            "meets_memo_threshold": final_score >= self.settings.memo_threshold,
        }

        log.info(
            "scoring_result", ticker=ticker,
            raw=raw_score, adjusted=adjusted_score, final=final_score,
            classification=classification,
            agreement=signal_agreement,
        )

        return result

    def _effective_signal_weights(self, agents: dict) -> tuple[dict, dict]:
        counted = {}
        for name, (agent, original_weight) in agents.items():
            status = (agent.raw_data or {}).get("status")
            if status in NO_OPINION_STATUSES and status not in ACTIVE_OPINION_STATUSES:
                continue
            counted[name] = (agent, original_weight)

        total_weight = sum(weight for _, weight in counted.values())
        weights = {name: 0.0 for name in agents}
        if total_weight <= 0:
            return counted, weights
        for name, (_, weight) in counted.items():
            weights[name] = weight / total_weight
        return counted, weights

    def _breakdown_entry(self, name: str, agent: AgentOutput, effective_weights: dict) -> dict:
        return {
            "score": round(agent.score, 3),
            "weight": SIGNAL_WEIGHTS[name],
            "effective_weight": round(effective_weights.get(name, 0.0), 4),
            "counted": effective_weights.get(name, 0.0) > 0,
            "direction": agent.direction,
            "confidence": agent.confidence,
            "status": (agent.raw_data or {}).get("status"),
        }

    def _classify(self, score: float) -> str:
        """Classify score into conviction levels."""
        if score >= SCORE_THRESHOLDS["high_conviction"]:
            return "high_conviction"
        elif score >= SCORE_THRESHOLDS["moderate"]:
            return "moderate"
        elif score >= SCORE_THRESHOLDS["low"]:
            return "low"
        return "no_action"
