"""Spec N §4.2: does the price file carry delisting collapses, or stop at a quote?

    python -m scripts.audit_delisting_returns --source fixture
    python -m scripts.audit_delisting_returns --source sharadar --snapshot 2026-09
    python -m scripts.audit_delisting_returns --source sharadar --resolve

Pulls the final bars for the twenty known performance-related delistings in
`data/prices/delisting_audit_list.py`, resolves each to a vendor symbol first
(`data/prices/audit.py`'s resolver — a vendor can key the same company under a
different post-event symbol, and a case that cannot be resolved at all must
never be silently reported as `missing`), classifies each resolved series as
`collapse`, `stop`, `missing` or `too_short`, and writes the result into
`price_snapshots.delisting_audit_json` for the named snapshot.

**Run this before paying a vendor, and again on every price-file refresh.** A
file that mostly *stops* is not unusable — it means every terminal return has to
be synthesised at Shumway's -30% / -55% — but it is a different file from one
that carries the collapse, and the difference is invisible unless it is measured.

`--resolve` runs no classification and stores nothing. It prints, for every
case, how it resolved (or did not) and — for an unresolved case with
candidates — a ready-to-paste `vendor_symbols` line for
`data/prices/delisting_audit_list.py`. It needs a real vendor key
(`SHARADAR_API_KEY`); a cloud worker session normally has none, so this mode
is meant to be run by whoever holds the key, who then opens a follow-up PR
with the filled-in list (`docs/OWNER_SETUP.md` §4).

Exit codes: 0 the audit ran; 1 the audit ran and the file needs synthesised
terminal returns (`--strict` only); 2 refused to run (disabled, misconfigured,
or too few testable cases).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from data.prices import config as plane_config
from data.prices import store
from data.prices.audit import DELISTING_AUDIT_LIST, resolve_case, run_audit
from data.prices.base import PricePlaneError


def _print_resolve_report(plane) -> None:
    for case in DELISTING_AUDIT_LIST:
        resolution = resolve_case(plane, case)
        if resolution.resolution == "explicit":
            print(f"{case.ticker}: already mapped -> {resolution.resolved_symbol}")
        elif resolution.resolution == "security_master":
            print(f"{case.ticker}: resolves as-is -> {resolution.resolved_symbol}")
        elif resolution.resolution == "asis":
            print(
                f"{case.ticker}: {plane.source!r} cannot search by name; "
                f"used {resolution.resolved_symbol!r} as-is"
            )
        elif resolution.resolution == "name_match":
            print(f"{case.ticker} ({case.company}): candidate found -> {resolution.resolved_symbol}")
            print(f'    vendor_symbols={{"{plane.source}": "{resolution.resolved_symbol}"}},')
        else:  # unresolved
            if resolution.candidates:
                print(
                    f"{case.ticker} ({case.company}): {len(resolution.candidates)} candidate(s) "
                    f"found, none matched by name: {list(resolution.candidates)}"
                )
            else:
                print(f"{case.ticker} ({case.company}): no candidates found; needs manual research")
    print(
        "\nPaste the vendor_symbols= lines above into the matching case in "
        "data/prices/delisting_audit_list.py, add a resolution_note recording "
        "how and when it was verified, and open a follow-up PR."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default=None, help="fixture | sharadar")
    parser.add_argument("--snapshot", default=None, help="snapshot slug to record the audit on")
    parser.add_argument("--window", type=int, default=None, help="sessions in the terminal return")
    parser.add_argument("--threshold", type=float, default=None, help="e.g. -0.60")
    parser.add_argument(
        "--min-testable", type=int, default=None,
        help="refuse (exit 2) below this many testable cases",
    )
    parser.add_argument("--json", action="store_true", help="print the full audit blob")
    parser.add_argument("--no-store", action="store_true", help="classify only; write nothing")
    parser.add_argument(
        "--strict", action="store_true",
        help="exit 1 when the file needs synthesised terminal returns",
    )
    parser.add_argument(
        "--resolve", action="store_true",
        help="print candidate vendor symbols for every case instead of running the audit",
    )
    args = parser.parse_args(argv)

    settings = plane_config.get_settings()
    try:
        plane_config.require_enabled(settings)
        plane = plane_config.build_plane(args.source, settings)
    except PricePlaneError as exc:
        print(f"delisting audit refused: {exc}", file=sys.stderr)
        return 2

    if args.resolve:
        _print_resolve_report(plane)
        return 0

    configured_start = (getattr(settings, "price_plane_history_start", "") or "").strip()
    history_start = date.fromisoformat(configured_start) if configured_start else None

    try:
        result = run_audit(
            plane,
            window=args.window if args.window is not None else settings.delisting_audit_window_sessions,
            collapse_threshold=(
                args.threshold if args.threshold is not None
                else settings.delisting_audit_collapse_threshold
            ),
            min_testable=(
                args.min_testable if args.min_testable is not None
                else settings.delisting_audit_min_testable_cases
            ),
            history_start=history_start,
        )
    except PricePlaneError as exc:
        print(f"delisting audit refused: {exc}", file=sys.stderr)
        return 2

    if not args.no_store:
        from database.db import get_session, init_db

        init_db(settings.database_url)
        snapshot = args.snapshot or settings.price_plane_snapshot
        with get_session() as session:
            existing = store.get_snapshot(session, snapshot)
            coverage = existing.coverage_summary if existing else {}
            store.record_snapshot(session, snapshot, plane.source, coverage, result)
        print(f"audit recorded on snapshot {snapshot!r}")

    counts = result["counts"]
    print(
        f"{plane.source}: {counts['collapse']} collapse, {counts['stop']} stop, "
        f"{counts['missing']} missing, {counts['too_short']} too short, "
        f"{counts['unresolved']} unresolved, {counts['out_of_window']} out of window "
        f"({result['n_testable']} of {result['n_cases']} testable)"
    )
    if result["terminal_returns_must_be_synthesised"]:
        print(
            "This file mostly stops at the last quote: terminal returns must be "
            "synthesised (Shumway -30% NYSE/AMEX, -55% Nasdaq)."
        )
    if not result["sources_verified_against_primary_filing"]:
        print(
            "Note: the delisting facts have not been checked against their Form 25 "
            "filings in this repo. See data/prices/delisting_audit_list.py."
        )
    if counts["unresolved"]:
        print(
            f"{counts['unresolved']} case(s) have no vendor symbol at all; run "
            "--resolve with a live vendor key to find candidates."
        )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))

    if args.strict and result["terminal_returns_must_be_synthesised"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
