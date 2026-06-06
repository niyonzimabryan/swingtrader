"""
Position Monitor — polls live prices every 60 seconds during market hours.
Complementary to OrderMonitor (which watches Alpaca order fill events).

Handles:
- Stop-loss breach detection (backup if Alpaca stop order didn't fire)
- Target approaching alerts (within 1.5%)
- Profit giveback alerts (gave back 3%+ from peak while still profitable)
- Time expiring / expired warnings
- Peak price tracking for drawdown detection
- Direction-aware (long and short positions)
"""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import or_

from database.db import get_session
from database.models import Trade, Ticker
from execution.alpaca_client import AlpacaClient
from utils.logger import get_logger

log = get_logger("position_monitor")

ET = ZoneInfo("America/New_York")
POLL_INTERVAL = 60  # seconds

# Thresholds
TARGET_APPROACHING_PCT = 0.015  # 1.5% from target
PROFIT_GIVEBACK_PCT = 0.03     # Gave back 3% from peak
TIME_WARNING_DAYS_BEFORE = 2   # Warn 2 days before max hold


# Portfolio-level thresholds
PORTFOLIO_STRONG_DAY_PCT = 2.0     # +2% in a day
PORTFOLIO_ROUGH_DAY_PCT = -2.0     # -2% in a day
PORTFOLIO_DRAWDOWN_WARN_PCT = 5.0  # 5% from peak equity
PORTFOLIO_CIRCUIT_BREAKER_PCT = 10.0  # 10% from peak

# Position-level thresholds
POSITION_BIG_GAIN_PCT = 10.0   # +10% on a single position
POSITION_NEAR_STOP_PCT = -5.0  # -5% (approaching default stop)


