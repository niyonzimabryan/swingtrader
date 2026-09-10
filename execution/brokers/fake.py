"""A deterministic in-memory broker — the contract every adapter must pass.

Spec L §5.2: Schwab is deferred, and what Phase 1 ships instead is the thing
that makes a second adapter cheap later — the ``BrokerCapabilities`` contract, a
fake broker, and contract tests every adapter must pass. When Schwab is asked
for, the work is ``execution/brokers/schwab.py`` against this contract plus its
auth flow, and nothing upstream changes.

This is not a mock. A mock asserts that a call happened; this returns data with
the same *shape* a real adapter returns, so a test can drive the sync, the
freshness rules, and the exposure math end to end without a network. It is also
the only broker in the repository whose behaviour a test may depend on:
``tests/fixtures/robinhood/`` carries recorded shapes for the real one.

Every capability defaults to what the *most constrained* real adapter declares
— specifically ``can_place_attached_stop=False``, because that is Robinhood's
verified answer and a fake that declared it True would let a capability bug
through the one test written to catch it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from execution.brokers.base import (
    FINISHED_ORDER_STATES,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerOrderReview,
    OpenOrder,
)
from execution.brokers.capabilities import BrokerCapabilities
from portfolio.records import (
    AccountRecord,
    AccountSnapshot,
    BrokerSnapshot,
    CashRecord,
    HoldingRecord,
    OrderRecord,
    TaxLotRecord,
)
from utils.timeutils import utcnow_naive


@dataclass
class FakeAccount:
    """One account's worth of canned state."""

    record: AccountRecord
    holdings: tuple[HoldingRecord, ...] = ()
    tax_lots: tuple[TaxLotRecord, ...] = ()
    cash: CashRecord | None = None
    orders: tuple[OrderRecord, ...] = ()
    #: Set to make this account's read raise, for the failure-policy tests.
    error: str = ""


