"""
Signal weight configuration for the scoring engine.
V2: Web research replaces Reddit sentiment. Opus clamping added.
Editable without touching agent logic.
"""

SIGNAL_WEIGHTS = {
    "catalyst": 0.35,       # down from 0.40 — web research absorbs some catalyst context
    "fundamental": 0.25,    # down from 0.30
    "pattern": 0.20,        # ~unchanged from 0.22
    "web_research": 0.20,   # replaces sentiment/reddit (was 0.08)
}

# Opus can adjust the final score by at most this much (either direction)
OPUS_MAX_DELTA = 0.30

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
    "no_action": 0.5,
}


def get_weights(context: dict = None) -> dict:
    """
    Interface for future contextual bandit / adaptive weights.
    Currently returns static weights. In future, could adjust based on:
    - Macro regime (weight catalyst more in risk-off)
    - Historical performance of each signal
    - Sector-specific performance
    """
    return SIGNAL_WEIGHTS.copy()
