#!/usr/bin/env python3
"""Record a passed Robinhood protective-exit probe — from an observation.

**This script places nothing.** It cannot: the only thing it ever asks the
adapter for is ``read_open_orders``, and there is no branch anywhere in it that
submits, reviews, modifies or cancels an order —
``tests/test_robinhood_stop_probe_gate.py`` asserts both, at the source level
and against a broker that raises on anything but a read. No agent places an
order (AGENTS.md non-negotiable 1), and
``docs/EXECUTION_LIFECYCLE.md`` §6 is explicit that the probe itself is an owner
action performed by hand. What this script does is **verify and record what the
owner already did**.

The probe, in one sentence: place a ``gtc`` ``stop_market`` in the live account
by hand, leave it overnight, and confirm it is still visible in
``get_equity_orders`` the next session. That is the one empirical fact behind
``ROBINHOOD_CAPABILITIES.can_place_standalone_gtc_stop``, which the whole Phase 6
protective-exit contract rests on and which has never been checked against the
live server. Run §6's runbook first; this is step 8.

Usage::

    python -m scripts.robinhood_stop_probe --status
        What is on record for the configured account, and what the live gate
        currently says. Touches no broker.

    python -m scripts.robinhood_stop_probe --list [--symbol F]
        Read the account's orders and print every candidate: the ``gtc``
        ``stop_market`` rows the probe could be recorded from.

    python -m scripts.robinhood_stop_probe --record --order-id <id> \\
        [--note "survived overnight, re-checked 2026-09-16"]
        Re-read that order from the broker, refuse unless it really is a ``gtc``
        ``stop_market`` in this account, and write the row.

Exit status is 0 when the thing asked for succeeded, 1 otherwise. ``--record``
refuses — loudly, changing nothing — if the order cannot be found, or is not a
``gtc`` ``stop_market``. A row that does not stand for an observation would be
worse than no row at all: absence is honest, a false row is permission.
"""

from __future__ import annotations

import argparse
import sys

from config.settings import Settings
from database.db import get_session
from execution.brokers.robinhood import RobinhoodMCPBroker
from portfolio import stop_probe
from utils.timeutils import utcnow_naive

#: What the probe has to have been, for the record to mean anything. Matched
#: leniently on substring because adapters spell these a few ways
#: (``stop_market`` / ``stop`` / ``STOP_MARKET``), and strictly on both halves
#: because a ``gfd`` stop proves nothing about overnight survival — which is the
#: unstated-GTC-horizon question the probe exists to answer.
STOP_TYPES = ("stop_market", "stop")
GTC = "gtc"


class ProbeRefused(Exception):
    """The observation does not support a record. Nothing is written."""


def _is_stop_market(order) -> bool:
    kind = (getattr(order, "order_type", "") or "").strip().lower().replace("-", "_")
    return any(token in kind for token in STOP_TYPES)


def _is_gtc(order) -> bool:
    return (getattr(order, "time_in_force", "") or "").strip().lower() == GTC


def candidates(broker, *, symbol: str | None = None) -> list:
    """Every order at the broker that could be a recorded probe.

    Read-only. ``read_open_orders`` is the same normalization the protection
    check uses, so a row this script accepts is a row Phase 6 would have counted
    as protection.
    """
    orders = broker.read_open_orders(symbol=symbol)
    return [o for o in orders if _is_stop_market(o) and _is_gtc(o)]


def find_order(broker, order_id: str, *, symbol: str | None = None):
    """The one order with this id, as the broker currently reports it."""
    wanted = (order_id or "").strip()
    if not wanted:
        raise ProbeRefused("an --order-id is required: the record is about one order.")
    for order in broker.read_open_orders(symbol=symbol):
        if (getattr(order, "order_id", "") or "").strip() == wanted:
            return order
    raise ProbeRefused(
        f"order {wanted!r} was not found in this account's orders. The probe "
        "record is written from what the broker reports now, not from what the "
        "owner remembers placing — if the order is gone, the probe has not "
        "passed and there is nothing to record."
    )


def verify(order) -> None:
    """Refuse anything that is not the fact the probe is supposed to establish."""
    problems = []
    if not _is_stop_market(order):
        problems.append(
            f"its type is {getattr(order, 'order_type', '') or '(none)'!r}, not a "
            "stop_market"
        )
    if not _is_gtc(order):
        problems.append(
            f"its time in force is {getattr(order, 'time_in_force', '') or '(none)'!r}, "
            "not gtc — and a gfd stop proves nothing about overnight survival, "
            "which is the whole question"
        )
    if problems:
        raise ProbeRefused(
            "this order does not establish the probe: " + "; ".join(problems) + ". "
            "Nothing was written."
        )


