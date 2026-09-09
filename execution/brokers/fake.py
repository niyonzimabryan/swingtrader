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
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerOrderReview,
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
