"""The portfolio sync job (Spec L §4). No inference, no model call, no writes to a broker.

    for each enabled account:
        pull positions, cash, orders, lots
        normalize    -> portfolio.records
        write append-style with a shared sync_id
        reconcile    -> page on a mismatch

Three failure policies, and they are the reason this module is longer than the
happy path needs:

**Never zero on error.** A broker that raises leaves every previous row intact
and marks the account stale with the error text. The alternative — writing the
empty result of a failed read — turns a network blip into "you own nothing",
which is a portfolio view that would pass every schema check and be catastrophic
to act on.

**Fail closed on mass deletion.** A sync that would supersede more than half of
an account's known holdings writes *nothing for that account* and pages. Real
liquidations happen; they are also exactly what a partially-successful broker
read looks like, and the two are indistinguishable from inside this process. A
human unblocks it (``--force-mass-deletion``), which is the correct cost.

**Append, never update.** Every write is an insert. A superseded row keeps its
values and gains ``superseded_at`` and ``superseded_by_sync_id``, so "what did I
hold on date D" is answered by a filter rather than by a backup.

The broker is a *parameter*, not an import: anything with
``fetch_ledger_snapshot()`` returning a :class:`portfolio.records.BrokerSnapshot`
works. That keeps ``portfolio/`` free of ``execution/`` and is what makes the
Spec L §6 import-graph assertion cheap to keep true.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select

from database.models import (
    BrokerageAccount,
    BrokerOrder,
    CashBalance,
    Holding,
    TaxLot,
)
from portfolio import paging
from portfolio.records import AccountSnapshot, BrokerSnapshot
from utils.logger import get_logger
from utils.timeutils import utcnow_naive

log = get_logger("portfolio_sync")

#: Spec L §4: "a sync that would delete more than 50% of known holdings fails
#: closed and pages instead of writing".
MASS_DELETION_THRESHOLD = 0.5


def new_sync_id() -> str:
    return uuid.uuid4().hex


@dataclass
class AccountSyncResult:
    account_key: str
    ok: bool = False
    error: str = ""
    holdings_written: int = 0
    holdings_superseded: int = 0
    lots_written: int = 0
    cash_written: int = 0
    orders_written: int = 0
    orders_updated: int = 0
    blocked_reason: str = ""
    dropped_symbols: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_reason)


@dataclass
class SyncResult:
    sync_id: str
    started_at: datetime
    finished_at: datetime | None = None
    broker: str = ""
    accounts: list[AccountSyncResult] = field(default_factory=list)
    error: str = ""
    pages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error and all(a.ok for a in self.accounts)

    def as_dict(self) -> dict:
        return {
            "sync_id": self.sync_id,
            "broker": self.broker,
            "ok": self.ok,
            "error": self.error,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "pages": list(self.pages),
            "accounts": [
                {
                    "account": a.account_key,
                    "ok": a.ok,
                    "error": a.error,
                    "blocked_reason": a.blocked_reason,
                    "holdings_written": a.holdings_written,
                    "holdings_superseded": a.holdings_superseded,
                    "lots_written": a.lots_written,
                    "cash_written": a.cash_written,
                    "orders_written": a.orders_written,
                    "orders_updated": a.orders_updated,
                    "dropped_symbols": list(a.dropped_symbols),
                }
                for a in self.accounts
            ],
        }


# --- account rows ----------------------------------------------------------

def upsert_account(session, record, *, capabilities: dict | None = None, now=None) -> BrokerageAccount:
    """Find or create the ``brokerage_accounts`` row for one account record.

    The account row is the one part of the ledger that *is* updated in place:
    it is a description of an account, not an observation of a position, and
    keeping a history of "the label changed" would bury the history that
    matters.
    """
    now = now or utcnow_naive()
    account = session.execute(
        select(BrokerageAccount).where(
            BrokerageAccount.broker == record.broker,
            BrokerageAccount.external_account_id == record.external_account_id,
        )
    ).scalar_one_or_none()
    if account is None:
        account = BrokerageAccount(
            broker=record.broker,
            external_account_id=record.external_account_id,
            created_at=now,
        )
        session.add(account)
    account.label = record.label or account.label or ""
    account.account_type = record.account_type
    account.currency = record.currency
    account.booking_method = record.booking_method
    account.agent_placeable = bool(record.agent_placeable)
    account.enabled = bool(record.enabled)
    if capabilities is not None:
        account.capabilities_json = json.dumps(capabilities, sort_keys=True)
        account.capabilities_probed_at = now
    account.updated_at = now
    session.flush()
    return account


def _current(session, model, account_id):
    return list(
        session.execute(
            select(model).where(model.account_id == account_id, model.superseded_at.is_(None))
        ).scalars()
    )


def _supersede(rows, *, sync_id: str, at: datetime) -> int:
    for row in rows:
        row.superseded_at = at
        row.superseded_by_sync_id = sync_id
    return len(rows)


# --- the sync itself -------------------------------------------------------

def _holding_key(row) -> tuple[str, str]:
    return (str(row.symbol).upper(), row.instrument_type or "equity")


def mass_deletion_check(
    current_rows, incoming, *, threshold: float = MASS_DELETION_THRESHOLD
) -> tuple[bool, tuple[str, ...], float]:
    """``(blocked, dropped_keys, fraction)`` for one account's holdings.

    "Deleted" means a holding this ledger currently believes in that the new
    snapshot does not mention at all. A position whose quantity merely fell is
    not a deletion, and counting it as one would block every trim.
    """
    known = {_holding_key(row) for row in current_rows}
    if not known:
        return False, (), 0.0
    incoming_keys = {(r.symbol.upper(), r.instrument_type) for r in incoming}
    dropped = known - incoming_keys
    fraction = len(dropped) / len(known)
    ordered = tuple(sorted(f"{sym}:{kind}" for sym, kind in dropped))
    return fraction > threshold, ordered, fraction


def _sync_account(
    session,
    snapshot: AccountSnapshot,
    *,
    sync_id: str,
    now: datetime,
    pager,
    capabilities: dict | None,
    threshold: float,
    force_mass_deletion: bool,
) -> AccountSyncResult:
    record = snapshot.account
    key = f"{record.broker}:{record.external_account_id}"
    result = AccountSyncResult(account_key=key)

    account = upsert_account(session, record, capabilities=capabilities, now=now)

    if not snapshot.ok:
        # The failure policy in one place: previous rows are untouched, the
        # account is marked stale, and nothing is zeroed.
        account.last_sync_error = snapshot.error
        account.last_sync_error_at = now
        result.error = snapshot.error
        pager(
            paging.SYNC_ACCOUNT_FAILED,
            {
                "account": key,
                "sync_id": sync_id,
                "error": snapshot.error,
                "detail": "previous rows left intact; the account is marked stale.",
            },
        )
        return result

    current_holdings = _current(session, Holding, account.id)
    blocked, dropped, fraction = mass_deletion_check(
        current_holdings, snapshot.holdings, threshold=threshold
    )
    if blocked and not force_mass_deletion:
        reason = (
            f"a sync would drop {len(dropped)} of {len(current_holdings)} known "
            f"holdings ({fraction:.0%} > {threshold:.0%}); nothing was written."
        )
        result.blocked_reason = reason
        result.dropped_symbols = dropped
        result.error = reason
        account.last_sync_error = reason
        account.last_sync_error_at = now
        pager(
            paging.MASS_DELETION_BLOCKED,
            {
                "account": key,
                "sync_id": sync_id,
                "known_holdings": len(current_holdings),
                "dropped": list(dropped),
                "fraction": round(fraction, 4),
                "threshold": threshold,
                "detail": (
                    "Fails closed: a partial broker read and a real liquidation "
                    "look identical from here. Re-run with force_mass_deletion "
                    "once a human has confirmed the positions are gone."
                ),
            },
        )
        return result

    as_of = snapshot.as_of_utc

    result.holdings_superseded = _supersede(current_holdings, sync_id=sync_id, at=now)
    for holding in snapshot.holdings:
        session.add(
            Holding(
                sync_id=sync_id,
                account_id=account.id,
                symbol=holding.symbol.upper(),
                instrument_type=holding.instrument_type,
                instrument_detail_json=json.dumps(holding.instrument_detail or {}, sort_keys=True),
                quantity=float(holding.quantity),
                average_cost=holding.average_cost,
                cost_basis=holding.cost_basis,
                last_price=holding.last_price,
                market_value=holding.market_value,
                notional=holding.notional,
                currency=holding.currency,
                as_of_utc=as_of,
                source=holding.source or record.broker,
                created_at=now,
            )
        )
        result.holdings_written += 1

    _supersede(_current(session, TaxLot, account.id), sync_id=sync_id, at=now)
    for lot in snapshot.tax_lots:
        session.add(
            TaxLot(
                sync_id=sync_id,
                account_id=account.id,
                symbol=lot.symbol.upper(),
                broker_lot_id=lot.broker_lot_id,
                open_date=lot.open_date,
                quantity=float(lot.quantity),
                cost_basis=lot.cost_basis,
                term=lot.term,
                booking_method=lot.booking_method or record.booking_method,
                as_of_utc=as_of,
                source=lot.source or record.broker,
                created_at=now,
            )
        )
        result.lots_written += 1

    if snapshot.cash is not None:
        _supersede(_current(session, CashBalance, account.id), sync_id=sync_id, at=now)
        session.add(
            CashBalance(
                sync_id=sync_id,
                account_id=account.id,
                settled_cash=snapshot.cash.settled_cash,
                unsettled_cash=snapshot.cash.unsettled_cash,
                buying_power=snapshot.cash.buying_power,
                currency=snapshot.cash.currency,
                pending_settlements_json=json.dumps(
                    [p.as_dict() for p in snapshot.cash.pending_settlements]
                ),
                as_of_utc=as_of,
                source=snapshot.cash.source or record.broker,
                created_at=now,
            )
        )
        result.cash_written = 1

    for order in snapshot.orders:
        existing = session.execute(
            select(BrokerOrder).where(
                BrokerOrder.account_id == account.id,
                BrokerOrder.broker_order_id == order.broker_order_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = BrokerOrder(
                account_id=account.id,
                broker_order_id=order.broker_order_id,
                created_at=now,
            )
            session.add(existing)
            result.orders_written += 1
        else:
            result.orders_updated += 1
        existing.ref_id = order.ref_id
        existing.symbol = order.symbol.upper()
        existing.side = order.side
        existing.quantity = order.quantity
        existing.order_type = order.order_type
        existing.time_in_force = order.time_in_force
        existing.limit_price = order.limit_price
        existing.stop_price = order.stop_price
        existing.status = order.status
        existing.submitted_at = order.submitted_at
        existing.filled_at = order.filled_at
        existing.filled_quantity = order.filled_quantity
        existing.average_fill_price = order.average_fill_price
        # `origin` is never upgraded to `system` here. An order this system
        # placed is linked at placement time, by the execution service; a sync
        # that inferred authorship from a matching quantity would eventually be
        # wrong about the one order it mattered for.
        if not existing.origin or existing.origin == "external":
            existing.origin = order.origin or "external"
        if order.execution_id:
            existing.execution_id = order.execution_id
        existing.sync_id = sync_id
        existing.as_of_utc = as_of
        existing.source = order.source or record.broker
        existing.raw_json = json.dumps(order.raw or {}, sort_keys=True, default=str)
        existing.updated_at = now

    account.last_sync_id = sync_id
    account.last_sync_at = as_of
    account.last_sync_error = ""
    result.ok = True
    session.flush()
    return result


def run_sync(
    session,
    broker,
    *,
    now: datetime | None = None,
    sync_id: str | None = None,
    pager=paging.log_pager,
    threshold: float = MASS_DELETION_THRESHOLD,
    force_mass_deletion: bool = False,
    reconciler=None,
) -> SyncResult:
    """Run one sync of ``broker`` into the ledger.

    ``broker`` needs ``fetch_ledger_snapshot()`` and, optionally,
    ``capabilities()``. Anything else about it is the adapter's business.

    ``reconciler(session, account, holdings)`` is called per successful account
    and returns mismatch dicts; a non-empty result pages
    ``reconciliation_required`` (Spec L §4.3). It is injected for the same
    reason the broker is: the existing reconciliation path lives in
    ``tracking/`` and this package does not reach into it.
    """
    now = now or utcnow_naive()
    sync_id = sync_id or new_sync_id()
    pages: list[str] = []

    def page(event, detail):
        pages.append(event)
        pager(event, detail)

    result = SyncResult(sync_id=sync_id, started_at=now, broker=getattr(broker, "name", ""))

    try:
        capabilities = (
            broker.capabilities().as_dict() if hasattr(broker, "capabilities") else None
        )
    except Exception as exc:  # a capability probe must not fail a read sync
        capabilities = None
        log.warning("capability_probe_failed", broker=result.broker, error=str(exc))

    try:
        snapshot: BrokerSnapshot = broker.fetch_ledger_snapshot()
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        result.finished_at = utcnow_naive()
        page(
            paging.SYNC_FAILED,
            {
                "broker": result.broker,
                "sync_id": sync_id,
                "error": result.error,
                "detail": "no rows were written; every previous row is intact.",
            },
        )
        result.pages = pages
        return result

    result.broker = snapshot.broker or result.broker
    if snapshot.error:
        result.error = snapshot.error
        page(
            paging.SYNC_FAILED,
            {"broker": result.broker, "sync_id": sync_id, "error": snapshot.error},
        )

    for account_snapshot in snapshot.accounts:
        account_result = _sync_account(
            session,
            account_snapshot,
            sync_id=sync_id,
            now=now,
            pager=page,
            capabilities=capabilities,
            threshold=threshold,
            force_mass_deletion=force_mass_deletion,
        )
        result.accounts.append(account_result)

        if account_result.ok and reconciler is not None:
            account = session.execute(
                select(BrokerageAccount).where(
                    BrokerageAccount.broker == account_snapshot.account.broker,
                    BrokerageAccount.external_account_id
                    == account_snapshot.account.external_account_id,
                )
            ).scalar_one()
            try:
                mismatches = reconciler(session, account, account_snapshot.holdings) or []
            except Exception as exc:
                log.error("reconciler_failed", account=account_result.account_key, error=str(exc))
                mismatches = []
            if mismatches:
                page(
                    paging.RECONCILIATION_REQUIRED,
                    {
                        "account": account_result.account_key,
                        "sync_id": sync_id,
                        "mismatches": mismatches,
                    },
                )

    session.commit()
    result.finished_at = utcnow_naive()
    result.pages = pages
    log.info("portfolio_sync_finished", **result.as_dict())
    return result