def record_from(session, settings, order, *, recorded_by: str, note: str):
    """Write the row for an order that has already passed :func:`verify`."""
    verify(order)
    account_number = str(getattr(settings, "robinhood_account_number", "") or "").strip()
    if not account_number:
        raise ProbeRefused(
            "ROBINHOOD_ACCOUNT_NUMBER is not set, so there is no account to key "
            "the record to. A probe is evidence about one account."
        )
    return stop_probe.record(
        session,
        broker=stop_probe.ROBINHOOD,
        account_number=account_number,
        observed_order_id=getattr(order, "order_id", "") or "",
        observed_at=utcnow_naive(),
        observed_symbol=getattr(order, "symbol", "") or "",
        observed_stop_price=getattr(order, "stop_price", None),
        recorded_by=recorded_by,
        note=note,
    )


def _describe(order) -> str:
    return (
        f"  order_id={getattr(order, 'order_id', '') or '?'} "
        f"symbol={getattr(order, 'symbol', '') or '?'} "
        f"type={getattr(order, 'order_type', '') or '?'} "
        f"tif={getattr(order, 'time_in_force', '') or '?'} "
        f"stop={getattr(order, 'stop_price', None)} "
        f"status={getattr(order, 'status', '') or '?'}"
    )


def _status(settings) -> int:
    account_number = str(getattr(settings, "robinhood_account_number", "") or "").strip()
    with get_session() as session:
        found = (
            stop_probe.find(
                session, broker=stop_probe.ROBINHOOD, account_number=account_number
            )
            if account_number
            else None
        )
        # `finding`, not `refusal`: --status reports what is on record, which is
        # the same answer in both modes. `refusal` would hide the finding behind
        # the advisory flag and log a warning for a question nobody is placing on.
        finding = stop_probe.finding(settings, session)
    enforced = stop_probe.required(settings)

    print(f"broker_primary: {getattr(settings, 'broker_primary', '') or '(unset)'}")
    print(f"account:        {stop_probe.mask(account_number) if account_number else '(unset)'}")
    print(f"enforced:       {'yes' if enforced else 'no (ROBINHOOD_STOP_PROBE_REQUIRED is not true)'}")
    if found is None:
        print("probe record:   NONE — absence means not probed, never permission.")
    else:
        for key, value in found.as_dict().items():
            print(f"  {key}: {value}")
    if finding is None:
        print("live gate:      the probe condition is satisfied for this account.")
    elif enforced:
        print(f"live gate:      REFUSES [{finding[0]}]")
        print(f"                {finding[1]}")
    else:
        print(f"live gate:      WARNS ONLY [{finding[0]}] — a live Robinhood entry")
        print("                would proceed unprobed. Set")
        print("                ROBINHOOD_STOP_PROBE_REQUIRED=true to refuse it.")
        print(f"                {finding[1]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Record a Robinhood protective-exit probe from an observed gtc "
            "stop_market. Places nothing."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--status", action="store_true", help="show what is on record")
    mode.add_argument("--list", action="store_true", help="list candidate stop orders")
    mode.add_argument("--record", action="store_true", help="write the record")
    parser.add_argument("--order-id", default="", help="the observed order (--record)")
    parser.add_argument("--symbol", default="", help="narrow the read to one symbol")
    parser.add_argument("--note", default="", help="free text stored with the record")
    parser.add_argument(
        "--recorded-by", default="owner", help="who ran the probe by hand"
    )
    args = parser.parse_args(argv)

    settings = Settings()
    if args.status:
        return _status(settings)

    symbol = (args.symbol or "").strip().upper() or None
    broker = RobinhoodMCPBroker(settings)

    if args.list:
        found = candidates(broker, symbol=symbol)
        if not found:
            print(
                "no gtc stop_market order is visible in this account. Either the "
                "probe has not been placed by hand yet, or it did not survive — "
                "and the second of those is the answer the probe exists to find."
            )
            return 1
        print(f"{len(found)} candidate order(s):")
        for order in found:
            print(_describe(order))
        print("\nRecord one with: --record --order-id <id>")
        return 0

    try:
        order = find_order(broker, args.order_id, symbol=symbol)
        with get_session() as session:
            written = record_from(
                session,
                settings,
                order,
                recorded_by=args.recorded_by,
                note=args.note,
            )
            session.commit()
    except ProbeRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    print("recorded:")
    for key, value in written.as_dict().items():
        print(f"  {key}: {value}")
    print(
        "\nThe probe condition is now satisfied for this account: it no longer "
        "refuses under ROBINHOOD_STOP_PROBE_REQUIRED=true, and no longer warns "
        "under the default. Every other gate — PHASE6_EXECUTION_ENABLED, "
        "ALLOW_LIVE_TRADING, EXECUTION_MODE, the kill switch, the risk "
        "re-evaluation — is unchanged."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
