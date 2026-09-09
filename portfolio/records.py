"""The normalized shapes a broker adapter hands the sync (Spec L §4).

Plain frozen dataclasses, no ORM and no broker vocabulary. An adapter's job is
to turn whatever its API returns into these; the sync's job is to write them
append-style. That split is what makes ``tests/test_fake_broker_contract`` a
real contract rather than a mock: the fake broker and the Robinhood adapter
produce the same records, so a future Schwab adapter has something to satisfy.

Two conventions the whole ledger depends on:

``None`` is unknown, not zero
    ``average_cost=None`` means the broker did not tell us. Nothing downstream
    may substitute a zero (Spec L §3, ``test_unknown_basis_is_null_not_zero``).

Non-equity positions are carried, never dropped
    A record with ``instrument_type != "equity"`` flows through the sync into
    ``holdings`` and out again as an ``unsupported_instrument_present`` warning
    with its notional. Filtering it here would make the failure invisible at
    exactly the layer that could still see it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

EQUITY = "equity"
OPTION = "option"
CRYPTO = "crypto"
OTHER = "other"

#: Everything the ledger stores but does not model for analysis.
UNSUPPORTED_INSTRUMENT_TYPES = frozenset({OPTION, CRYPTO, OTHER})


@dataclass(frozen=True)
class AccountRecord:
    """One account the broker token can see.

    ``agent_placeable`` mirrors Robinhood's own ``agentic_allowed``: reads span
    every account, placement is confined to the Agentic one (Spec L §5.1).
    """

    broker: str
    external_account_id: str
    label: str = ""
    account_type: str = "cash"
    currency: str = "USD"
    booking_method: str = "NONE"
    agent_placeable: bool = False
    enabled: bool = True


@dataclass(frozen=True)
class HoldingRecord:
    symbol: str
    quantity: float
    instrument_type: str = EQUITY
    average_cost: float | None = None
    cost_basis: float | None = None
    last_price: float | None = None
    market_value: float | None = None
    notional: float | None = None
    currency: str = "USD"
    instrument_detail: dict = field(default_factory=dict)
    source: str = ""

    @property
    def is_unsupported_instrument(self) -> bool:
        return self.instrument_type in UNSUPPORTED_INSTRUMENT_TYPES


@dataclass(frozen=True)
class TaxLotRecord:
    symbol: str
    quantity: float
    broker_lot_id: str | None = None
    open_date: date | None = None
    cost_basis: float | None = None
    term: str = "unknown"
    booking_method: str = "NONE"
    source: str = ""


@dataclass(frozen=True)
class PendingSettlement:
    """Sale proceeds that are not redeployable yet (cash account, T+1)."""

    amount: float
    settles_on: date
    source: str = ""

    def as_dict(self) -> dict:
        return {
            "amount": float(self.amount),
            "settles_on": self.settles_on.isoformat(),
            "source": self.source,
        }


@dataclass(frozen=True)
class CashRecord:
    settled_cash: float | None = None
    unsettled_cash: float | None = None
    buying_power: float | None = None
    currency: str = "USD"
    pending_settlements: tuple[PendingSettlement, ...] = ()
    source: str = ""


@dataclass(frozen=True)
class OrderRecord:
    broker_order_id: str
    symbol: str
    side: str = ""
    quantity: float | None = None
    order_type: str = ""
    time_in_force: str = ""
    limit_price: float | None = None
    stop_price: float | None = None
    status: str = ""
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    filled_quantity: float | None = None
    average_fill_price: float | None = None
    ref_id: str | None = None
    #: ``external`` unless the row can be tied to an execution this system
    #: originated. The sync never invents ``system``.
    origin: str = "external"
    execution_id: str | None = None
    raw: dict = field(default_factory=dict)
    source: str = ""


@dataclass(frozen=True)
class AccountSnapshot:
    """Everything one account looked like at one moment.

    ``error`` set means the adapter could not read this account. The sync then
    leaves every previous row for the account intact and marks it stale; it
    never treats an unreadable account as an empty one (Spec L §4 failure
    policy, ``test_sync_never_zeroes_on_error``).
    """

    account: AccountRecord
    as_of_utc: datetime
    holdings: tuple[HoldingRecord, ...] = ()
    tax_lots: tuple[TaxLotRecord, ...] = ()
    cash: CashRecord | None = None
    orders: tuple[OrderRecord, ...] = ()
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass(frozen=True)
class BrokerSnapshot:
    """What one adapter returns for one sync run, across all its accounts."""

    broker: str
    accounts: tuple[AccountSnapshot, ...] = ()
    error: str = ""
