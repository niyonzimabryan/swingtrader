"""
Alpaca paper trading API wrapper.
"""

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest, StopOrderRequest, GetOrdersRequest, StopLossRequest,
)
from alpaca.trading.enums import OrderClass, OrderSide, OrderType, TimeInForce, OrderStatus, QueryOrderStatus
from utils.logger import get_logger

log = get_logger("alpaca_client")


class AlpacaClient:
    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        self.client = None
        if api_key and secret_key:
            self.client = TradingClient(api_key, secret_key, paper=paper)
            log.info("alpaca_connected", paper=paper)

    def get_account_info(self) -> dict:
        """Get account equity, cash, buying power."""
        if not self.client:
            return self._mock_account()
        try:
            account = self.client.get_account()
            return {
                "equity": float(account.equity),
                "cash": float(account.cash),
                "buying_power": float(account.buying_power),
                "portfolio_value": float(account.portfolio_value),
                "pnl_today": float(account.equity) - float(account.last_equity),
                "pnl_today_pct": (float(account.equity) - float(account.last_equity)) / float(account.last_equity) * 100 if float(account.last_equity) > 0 else 0,
            }
        except Exception as e:
            log.error("get_account_failed", error=str(e))
            return self._mock_account()

    def submit_limit_buy(self, ticker: str, qty: int, limit_price: float) -> str:
        """Submit a limit buy order. Returns order ID."""
        if not self.client:
            log.info("mock_limit_buy", ticker=ticker, qty=qty, price=limit_price)
            return "mock_order_id"
        try:
            order_data = LimitOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.BUY,
                type="limit",
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
            )
            order = self.client.submit_order(order_data)
            log.info("limit_buy_submitted", ticker=ticker, qty=qty, price=limit_price, order_id=str(order.id))
            return str(order.id)
        except Exception as e:
            log.error("limit_buy_failed", ticker=ticker, error=str(e))
            raise

    def submit_stop_loss(self, ticker: str, qty: int, stop_price: float, direction: str = "long") -> str:
        """Submit a stop-loss order. Direction-aware: SELL for long, BUY for short."""
        side = OrderSide.BUY if direction == "short" else OrderSide.SELL
        if not self.client:
            log.info("mock_stop_loss", ticker=ticker, qty=qty, price=stop_price, direction=direction)
            return "mock_stop_id"
        try:
            order_data = StopOrderRequest(
                symbol=ticker,
                qty=qty,
                side=side,
                type="stop",
                time_in_force=TimeInForce.GTC,
                stop_price=stop_price,
            )
            order = self.client.submit_order(order_data)
            log.info("stop_loss_submitted", ticker=ticker, qty=qty, price=stop_price, side=side.value, order_id=str(order.id))
            return str(order.id)
        except Exception as e:
            log.error("stop_loss_failed", ticker=ticker, error=str(e))
            raise

    def submit_limit_short_entry(self, ticker: str, qty: int, limit_price: float) -> str:
        """Submit a limit sell to open a short position. DAY TIF."""
        if not self.client:
            log.info("mock_short_entry", ticker=ticker, qty=qty, price=limit_price)
            return "mock_short_entry_id"
        try:
            order_data = LimitOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.SELL,
                type="limit",
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
            )
            order = self.client.submit_order(order_data)
            log.info("short_entry_submitted", ticker=ticker, qty=qty, price=limit_price, order_id=str(order.id))
            return str(order.id)
        except Exception as e:
            log.error("short_entry_failed", ticker=ticker, error=str(e))
            raise

    def submit_protected_limit_entry(
        self,
        ticker: str,
        qty: int,
        limit_price: float,
        stop_price: float,
        direction: str = "long",
    ) -> dict:
        """
        Submit a limit entry with an attached stop-loss as an OTO order.

        The trade stays pending until Alpaca fills the parent entry order, so the
        app does not place a standalone stop before a position exists.
        """
        if not self.client:
            log.info(
                "mock_protected_limit_entry",
                ticker=ticker,
                qty=qty,
                price=limit_price,
                stop=stop_price,
                direction=direction,
            )
            return {
                "entry_order_id": "mock_order_id",
                "stop_order_id": "mock_stop_id",
                "order_strategy": "oto",
                "stop_price": stop_price,
            }

        side = OrderSide.SELL if direction == "short" else OrderSide.BUY
        try:
            order_data = LimitOrderRequest(
                symbol=ticker,
                qty=qty,
                side=side,
                type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
                order_class=OrderClass.OTO,
                stop_loss=StopLossRequest(stop_price=stop_price),
            )
            order = self.client.submit_order(order_data)
            stop_order_id = ""
            for leg in getattr(order, "legs", None) or []:
                if getattr(leg, "stop_price", None) is not None:
                    stop_order_id = str(leg.id)
                    break
            log.info(
                "protected_limit_entry_submitted",
                ticker=ticker,
                qty=qty,
                price=limit_price,
                stop=stop_price,
                direction=direction,
                order_id=str(order.id),
                stop_order_id=stop_order_id,
            )
            return {
                "entry_order_id": str(order.id),
                "stop_order_id": stop_order_id,
                "order_strategy": "oto",
                "stop_price": stop_price,
            }
        except Exception as e:
            log.error("protected_limit_entry_failed", ticker=ticker, error=str(e))
            raise

    def submit_limit_sell(self, ticker: str, qty: int, limit_price: float) -> str:
        """Submit a limit sell order (long exit / target). GTC TIF."""
        if not self.client:
            return "mock_sell_id"
        try:
            order_data = LimitOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.SELL,
                type="limit",
                time_in_force=TimeInForce.GTC,
                limit_price=limit_price,
            )
            order = self.client.submit_order(order_data)
            return str(order.id)
        except Exception as e:
            log.error("limit_sell_failed", ticker=ticker, error=str(e))
            raise

    def submit_limit_cover(self, ticker: str, qty: int, limit_price: float) -> str:
        """Submit a limit buy to cover a short position (target exit). GTC TIF."""
        if not self.client:
            return "mock_cover_id"
        try:
            order_data = LimitOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.BUY,
                type="limit",
                time_in_force=TimeInForce.GTC,
                limit_price=limit_price,
            )
            order = self.client.submit_order(order_data)
            log.info("limit_cover_submitted", ticker=ticker, qty=qty, price=limit_price, order_id=str(order.id))
            return str(order.id)
        except Exception as e:
            log.error("limit_cover_failed", ticker=ticker, error=str(e))
            raise

    def get_positions_detail(self) -> list[dict]:
        """Get all open positions with detail."""
        if not self.client:
            return []
        try:
            positions = self.client.get_all_positions()
            return [
                {
                    "ticker": pos.symbol,
                    "qty": int(pos.qty),
                    "entry_price": float(pos.avg_entry_price),
                    "current_price": float(pos.current_price),
                    "market_value": float(pos.market_value),
                    "pnl_abs": float(pos.unrealized_pl),
                    "pnl_pct": float(pos.unrealized_plpc) * 100,
                    "side": pos.side.value,
                }
                for pos in positions
            ]
        except Exception as e:
            log.error("get_positions_failed", error=str(e))
            return []

    def close_position(self, ticker: str) -> dict:
        """Close an entire position."""
        if not self.client:
            return {"success": True, "mock": True}
        try:
            self.client.close_position(ticker)
            log.info("position_closed", ticker=ticker)
            return {"success": True}
        except Exception as e:
            log.error("close_position_failed", ticker=ticker, error=str(e))
            return {"success": False, "error": str(e)}

    def get_order_status(self, order_id: str) -> dict:
        """Get status of a specific order."""
        if not self.client:
            return {"status": "filled", "mock": True}
        try:
            order = self.client.get_order_by_id(order_id)
            return {
                "id": str(order.id),
                "status": order.status.value,
                "filled_qty": int(order.filled_qty) if order.filled_qty else 0,
                "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else 0,
                "symbol": order.symbol,
            }
        except Exception as e:
            log.error("get_order_failed", order_id=order_id, error=str(e))
            return {}

    def get_orders(self, status: str | None = None) -> list[dict]:
        """Get recent orders."""
        if not self.client:
            return []
        try:
            query_status = QueryOrderStatus.ALL
            if status in ("open", "new", "accepted", "pending_fill"):
                query_status = QueryOrderStatus.OPEN
            elif status in ("closed", "filled", "cancelled", "canceled"):
                query_status = QueryOrderStatus.CLOSED
            orders = self.client.get_orders(filter=GetOrdersRequest(status=query_status, limit=20))
            return [
                {
                    "id": str(order.id),
                    "symbol": order.symbol,
                    "side": order.side.value if hasattr(order.side, "value") else str(order.side),
                    "type": order.type.value if hasattr(order.type, "value") else str(order.type),
                    "status": order.status.value if hasattr(order.status, "value") else str(order.status),
                    "quantity": float(order.qty) if order.qty else 0,
                    "filled_quantity": float(order.filled_qty) if order.filled_qty else 0,
                    "limit_price": float(order.limit_price) if order.limit_price else None,
                    "average_price": float(order.filled_avg_price) if order.filled_avg_price else None,
                    "created_at": str(order.created_at),
                }
                for order in orders
            ]
        except Exception as e:
            log.error("get_orders_failed", status=status, error=str(e))
            return []

    def cancel_order(self, order_id: str):
        """Cancel a pending order."""
        if not self.client:
            return
        try:
            self.client.cancel_order_by_id(order_id)
            log.info("order_cancelled", order_id=order_id)
        except Exception as e:
            log.error("cancel_order_failed", order_id=order_id, error=str(e))

    def _mock_account(self) -> dict:
        return {
            "equity": 100_000.0,
            "cash": 100_000.0,
            "buying_power": 200_000.0,
            "portfolio_value": 100_000.0,
            "pnl_today": 0,
            "pnl_today_pct": 0,
        }
