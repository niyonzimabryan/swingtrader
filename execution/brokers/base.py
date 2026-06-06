"""Normalized broker contracts used by execution and Telegram commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class BrokerOrderRequest:
    symbol: str
    side: str
    order_type: str
    time_in_force: str = "gfd"
    market_hours: str = "regular_hours"
    quantity: float | None = None
    dollar_amount: float | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    direction: str = "long"
    stop_loss: float | None = None
    target_1: float | None = None
    target_2: float | None = None
    requested_notional: float | None = None
    client_context: dict[str, Any] = field(default_factory=dict)


@dataclass
class BrokerOrderReview:
    broker: str
    request: BrokerOrderRequest
    approved: bool
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    estimated_notional: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class BrokerOrderResult:
    broker: str
    success: bool
    order_id: str = ""
    stop_order_id: str = ""
    status: str = "submitted"
    order_strategy: str = "simple"
    filled_qty: float | None = None
    filled_avg_price: float | None = None
    filled_notional: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class BrokerClient(Protocol):
    name: str
    supports_fractional: bool
    supports_order_review: bool
    live_trading: bool

    def get_account_info(self) -> dict: ...
    def get_positions_detail(self) -> list[dict]: ...
    def get_orders(self, status: str | None = None) -> list[dict]: ...
    def get_quotes(self, symbols: list[str]) -> dict[str, dict]: ...
    def get_tradability(self, symbol: str) -> dict: ...
    def review_order(self, order: BrokerOrderRequest) -> BrokerOrderReview: ...
    def place_order(self, reviewed_order: BrokerOrderReview) -> BrokerOrderResult: ...
    def get_order_status(self, order_id: str) -> dict: ...
    def cancel_order(self, order_id: str): ...
    def close_position(self, ticker: str) -> dict: ...
