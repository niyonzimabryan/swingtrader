"""
Signal attribution analysis — stub for Phase 1.
Full implementation requires 30+ days of trade data.
"""

from utils.logger import get_logger

log = get_logger("attribution")


def get_signal_attribution() -> dict:
    """Analyze which signals contributed to winners vs losers."""
    # Stub — requires accumulated trade history
    return {
        "status": "insufficient_data",
        "message": "Signal attribution requires 30+ closed trades. Keep trading to accumulate data.",
    }
