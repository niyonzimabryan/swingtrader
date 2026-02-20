"""
Signal weight configuration for the scoring engine.
Editable without touching agent logic.
"""

SIGNAL_WEIGHTS = {
    "catalyst": 0.40,
    "fundamental": 0.30,
    "pattern": 0.22,
    "sentiment": 0.08,
}

# Direction alignment penalty
DIRECTION_PENALTY_PARTIAL = 0.85   # Some signals disagree
DIRECTION_PENALTY_MAJOR = 0.75     # Significant disagreement

# Score classification thresholds
SCORE_THRESHOLDS = {
    "high_conviction": 0.75,
    "moderate": 0.55,
    "low": 0.40,
    "no_action": 0.0,
}

# Conviction multiplier for position sizing
CONVICTION_MULTIPLIERS = {
    "high_conviction": 1.3,
    "moderate": 1.0,
    "low": 0.7,
    "no_action": 0.5,  # Was 0.0 — use 0.5 for testing to see trade params in memos
}
