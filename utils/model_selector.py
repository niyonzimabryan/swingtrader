"""
Maps task types to Claude model IDs.
Three-tier escalation: Haiku (filter) → Sonnet (analyst) → Opus (judge).
"""


# Default model IDs — overridable via Settings
HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"
OPUS = "claude-opus-4-6"

TASK_MODEL_MAP = {
    # Tier 1 — Haiku (scraper + fast filter)
    "news_filter": HAIKU,
    "sentiment_classify": HAIKU,
    "filing_classify": HAIKU,
    "earnings_parse": HAIKU,
    "catalyst_prescreen": HAIKU,
    "pattern_detect": HAIKU,
    # Tier 2 — Sonnet (analyst — reasoning & synthesis)
    "catalyst_analyze": SONNET,
    "fundamental_narrative": SONNET,
    "peer_comparison": SONNET,
    "pattern_interpret": SONNET,
    "sentiment_synthesis": SONNET,
    "memo_draft": SONNET,
    "web_research": SONNET,
    "test_analyze": SONNET,
    "ask_query": SONNET,
    # Tier 3 — Opus (portfolio manager — judgment & scoring)
    "trade_score": OPUS,
    "stress_test": OPUS,
    "signal_adjudicate": OPUS,
    "regime_borderline": OPUS,
    "monthly_report": OPUS,
    "risk_assess": OPUS,
}


def get_model(task_type: str, settings=None) -> str:
    """Get the model ID for a given task type."""
    if settings:
        # Allow override from settings
        tier = TASK_MODEL_MAP.get(task_type, SONNET)
        if tier == OPUS:
            return settings.scoring_model
        elif tier == SONNET:
            return settings.analyst_model
        elif tier == HAIKU:
            return settings.filter_model
    return TASK_MODEL_MAP.get(task_type, SONNET)
