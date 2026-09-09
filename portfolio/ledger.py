"""The read side of the ledger — what ``portfolio_overview`` and friends return.

Pure reads over the Spec L §3 tables. No broker, no network, no model call: the
workspace service imports this module, and Spec L §6's first invariant is that
nothing reachable from an agent surface can reach a placement method.

**On-demand sync is deliberately not here.** Spec L §4 wants a tool call that
finds the freshness budget exceeded to trigger a sync. Triggering a sync means
reaching a broker adapter, and the workspace may not import ``execution`` at
all — the import-graph test is the enforcement and it does not have an
exception for good intentions. So a read tool does what Spec K §4.2 says
instead: it returns the stale value **with the flag set**, and names the sync
that would fix it. The sync runs in the bot process, on the Spec L §4 cadence
and on demand from ``scripts/portfolio_sync.py``. Documented in
``docs/PORTFOLIO_LEDGER.md`` rather than left as a silent gap.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from database.models import (
    BrokerageAccount,
    BrokerOrder,
    CashBalance,
    ExposureTag,
    Holding,
    TaxLot,
)
from portfolio.exposure import aggregate_exposure
from portfolio.freshness import DEFAULT_FRESHNESS_BUDGET_MINUTES, provenance
from portfolio.guards import basis_is_known
from portfolio.settlement import settlement_view
from utils.timeutils import utcnow_naive

#: Order states that count as "open" at a broker. Anything terminal is excluded
#: and shown separately as recently filled.
OPEN_ORDER_STATES = frozenset(
    {"queued", "unconfirmed", "confirmed", "open", "partially_filled", "pending", "new", "accepted"}
)
TERMINAL_ORDER_STATES = frozenset({"filled", "cancelled", "canceled", "rejected", "failed", "expired"})

#: Per-field source labels for the provenance block (Spec K §4.2).
SOURCE_BROKER_SYNC = "broker_sync"
SOURCE_LOCAL_REFERENCE = "local_reference"
SOURCE_DERIVED = "derived"


def current_accounts(session, *, enabled_only: bool = True):
    """Accounts the ledger reads. Public because the snapshot job needs it too."""
    return _accounts(session, enabled_only=enabled_only)


def current_holdings(session, account_ids):
    """The un-superseded holding rows for these accounts."""
    return _current_holdings(session, account_ids)


def current_cash(session, account_ids):
    """The un-superseded cash rows for these accounts."""
    return _current_cash(session, account_ids)


def exposure_tags(session) -> dict:
    """``symbol -> [tag]`` for every tag that has not been retired."""
    return _exposure_tags(session)


def _accounts(session, *, enabled_only: bool = True):
    statement = select(BrokerageAccount)
    if enabled_only:
        statement = statement.where(BrokerageAccount.enabled.is_(True))
    return list(session.execute(statement.order_by(BrokerageAccount.id)).scalars())


def _current_holdings(session, account_ids):
    if not account_ids:
        return []
    return list(
        session.execute(
            select(Holding)
            .where(Holding.account_id.in_(account_ids), Holding.superseded_at.is_(None))
            .order_by(Holding.symbol)
        ).scalars()
    )


def _current_cash(session, account_ids):
    if not account_ids:
        return []
    return list(
        session.execute(
            select(CashBalance).where(
                CashBalance.account_id.in_(account_ids), CashBalance.superseded_at.is_(None)
            )
        ).scalars()
    )


def _current_lots(session, account_ids, symbol=None):
    if not account_ids:
        return []
    statement = select(TaxLot).where(
        TaxLot.account_id.in_(account_ids), TaxLot.superseded_at.is_(None)
    )
    if symbol:
        statement = statement.where(TaxLot.symbol == symbol.upper())
    return list(session.execute(statement.order_by(TaxLot.open_date, TaxLot.id)).scalars())


def _exposure_tags(session) -> dict:
    tags: dict[str, list[str]] = {}
    for row in session.execute(
        select(ExposureTag).where(ExposureTag.retired_at.is_(None))
    ).scalars():
        tags.setdefault(row.symbol.upper(), []).append(row.tag)
    return {symbol: sorted(set(values)) for symbol, values in tags.items()}


def ledger_as_of(accounts, holdings, cash_rows) -> datetime | None:
    """The **oldest** current ``as_of_utc`` across the ledger.

    Oldest, not newest, on purpose: an overview is only as fresh as its stalest
    account, and taking the newest would let one account that syncs cleanly hide
    another that has been failing for a day.
    """
    stamps = [row.as_of_utc for row in list(holdings) + list(cash_rows) if row.as_of_utc]
    stamps += [a.last_sync_at for a in accounts if a.last_sync_at]
    return min(stamps) if stamps else None


def _account_payload(account) -> dict:
    return {
        "id": account.id,
        "broker": account.broker,
        "account": account.external_account_id,
        "label": account.label,
        "account_type": account.account_type,
        "currency": account.currency,
        "booking_method": account.booking_method,
        "agent_placeable": bool(account.agent_placeable),
        "enabled": bool(account.enabled),
        "capabilities": account.capabilities,
        "last_sync_at": account.last_sync_at.isoformat() if account.last_sync_at else None,
        "last_sync_id": account.last_sync_id,
        "last_sync_error": account.last_sync_error or "",
    }


def _holding_payload(holding, label: str) -> dict:
    return {
        "symbol": holding.symbol,
        "instrument_type": holding.instrument_type,
        "account": label,
        "quantity": holding.quantity,
        # Null basis stays null all the way to the wire. A consumer that wants
        # a number must ask `portfolio.guards.require_cost_basis` and handle the
        # refusal (Spec L §3, test_unknown_basis_is_null_not_zero).
        "average_cost": holding.average_cost,
        "cost_basis": holding.cost_basis,
        "cost_basis_known": basis_is_known(holding.cost_basis),
        "last_price": holding.last_price,
        "market_value": holding.market_value,
        "notional": holding.notional,
        "currency": holding.currency,
        "as_of_utc": holding.as_of_utc.isoformat() if holding.as_of_utc else None,
        "source": holding.source,
        "instrument_detail": holding.instrument_detail,
    }


def _cash_payload(row, label: str, as_of_date) -> dict:
    view = settlement_view(
        settled_cash=row.settled_cash,
        unsettled_cash=row.unsettled_cash,
        pending_settlements=row.pending_settlements,
        as_of_date=as_of_date,
    )
    return {
        "account": label,
        "settled_cash": row.settled_cash,
        "unsettled_cash": row.unsettled_cash,
        "buying_power": row.buying_power,
        "currency": row.currency,
        "spendable_today": view.available,
        "pending_settlements": [
            {"settles_on": day.isoformat(), "amount": round(amount, 2)}
            for day, amount in sorted(view.pending_by_date.items())
        ],
        "as_of_utc": row.as_of_utc.isoformat() if row.as_of_utc else None,
    }


def portfolio_overview(
    session,
    *,
    now: datetime | None = None,
    budget_minutes: int = DEFAULT_FRESHNESS_BUDGET_MINUTES,
    sectors: dict | None = None,
    include_disabled: bool = False,
) -> dict:
    """Holdings, cash, exposure and freshness for every enabled account.

    Exposure spans **all** accounts (Spec L §5.1): a name held in the read-only
    primary account is counted when sizing anything in the Agentic one.
    """
    now = now or utcnow_naive()
    accounts = _accounts(session, enabled_only=not include_disabled)
    labels = {a.id: (a.label or f"{a.broker}:{a.external_account_id}") for a in accounts}
    account_ids = [a.id for a in accounts]

    holdings = _current_holdings(session, account_ids)
    cash_rows = _current_cash(session, account_ids)
    tags = _exposure_tags(session)

    as_of = ledger_as_of(accounts, holdings, cash_rows)
    as_of_date = (as_of or now).date()

    cash_total = sum(float(row.settled_cash or 0.0) + float(row.unsettled_cash or 0.0) for row in cash_rows)
    exposure = aggregate_exposure(
        holdings,
        cash_total=cash_total,
        sectors=sectors,
        tags=tags,
        account_labels=labels,
    )

    warnings = list(exposure.warnings)
    unknown_basis = sorted({h.symbol for h in holdings if not basis_is_known(h.cost_basis)})
    if unknown_basis:
        warnings.append(
            "unknown_cost_basis: the broker supplied no basis for "
            f"{', '.join(unknown_basis)}. Null is unknown, never zero — no "
            "return is computed for these."
        )
    stale_accounts = [labels[a.id] for a in accounts if a.last_sync_error]
    if stale_accounts:
        warnings.append(
            "account_sync_error: the last sync failed for "
            f"{', '.join(stale_accounts)}; their rows are the previous good "
            "ones, not zeros."
        )

    block = provenance(
        as_of,
        now,
        budget_minutes=budget_minutes,
        sources={
            "holdings": SOURCE_BROKER_SYNC,
            "cash": SOURCE_BROKER_SYNC,
            "exposure": SOURCE_DERIVED,
            "sectors": SOURCE_LOCAL_REFERENCE,
            "exposure_tags": SOURCE_LOCAL_REFERENCE,
        },
        warnings=warnings,
    )
    if block.stale:
        warnings.append(
            "A read tool cannot trigger a sync: the workspace may not import a "
            "broker adapter (Spec L §6). Run `python -m scripts.portfolio_sync` "
            "or wait for the scheduled sync."
        )
        block = provenance(
            as_of,
            now,
            budget_minutes=budget_minutes,
            sources=block.sources,
            warnings=warnings,
        )

    return {
        "accounts": [_account_payload(a) for a in accounts],
        "holdings": [_holding_payload(h, labels[h.account_id]) for h in holdings],
        "cash": [_cash_payload(row, labels[row.account_id], as_of_date) for row in cash_rows],
        "cash_total": round(cash_total, 2),
        "exposure": exposure.as_dict(),
        "warnings": warnings,
        "provenance": block.as_dict(),
    }


def position_detail(
    session,
    symbol: str,
    *,
    now: datetime | None = None,
    budget_minutes: int = DEFAULT_FRESHNESS_BUDGET_MINUTES,
    include_disabled: bool = False,
) -> dict:
    """One position across every account: rows, lots, basis, unrealized.

    ``unrealized`` is ``None`` — not zero — wherever the basis is unknown. The
    linked thesis arrives with Spec M in Phase 2; the key is present and null so
    a client written against this response does not change shape then.
    """
    now = now or utcnow_naive()
    symbol = (symbol or "").strip().upper()
    accounts = _accounts(session, enabled_only=not include_disabled)
    labels = {a.id: (a.label or f"{a.broker}:{a.external_account_id}") for a in accounts}
    account_ids = [a.id for a in accounts]

    rows = [h for h in _current_holdings(session, account_ids) if h.symbol == symbol]
    lots = [lot for lot in _current_lots(session, account_ids, symbol)]
    cash_rows = _current_cash(session, account_ids)
    as_of = ledger_as_of(accounts, rows, cash_rows)

    total_quantity = sum(float(h.quantity or 0.0) for h in rows)
    market_value = sum(float(h.market_value or 0.0) for h in rows)
    basis_known = bool(rows) and all(basis_is_known(h.cost_basis) for h in rows)
    total_basis = sum(float(h.cost_basis or 0.0) for h in rows) if basis_known else None
    unrealized = (market_value - total_basis) if (basis_known and rows) else None

    warnings: list[str] = []
    if rows and not basis_known:
        warnings.append(
            "unknown_cost_basis: at least one lot of this position has no basis "
            "from the broker, so unrealized P&L is null rather than computed."
        )
    unsupported = [h for h in rows if h.instrument_type != "equity"]
    if unsupported:
        warnings.append(
            "unsupported_instrument_present: "
            f"{len(unsupported)} non-equity position(s) in {symbol}, total notional "
            f"${sum(abs(float(h.notional or h.market_value or 0.0)) for h in unsupported):,.2f}."
        )

    return {
        "symbol": symbol,
        "found": bool(rows),
        "total_quantity": total_quantity,
        "market_value": round(market_value, 2) if rows else None,
        "cost_basis": round(total_basis, 2) if total_basis is not None else None,
        "cost_basis_known": basis_known,
        "unrealized": round(unrealized, 2) if unrealized is not None else None,
        "positions": [_holding_payload(h, labels[h.account_id]) for h in rows],
        "tax_lots": [
            {
                "account": labels.get(lot.account_id, str(lot.account_id)),
                "broker_lot_id": lot.broker_lot_id,
                "open_date": lot.open_date.isoformat() if lot.open_date else None,
                "quantity": lot.quantity,
                "cost_basis": lot.cost_basis,
                "cost_basis_known": basis_is_known(lot.cost_basis),
                "term": lot.term,
                "booking_method": lot.booking_method,
            }
            for lot in lots
        ],
        "exposure_tags": _exposure_tags(session).get(symbol, []),
        "thesis": None,
        "warnings": warnings,
        "provenance": provenance(
            as_of,
            now,
            budget_minutes=budget_minutes,
            sources={
                "positions": SOURCE_BROKER_SYNC,
                "tax_lots": SOURCE_BROKER_SYNC,
                "unrealized": SOURCE_DERIVED,
                "exposure_tags": SOURCE_LOCAL_REFERENCE,
                "thesis": "not_available_until_phase_2",
            },
            warnings=warnings,
        ).as_dict(),
    }


def orders_open(
    session,
    *,
    now: datetime | None = None,
    budget_minutes: int = DEFAULT_FRESHNESS_BUDGET_MINUTES,
    include_recent_fills: bool = True,
    include_disabled: bool = False,
) -> dict:
    """Pending and recently filled orders across every account.

    An order Bryan placed in the Robinhood app appears here with
    ``origin='external'`` and a null ``execution_id``; that is the normal case,
    not an anomaly, and the field says which is which.
    """
    now = now or utcnow_naive()
    accounts = _accounts(session, enabled_only=not include_disabled)
    labels = {a.id: (a.label or f"{a.broker}:{a.external_account_id}") for a in accounts}
    account_ids = [a.id for a in accounts]

    rows = (
        list(
            session.execute(
                select(BrokerOrder)
                .where(BrokerOrder.account_id.in_(account_ids))
                .order_by(BrokerOrder.submitted_at.desc(), BrokerOrder.id.desc())
            ).scalars()
        )
        if account_ids
        else []
    )

    def payload(order):
        return {
            "broker_order_id": order.broker_order_id,
            "ref_id": order.ref_id,
            "account": labels.get(order.account_id, str(order.account_id)),
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
            "order_type": order.order_type,
            "time_in_force": order.time_in_force,
            "limit_price": order.limit_price,
            "stop_price": order.stop_price,
            "status": order.status,
            "origin": order.origin,
            "execution_id": order.execution_id,
            "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
            "filled_at": order.filled_at.isoformat() if order.filled_at else None,
            "filled_quantity": order.filled_quantity,
            "average_fill_price": order.average_fill_price,
        }

    open_orders = [payload(o) for o in rows if (o.status or "").lower() in OPEN_ORDER_STATES]
    recent = (
        [payload(o) for o in rows if (o.status or "").lower() in TERMINAL_ORDER_STATES][:25]
        if include_recent_fills
        else []
    )
    as_of = ledger_as_of(accounts, [], [])

    return {
        "open_orders": open_orders,
        "recent_orders": recent,
        "external_count": sum(1 for o in open_orders if o["origin"] == "external"),
        "provenance": provenance(
            as_of,
            now,
            budget_minutes=budget_minutes,
            sources={"orders": SOURCE_BROKER_SYNC},
        ).as_dict(),
    }


def last_sync_age_seconds(session, *, now: datetime | None = None) -> float | None:
    """For ``/health`` (Spec K §6). ``None`` when no sync has ever completed."""
    now = now or utcnow_naive()
    stamps = [
        a.last_sync_at
        for a in _accounts(session, enabled_only=True)
        if a.last_sync_at is not None
    ]
    return None if not stamps else max(0.0, (now - min(stamps)).total_seconds())