@dataclass
class FakeBroker:
    """A broker adapter with no network and no surprises.

    ``fail_with`` makes ``fetch_ledger_snapshot`` raise, which is the
    whole-broker failure the sync must survive without zeroing anything.
    """

    name: str = "fake"
    accounts: list[FakeAccount] = field(default_factory=list)
    declared_capabilities: BrokerCapabilities = field(
        default_factory=lambda: BrokerCapabilities(
            can_read_positions=True,
            can_read_tax_lots=True,
            can_read_orders=True,
            can_read_cash=True,
            can_place_equity_market=True,
            can_place_equity_limit=True,
            can_place_attached_stop=False,
            can_place_standalone_gtc_stop=True,
            supports_fractional=True,
            supports_specified_lot_sale=True,
        )
    )
    as_of_utc: datetime | None = None
    fail_with: str = ""
    live_trading: bool = False
    supports_order_review: bool = True

    # --- the ledger source contract ---------------------------------------
    @property
    def supports_fractional(self) -> bool:
        return self.declared_capabilities.supports_fractional

    def capabilities(self) -> BrokerCapabilities:
        return self.declared_capabilities

    def fetch_ledger_snapshot(self) -> BrokerSnapshot:
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        as_of = self.as_of_utc or utcnow_naive()
        return BrokerSnapshot(
            broker=self.name,
            accounts=tuple(
                AccountSnapshot(
                    account=account.record,
                    as_of_utc=as_of,
                    holdings=tuple(account.holdings),
                    tax_lots=tuple(account.tax_lots),
                    cash=account.cash,
                    orders=tuple(account.orders),
                    error=account.error,
                )
                for account in self.accounts
            ),
        )

    # --- the execution-side BrokerClient protocol -------------------------
    # Present so a future adapter has the full surface to satisfy. Every write
    # path refuses: this broker exists for read-path tests, and a fake that
    # cheerfully "filled" an order would make an execution test pass for the
    # wrong reason.
    def get_account_info(self) -> dict:
        account = self.accounts[0] if self.accounts else None
        cash = account.cash if account and account.cash else CashRecord()
        equity = sum(
            float(h.market_value or 0.0) for a in self.accounts for h in a.holdings
        )
        return {
            "broker": self.name,
            "configured": True,
            "equity": equity,
            "cash": float(cash.settled_cash or 0.0),
            "buying_power": float(cash.buying_power or 0.0),
            "portfolio_value": equity + float(cash.settled_cash or 0.0),
        }

    def get_positions_detail(self) -> list[dict]:
        rows = []
        for account in self.accounts:
            for holding in account.holdings:
                rows.append(
                    {
                        "ticker": holding.symbol.upper(),
                        "qty": holding.quantity,
                        "entry_price": holding.average_cost,
                        "current_price": holding.last_price,
                        "market_value": holding.market_value,
                        "instrument_type": holding.instrument_type,
                        "side": "long" if holding.quantity >= 0 else "short",
                    }
                )
        return rows

    def get_orders(self, status: str | None = None) -> list[dict]:
        rows = []
        for account in self.accounts:
            for order in account.orders:
                if status and (order.status or "").lower() != status.lower():
                    continue
                rows.append(
                    {
                        "id": order.broker_order_id,
                        "symbol": order.symbol,
                        "side": order.side,
                        "quantity": order.quantity,
                        "type": order.order_type,
                        "state": order.status,
                        "ref_id": order.ref_id,
                    }
                )
        return rows

    def get_quotes(self, symbols: list[str]) -> dict[str, dict]:
        prices = {
            holding.symbol.upper(): holding.last_price
            for account in self.accounts
            for holding in account.holdings
            if holding.last_price is not None
        }
        return {
            symbol.upper(): {"symbol": symbol.upper(), "last_price": prices.get(symbol.upper())}
            for symbol in symbols
        }

    def get_tradability(self, symbol: str) -> dict:
        return {
            "symbol": symbol.upper(),
            "tradable": True,
            "fractional": self.declared_capabilities.supports_fractional,
        }

    def review_order(self, order: BrokerOrderRequest) -> BrokerOrderReview:
        return BrokerOrderReview(
            broker=self.name,
            request=order,
            approved=False,
            errors=["fake broker: review is not a placement path and never approves."],
        )

    def place_order(self, reviewed_order: BrokerOrderReview) -> BrokerOrderResult:
        return BrokerOrderResult(
            broker=self.name,
            success=False,
            error="fake broker: placement is refused by construction.",
        )

    def get_order_status(self, order_id: str) -> dict:
        for account in self.accounts:
            for order in account.orders:
                if order.broker_order_id == order_id:
                    return {
                        "id": order.broker_order_id,
                        "status": order.status,
                        "filled_qty": order.filled_quantity or 0.0,
                        "filled_avg_price": order.average_fill_price or 0.0,
                        "symbol": order.symbol,
                    }
        return {}

    def cancel_order(self, order_id: str):
        return {"success": False, "error": "fake broker: cancellation is refused."}

    def close_position(self, ticker: str) -> dict:
        return {"success": False, "error": "fake broker: closing is a placement path."}

    # --- the protective-exit contract (Spec L §5.1) ------------------------
    # `read_open_orders` is a read and answers from the canned orders like the
    # other read paths. `place_stop` is a placement and refuses by
    # construction, exactly as `place_order` does: this broker exists for
    # read-path tests, and a fake that "protected" a position would make an
    # execution test pass for the wrong reason. The execution lifecycle drives
    # `FakeExecutionBroker` below instead.

    def place_stop(self, *, symbol, quantity, stop_price, ref_id, side="sell") -> BrokerOrderResult:
        return BrokerOrderResult(
            broker=self.name,
            success=False,
            error="fake broker: placement is refused by construction.",
        )

    def read_open_orders(self, *, symbol: str | None = None) -> list[OpenOrder]:
        wanted = (symbol or "").strip().upper()
        out: list[OpenOrder] = []
        for account in self.accounts:
            for order in account.orders:
                row_symbol = (order.symbol or "").upper()
                if wanted and row_symbol != wanted:
                    continue
                out.append(
                    OpenOrder(
                        broker=self.name,
                        order_id=order.broker_order_id,
                        symbol=row_symbol,
                        side=order.side,
                        order_type=order.order_type,
                        status=order.status,
                        quantity=order.quantity,
                        stop_price=getattr(order, "stop_price", None),
                        limit_price=getattr(order, "limit_price", None),
                        time_in_force=getattr(order, "time_in_force", ""),
                        ref_id=order.ref_id or "",
                    )
                )
        return out


