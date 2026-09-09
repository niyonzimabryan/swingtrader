"""Spec N §4.2: does the price file carry delisting collapses, or stop at a quote?

    python -m scripts.audit_delisting_returns --source fixture
    python -m scripts.audit_delisting_returns --source sharadar --snapshot 2026-09

Pulls the final bars for the twenty known performance-related delistings in
`data/prices/delisting_audit_list.py`, classifies each series as `collapse`,
`stop`, `missing` or `too_short`, and writes the result into
`price_snapshots.delisting_audit_json` for the named snapshot.

**Run this before paying a vendor, and again on every price-file refresh.** A
file that mostly *stops* is not unusable — it means every terminal return has to
be synthesised at Shumway's -30% / -55% — but it is a different file from one
that carries the collapse, and the difference is invisible unless it is measured.

Exit codes: 0 the audit ran; 1 the audit ran and the file needs synthesised
terminal returns (`--strict` only); 2 refused to run.
"""

from __future__ import annotations

import argparse
import json
import sys

from data.prices import config as plane_config
from data.prices import store
from data.prices.audit import run_audit
from data.prices.base import PricePlaneError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default=None, help="fixture | sharadar")
    parser.add_argument("--snapshot", default=None, help="snapshot slug to record the audit on")
    parser.add_argument("--window", type=int, default=None, help="sessions in the terminal return")
    parser.add_argument("--threshold", type=float, default=None, help="e.g. -0.60")
    parser.add_argument("--json", action="store_true", help="print the full audit blob")
    parser.add_argument("--no-store", action="store_true", help="classify only; write nothing")
    parser.add_argument(
        "--strict", action="store_true",
        help="exit 1 when the file needs synthesised terminal returns",
    )
    args = parser.parse_args(argv)

    settings = plane_config.get_settings()
    try:
        plane_config.require_enabled(settings)
        plane = plane_config.build_plane(args.source, settings)
    except PricePlaneError as exc:
        print(f"delisting audit refused: {exc}", file=sys.stderr)
        return 2

    result = run_audit(
        plane,
        window=args.window if args.window is not None else settings.delisting_audit_window_sessions,
        collapse_threshold=(
            args.threshold if args.threshold is not None
            else settings.delisting_audit_collapse_threshold
        ),
    )

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
        f"{counts['missing']} missing, {counts['too_short']} too short "
        f"(of {result['n_cases']})"
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
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))

    if args.strict and result["terminal_returns_must_be_synthesised"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
