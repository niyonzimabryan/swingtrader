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


@dataclass
class OpenOrder:
    """One order as the broker currently reports it, normalized across adapters.

    The Phase 6 protection check reads this back from the broker after placing a
    stop (Spec L §5.1: the position is ``protected`` only once the ``gtc``
    ``stop_market`` is *readable*, never merely because a placement call
    returned). ``ref_id`` is the client idempotency key and is what the check
    matches on: an order id can change shape between adapters, but the ref_id is
    ours and we generated it.

    ``raw`` keeps the adapter's own payload so a mismatch is diagnosable without
    a second round trip.
    """

    broker: str
    order_id: str
    symbol: str
    side: str
    order_type: str
    status: str
    quantity: float | None = None
    stop_price: float | None = None
    limit_price: float | None = None
    time_in_force: str = ""
    ref_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        """Whether this order is still working at the broker.

        Spelled as "not in the finished set" rather than "in the working set"
        so that a status string this code has never seen counts as *open*. The
        consequence of guessing wrong in that direction is a redundant stop that
        the broker's own ``ref_id`` deduplication absorbs; guessing the other
        way would leave a position unprotected while reporting it protected.
        """
        return (self.status or "").strip().lower() not in FINISHED_ORDER_STATES


#: Order states that mean the order is no longer working. Anything else — an
#: empty string included — is treated as open, per :meth:`OpenOrder.is_open`.
FINISHED_ORDER_STATES = frozenset(
    {
        "filled",
        "cancelled",
        "canceled",
        "rejected",
        "expired",
        "failed",
        "done",
        "replaced",
    }
)


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

    # --- the protective exit (Spec L §5.1, added in Phase 6) ---------------
    # Two methods, because the protection contract has two halves and only
    # having the first is how a position ends up unprotected while a log line
    # says otherwise.

    def place_stop(
        self,
        *,
        symbol: str,
        quantity: int,
        stop_price: float,
        ref_id: str,
        side: str = "sell",
    ) -> BrokerOrderResult:
        """Place the standalone protective exit: ``stop_market``, ``gtc``,
        ``regular_hours``, whole shares, carrying ``ref_id``.

        Every one of those is forced by the verified Robinhood schema rather
        than chosen: ``type`` has no bracket or attach-stop option, stops are
        rejected outside regular hours, stops are whole-share only, and
        ``ref_id`` is the idempotency key that makes the fill-to-stop retry
        safe.
        """
        ...

    def read_open_orders(self, *, symbol: str | None = None) -> list[OpenOrder]:
        """Read orders back from the broker. The verification half.

        A placement call that returned successfully is a claim; this is the
        evidence. A position is ``protected`` only when its stop comes back from
        here (Spec L §5.1), and the daily job re-places any stop that has
        stopped coming back, so a twenty-session hold cannot outlive its stop
        whatever Robinhood's unstated GTC horizon turns out to be.
        """
        ...
