"""
Position manager — sizing, stop-loss, and target calculations.
"""

from data.market_data import MarketDataAdapter
from scoring.weights import CONVICTION_MULTIPLIERS
from utils.logger import get_logger

log = get_logger("position_manager")


class PositionManager:
    def __init__(self, settings):
        self.settings = settings
        self.market_data = MarketDataAdapter()

    def calculate_position_size(
        self,
        portfolio_value: float,
        regime: dict,
        composite_score: float,
        classification: str,
        ticker: str,
    ) -> dict:
        """Calculate position size with all adjustments."""
        current_price = self.market_data.get_current_price(ticker).get("price", 0)
        atr = self.market_data.get_atr(ticker)

        if current_price <= 0:
            return {"shares": 0, "position_pct": 0, "dollar_amount": 0}

        regime_multiplier = regime.get("position_size_multiplier", 1.0)
        conviction_multiplier = CONVICTION_MULTIPLIERS.get(classification, 1.0)

        # Volatility adjustment
        vol_adjustment = 1.0
        if atr > 0 and current_price > 0:
            atr_pct = atr / current_price
            if atr_pct > 0.03:
                vol_adjustment = 0.7
            elif atr_pct > 0.02:
                vol_adjustment = 0.85

        position_pct = (
            self.settings.base_position_pct
            * regime_multiplier
            * conviction_multiplier
            * vol_adjustment
        )
        position_pct = max(self.settings.min_position_pct, min(self.settings.max_position_pct, position_pct))

        dollar_amount = portfolio_value * position_pct
        shares = int(dollar_amount / current_price)

        return {
            "shares": shares,
            "position_pct": round(position_pct * 100, 2),
            "dollar_amount": round(dollar_amount, 2),
            "entry_price": round(current_price * 1.001, 2),  # Slight buffer for limit order
            "regime_multiplier": regime_multiplier,
            "conviction_multiplier": conviction_multiplier,
            "vol_adjustment": vol_adjustment,
        }

    def calculate_stop_loss(self, entry_price: float, ticker: str) -> float:
        """Calculate stop-loss price."""
        atr = self.market_data.get_atr(ticker)
        atr_stop_pct = (2 * atr / entry_price) if entry_price > 0 and atr > 0 else self.settings.default_stop_loss_pct
        stop_pct = max(self.settings.default_stop_loss_pct, atr_stop_pct)
        stop_pct = min(stop_pct, self.settings.max_stop_loss_pct)
        return round(entry_price * (1 - stop_pct), 2)

    def calculate_targets(self, entry_price: float, stop_loss: float) -> dict:
        """Calculate profit targets based on risk/reward."""
        risk = entry_price - stop_loss
        target_1 = round(entry_price + 2 * risk, 2)  # 2:1 R/R
        target_2 = round(entry_price + 3 * risk, 2)  # 3:1 R/R
        risk_reward = 2.0 if risk > 0 else 0
        return {
            "target_1": target_1,
            "target_2": target_2,
            "risk_reward": risk_reward,
        }
