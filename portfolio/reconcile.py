"""Reconciliation: what the system believes it opened versus what the broker reports.

Spec L §4.3. A mismatch in either direction raises ``reconciliation_required``
and pages the out-of-band channel. Detection only — this module does not
*repair* anything. ``tracking/position_reconciliation.py`` already owns the
repair path (it creates or updates ``Trade`` rows from broker positions), and a
sync that silently rewrote trade rows would make the ledger's history depend on
the order the two jobs happened to run in.

Two directions, and they are different failures:

``missing_at_broker``
    the system believes a position is open that the broker does not report. A
    stop filled overnight, a manual close, or a position this system never
    actually opened.
``unknown_to_system``
    the broker reports a position with no matching open trade. Bryan bought it
    in the app — expected and normal — or an order this system placed was never
    recorded, which is not.
"""

from __future__ import annotations

from sqlalchemy import select

from database.models import Ticker, Trade

ACTIVE_TRADE_STATUSES = ("open", "pending_fill")

MISSING_AT_BROKER = "missing_at_broker"
UNKNOWN_TO_SYSTEM = "unknown_to_system"


def detect_mismatches(session, account, holdings) -> list[dict]:
    """Compare one account's broker holdings against this system's open trades.

    ``unknown_to_system`` is reported only for accounts the agent can place in.
    In a read-only account every position is one Bryan opened himself, so
    reporting each of them as a mismatch would page on the normal case and the
    page would be ignored within a week.
    """
    broker = (account.broker or "").lower()
    broker_symbols = {
        str(h.symbol).upper()
        for h in holdings
        if getattr(h, "instrument_type", "equity") == "equity" and float(h.quantity or 0) != 0
    }

    # Which open trades belong to *this* account. A trade tagged with the
    # account matches it; an untagged one matches only the agent-placeable
    # account, because that is the only account this system opens trades in.
    # Without that scoping, reconciling the read-only account would report
    # every Agentic position as missing, and the page would fire on the normal
    # case from the first run.
    account_id = account.external_account_id
    rows = [
        (trade, ticker)
        for trade, ticker in session.execute(
            select(Trade, Ticker)
            .join(Ticker, Trade.ticker_id == Ticker.id)
            .where(Trade.status.in_(ACTIVE_TRADE_STATUSES), Trade.broker == broker)
        ).all()
        if trade.broker_account_id == account_id
        or (not trade.broker_account_id and getattr(account, "agent_placeable", False))
    ]

    system_symbols = {str(ticker.symbol).upper(): trade for trade, ticker in rows}

    mismatches: list[dict] = []
    for symbol, trade in sorted(system_symbols.items()):
        if symbol not in broker_symbols:
            mismatches.append(
                {
                    "kind": MISSING_AT_BROKER,
                    "symbol": symbol,
                    "trade_id": trade.id,
                    "detail": (
                        "this system has an open trade the broker does not "
                        "report as a position."
                    ),
                }
            )

    if getattr(account, "agent_placeable", False):
        for symbol in sorted(broker_symbols - set(system_symbols)):
            mismatches.append(
                {
                    "kind": UNKNOWN_TO_SYSTEM,
                    "symbol": symbol,
                    "detail": (
                        "the broker reports a position in an agent-placeable "
                        "account with no matching open trade."
                    ),
                }
            )
    return mismatches