@dataclass
class _StoredOrder:
    order_id: str
    symbol: str
    side: str
    order_type: str
    status: str
    quantity: float
    ref_id: str = ""
    stop_price: float | None = None
    limit_price: float | None = None
    time_in_force: str = ""
    filled_quantity: float = 0.0
    average_fill_price: float | None = None


@dataclass
class FakeExecutionBroker:
    """A deterministic broker that actually *runs* the Phase 6 lifecycle.

    The read-only :class:`FakeBroker` above refuses every placement by
    construction, which is correct for the sync and exposure tests: a fake that
    cheerfully "filled" an order would make an execution test pass for the wrong
    reason. But the lifecycle **is** the thing Phase 6 has to test — fill
    detection, the protective stop, reading it back, the unprotected page — so
    that test needs a broker whose placements resolve, under the test's control
    and never over a network.

    This is that broker, and every failure mode the lifecycle must survive is a
    flag on it rather than a mock's side effect:

    ``fill_entry``           whether the entry fills (else it stays working).
    ``stop_readable``        whether the placed stop comes back from
                             :meth:`read_open_orders` — ``False`` is the
                             unprotected-fill case that must page and block.
    ``stop_place_succeeds``  whether :meth:`place_stop` succeeds at all.
    ``review_approves``      whether ``review_order`` approves the entry.
    ``drop_stops``           symbols whose stops silently vanish, for the daily
                             missing-stop replacement job.

    Phase 5 (Spec Q §12) adds the rest of the lifecycle's failure vocabulary,
    each one a flag for the same reason:

    ``fill_ratio``           the fraction of the requested quantity that fills.
                             ``0.5`` is the partial fill that protection has to
                             be resized for.
    ``place_rejects``        the broker rejects the entry outright — terminal,
                             no order exists, the reservation releases.
    ``placement_unknown``    the placement response is *ambiguous*: an error
                             with a payload and no order id. Not terminal, and
                             it may not release its reservation until
                             reconciliation says so (§12 invariant 12).
    ``phantom_ref_ids``      ref_ids that :meth:`find_order_by_ref_id` finds
                             even though placement reported failure — the
                             "unknown outcome, but the order does exist" case.
                             An unknown outcome whose ref_id is *not* here is
                             the verified-no-order case.

    Two properties exist for the safety regressions rather than for the
    lifecycle: :attr:`calls` counts every method by name, and
    :attr:`order_calls` counts only the two that can create an order at a real
    broker. A safety test asserts that number is zero; counting it here rather
    than mocking is what makes "zero order calls" a statement about the code
    under test instead of about a patch.

    It declares the same capabilities as Robinhood, so a capability gate that
    passes here passes there, and it enforces the same whole-share rule on
    stops so a fractional stop is a failure here too.
    """

    name: str = "fake_exec"
    live_trading: bool = True
    supports_order_review: bool = True
    declared_capabilities: BrokerCapabilities = field(
        default_factory=lambda: BrokerCapabilities(
            can_read_positions=True,
            can_read_orders=True,
            can_read_cash=True,
            can_place_equity_market=True,
            can_place_equity_limit=True,
            can_place_attached_stop=False,
            can_place_standalone_gtc_stop=True,
            supports_fractional=True,
            supports_specified_lot_sale=True,
        )
    )
    fill_entry: bool = True
    fill_price: float | None = None
    stop_readable: bool = True
    stop_place_succeeds: bool = True
    review_approves: bool = True
    drop_stops: tuple = ()
    #: Left empty by default: an undeclared adapter is accepted at any venue,
    #: which is what a fake standing in for either side needs. Set it to
    #: exercise the mis-registration refusal.
    venue: str = ""
    fill_ratio: float = 1.0
    place_rejects: bool = False
    placement_unknown: bool = False
    phantom_ref_ids: tuple = ()

    def __post_init__(self):
        self._orders: dict[str, _StoredOrder] = {}
        self._seq = 0
        self.placed_entries: list[BrokerOrderRequest] = []
        self.placed_stops: list[dict] = []
        self.calls: dict[str, int] = {}

    def _record(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    @property
    def order_calls(self) -> int:
        """Calls that could create an order at a real broker. Zero is the claim."""
        return self.calls.get("place_order", 0) + self.calls.get("place_stop", 0)

    def _live_order_with_ref(self, ref_id: str, order_type: str | None = None):
        """A *currently open* order carrying ``ref_id``, or ``None``.

        The idempotency key deduplicates against live orders only, which is what
        the upstream does and what the daily replacement job needs: a stop that
        has vanished from the book must be re-placeable under the same ref_id,
        while a retry against a stop that is still there must not create a
        second one.
        """
        if not ref_id:
            return None
        for order in self._orders.values():
            if order.ref_id != ref_id:
                continue
            if order_type and order.order_type != order_type:
                continue
            if (order.status or "").strip().lower() in FINISHED_ORDER_STATES:
                continue
            return order
        return None

    @property
    def supports_fractional(self) -> bool:
        return self.declared_capabilities.supports_fractional

    def capabilities(self) -> BrokerCapabilities:
        return self.declared_capabilities

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    def review_order(self, order: BrokerOrderRequest) -> BrokerOrderReview:
        self._record("review_order")
        if not self.review_approves:
            return BrokerOrderReview(
                broker=self.name,
                request=order,
                approved=False,
                errors=["fake_exec: review configured to reject."],
            )
        notional = None
        if order.quantity and (order.limit_price or self.fill_price):
            notional = float(order.quantity) * float(order.limit_price or self.fill_price)
        return BrokerOrderReview(
            broker=self.name, request=order, approved=True, estimated_notional=notional
        )

    def place_order(self, reviewed_order: BrokerOrderReview) -> BrokerOrderResult:
        self._record("place_order")
        order = reviewed_order.request
        if not reviewed_order.approved:
            return BrokerOrderResult(broker=self.name, success=False, error="not approved")
        ref_id = order.client_context.get("ref_id", "")

        # Idempotency, as the upstream does it: a retry carrying a ref_id that
        # is already working returns *that* order rather than a second one.
        # This is the whole reason the execution service may retry the
        # fill-to-stop race at all (Spec L §5.1).
        existing = self._live_order_with_ref(ref_id)
        if existing is not None:
            return BrokerOrderResult(
                broker=self.name,
                success=True,
                order_id=existing.order_id,
                status=existing.status,
                filled_qty=existing.filled_quantity or None,
                filled_avg_price=existing.average_fill_price,
                raw={"ref_id": ref_id, "deduplicated": True},
            )

        if self.place_rejects:
            return BrokerOrderResult(
                broker=self.name,
                success=False,
                error="fake_exec: the broker rejected the entry.",
            )
        if self.placement_unknown:
            # Ambiguous, not failed: an error carrying a payload and no order
            # id. Spec Q §12 invariant 8 — never guessed successful, never
            # released. Whether an order actually exists is answered by
            # `find_order_by_ref_id`, which is what `phantom_ref_ids` controls.
            return BrokerOrderResult(
                broker=self.name,
                success=False,
                error="fake_exec: the placement response was unknown.",
                raw={"ref_id": ref_id, "http_status": 504},
            )

        order_id = self._next_id("entry")
        price = self.fill_price or order.limit_price or 0.0
        requested = float(order.quantity or 0)
        if self.fill_entry:
            filled_qty = float(int(requested * float(self.fill_ratio)))
            status = "filled" if filled_qty >= requested else "partially_filled"
            avg = float(price)
            if filled_qty <= 0:
                status, avg = "accepted", None
        else:
            status, filled_qty, avg = "accepted", 0.0, None
        self._orders[order_id] = _StoredOrder(
            order_id=order_id,
            symbol=order.symbol.upper(),
            side=order.side,
            order_type=order.order_type,
            status=status,
            quantity=requested,
            ref_id=ref_id,
            limit_price=order.limit_price,
            time_in_force=order.time_in_force,
            filled_quantity=filled_qty,
            average_fill_price=avg,
        )
        self.placed_entries.append(order)
        return BrokerOrderResult(
            broker=self.name,
            success=True,
            order_id=order_id,
            status=status,
            filled_qty=filled_qty or None,
            filled_avg_price=avg,
            raw={"ref_id": ref_id},
        )

    def place_stop(self, *, symbol, quantity, stop_price, ref_id, side="sell") -> BrokerOrderResult:
        self._record("place_stop")
        if int(quantity) != float(quantity) or int(quantity) <= 0:
            return BrokerOrderResult(
                broker=self.name,
                success=False,
                error=f"fake_exec: protective stop is whole-share only; refusing {quantity!r}.",
            )
        if not ref_id:
            return BrokerOrderResult(broker=self.name, success=False, error="fake_exec: stop needs a ref_id.")
        existing = self._live_order_with_ref(ref_id, order_type="stop_market")
        if existing is not None:
            # Same idempotency rule as the entry. A stop that is still working
            # is not re-placed, which is what makes the protection retry safe.
            return BrokerOrderResult(
                broker=self.name,
                success=True,
                order_id=existing.order_id,
                stop_order_id=existing.order_id,
                status=existing.status,
                order_strategy="standalone_gtc_stop",
                raw={"ref_id": ref_id, "deduplicated": True},
            )
        self.placed_stops.append(
            {"symbol": symbol.upper(), "quantity": int(quantity), "stop_price": float(stop_price), "ref_id": ref_id}
        )
        if not self.stop_place_succeeds:
            return BrokerOrderResult(broker=self.name, success=False, error="fake_exec: stop placement configured to fail.")
        order_id = self._next_id("stop")
        # A stop that is not readable is one the broker "accepted" but never
        # surfaces — the exact unprotected-fill shape the window has to catch.
        if self.stop_readable and symbol.upper() not in {s.upper() for s in self.drop_stops}:
            self._orders[order_id] = _StoredOrder(
                order_id=order_id,
                symbol=symbol.upper(),
                side=side,
                order_type="stop_market",
                status="accepted",
                quantity=int(quantity),
                ref_id=ref_id,
                stop_price=float(stop_price),
                time_in_force="gtc",
            )
        return BrokerOrderResult(
            broker=self.name,
            success=True,
            order_id=order_id,
            stop_order_id=order_id,
            status="accepted",
            order_strategy="standalone_gtc_stop",
            raw={"ref_id": ref_id},
        )

    def read_open_orders(self, *, symbol: str | None = None):
        self._record("read_open_orders")
        wanted = (symbol or "").strip().upper()
        out = []
        for order in self._orders.values():
            if wanted and order.symbol != wanted:
                continue
            out.append(
                OpenOrder(
                    broker=self.name,
                    order_id=order.order_id,
                    symbol=order.symbol,
                    side=order.side,
                    order_type=order.order_type,
                    status=order.status,
                    quantity=order.quantity,
                    stop_price=order.stop_price,
                    limit_price=order.limit_price,
                    time_in_force=order.time_in_force,
                    ref_id=order.ref_id,
                    raw={},
                )
            )
        return out

    def get_order_status(self, order_id: str) -> dict:
        self._record("get_order_status")
        order = self._orders.get(order_id)
        if order is None:
            return {}
        return {
            "id": order.order_id,
            "status": order.status,
            "filled_qty": order.filled_quantity,
            "filled_avg_price": order.average_fill_price or 0.0,
            "quantity": order.quantity,
            "symbol": order.symbol,
        }

    def drop_stop(self, symbol: str) -> int:
        """Remove every open stop for ``symbol``; returns how many. Test seam.

        Models Robinhood's unstated GTC horizon expiring a stop between
        sessions, which is exactly what the daily re-placement job exists for.
        """
        symbol = symbol.upper()
        removed = [
            oid
            for oid, order in self._orders.items()
            if order.symbol == symbol and order.order_type == "stop_market"
        ]
        for oid in removed:
            del self._orders[oid]
        return len(removed)

    def cancel_order(self, order_id: str):
        self._record("cancel_order")
        order = self._orders.get(order_id)
        if order is None:
            # Idempotent: cancelling an order that is already gone succeeds,
            # because the caller's intent ("this must not be working") holds.
            return {"success": True, "already_gone": True}
        order.status = "cancelled"
        return {"success": True}

    # --- reconciliation reads (Spec Q §12, Phase 5) ------------------------

    def find_order_by_ref_id(self, ref_id: str) -> dict | None:
        """Resolve an ambiguous placement: does an order with this ref_id exist?

        The one question that decides whether an unknown outcome releases its
        reservation. ``phantom_ref_ids`` is the "yes, it did land" case; a
        ref_id absent from both the live book and that tuple is the
        verified-no-order case, which is terminal.
        """
        self._record("find_order_by_ref_id")
        if not ref_id:
            return None
        order = self._live_order_with_ref(ref_id)
        if order is not None:
            return {
                "id": order.order_id,
                "ref_id": order.ref_id,
                "symbol": order.symbol,
                "state": order.status,
                "quantity": order.quantity,
                "filled_quantity": order.filled_quantity,
            }
        if ref_id in set(self.phantom_ref_ids):
            return {"id": f"phantom-{ref_id}", "ref_id": ref_id, "state": "queued"}
        return None

    def get_positions_detail(self) -> list[dict]:
        """Positions implied by what has filled here, for reconciliation tests."""
        self._record("get_positions_detail")
        rows: dict[str, dict] = {}
        for order in self._orders.values():
            if order.order_type == "stop_market" or (order.side or "").lower() != "buy":
                continue
            if not order.filled_quantity:
                continue
            row = rows.setdefault(
                order.symbol,
                {
                    "ticker": order.symbol,
                    "qty": 0.0,
                    "entry_price": order.average_fill_price or 0.0,
                    "side": "long",
                },
            )
            row["qty"] += float(order.filled_quantity)
        for row in rows.values():
            row["market_value"] = row["qty"] * float(row["entry_price"] or 0.0)
        return list(rows.values())

    def fill_remainder(self, order_id: str) -> float:
        """Fill the rest of a partially filled entry. Test seam.

        Models the second half of a partial fill arriving after the first stop
        was already placed, which is the case §12 requires protection to be
        resized for, idempotently.
        """
        order = self._orders.get(order_id)
        if order is None:
            return 0.0
        remainder = float(order.quantity) - float(order.filled_quantity)
        if remainder <= 0:
            return 0.0
        order.filled_quantity = float(order.quantity)
        order.status = "filled"
        return remainder

    def reject_order(self, order_id: str) -> bool:
        """Move a working order to ``rejected``. Test seam for the timeout path."""
        order = self._orders.get(order_id)
        if order is None:
            return False
        order.status = "rejected"
        return True
