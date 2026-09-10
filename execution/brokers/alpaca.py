"""Alpaca adapter for the normalized broker interface."""

from __future__ import annotations

from execution.brokers.base import (
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerOrderReview,
    OpenOrder,
)


class AlpacaBroker:
    name = "alpaca"
    supports_fractional = False
    supports_order_review = False
    live_trading = False

    def __init__(self, alpaca_client):
        self.client = alpaca_client

    def get_account_info(self) -> dict:
        return self.client.get_account_info()

    def get_positions_detail(self) -> list[dict]:
        return self.client.get_positions_detail()

    def get_orders(self, status: str | None = None) -> list[dict]:
        if hasattr(self.client, "get_orders"):
            return self.client.get_orders(status=status)
        return []

    def get_quotes(self, symbols: list[str]) -> dict[str, dict]:
        return {}

    def get_tradability(self, symbol: str) -> dict:
        return {"symbol": symbol, "tradable": True, "fractional": self.supports_fractional}

    def review_order(self, order: BrokerOrderRequest) -> BrokerOrderReview:
        estimated = order.requested_notional
        if estimated is None and order.quantity and order.limit_price:
            estimated = float(order.quantity) * float(order.limit_price)
        return BrokerOrderReview(
            broker=self.name,
            request=order,
            approved=True,
            estimated_notional=estimated,
            raw={"message": "Alpaca paper adapter does not require broker-side review."},
        )

    def place_order(self, reviewed_order: BrokerOrderReview) -> BrokerOrderResult:
        order = reviewed_order.request
        qty = int(order.quantity or 0)
        limit_price = float(order.limit_price or 0)
        stop_loss = float(order.stop_loss or 0)
        direction = order.direction or "long"

        if qty <= 0 or limit_price <= 0:
            return BrokerOrderResult(
                broker=self.name,
                success=False,
                error="Invalid Alpaca order parameters.",
            )

        if hasattr(self.client, "submit_protected_limit_entry") and stop_loss > 0:
            result = self.client.submit_protected_limit_entry(
                ticker=order.symbol,
                qty=qty,
                limit_price=limit_price,
                stop_price=stop_loss,
                direction=direction,
            )
            return BrokerOrderResult(
                broker=self.name,
                success=True,
                order_id=result.get("entry_order_id", ""),
                stop_order_id=result.get("stop_order_id", ""),
                status="pending_fill",
                order_strategy=result.get("order_strategy", "oto"),
                raw=result,
            )

        if direction == "short":
            order_id = self.client.submit_limit_short_entry(order.symbol, qty, limit_price)
        else:
            order_id = self.client.submit_limit_buy(order.symbol, qty, limit_price)
        return BrokerOrderResult(
            broker=self.name,
            success=True,
            order_id=order_id,
            status="pending_fill",
            order_strategy="simple",
        )

    def get_order_status(self, order_id: str) -> dict:
        return self.client.get_order_status(order_id)

    def cancel_order(self, order_id: str):
        return self.client.cancel_order(order_id)

    def close_position(self, ticker: str) -> dict:
        return self.client.close_position(ticker)

    def submit_limit_sell(self, ticker: str, qty: int, price: float) -> str:
        return self.client.submit_limit_sell(ticker, qty, price)

    def submit_limit_cover(self, ticker: str, qty: int, price: float) -> str:
        return self.client.submit_limit_cover(ticker, qty, price)

    def submit_stop_loss(self, ticker: str, qty: int, stop_price: float, direction: str = "long") -> str:
        return self.client.submit_stop_loss(ticker, qty, stop_price, direction=direction)

    # --- the protective exit (Spec L §5.1; Spec Q §11 for who may use it) ---
    # A paper arm may reach only this adapter, whatever the application-wide
    # primary broker is, and it runs the same lifecycle as live: fill, stop,
    # read the stop back, only then `protected`. Paper that skipped the
    # verification step would be testing a different state machine from the one
    # live capital runs on, which is the one thing paper is for.

    def place_stop(
        self,
        *,
        symbol: str,
        quantity: int,
        stop_price: float,
        ref_id: str,
        side: str = "sell",
    ) -> BrokerOrderResult:
        """A ``gtc`` stop through the paper client.

        Whole shares are asserted rather than rounded, matching the Robinhood
        adapter — not because Alpaca requires it, but because the paper venue
        exists to rehearse the live one and a rehearsal that accepts an order
        shape the live venue rejects teaches the wrong thing.

        Alpaca's client here takes no idempotency key, so ``ref_id`` is carried
        on the result for the lifecycle's own bookkeeping and the read-back
        matches on symbol, type and stop price instead. That is weaker than the
        Robinhood path and is stated rather than hidden.
        """
        if int(quantity) != float(quantity) or int(quantity) <= 0:
            return BrokerOrderResult(
                broker=self.name,
                success=False,
                error=f"a protective stop is whole-share only; refusing quantity={quantity!r}.",
            )
        direction = "short" if side == "buy" else "long"
        try:
            order_id = self.client.submit_stop_loss(
                symbol.upper(), int(quantity), float(stop_price), direction=direction
            )
        except Exception as exc:  # pragma: no cover - client-specific
            return BrokerOrderResult(broker=self.name, success=False, error=str(exc))
        return BrokerOrderResult(
            broker=self.name,
            success=True,
            order_id=str(order_id),
            stop_order_id=str(order_id),
            status="submitted",
            order_strategy="standalone_gtc_stop",
            raw={"ref_id": ref_id},
        )

    def read_open_orders(self, *, symbol: str | None = None) -> list[OpenOrder]:
        wanted = (symbol or "").strip().upper()
        out: list[OpenOrder] = []
        for raw in self.get_orders():
            row_symbol = str(raw.get("symbol") or "").upper()
            if wanted and row_symbol != wanted:
                continue
            out.append(
                OpenOrder(
                    broker=self.name,
                    order_id=str(raw.get("id") or ""),
                    symbol=row_symbol,
                    side=str(raw.get("side") or ""),
                    order_type=str(raw.get("type") or ""),
                    status=str(raw.get("status") or ""),
                    quantity=raw.get("quantity"),
                    stop_price=raw.get("stop_price"),
                    limit_price=raw.get("limit_price"),
                    time_in_force=str(raw.get("time_in_force") or ""),
                    ref_id=str(raw.get("client_order_id") or ""),
                    raw=raw,
                )
            )
        return out