class PositionMonitor:
    def __init__(self, alpaca: AlpacaClient, notification_manager, settings):
        self.alpaca = alpaca
        self.nm = notification_manager
        self.settings = settings
        self._running = False
        self._task = None
        # Portfolio alert state (reset daily)
        self._strong_day_sent = False
        self._rough_day_sent = False
        self._drawdown_warn_sent = False
        self._circuit_breaker_sent = False
        self._position_big_gain_sent: set[str] = set()
        self._position_near_stop_sent: set[str] = set()
        self._last_alert_date = None
        self._peak_equity = None

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        log.info("position_monitor_started", poll_interval=POLL_INTERVAL)

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
        log.info("position_monitor_stopped")

    def _is_market_hours(self) -> bool:
        """Check if US market is open (9:30 AM - 4:00 PM ET, weekdays)."""
        now = datetime.now(ET)
        if now.weekday() >= 5:
            return False
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return market_open <= now <= market_close

    async def _monitor_loop(self):
        while self._running:
            try:
                if self._is_market_hours():
                    self._reset_daily_alerts_if_needed()
                    await self._check_portfolio_thresholds()
                    await self._check_positions()
            except Exception as e:
                log.error("position_monitor_error", error=str(e))
            await asyncio.sleep(POLL_INTERVAL)

    def _reset_daily_alerts_if_needed(self):
        """Reset per-day alert flags at the start of each trading day."""
        today = datetime.now(ET).date()
        if self._last_alert_date != today:
            self._strong_day_sent = False
            self._rough_day_sent = False
            self._position_big_gain_sent = set()
            self._position_near_stop_sent = set()
            self._last_alert_date = today
            # Don't reset drawdown/circuit breaker — those persist

    async def _check_portfolio_thresholds(self):
        """Check portfolio-level alerts: daily P&L and drawdown from peak."""
        try:
            account = self.alpaca.get_account_info()
        except Exception as e:
            log.error("portfolio_threshold_check_failed", error=str(e))
            return

        equity = account.get("equity", 0)
        pnl_today_pct = account.get("pnl_today_pct", 0)

        if equity <= 0:
            return

        # Track peak equity for drawdown calculation
        if self._peak_equity is None or equity > self._peak_equity:
            self._peak_equity = equity
            self._drawdown_warn_sent = False  # Reset on new peak
            self._circuit_breaker_sent = False

        # Daily P&L alerts
        if pnl_today_pct >= PORTFOLIO_STRONG_DAY_PCT and not self._strong_day_sent:
            self._strong_day_sent = True
            if self.nm:
                await self.nm.portfolio_strong_day(pnl_today_pct)
                log.info("portfolio_alert_sent", type="strong_day", pnl_pct=pnl_today_pct)

        if pnl_today_pct <= PORTFOLIO_ROUGH_DAY_PCT and not self._rough_day_sent:
            self._rough_day_sent = True
            if self.nm:
                await self.nm.portfolio_rough_day(pnl_today_pct)
                log.info("portfolio_alert_sent", type="rough_day", pnl_pct=pnl_today_pct)

        # Drawdown from peak equity
        if self._peak_equity > 0:
            drawdown_pct = (self._peak_equity - equity) / self._peak_equity * 100

            if drawdown_pct >= PORTFOLIO_CIRCUIT_BREAKER_PCT and not self._circuit_breaker_sent:
                self._circuit_breaker_sent = True
                if self.nm:
                    await self.nm.portfolio_circuit_breaker(drawdown_pct)
                    log.warning("portfolio_circuit_breaker", drawdown_pct=drawdown_pct)

            elif drawdown_pct >= PORTFOLIO_DRAWDOWN_WARN_PCT and not self._drawdown_warn_sent:
                self._drawdown_warn_sent = True
                if self.nm:
                    await self.nm.portfolio_drawdown_warning(drawdown_pct)
                    log.warning("portfolio_drawdown_warning", drawdown_pct=drawdown_pct)

    async def _check_positions(self):
        """Check all open positions against their stored parameters."""
        positions = self.alpaca.get_positions_detail()
        if not positions:
            return

        with get_session() as session:
            for pos in positions:
                ticker = pos["ticker"]
                current_price = pos["current_price"]

                trade = session.query(Trade).join(Ticker).filter(
                    Trade.status == "open",
                    Ticker.symbol == ticker,
                    or_(Trade.broker == "alpaca", Trade.broker.is_(None)),
                ).first()

                if not trade:
                    continue

                try:
                    await self._monitor_trade(trade, ticker, current_price, session)
                except Exception as e:
                    log.error("trade_monitor_failed", ticker=ticker, error=str(e))

    async def _monitor_trade(self, trade: Trade, ticker: str, current_price: float, session):
        """Run all checks for a single open trade."""
        direction = trade.direction or "long"
        entry_price = trade.entry_price
        if entry_price <= 0:
            return

        # Calculate P&L based on direction
        if direction == "long":
            pnl_pct = (current_price - entry_price) / entry_price
        else:
            pnl_pct = (entry_price - current_price) / entry_price

        # 1. Stop-loss breach check
        await self._check_stop(trade, ticker, current_price, direction, pnl_pct)

        # 2. Target checks (approaching + hit)
        await self._check_targets(trade, ticker, current_price, direction, pnl_pct)

        # 3. Time-based checks
        await self._check_time(trade, ticker, pnl_pct)

        # 4. Peak tracking + profit giveback
        await self._check_peak_drawdown(trade, ticker, current_price, direction, pnl_pct, session)

        # 5. Position-level threshold alerts (big gain / near stop)
        await self._check_position_thresholds(trade, ticker, pnl_pct)

    async def _check_stop(self, trade: Trade, ticker: str, current_price: float, direction: str, pnl_pct: float):
        """Alert if price has breached the stop-loss level."""
        if trade.stop_loss <= 0:
            return

        breached = False
        if direction == "long" and current_price <= trade.stop_loss:
            breached = True
        elif direction == "short" and current_price >= trade.stop_loss:
            breached = True

        if breached and self.nm:
            pnl_abs = self._pnl_abs(trade, pnl_pct)
            await self.nm.position_stop_breached(
                ticker=ticker,
                current_price=current_price,
                stop_price=trade.stop_loss,
                pnl_pct=pnl_pct * 100,
                pnl_abs=pnl_abs,
                direction=direction,
                trade_id=trade.id,
            )

    async def _check_targets(self, trade: Trade, ticker: str, current_price: float, direction: str, pnl_pct: float):
        """Check target approaching and target hit."""
        # T1 check
        if trade.target_1 > 0 and not trade.t1_hit:
            # Check if approaching
            if trade.target_1 > 0:
                distance = abs(current_price - trade.target_1) / trade.target_1
                hit = (direction == "long" and current_price >= trade.target_1) or \
                      (direction == "short" and current_price <= trade.target_1)

                if hit:
                    trade.t1_hit = True
                    pnl_abs = self._pnl_abs(trade, pnl_pct)
                    if self.nm:
                        await self.nm.position_target_hit(
                            ticker=ticker,
                            target_num=1,
                            current_price=current_price,
                            target_price=trade.target_1,
                            pnl_pct=pnl_pct * 100,
                            pnl_abs=pnl_abs,
                            entry_price=trade.entry_price,
                            trade_id=trade.id,
                        )
                elif distance <= TARGET_APPROACHING_PCT and not trade.t1_approaching_sent:
                    trade.t1_approaching_sent = True
                    pnl_abs = self._pnl_abs(trade, pnl_pct)
                    if self.nm:
                        await self.nm.position_target_approaching(
                            ticker=ticker,
                            target_num=1,
                            current_price=current_price,
                            target_price=trade.target_1,
                            distance_pct=distance * 100,
                            pnl_pct=pnl_pct * 100,
                            pnl_abs=pnl_abs,
                            trade_id=trade.id,
                        )

        # T2 check (only if T1 already hit)
        if trade.target_2 > 0 and trade.t1_hit and not trade.t2_hit:
            hit = (direction == "long" and current_price >= trade.target_2) or \
                  (direction == "short" and current_price <= trade.target_2)

            if hit:
                trade.t2_hit = True
                pnl_abs = self._pnl_abs(trade, pnl_pct)
                if self.nm:
                    await self.nm.position_target_hit(
                        ticker=ticker,
                        target_num=2,
                        current_price=current_price,
                        target_price=trade.target_2,
                        pnl_pct=pnl_pct * 100,
                        pnl_abs=pnl_abs,
                        entry_price=trade.entry_price,
                        trade_id=trade.id,
                    )

    async def _check_time(self, trade: Trade, ticker: str, pnl_pct: float):
        """Check time-based exit warnings."""
        if not trade.entry_date:
            return

        days_held = (datetime.utcnow() - trade.entry_date).days
        max_days = self.settings.max_holding_days

        # Time expiring warning (2 days before max)
        if days_held >= max_days - TIME_WARNING_DAYS_BEFORE and not trade.time_warning_sent and days_held < max_days:
            trade.time_warning_sent = True
            pnl_abs = self._pnl_abs(trade, pnl_pct)
            if self.nm:
                await self.nm.position_time_expiring(
                    ticker=ticker,
                    days_held=days_held,
                    max_days=max_days,
                    pnl_pct=pnl_pct * 100,
                    pnl_abs=pnl_abs,
                    trade_id=trade.id,
                )

        # Time expired (at max hold)
        if days_held >= max_days and self.nm:
            pnl_abs = pnl_pct * trade.entry_price * trade.shares
            await self.nm.position_time_expired(
                ticker=ticker,
                days_held=days_held,
                max_days=max_days,
                pnl_pct=pnl_pct * 100,
                pnl_abs=pnl_abs,
                trade_id=trade.id,
            )

    async def _check_peak_drawdown(self, trade: Trade, ticker: str, current_price: float, direction: str, pnl_pct: float, session):
        """Track peak price and alert on profit giveback."""
        # Initialize peak_price if not set
        if trade.peak_price is None:
            trade.peak_price = current_price

        # Update peak based on direction
        if direction == "long":
            if current_price > trade.peak_price:
                trade.peak_price = current_price
                trade.drawdown_alert_sent = False  # Reset if new peak
        else:
            # For shorts, "peak" means lowest price (best for short)
            if current_price < trade.peak_price:
                trade.peak_price = current_price
                trade.drawdown_alert_sent = False

        # Calculate giveback from peak
        if direction == "long" and trade.peak_price > 0:
            peak_pnl_pct = (trade.peak_price - trade.entry_price) / trade.entry_price
            giveback = peak_pnl_pct - pnl_pct
        elif direction == "short" and trade.peak_price > 0:
            peak_pnl_pct = (trade.entry_price - trade.peak_price) / trade.entry_price
            giveback = peak_pnl_pct - pnl_pct
        else:
            return

        # Alert if gave back 3%+ while still in profit
        if giveback >= PROFIT_GIVEBACK_PCT and pnl_pct > 0 and not trade.drawdown_alert_sent:
            trade.drawdown_alert_sent = True
            if self.nm:
                await self.nm.position_profit_giveback(
                    ticker=ticker,
                    peak_pnl_pct=peak_pnl_pct * 100,
                    current_pnl_pct=pnl_pct * 100,
                    giveback_pct=giveback * 100,
                    trade_id=trade.id,
                )

    async def _check_position_thresholds(self, trade: Trade, ticker: str, pnl_pct: float):
        """Check position-level threshold alerts (big gain, near stop)."""
        pnl_pct_100 = pnl_pct * 100

        # Big gain alert (+10%)
        if pnl_pct_100 >= POSITION_BIG_GAIN_PCT and ticker not in self._position_big_gain_sent:
            self._position_big_gain_sent.add(ticker)
            if self.nm:
                await self.nm.position_big_gain(ticker, pnl_pct_100, trade.id)
                log.info("position_alert_sent", type="big_gain", ticker=ticker, pnl_pct=pnl_pct_100)

        # Near stop alert (-5%)
        if pnl_pct_100 <= POSITION_NEAR_STOP_PCT and ticker not in self._position_near_stop_sent:
            self._position_near_stop_sent.add(ticker)
            if self.nm:
                await self.nm.position_near_stop(ticker, pnl_pct_100, trade.stop_loss, trade.id)
                log.info("position_alert_sent", type="near_stop", ticker=ticker, pnl_pct=pnl_pct_100)

    def _pnl_abs(self, trade: Trade, pnl_pct: float) -> float:
        basis = trade.filled_notional or (trade.entry_price * trade.shares)
        return basis * pnl_pct
