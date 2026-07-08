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
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_

from database.db import get_session
from database.models import Trade, Ticker
from execution.alpaca_client import AlpacaClient
from utils.async_call import call_with_timeout
from utils.logger import get_logger

log = get_logger("order_monitor")

# Poll interval in seconds
POLL_INTERVAL = 30
OPEN_ORDER_STATUSES = {
    "new",
    "accepted",
    "pending_new",
    "pending_replace",
    "pending_cancel",
    "partially_filled",
    "held",
    "open",
}
CANCELLED_OR_FINAL_STATUSES = {
    "canceled",
    "cancelled",
    "expired",
    "filled",
    "rejected",
    "done_for_day",
}


class OrderMonitor:
    name = "order_monitor"

    def __init__(self, alpaca: AlpacaClient, notification_manager, settings):
        self.alpaca = alpaca
        self.nm = notification_manager
        self.settings = settings
        self._running = False
        self._task = None
        self._last_tick: datetime | None = None  # heartbeat for the watchdog

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

    @property
    def last_tick(self) -> datetime | None:
        """UTC timestamp of the last loop iteration (heartbeat for the watchdog)."""
        return self._last_tick

    async def _broker_call(self, fn, *args, **kwargs):
        """Run a sync broker call under a timeout so a stall can't freeze the loop."""
        timeout_s = getattr(self.settings, "monitor_broker_call_timeout_s", 30)
        return await call_with_timeout(fn, *args, timeout_s=timeout_s, **kwargs)

    async def _monitor_loop(self):
        """Main loop: check open trades every POLL_INTERVAL seconds."""
        while self._running:
            # Heartbeat first so the watchdog sees a fresh tick unless the loop is
            # genuinely stuck inside a broker call.
            self._last_tick = datetime.now(timezone.utc)
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
            entry_status = await self._broker_call(self.alpaca.get_order_status, entry_order_id)

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
                stop_status = await self._broker_call(self.alpaca.get_order_status, stop_order_id)
                if stop_status and stop_status.get("status") == "filled":
                    await self._handle_stop_triggered(trade, ticker, stop_status, session)
                    return

            # Check OCO stop legs — a stop-side fill only shows on the leg order;
            # the tracked parent (take-profit) just flips to 'canceled'.
            for _, leg_id in self._get_stop_leg_order_ids(trade):
                if not leg_id:
                    continue
                leg_status = await self._broker_call(self.alpaca.get_order_status, leg_id)
                if leg_status and leg_status.get("status") == "filled":
                    await self._handle_stop_triggered(trade, ticker, leg_status, session)
                    return

            # Check for target limit sells (stored as comma-separated IDs in operator_notes for now)
            target_order_ids = self._get_target_order_ids(trade)
            for target_num, order_id in target_order_ids:
                if not order_id:
                    continue
                target_status = await self._broker_call(self.alpaca.get_order_status, order_id)
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
        """Place target orders after entry fill. Direction-aware.

        Safety invariants:
        - OCO stop-leg order ids are stored (STOPLEGS:) so a stop-side fill is
          detected and booked — the OCO parent only shows 'canceled' on a stop-out.
        - If any placement fails after the previous protective stop was cancelled,
          the uncovered shares get a plain stop re-placed and the operator is paged.
        """
        target_plan = self._target_order_plan(trade, shares)
        if not target_plan:
            return

        required_qty = sum(qty for _, qty, _ in target_plan)
        ok, cancelled_prior_protection = await self._ensure_exit_qty_available(
            trade, ticker, shares, required_qty
        )
        if not ok:
            return

        target_ids = []
        stop_leg_ids = []
        uncovered_qty = 0
        direction = trade.direction or "long"

        for target_num, qty, price in target_plan:
            try:
                order_id, stop_leg_id, strategy = await self._submit_target_order(
                    ticker, qty, price, trade.stop_loss, direction
                )
                target_ids.append(f"t{target_num}:{order_id}")
                if stop_leg_id:
                    stop_leg_ids.append(f"t{target_num}:{stop_leg_id}")
                log.info(
                    f"target_{target_num}_order_placed",
                    ticker=ticker,
                    shares=qty,
                    price=price,
                    direction=direction,
                    order_strategy=strategy,
                )
            except Exception as e:
                uncovered_qty += qty
                log.error(f"target_{target_num}_order_failed", ticker=ticker, shares=qty, error=str(e))

        # Store target/stop-leg order IDs in operator_notes (no schema change)
        if target_ids:
            self._replace_note_segment(trade, "TARGETS", target_ids)
            self._replace_note_segment(trade, "STOPLEGS", stop_leg_ids)

        fully_covered = uncovered_qty == 0 and len(target_ids) == len(target_plan)
        if fully_covered and stop_leg_ids:
            # Every share now carries an OCO stop leg; retire the standalone stop ids.
            trade.alpaca_stop_order_id = None
            trade.broker_stop_order_id = None
        elif uncovered_qty > 0:
            await self._reprotect_uncovered_shares(
                trade, ticker, uncovered_qty, direction, cancelled_prior_protection
            )
        session.commit()

    async def _reprotect_uncovered_shares(
        self, trade: Trade, ticker: str, uncovered_qty: int, direction: str, stop_was_cancelled: bool
    ) -> None:
        """Exit-order placement partially failed. Restore stop protection + page.

        If the prior protective stop is still live (nothing was cancelled), the
        shares remain protected — alert only. If we cancelled it to free quantity,
        re-place a plain stop for the uncovered shares before alerting.
        """
        if not stop_was_cancelled:
            await self._send_system_message(
                f"⚠️ {ticker}: {uncovered_qty} share(s) have no target order (placement failed), "
                f"but the original stop order is still active. Review and re-place targets."
            )
            return

        replacement_id = None
        if trade.stop_loss > 0 and hasattr(self.alpaca, "submit_stop_loss"):
            try:
                replacement_id = await self._broker_call(
                    self.alpaca.submit_stop_loss, ticker, uncovered_qty, trade.stop_loss, direction=direction
                )
                trade.alpaca_stop_order_id = str(replacement_id)
                log.info(
                    "exit_reprotection_stop_placed",
                    ticker=ticker,
                    trade_id=trade.id,
                    shares=uncovered_qty,
                    stop_price=trade.stop_loss,
                    order_id=str(replacement_id),
                )
            except Exception as exc:
                log.critical("exit_reprotection_failed", ticker=ticker, trade_id=trade.id, error=str(exc))

        if replacement_id:
            await self._send_system_message(
                f"⚠️ {ticker}: target placement failed for {uncovered_qty} share(s) after the old stop "
                f"was cancelled. A replacement stop at {trade.stop_loss} was placed. Review targets."
            )
        else:
            await self._send_system_message(
                f"🚨 {ticker}: {uncovered_qty} share(s) are UNPROTECTED — target placement failed after "
                f"the stop was cancelled, and re-placing a stop also failed. Manual action required."
            )

    def _target_order_plan(self, trade: Trade, shares: int) -> list[tuple[int, int, float]]:
        plan = []
        t1_shares = shares // 2
        if t1_shares > 0 and trade.target_1 > 0:
            plan.append((1, t1_shares, trade.target_1))
        t2_shares = shares - t1_shares
        if t2_shares > 0 and trade.target_2 > 0:
            plan.append((2, t2_shares, trade.target_2))
        return plan

    async def _submit_target_order(self, ticker: str, qty: int, price: float, stop_loss: float, direction: str) -> tuple[str, str, str]:
        """Returns (order_id, stop_leg_id_or_empty, strategy)."""
        if stop_loss > 0 and hasattr(self.alpaca, "submit_oco_exit"):
            oco = await self._broker_call(
                self.alpaca.submit_oco_exit, ticker, qty, price, stop_loss, direction=direction
            )
            if isinstance(oco, dict):
                return str(oco.get("order_id", "")), str(oco.get("stop_leg_id", "") or ""), "oco"
            # Tolerate brokers/mocks still returning a bare order id.
            return str(oco), "", "oco"
        if direction == "short":
            return await self._broker_call(self.alpaca.submit_limit_cover, ticker, qty, price), "", "limit"
        return await self._broker_call(self.alpaca.submit_limit_sell, ticker, qty, price), "", "limit"

    async def _ensure_exit_qty_available(
        self,
        trade: Trade,
        ticker: str,
        position_qty: int,
        required_qty: int,
    ) -> tuple[bool, bool]:
        """Returns (ok_to_place, cancelled_prior_protection).

        The second flag tells the caller whether the trade's previous protective
        orders were cancelled to free quantity — if placement then fails, those
        shares must be re-protected.
        """
        holding_orders = await self._open_exit_holding_orders(trade, ticker)
        held_qty = sum(self._open_order_qty(order) for order in holding_orders)
        available_qty = max(0.0, float(position_qty) - held_qty)
        if available_qty >= required_qty:
            return True, False

        managed_ids = self._managed_exit_order_ids(trade)
        unrecognized = [order for order in holding_orders if self._order_id(order) not in managed_ids]
        if unrecognized:
            await self._alert_exit_order_conflict(
                trade=trade,
                ticker=ticker,
                blocking_orders=unrecognized,
                available_qty=available_qty,
                required_qty=required_qty,
                held_qty=held_qty,
            )
            return False, False

        cancelled_any = False
        for order in holding_orders:
            if not await self._cancel_and_confirm_exit_order(trade, ticker, order):
                return False, cancelled_any
            cancelled_any = True

        log.info(
            "exit_order_cancel_replace_ready",
            ticker=ticker,
            trade_id=trade.id,
            cancelled_orders=[self._order_id(order) for order in holding_orders],
            required_qty=required_qty,
        )
        return True, cancelled_any

    async def _open_exit_holding_orders(self, trade: Trade, ticker: str) -> list[dict]:
        if not hasattr(self.alpaca, "get_orders"):
            return []
        try:
            open_orders = await self._broker_call(self.alpaca.get_orders, status="open") or []
        except Exception as exc:
            log.warning("exit_order_open_orders_failed", ticker=ticker, trade_id=trade.id, error=str(exc))
            return []

        direction = trade.direction or "long"
        exit_side = "buy" if direction == "short" else "sell"
        result = []
        for order in open_orders:
            if self._order_symbol(order) != ticker.upper():
                continue
            if self._order_side(order) != exit_side:
                continue
            if self._order_status(order) not in OPEN_ORDER_STATUSES:
                continue
            if self._open_order_qty(order) <= 0:
                continue
            result.append(order)
        return result

    async def _cancel_and_confirm_exit_order(self, trade: Trade, ticker: str, order: dict) -> bool:
        order_id = self._order_id(order)
        if not order_id:
            return False
        log.info("exit_order_cancel_requested", ticker=ticker, trade_id=trade.id, order_id=order_id)
        try:
            await self._broker_call(self.alpaca.cancel_order, order_id)
        except Exception as exc:
            log.warning("exit_order_cancel_failed", ticker=ticker, trade_id=trade.id, order_id=order_id, error=str(exc))
            await self._send_system_message(
                f"Exit orders for {ticker} were not replaced because bot-managed order {order_id} could not be cancelled."
            )
            return False

        for _ in range(3):
            status = {}
            if hasattr(self.alpaca, "get_order_status"):
                status = await self._broker_call(self.alpaca.get_order_status, order_id) or {}
            normalized = self._order_status(status)
            if not status or normalized in CANCELLED_OR_FINAL_STATUSES:
                log.info("exit_order_cancel_confirmed", ticker=ticker, trade_id=trade.id, order_id=order_id, status=normalized)
                return True
            await asyncio.sleep(0.25)

        log.warning("exit_order_cancel_unconfirmed", ticker=ticker, trade_id=trade.id, order_id=order_id)
        await self._send_system_message(
            f"Exit orders for {ticker} were not replaced because cancellation of bot-managed order {order_id} was not confirmed."
        )
        return False

    async def _alert_exit_order_conflict(
        self,
        *,
        trade: Trade,
        ticker: str,
        blocking_orders: list[dict],
        available_qty: float,
        required_qty: int,
        held_qty: float,
    ) -> None:
        blocking_ids = [self._order_id(order) for order in blocking_orders]
        log.warning(
            "exit_order_conflict",
            ticker=ticker,
            trade_id=trade.id,
            blocking_order_ids=blocking_ids,
            available_qty=available_qty,
            required_qty=required_qty,
            held_qty=held_qty,
        )
        await self._send_system_message(
            f"Exit orders for {ticker} were not placed because unrecognized open order(s) "
            f"{', '.join(blocking_ids)} hold {held_qty:g} shares; {required_qty:g} required, {available_qty:g} available."
        )

    async def _send_system_message(self, message: str) -> None:
        if self.nm:
            await self.nm.system_message(message)

    def _managed_exit_order_ids(self, trade: Trade) -> set[str]:
        ids = {
            str(value)
            for value in (trade.broker_stop_order_id, trade.alpaca_stop_order_id)
            if value
        }
        ids.update(order_id for _, order_id in self._get_target_order_ids(trade) if order_id)
        ids.update(order_id for _, order_id in self._get_stop_leg_order_ids(trade) if order_id)
        return ids

    def _replace_note_segment(self, trade: Trade, prefix: str, ids: list[str]) -> None:
        """Replace (or drop, when ids is empty) the '<prefix>:...' segment in operator_notes."""
        existing_parts = [
            part for part in (trade.operator_notes or "").split("|")
            if part and not part.startswith(f"{prefix}:")
        ]
        if ids:
            existing_parts.append(f"{prefix}:" + ",".join(ids))
        trade.operator_notes = "|".join(existing_parts)

    def _get_target_order_ids(self, trade: Trade) -> list:
        """Extract target order IDs from operator_notes."""
        return self._get_note_segment_ids(trade, "TARGETS")

    def _get_stop_leg_order_ids(self, trade: Trade) -> list:
        """Extract OCO stop-leg order IDs from operator_notes."""
        return self._get_note_segment_ids(trade, "STOPLEGS")

    def _get_note_segment_ids(self, trade: Trade, prefix: str) -> list:
        notes = trade.operator_notes or ""
        marker = f"{prefix}:"
        if marker not in notes:
            return []
        segment = notes.split(marker, 1)[1].split("|", 1)[0]
        result = []
        for part in segment.split(","):
            if part.startswith("t1:"):
                result.append((1, part[3:]))
            elif part.startswith("t2:"):
                result.append((2, part[3:]))
        return result

    def _order_id(self, order: dict) -> str:
        return str(order.get("id") or order.get("order_id") or "")

    def _order_symbol(self, order: dict) -> str:
        return str(order.get("symbol") or order.get("ticker") or "").upper()

    def _order_side(self, order: dict) -> str:
        return str(order.get("side") or "").lower()

    def _order_status(self, order: dict) -> str:
        return str(order.get("status") or "").lower()

    def _open_order_qty(self, order: dict) -> float:
        qty = self._float_value(order.get("quantity", order.get("qty", 0)))
        filled = self._float_value(order.get("filled_quantity", order.get("filled_qty", 0)))
        return max(0.0, qty - filled)

    def _float_value(self, value) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

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
        await self._cancel_target_orders(trade)

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
        remaining_position = await self._broker_call(self.alpaca.get_positions_detail)
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
                await self._broker_call(self.alpaca.cancel_order, stop_order_id)

            log.info("target_hit_full_exit", ticker=ticker, target=target_num, pnl_pct=pnl_pct)
            if self.nm:
                await self.nm.target_hit(ticker, target_num, exit_price, pnl_pct, pnl_abs, partial=False)

    async def _handle_time_exit(self, trade: Trade, ticker: str, session):
        """Close position that exceeded max holding days."""
        days_held = (datetime.utcnow() - trade.entry_date).days
        log.info("time_exit_triggered", ticker=ticker, days_held=days_held)

        try:
            result = await self._broker_call(self.alpaca.close_position, ticker)
            if not result.get("success"):
                error = result.get("error", "")
                if result.get("position_not_found") or self._is_position_not_found(error, result.get("code")):
                    await self._handle_missing_position_reconciliation(trade, ticker, error, session)
                    return
                log.error("time_exit_close_failed", ticker=ticker, error=result.get("error"))
                return
        except Exception as e:
            if self._is_position_not_found(str(e), getattr(e, "code", None)):
                await self._handle_missing_position_reconciliation(trade, ticker, str(e), session)
                return
            log.error("time_exit_failed", ticker=ticker, error=str(e))
            return

        # Get current price for P&L
        positions = await self._broker_call(self.alpaca.get_positions_detail)
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
            await self._broker_call(self.alpaca.cancel_order, stop_order_id)
        await self._cancel_target_orders(trade)

        if self.nm:
            dir_label = "SHORT" if direction == "short" else "LONG"
            await self.nm.system_message(
                f"Time exit: {ticker} ({dir_label}) closed after {days_held} days. "
                f"P&L: {pnl_pct:+.2f}% (${pnl_abs:+,.2f})"
            )

    def _is_position_not_found(self, error: str | None, code=None) -> bool:
        """Return True when Alpaca says the DB trade no longer has a live position."""
        normalized = (error or "").lower()
        return str(code or "") in {"404", "40410000"} or "40410000" in normalized or "position not found" in normalized

    async def _handle_missing_position_reconciliation(self, trade: Trade, ticker: str, error: str, session):
        """
        Stop monitoring a stale DB trade when Alpaca has no matching position.

        This preserves the audit trail without fabricating P&L. The likely causes
        are manual closure, historical DB drift, or an old monitor bug.
        """
        fill = await self._latest_exit_fill(ticker, trade.direction or "long")
        if fill and fill.get("average_price"):
            exit_price = float(fill["average_price"])
            direction = trade.direction or "long"
            if direction == "short":
                pnl_pct = ((trade.entry_price - exit_price) / trade.entry_price * 100) if trade.entry_price > 0 else 0
            else:
                pnl_pct = ((exit_price - trade.entry_price) / trade.entry_price * 100) if trade.entry_price > 0 else 0
            trade.exit_price = exit_price
            trade.pnl_pct = round(pnl_pct, 2)
            trade.pnl_absolute = round(self._pnl_abs(trade, pnl_pct), 2)
            fill_note = f"RECONCILED_EXIT_FILL:{fill.get('id', '')}"
        else:
            trade.exit_price = None
            trade.pnl_pct = None
            trade.pnl_absolute = None
            fill_note = "RECONCILED_EXIT_UNKNOWN"

        trade.status = "closed"
        trade.exit_reason = "reconciled_missing_position"
        trade.exit_date = datetime.utcnow()
        existing_notes = trade.operator_notes or ""
        note = f"RECONCILED_MISSING_POSITION:{error[:180]}|{fill_note}"
        trade.operator_notes = f"{existing_notes}|{note}" if existing_notes else note
        session.commit()

        stop_order_id = trade.broker_stop_order_id or trade.alpaca_stop_order_id
        if stop_order_id:
            try:
                await self._broker_call(self.alpaca.cancel_order, stop_order_id)
            except Exception:
                pass
        await self._cancel_target_orders(trade)

        log.info(
            "trade_reconciled_missing_position",
            ticker=ticker,
            trade_id=trade.id,
            error=error,
            exit_price=trade.exit_price,
            pnl_pct=trade.pnl_pct,
        )

        if self.nm:
            await self.nm.system_message(
                f"Reconciled stale trade: {ticker} is marked closed because Alpaca has no matching position."
            )

    async def _latest_exit_fill(self, ticker: str, direction: str) -> dict | None:
        if not hasattr(self.alpaca, "get_orders"):
            return None
        try:
            orders = await self._broker_call(self.alpaca.get_orders, status="closed") or []
        except Exception as exc:
            log.warning("reconcile_fills_fetch_failed", ticker=ticker, error=str(exc))
            return None
        exit_side = "buy" if direction == "short" else "sell"
        filled = [
            order for order in orders
            if self._order_symbol(order) == ticker.upper()
            and self._order_side(order) == exit_side
            and self._order_status(order) == "filled"
            and order.get("average_price")
        ]
        if not filled:
            return None
        return sorted(filled, key=lambda order: str(order.get("created_at") or ""), reverse=True)[0]

    async def _handle_entry_cancelled(self, trade: Trade, ticker: str, session):
        """Handle entry order cancellation/expiry."""
        trade.status = "cancelled"
        trade.exit_reason = "order_expired"
        session.commit()

        # Cancel any stop-loss order
        stop_order_id = trade.broker_stop_order_id or trade.alpaca_stop_order_id
        if stop_order_id:
            await self._broker_call(self.alpaca.cancel_order, stop_order_id)

        log.info("entry_cancelled", ticker=ticker, trade_id=trade.id)

        if self.nm:
            await self.nm.system_message(f"Entry order for {ticker} expired/cancelled. Trade cancelled.")

    async def _cancel_target_orders(self, trade: Trade):
        """Cancel any outstanding target orders (OCO parents cancel their legs too)."""
        target_ids = self._get_target_order_ids(trade)
        for _, order_id in target_ids:
            if order_id:
                try:
                    await self._broker_call(self.alpaca.cancel_order, order_id)
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
