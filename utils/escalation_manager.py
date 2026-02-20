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
            "Respond with JSON:\n"
            "{\n"
            '  "catalyst_type": "string",\n'
            '  "catalyst_summary": "2-3 sentence summary",\n'
            '  "magnitude": 1-5,\n'
            '  "direction": "bullish|bearish|ambiguous",\n'
            '  "expected_impact_pct": {"low": float, "mid": float, "high": float},\n'
            '  "time_horizon_days": int,\n'
            '  "confidence": 0.0-1.0,\n'
            '  "reasoning": "detailed analysis (3-5 sentences)",\n'
            '  "counter_arguments": "what could go wrong (2-3 sentences)"\n'
            "}"
        )
        result = self.client.analyze_json(model, system, prompt, max_tokens=1500)
        log.info(
            "sonnet_analyze",
            ticker=ticker,
            magnitude=result.get("magnitude"),
            confidence=result.get("confidence"),
        )
        return result

    def opus_evaluate(self, ticker: str, all_signals: dict, portfolio_context: str = "") -> dict:
        """
        Opus final evaluation: Is this actually a good trade?
        Receives all signal layers and stress-tests the thesis.
        """
        model = get_model("trade_score", self.settings)
        system = (
            "You are a portfolio manager making the final decision on whether to take a swing trade. "
            "You receive analysis from your research team (catalyst, fundamental, pattern, sentiment) "
            "and the current macro regime. Your job is to:\n"
            "1. Assign a final conviction score (0.0-1.0)\n"
            "2. Stress-test the bull case — actively look for weaknesses\n"
            "3. Assess whether the risk/reward justifies a position\n"
            "4. Flag any signal disagreements and adjudicate them\n\n"
            "Be skeptical by default. Your job is to protect capital."
        )
        prompt = (
            f"Ticker: {ticker}\n\n"
            f"=== SIGNAL PACKAGE ===\n\n"
            f"CATALYST:\n{_format_signal(all_signals.get('catalyst', {}))}\n\n"
            f"FUNDAMENTALS:\n{_format_signal(all_signals.get('fundamental', {}))}\n\n"
            f"HISTORICAL PATTERN:\n{_format_signal(all_signals.get('pattern', {}))}\n\n"
            f"REDDIT SENTIMENT:\n{_format_signal(all_signals.get('sentiment', {}))}\n\n"
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
            '  "reasoning": "3-5 sentences on your overall assessment"\n'
            "}"
        )
        result = self.client.analyze_json_with_fallback(model, system, prompt, max_tokens=2000)
        log.info(
            "opus_evaluate",
            ticker=ticker,
            final_score=result.get("final_score"),
            conviction=result.get("conviction"),
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
