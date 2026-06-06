"""
Background Order Monitor — polls Alpaca for fill/stop/target status changes.
Runs as an async loop alongside the Telegram bot.

Handles:
- Entry fill detection → update Trade, notify, place target sells
- Stop-loss triggers → update Trade with P&L, notify
- Target hits → update Trade with partial/full exit, notify
- Time-based exits → close stale positions past max_holding_days
- Cancelled/expired orders → clean up Trade records
"""

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import or_

from database.db import get_session
from database.models import Trade, Ticker
from execution.alpaca_client import AlpacaClient
from utils.logger import get_logger

log = get_logger("order_monitor")

# Poll interval in seconds
POLL_INTERVAL = 30


class OrderMonitor:
    def __init__(self, alpaca: AlpacaClient, notification_manager, settings):
        self.alpaca = alpaca
        self.nm = notification_manager
        self.settings = settings
        self._running = False
        self._task = None

    async def start(self):
        """Start the background monitoring loop."""
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        log.info("order_monitor_started", poll_interval=POLL_INTERVAL)

    async def stop(self):
        """Stop the monitoring loop."""
        self._running = False
        if self._task:
            self._task.cancel()
        log.info("order_monitor_stopped")

    async def _monitor_loop(self):
        """Main loop: check open trades every POLL_INTERVAL seconds."""
        while self._running:
            try:
                await self._check_open_trades()
            except Exception as e:
                log.error("monitor_loop_error", error=str(e))
            await asyncio.sleep(POLL_INTERVAL)

    async def _check_open_trades(self):
        """Check all open trades for status changes."""
        with get_session() as session:
            # Only manage Alpaca trades (incl. legacy NULL-broker rows). This
            # monitor speaks the Alpaca order API; Robinhood trades are managed
            # via callbacks/manual close.
            open_trades = session.query(Trade).filter(
                Trade.status.in_(["open", "pending_fill"]),
                or_(Trade.broker == "alpaca", Trade.broker.is_(None)),
            ).all()

            if not open_trades:
                return

            for trade in open_trades:
                try:
                    ticker_symbol = trade.ticker.symbol if trade.ticker else "?"
                    await self._check_trade(trade, ticker_symbol, session)
                except Exception as e:
                    log.error(
                        "trade_check_failed",
                        trade_id=trade.id,
                        error=str(e),
                    )

    async def _check_trade(self, trade: Trade, ticker: str, session):
        """Check a single trade's order statuses and handle state transitions."""

        # 1. Check entry order
        entry_order_id = trade.broker_order_id or trade.alpaca_entry_order_id
        if entry_order_id and trade.status in ("open", "pending_fill"):
            entry_status = self.alpaca.get_order_status(entry_order_id)

            if not entry_status:
                return

            order_state = entry_status.get("status", "")

            # Entry filled — confirm trade is active
            if order_state == "filled" and trade.status != "open":
                await self._handle_entry_fill(trade, ticker, entry_status, session)
                return

            # Entry cancelled/expired — clean up
            if order_state in ("canceled", "cancelled", "expired"):
                await self._handle_entry_cancelled(trade, ticker, session)
                return

        # 2. For open trades with confirmed fills, check stop and target orders
        if trade.status == "open":
            # Check stop-loss order
            stop_order_id = trade.broker_stop_order_id or trade.alpaca_stop_order_id
            if stop_order_id:
                stop_status = self.alpaca.get_order_status(stop_order_id)
                if stop_status and stop_status.get("status") == "filled":
                    await self._handle_stop_triggered(trade, ticker, stop_status, session)
                    return

            # Check for target limit sells (stored as comma-separated IDs in operator_notes for now)
            target_order_ids = self._get_target_order_ids(trade)
            for target_num, order_id in target_order_ids:
                if not order_id:
                    continue
                target_status = self.alpaca.get_order_status(order_id)
                if target_status and target_status.get("status") == "filled":
                    await self._handle_target_hit(
                        trade, ticker, target_num, target_status, session
                    )
                    return

            # 3. Check time-based exit
            if trade.entry_date:
                days_held = (datetime.utcnow() - trade.entry_date).days
                if days_held >= self.settings.max_holding_days:
                    await self._handle_time_exit(trade, ticker, session)
                    return

    async def _handle_entry_fill(self, trade: Trade, ticker: str, fill_info: dict, session):
        """Handle entry order fill — update trade, notify, place targets."""
        actual_price = fill_info.get("filled_avg_price", trade.entry_price)
        filled_qty = fill_info.get("filled_qty", trade.shares)
        filled_notional = fill_info.get("filled_notional")
        direction = trade.direction or "long"

        trade.entry_price = actual_price
        trade.shares = int(float(filled_qty or 0))
        if filled_notional:
            trade.filled_notional = float(filled_notional)
        elif actual_price and filled_qty:
            trade.filled_notional = float(actual_price) * float(filled_qty)
        trade.entry_date = datetime.utcnow()
        trade.status = "open"
        session.commit()

        log.info("entry_filled", ticker=ticker, price=actual_price, shares=filled_qty, direction=direction)

        # Notify operator
        if self.nm:
            position_pct = trade.position_pct or 0
            side = "sell_short" if direction == "short" else "buy"
            await self.nm.order_filled(
                ticker=ticker,
                shares=trade.shares,
                price=actual_price,
                side=side,
                stop_loss=trade.stop_loss,
                position_pct=position_pct,
            )

        # Place target orders (direction-aware)
        await self._place_target_orders(trade, ticker, trade.shares, session)

    async def _place_target_orders(self, trade: Trade, ticker: str, shares: int, session):
        """Place target orders after entry fill. Direction-aware."""
        target_ids = []
        direction = trade.direction or "long"

        # Target 1: 50% of position
        t1_shares = shares // 2
        if t1_shares > 0 and trade.target_1 > 0:
            try:
                if direction == "short":
                    t1_id = self.alpaca.submit_limit_cover(ticker, t1_shares, trade.target_1)
                else:
                    t1_id = self.alpaca.submit_limit_sell(ticker, t1_shares, trade.target_1)
                target_ids.append(f"t1:{t1_id}")
                log.info("target_1_order_placed", ticker=ticker, shares=t1_shares, price=trade.target_1, direction=direction)
            except Exception as e:
                log.error("target_1_order_failed", ticker=ticker, error=str(e))

        # Target 2: remaining shares
        t2_shares = shares - t1_shares
        if t2_shares > 0 and trade.target_2 > 0:
            try:
                if direction == "short":
                    t2_id = self.alpaca.submit_limit_cover(ticker, t2_shares, trade.target_2)
                else:
                    t2_id = self.alpaca.submit_limit_sell(ticker, t2_shares, trade.target_2)
                target_ids.append(f"t2:{t2_id}")
                log.info("target_2_order_placed", ticker=ticker, shares=t2_shares, price=trade.target_2, direction=direction)
            except Exception as e:
                log.error("target_2_order_failed", ticker=ticker, error=str(e))

        # Store target order IDs in operator_notes (simple approach, no schema change)
        if target_ids:
            existing_notes = trade.operator_notes or ""
            trade.operator_notes = existing_notes + "|TARGETS:" + ",".join(target_ids)
            session.commit()

    def _get_target_order_ids(self, trade: Trade) -> list:
        """Extract target order IDs from operator_notes."""
        notes = trade.operator_notes or ""
        if "|TARGETS:" not in notes:
            return []

        targets_str = notes.split("|TARGETS:")[1].split("|")[0]
        result = []
        for part in targets_str.split(","):
            if part.startswith("t1:"):
                result.append((1, part[3:]))
            elif part.startswith("t2:"):
                result.append((2, part[3:]))
        return result

    async def _handle_stop_triggered(self, trade: Trade, ticker: str, fill_info: dict, session):
        """Handle stop-loss fill — close trade, compute P&L, notify."""
        exit_price = fill_info.get("filled_avg_price", trade.stop_loss)
        direction = trade.direction or "long"

        if direction == "short":
            pnl_pct = ((trade.entry_price - exit_price) / trade.entry_price * 100) if trade.entry_price > 0 else 0
            pnl_abs = self._pnl_abs(trade, pnl_pct)
        else:
            pnl_pct = ((exit_price - trade.entry_price) / trade.entry_price * 100) if trade.entry_price > 0 else 0
            pnl_abs = self._pnl_abs(trade, pnl_pct)

        trade.exit_price = exit_price
        trade.exit_date = datetime.utcnow()
        trade.pnl_pct = round(pnl_pct, 2)
        trade.pnl_absolute = round(pnl_abs, 2)
        trade.exit_reason = "stop_loss"
        trade.status = "closed"
        session.commit()

        log.info("stop_triggered", ticker=ticker, exit_price=exit_price, pnl_pct=pnl_pct)

        # Cancel any outstanding target orders
        self._cancel_target_orders(trade)

        if self.nm:
            await self.nm.stop_triggered(
                ticker=ticker,
                shares=trade.shares,
                entry_price=trade.entry_price,
                exit_price=exit_price,
                pnl_pct=pnl_pct,
                pnl_abs=pnl_abs,
            )

    async def _handle_target_hit(self, trade: Trade, ticker: str, target_num: int, fill_info: dict, session):
        """Handle profit target fill — partial or full exit."""
        exit_price = fill_info.get("filled_avg_price", 0)
        filled_qty = fill_info.get("filled_qty", 0)
        direction = trade.direction or "long"

        if direction == "short":
            pnl_pct = ((trade.entry_price - exit_price) / trade.entry_price * 100) if trade.entry_price > 0 else 0
            pnl_abs = self._pnl_abs(trade, pnl_pct, quantity=filled_qty)
        else:
            pnl_pct = ((exit_price - trade.entry_price) / trade.entry_price * 100) if trade.entry_price > 0 else 0
            pnl_abs = self._pnl_abs(trade, pnl_pct, quantity=filled_qty)

        # Check if this is partial (target_1) or full exit (target_2 or all shares sold)
        remaining_position = self.alpaca.get_positions_detail()
        still_holds = any(p["ticker"] == ticker for p in remaining_position)

        if still_holds:
            # Partial exit (target 1 hit, still holding for target 2)
            log.info("target_hit_partial", ticker=ticker, target=target_num, pnl_pct=pnl_pct)
            if self.nm:
                await self.nm.target_hit(ticker, target_num, exit_price, pnl_pct, pnl_abs, partial=True)
        else:
            # Full exit — all targets hit or position fully closed
            trade.exit_price = exit_price
            trade.exit_date = datetime.utcnow()
            trade.pnl_pct = round(pnl_pct, 2)
            trade.pnl_absolute = round(pnl_abs, 2)
            trade.exit_reason = f"target_{target_num}"
            trade.status = "closed"
            session.commit()

            # Cancel stop-loss order
            stop_order_id = trade.broker_stop_order_id or trade.alpaca_stop_order_id
            if stop_order_id:
                self.alpaca.cancel_order(stop_order_id)

            log.info("target_hit_full_exit", ticker=ticker, target=target_num, pnl_pct=pnl_pct)
            if self.nm:
                await self.nm.target_hit(ticker, target_num, exit_price, pnl_pct, pnl_abs, partial=False)

    async def _handle_time_exit(self, trade: Trade, ticker: str, session):
        """Close position that exceeded max holding days."""
        days_held = (datetime.utcnow() - trade.entry_date).days
        log.info("time_exit_triggered", ticker=ticker, days_held=days_held)

        try:
            result = self.alpaca.close_position(ticker)
            if not result.get("success"):
                error = result.get("error", "")
                if self._is_position_not_found(error):
                    await self._handle_missing_position_reconciliation(trade, ticker, error, session)
                    return
                log.error("time_exit_close_failed", ticker=ticker, error=result.get("error"))
                return
        except Exception as e:
            log.error("time_exit_failed", ticker=ticker, error=str(e))
            return

        # Get current price for P&L
        positions = self.alpaca.get_positions_detail()
        current_price = trade.entry_price  # fallback
        for p in positions:
            if p["ticker"] == ticker:
                current_price = p.get("current_price", trade.entry_price)
                break

        direction = trade.direction or "long"
        if direction == "short":
            pnl_pct = ((trade.entry_price - current_price) / trade.entry_price * 100) if trade.entry_price > 0 else 0
            pnl_abs = self._pnl_abs(trade, pnl_pct)
        else:
            pnl_pct = ((current_price - trade.entry_price) / trade.entry_price * 100) if trade.entry_price > 0 else 0
            pnl_abs = self._pnl_abs(trade, pnl_pct)

        trade.exit_price = current_price
        trade.exit_date = datetime.utcnow()
        trade.pnl_pct = round(pnl_pct, 2)
        trade.pnl_absolute = round(pnl_abs, 2)
        trade.exit_reason = "time_exit"
        trade.status = "closed"
        session.commit()

        # Cancel outstanding orders
        stop_order_id = trade.broker_stop_order_id or trade.alpaca_stop_order_id
        if stop_order_id:
            self.alpaca.cancel_order(stop_order_id)
        self._cancel_target_orders(trade)

        if self.nm:
            dir_label = "SHORT" if direction == "short" else "LONG"
            await self.nm.system_message(
                f"Time exit: {ticker} ({dir_label}) closed after {days_held} days. "
                f"P&L: {pnl_pct:+.2f}% (${pnl_abs:+,.2f})"
            )

    def _is_position_not_found(self, error: str | None) -> bool:
        """Return True when Alpaca says the DB trade no longer has a live position."""
        normalized = (error or "").lower()
        return "position not found" in normalized

    async def _handle_missing_position_reconciliation(self, trade: Trade, ticker: str, error: str, session):
        """
        Stop monitoring a stale DB trade when Alpaca has no matching position.

        This preserves the audit trail without fabricating P&L. The likely causes
        are manual closure, historical DB drift, or an old monitor bug.
        """
        trade.status = "closed"
        trade.exit_reason = "reconciled_missing_position"
        trade.exit_date = datetime.utcnow()
        existing_notes = trade.operator_notes or ""
        note = f"RECONCILED_MISSING_POSITION:{error[:240]}"
        trade.operator_notes = f"{existing_notes}|{note}" if existing_notes else note
        session.commit()

        stop_order_id = trade.broker_stop_order_id or trade.alpaca_stop_order_id
        if stop_order_id:
            try:
                self.alpaca.cancel_order(stop_order_id)
            except Exception:
                pass
        self._cancel_target_orders(trade)

        log.warning(
            "trade_reconciled_missing_position",
            ticker=ticker,
            trade_id=trade.id,
            error=error,
        )

        if self.nm:
            await self.nm.system_message(
                f"Reconciled stale trade: {ticker} is marked closed because Alpaca has no matching position."
            )

    async def _handle_entry_cancelled(self, trade: Trade, ticker: str, session):
        """Handle entry order cancellation/expiry."""
        trade.status = "cancelled"
        trade.exit_reason = "order_expired"
        session.commit()

        # Cancel any stop-loss order
        stop_order_id = trade.broker_stop_order_id or trade.alpaca_stop_order_id
        if stop_order_id:
            self.alpaca.cancel_order(stop_order_id)

        log.info("entry_cancelled", ticker=ticker, trade_id=trade.id)

        if self.nm:
            await self.nm.system_message(f"Entry order for {ticker} expired/cancelled. Trade cancelled.")

    def _cancel_target_orders(self, trade: Trade):
        """Cancel any outstanding target limit sell orders."""
        target_ids = self._get_target_order_ids(trade)
        for _, order_id in target_ids:
            if order_id:
                try:
                    self.alpaca.cancel_order(order_id)
                except Exception:
                    pass

    def _pnl_abs(self, trade: Trade, pnl_pct: float, quantity: float | None = None) -> float:
        """Compute absolute P&L using filled notional when fractional shares are tracked."""
        if trade.filled_notional:
            if quantity and trade.shares:
                basis = trade.filled_notional * (float(quantity) / float(trade.shares))
            else:
                basis = trade.filled_notional
            return basis * (pnl_pct / 100)
        qty = quantity if quantity is not None else trade.shares
        return trade.entry_price * qty * (pnl_pct / 100)
