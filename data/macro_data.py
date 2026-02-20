"""
Macro data adapter — FRED (Federal Reserve Economic Data).
Provides: fed funds rate, yield curve, credit spreads.
"""

from fredapi import Fred
from utils.logger import get_logger

log = get_logger("macro_data")


class MacroDataAdapter:
    def __init__(self, api_key: str):
        self.fred = Fred(api_key=api_key) if api_key else None

    def get_fed_funds_rate(self) -> dict:
        """Current federal funds effective rate."""
        try:
            data = self.fred.get_series("DFF", observation_start="2024-01-01")
            current = float(data.dropna().iloc[-1])
            prev_month = float(data.dropna().iloc[-22]) if len(data.dropna()) > 22 else current
            return {
                "rate": round(current, 2),
                "prev_month": round(prev_month, 2),
                "direction": "rising" if current > prev_month else "falling" if current < prev_month else "stable",
            }
        except Exception as e:
            log.error("fed_funds_failed", error=str(e))
            return {"rate": 5.0, "prev_month": 5.0, "direction": "stable"}

    def get_yield_curve(self) -> dict:
        """2Y/10Y Treasury spread."""
        try:
            t10y = self.fred.get_series("DGS10", observation_start="2024-01-01")
            t2y = self.fred.get_series("DGS2", observation_start="2024-01-01")
            t10_current = float(t10y.dropna().iloc[-1])
            t2_current = float(t2y.dropna().iloc[-1])
            spread = t10_current - t2_current
            # Check historical spread for trend
            t10_prev = float(t10y.dropna().iloc[-22]) if len(t10y.dropna()) > 22 else t10_current
            t2_prev = float(t2y.dropna().iloc[-22]) if len(t2y.dropna()) > 22 else t2_current
            prev_spread = t10_prev - t2_prev
            return {
                "t10y": round(t10_current, 3),
                "t2y": round(t2_current, 3),
                "spread": round(spread, 3),
                "inverted": spread < 0,
                "steepening": spread > prev_spread,
                "prev_spread": round(prev_spread, 3),
            }
        except Exception as e:
            log.error("yield_curve_failed", error=str(e))
            return {"spread": 0.0, "inverted": False, "steepening": False}

    def get_credit_spreads(self) -> dict:
        """Investment-grade credit spread (OAS)."""
        try:
            # ICE BofA US Corporate Index OAS
            oas = self.fred.get_series("BAMLC0A0CM", observation_start="2024-01-01")
            current = float(oas.dropna().iloc[-1])
            prev_month = float(oas.dropna().iloc[-22]) if len(oas.dropna()) > 22 else current
            ma_60 = float(oas.dropna().tail(60).mean()) if len(oas.dropna()) >= 60 else current
            return {
                "oas": round(current, 2),
                "prev_month": round(prev_month, 2),
                "ma_60": round(ma_60, 2),
                "widening": current > prev_month,
                "elevated": current > ma_60 * 1.2,
            }
        except Exception as e:
            log.error("credit_spreads_failed", error=str(e))
            return {"oas": 1.0, "widening": False, "elevated": False}

    def get_all_macro_inputs(self) -> dict:
        """Aggregate all macro data for the regime agent."""
        return {
            "fed_funds": self.get_fed_funds_rate(),
            "yield_curve": self.get_yield_curve(),
            "credit_spreads": self.get_credit_spreads(),
        }
