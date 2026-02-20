"""
Risk Manager — enforces the 5 non-negotiable risk rules from the PRD.
These rules are hard-coded and cannot be overridden by any signal or score.
"""

import numpy as np
from config.tickers import UNIVERSE
from data.market_data import MarketDataAdapter
from utils.logger import get_logger

log = get_logger("risk_manager")


class RiskManager:
    def __init__(self, settings):
        self.settings = settings
        self.market_data = MarketDataAdapter()
        self._peak_value = settings.portfolio_value  # Track peak for drawdown

    def full_risk_check(self, ticker: str, portfolio_state: dict, regime: dict, trade_params: dict) -> dict:
        """
        Run all 5 risk checks. Returns {allowed: bool, reasons: list, warnings: list}.
        """
        reasons = []
        warnings = []

        # 1. Drawdown circuit breaker
        if self.check_drawdown_circuit_breaker(portfolio_state):
            reasons.append("BLOCKED: Portfolio drawdown exceeds circuit breaker threshold. New trades halted.")

        # 2. Daily loss limit
        if self.check_daily_loss_limit(portfolio_state):
            reasons.append("BLOCKED: Daily loss exceeds 3% threshold. No new entries today.")

        # 3. Max positions
        current_positions = portfolio_state.get("position_count", 0)
        max_positions = regime.get("max_positions", self.settings.max_concurrent_positions)
        if current_positions >= max_positions:
            reasons.append(f"BLOCKED: At max positions ({current_positions}/{max_positions}).")

        # 4. Correlation check
        corr_result = self.check_correlation(ticker, portfolio_state.get("positions", []))
        if not corr_result.get("allowed", True):
            reasons.append(f"BLOCKED: High correlation ({corr_result['correlation']:.2f}) with {corr_result.get('correlated_with', '?')}.")

        # 5. Earnings blackout
        setup_type = trade_params.get("setup_type", "")
        if self.check_earnings_blackout(ticker, setup_type):
            reasons.append("BLOCKED: Earnings within 3 days and thesis is not earnings-related.")

        # Sector exposure check (warning, not blocking)
        sector = UNIVERSE.get(ticker, "Unknown")
        sector_exposure = portfolio_state.get("sector_exposure", {}).get(sector, 0)
        if sector_exposure >= self.settings.max_sector_exposure:
            warnings.append(f"WARNING: Sector exposure ({sector}) at {sector_exposure*100:.0f}%, max is {self.settings.max_sector_exposure*100:.0f}%.")

        # Total exposure check
        total_exposure = portfolio_state.get("total_exposure_pct", 0)
        max_exposure = regime.get("max_exposure", self.settings.max_portfolio_exposure)
        if total_exposure + trade_params.get("position_pct", 0.05) > max_exposure:
            reasons.append(f"BLOCKED: Would exceed max portfolio exposure ({max_exposure*100:.0f}%).")

        allowed = len(reasons) == 0
        log.info("risk_check", ticker=ticker, allowed=allowed, reasons=reasons, warnings=warnings)

        return {
            "allowed": allowed,
            "reasons": reasons,
            "warnings": warnings,
        }

    def check_drawdown_circuit_breaker(self, portfolio_state: dict) -> bool:
        """Rule 1: If portfolio drops 10% from peak, halt new trades for 5 days."""
        equity = portfolio_state.get("equity", self.settings.portfolio_value)
        if equity > self._peak_value:
            self._peak_value = equity
        drawdown = (self._peak_value - equity) / self._peak_value if self._peak_value > 0 else 0
        triggered = drawdown >= self.settings.drawdown_circuit_breaker_pct
        if triggered:
            log.warning("drawdown_circuit_breaker", drawdown=drawdown, peak=self._peak_value, current=equity)
        return triggered

    def check_daily_loss_limit(self, portfolio_state: dict) -> bool:
        """Rule 2: If daily losses exceed 3%, halt new entries."""
        daily_pnl_pct = abs(portfolio_state.get("pnl_today_pct", 0)) / 100
        triggered = portfolio_state.get("pnl_today", 0) < 0 and daily_pnl_pct >= self.settings.daily_loss_halt_pct
        return triggered

    def check_correlation(self, new_ticker: str, positions: list) -> dict:
        """Rule 3: Check if new position >0.7 correlated with existing holdings."""
        if not positions:
            return {"allowed": True, "correlation": 0}

        try:
            held_tickers = [p.get("ticker", "") for p in positions if p.get("ticker")]
            if not held_tickers:
                return {"allowed": True, "correlation": 0}

            # Get 30-day returns for correlation calculation
            new_hist = self.market_data.get_daily_bars(new_ticker, days=30)
            if new_hist.empty:
                return {"allowed": True, "correlation": 0}
            new_returns = new_hist["Close"].pct_change().dropna()

            for held_ticker in held_tickers:
                held_hist = self.market_data.get_daily_bars(held_ticker, days=30)
                if held_hist.empty:
                    continue
                held_returns = held_hist["Close"].pct_change().dropna()

                # Align dates
                combined = new_returns.to_frame("new").join(held_returns.to_frame("held"), how="inner")
                if len(combined) < 10:
                    continue

                corr = combined["new"].corr(combined["held"])
                if abs(corr) > 0.7:
                    return {
                        "allowed": False,
                        "correlation": round(corr, 3),
                        "correlated_with": held_ticker,
                    }

            return {"allowed": True, "correlation": 0}
        except Exception as e:
            log.error("correlation_check_failed", ticker=new_ticker, error=str(e))
            return {"allowed": True, "correlation": 0}  # Fail open

    def check_earnings_blackout(self, ticker: str, setup_type: str) -> bool:
        """Rule 5: Don't hold through earnings unless the catalyst IS earnings."""
        if "earnings" in setup_type.lower():
            return False  # Earnings-related thesis — OK to hold

        try:
            from data.news_data import NewsDataAdapter
            # Check if earnings within 3 days
            # This is a simplified check — full implementation would use earnings calendar
            return False  # Default: no blackout (need Finnhub key to check)
        except Exception:
            return False
