"""Builders for portfolio-ledger tests.

Thin: a fake broker with canned accounts, and a database. Everything else in
the ledger tests goes through the production code paths — ``run_sync``,
``portfolio.ledger``, the real Robinhood normalizers replayed over fixtures —
because a test that assembles ORM rows by hand proves the reader works and says
nothing about the writer.

The name is deliberately not ``test_*``: ``unittest discover -p "test_*.py"``
would otherwise import it as a test module.
"""

from __future__ import annotations

from datetime import date, datetime

from execution.brokers.fake import FakeAccount, FakeBroker
from portfolio.records import (
    AccountRecord,
    CashRecord,
    HoldingRecord,
    OrderRecord,
    PendingSettlement,
    TaxLotRecord,
)

NOW = datetime(2026, 9, 9, 14, 0, 0)

AGENTIC = AccountRecord(
    broker="fake",
    external_account_id="****4021",
    label="Agentic",
    account_type="cash",
    booking_method="STRICT",
    agent_placeable=True,
    enabled=True,
)

PRIMARY = AccountRecord(
    broker="fake",
    external_account_id="****7788",
    label="Primary",
    account_type="margin",
    booking_method="FIFO",
    agent_placeable=False,
    enabled=True,
)


def holding(symbol, quantity, *, price=100.0, basis=None, instrument_type="equity", notional=None, detail=None):
    return HoldingRecord(
        symbol=symbol,
        quantity=quantity,
        instrument_type=instrument_type,
        average_cost=(basis / quantity) if (basis is not None and quantity) else None,
        cost_basis=basis,
        last_price=price,
        market_value=quantity * price,
        notional=notional if notional is not None else quantity * price,
        instrument_detail=detail or {},
        source="fake",
    )


def two_account_broker(*, as_of=NOW, agentic_holdings=None, primary_holdings=None, **kwargs):
    """The shape that matters: one placeable cash account, one read-only one."""
    agentic = FakeAccount(
        record=AGENTIC,
        holdings=tuple(
            agentic_holdings
            if agentic_holdings is not None
            else (holding("AMD", 6, price=151.2, basis=852.9), holding("TDW", 9, price=41.93))
        ),
        tax_lots=(
            TaxLotRecord(
                symbol="AMD",
                quantity=6,
                broker_lot_id="lot-9f2c",
                open_date=date(2026, 8, 18),
                cost_basis=852.9,
                term="short",
                booking_method="STRICT",
            ),
            TaxLotRecord(
                symbol="TDW",
                quantity=9,
                broker_lot_id="lot-4b71",
                open_date=date(2026, 7, 2),
                cost_basis=None,
                term="unknown",
                booking_method="STRICT",
            ),
        ),
        cash=CashRecord(
            settled_cash=80.0,
            unsettled_cash=145.0,
            buying_power=80.0,
            pending_settlements=(
                PendingSettlement(
                    amount=145.0,
                    settles_on=date(2026, 9, 10),
                    source="derived: T+1 from trade date",
                ),
            ),
        ),
        orders=(
            OrderRecord(
                broker_order_id="ord-7731",
                symbol="AMD",
                side="buy",
                quantity=2.0,
                order_type="limit",
                time_in_force="gtc",
                limit_price=148.0,
                status="queued",
                submitted_at=datetime(2026, 9, 9, 13, 31, 4),
                ref_id="d2f1c0a4",
                origin="external",
            ),
        ),
    )
    primary = FakeAccount(
        record=PRIMARY,
        holdings=tuple(
            primary_holdings
            if primary_holdings is not None
            else (holding("AMD", 45, price=151.2, basis=4428.0),)
        ),
        cash=CashRecord(settled_cash=1000.0, unsettled_cash=0.0, buying_power=2000.0),
    )
    return FakeBroker(accounts=[agentic, primary], as_of_utc=as_of, **kwargs)
