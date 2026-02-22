"""
Manages the Haiku → Sonnet → Opus escalation chain.
Haiku pre-screens → Sonnet analyzes if relevant → Opus judges if trade-worthy.
"""

from utils.anthropic_client import AnthropicClient
from utils.model_selector import get_model
from utils.logger import get_logger

log = get_logger("escalation")


class EscalationManager:
    def __init__(self, client: AnthropicClient, settings=None):
        self.client = client
        self.settings = settings
        self.threshold = settings.catalyst_escalation_threshold if settings else 3

    def haiku_prescreen(self, content: str, ticker: str, context: str = "") -> dict:
        """
        Haiku pre-screening: Is this content relevant and material?
        Returns: {relevant: bool, score: 1-5, category: str, summary: str}
        """
        model = get_model("catalyst_prescreen", self.settings)
        system = (
            "You are a financial news relevance filter. Score the materiality of this news/filing "
            "for potential swing trade impact (1-5 days to 20 days holding period).\n"
            "Score 1: Routine/irrelevant\n"
            "Score 2: Mildly interesting but unlikely to move stock\n"
            "Score 3: Potentially material — worth deeper analysis\n"
            "Score 4: Likely material — clear potential catalyst\n"
            "Score 5: Highly material — significant event (earnings surprise, M&A, FDA, activist)"
        )
        prompt = (
            f"Ticker: {ticker}\n"
            f"Context: {context}\n\n"
            f"Content to evaluate:\n{content[:3000]}\n\n"
            'Respond with JSON: {{"score": 1-5, "category": "earnings_surprise|insider_buying|'
            'analyst_revision|m_and_a|product_regulatory|management_change|capital_allocation|'
            'sector_macro|other", "summary": "one sentence", "direction": "bullish|bearish|ambiguous"}}'
        )
        result = self.client.analyze_json(model, system, prompt, max_tokens=300)
        score = result.get("score", 1)
        result["relevant"] = score >= self.threshold
        log.info("haiku_prescreen", ticker=ticker, score=score, relevant=result["relevant"])
        return result

    def sonnet_analyze(self, ticker: str, catalyst_text: str, haiku_result: dict,
                       company_context: str = "") -> dict:
        """
        Sonnet deep analysis: What does this catalyst mean for the stock?
        Called when Haiku score >= threshold.
        V2: Returns materiality + direction_confidence instead of single confidence.
        """
        model = get_model("catalyst_analyze", self.settings)
        system = (
            "You are a senior equity research analyst evaluating a potential catalyst for a swing trade "
            "(1-20 trading day holding period). Provide a thorough analysis of the catalyst's likely "
            "impact on the stock price."
        )
        prompt = (
            f"Ticker: {ticker}\n"
            f"Catalyst category: {haiku_result.get('category', 'unknown')}\n"
            f"Initial assessment: {haiku_result.get('summary', '')}\n\n"
            f"Company context:\n{company_context}\n\n"
            f"Full catalyst content:\n{catalyst_text[:5000]}\n\n"
            "CATALYST TYPE HIERARCHY (use the HIGHEST applicable type):\n"
            "1. earnings_surprise — if earnings within 5 trading days, ALWAYS primary\n"
            "2. m_and_a — acquisition, merger, activist campaign, spinoff\n"
            "3. fda_regulatory — FDA decision, regulatory ruling\n"
            "4. insider_buying — cluster purchases (3+ insiders in 14 days)\n"
            "5. analyst_revision — upgrade/downgrade cluster, price target changes\n"
            "6. product_launch — major product release, contract win\n"
            "7. management_change — CEO/CFO appointment or departure\n"
            "8. sector_macro — sector rotation, tariff, policy change\n\n"
            "RULE: If earnings are within 5 trading days, catalyst_type MUST be an\n"
            "earnings_* variant. Other factors become catalyst_modifiers.\n\n"
            "Respond with JSON:\n"
            "{\n"
            '  "catalyst_type": "string (use hierarchy above)",\n'
            '  "catalyst_modifiers": ["list of secondary factors, if any"],\n'
            '  "catalyst_summary": "2-3 sentence summary",\n'
            '  "magnitude": 1-5,\n'
            '  "direction": "bullish|bearish|ambiguous",\n'
            '  "materiality": 0.0-1.0,\n'
            '  "direction_confidence": 0.0-1.0,\n'
            '  "expected_impact_pct": {"low": float, "mid": float, "high": float},\n'
            '  "time_horizon_days": int,\n'
            '  "reasoning": "detailed analysis (3-5 sentences)",\n'
            '  "risk_analysis": {\n'
            '    "risks": [\n'
            '      {"risk": "specific risk statement", "probability": "likely|possible|unlikely",\n'
            '       "severity_pct": float, "trigger": "what to watch", "timeline": "when"}\n'
            '    ],\n'
            '    "failure_mode": "1-2 sentence MOST LIKELY way this trade loses money"\n'
            '  }\n'
            "}\n\n"
            "SCORING GUIDANCE:\n"
            "- materiality: How significant/confirmed is this event? "
            "(0.9+ = major confirmed event like earnings beat, FDA approval; "
            "0.5-0.8 = notable but uncertain; <0.5 = minor/unconfirmed)\n"
            "- direction_confidence: How confident in the price direction? "
            "(0.8+ = clear directional signal; 0.5-0.7 = likely but uncertain; "
            "<0.5 = genuinely ambiguous)\n\n"
            "RISK ANALYSIS GUIDANCE:\n"
            "- Provide exactly 3 risks, ranked by expected impact (probability x severity)\n"
            "- Be specific, not generic. 'Market could go down' is not useful.\n"
            "- severity_pct: estimated max drawdown if this risk materializes\n"
            "- failure_mode: the single most likely scenario where this trade loses money\n"
            "- Do NOT hedge both sides. Take a view on the most probable outcome."
        )
        result = self.client.analyze_json(model, system, prompt, max_tokens=2000)
        # Backwards compatibility: if model returns 'confidence' instead of new fields
        if "confidence" in result and "materiality" not in result:
            result["materiality"] = result["confidence"]
            result["direction_confidence"] = result["confidence"]
        # Backwards compatibility: wrap old counter_arguments into risk_analysis
        if "counter_arguments" in result and "risk_analysis" not in result:
            result["risk_analysis"] = {
                "risks": [{"risk": result["counter_arguments"], "probability": "possible",
                           "severity_pct": 5.0, "trigger": "N/A", "timeline": "N/A"}],
                "failure_mode": result["counter_arguments"],
            }
        log.info(
            "sonnet_analyze",
            ticker=ticker,
            magnitude=result.get("magnitude"),
            materiality=result.get("materiality"),
            direction_confidence=result.get("direction_confidence"),
        )
        return result

    def opus_evaluate(self, ticker: str, all_signals: dict, portfolio_context: str = "") -> dict:
        """
        Opus final evaluation: Is this actually a good trade?
        Receives all signal layers and stress-tests the thesis.
        V2: Uses extended thinking for deeper reasoning when budget > 0.
        """
        model = get_model("trade_score", self.settings)
        system = (
            "You are a portfolio manager making the final decision on whether to take a swing trade. "
            "You receive analysis from your research team (catalyst, fundamental, pattern, web research) "
            "and the current macro regime. Your job is to:\n"
            "1. Assign a final conviction score (0.0-1.0)\n"
            "2. Stress-test the bull case — actively look for weaknesses\n"
            "3. Assess whether the risk/reward justifies a position\n"
            "4. Flag any signal disagreements and adjudicate them\n"
            "5. Adjudicate Sonnet's risk ranking — confirm, re-rank, or flag missed risks\n\n"
            "Be skeptical by default. Your job is to protect capital."
        )
        prompt = (
            f"Ticker: {ticker}\n\n"
            f"=== SIGNAL PACKAGE ===\n\n"
            f"CATALYST:\n{_format_signal(all_signals.get('catalyst', {}))}\n\n"
            f"FUNDAMENTALS:\n{_format_signal(all_signals.get('fundamental', {}))}\n\n"
            f"HISTORICAL PATTERN:\n{_format_signal(all_signals.get('pattern', {}))}\n\n"
            f"WEB RESEARCH:\n{_format_signal(all_signals.get('web_research', {}))}\n\n"
            f"MACRO REGIME:\n{_format_signal(all_signals.get('macro', {}))}\n\n"
            f"PORTFOLIO CONTEXT:\n{portfolio_context}\n\n"
            "Respond with JSON:\n"
            "{\n"
            '  "final_score": 0.0-1.0,\n'
            '  "conviction": "high|moderate|low|pass",\n'
            '  "stress_test": "2-3 sentences poking holes in the bull case",\n'
            '  "signal_agreement": "all_aligned|mostly_aligned|mixed|conflicting",\n'
            '  "key_risk": "single biggest risk in one sentence",\n'
            '  "recommendation": "proceed|reduce_size|watchlist|pass",\n'
            '  "position_size_adjustment": 0.5-1.5,\n'
            '  "reasoning": "3-5 sentences on your overall assessment",\n'
            '  "risk_ranking_correct": true/false,\n'
            '  "missed_risks": "any risks Sonnet missed (or empty string)",\n'
            '  "failure_mode_assessment": "agree|disagree — brief reason"\n'
            "}"
        )

        # V2: Use extended thinking when budget is configured
        thinking_budget = getattr(self.settings, "opus_thinking_budget", 0) if self.settings else 0

        if thinking_budget > 0:
            log.info("opus_evaluate_with_thinking", ticker=ticker, budget=thinking_budget)
            result = self.client.analyze_json_with_thinking_and_fallback(
                model, system, prompt,
                budget_tokens=thinking_budget,
                max_tokens=max(16000, thinking_budget + 8192),
            )
        else:
            result = self.client.analyze_json_with_fallback(model, system, prompt, max_tokens=4000)

        log.info(
            "opus_evaluate",
            ticker=ticker,
            final_score=result.get("final_score"),
            conviction=result.get("conviction"),
            recommendation=result.get("recommendation"),
        )
        return result

    def opus_reevaluate(
        self, ticker: str, original_evaluation: dict,
        deep_research_report: str, original_score: float,
    ) -> dict:
        """
        Opus re-evaluation after deep research completes.
        Can confirm, upgrade, or downgrade the original recommendation.
        """
        model = get_model("trade_score", self.settings)
        system = (
            "You are a portfolio manager reviewing a trade proposal that has been enhanced with "
            "deep research findings. You previously evaluated this trade and now have significantly "
            "more information. Re-evaluate your original decision.\n\n"
            "You may:\n"
            "1. CONFIRM your original recommendation (with added confidence)\n"
            "2. UPGRADE from watchlist/pass to proceed/reduce_size\n"
            "3. DOWNGRADE from proceed to reduce_size/watchlist/pass\n"
            "4. ADJUST position size or trade parameters\n\n"
            "Be specific about what the deep research changed in your assessment."
        )
        prompt = (
            f"Ticker: {ticker}\n\n"
            f"=== ORIGINAL EVALUATION ===\n"
            f"Score: {original_score:.2f}\n"
            f"Conviction: {original_evaluation.get('conviction', '?')}\n"
            f"Recommendation: {original_evaluation.get('recommendation', '?')}\n"
            f"Key Risk: {original_evaluation.get('key_risk', 'N/A')}\n"
            f"Reasoning: {original_evaluation.get('reasoning', 'N/A')}\n\n"
            f"=== DEEP RESEARCH FINDINGS ===\n"
            f"{deep_research_report[:8000]}\n\n"
            "Based on the deep research findings, re-evaluate this trade.\n"
            "Respond with JSON:\n"
            "{\n"
            '  "final_score": 0.0-1.0,\n'
            '  "conviction": "high|moderate|low|pass",\n'
            '  "recommendation": "proceed|reduce_size|watchlist|pass",\n'
            '  "recommendation_changed": true/false,\n'
            '  "position_size_adjustment": 0.5-1.5,\n'
            '  "key_insight_from_research": "most impactful finding from deep research",\n'
            '  "updated_key_risk": "revised key risk based on new information",\n'
            '  "reasoning": "3-5 sentences explaining what changed and why"\n'
            "}"
        )
        # V2: Use extended thinking for re-evaluation (same budget as initial eval)
        thinking_budget = getattr(self.settings, "opus_thinking_budget", 0) if self.settings else 0

        if thinking_budget > 0:
            log.info("opus_reevaluate_with_thinking", ticker=ticker, budget=thinking_budget)
            result = self.client.analyze_json_with_thinking_and_fallback(
                model, system, prompt,
                budget_tokens=thinking_budget,
                max_tokens=max(16000, thinking_budget + 8192),
            )
        else:
            result = self.client.analyze_json_with_fallback(model, system, prompt, max_tokens=4000)

        log.info(
            "opus_reevaluate",
            ticker=ticker,
            original_score=original_score,
            new_score=result.get("final_score"),
            changed=result.get("recommendation_changed"),
            recommendation=result.get("recommendation"),
        )
        return result


def _format_signal(signal: dict) -> str:
    """Format a signal dict for inclusion in a prompt."""
    if not signal:
        return "No data available."
    lines = []
    for k, v in signal.items():
        if isinstance(v, dict):
            lines.append(f"  {k}:")
            for k2, v2 in v.items():
                lines.append(f"    {k2}: {v2}")
        else:
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)
