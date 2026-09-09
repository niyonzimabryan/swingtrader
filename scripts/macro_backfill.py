#!/usr/bin/env python3
"""Backfill ALFRED vintages into ``source_observations``.

    python -m scripts.macro_backfill --regime-inputs
    python -m scripts.macro_backfill --series CPIAUCSL USREC --since 2015-01-01
    python -m scripts.macro_backfill --regime-inputs --fixtures tests/fixtures/macro

Every vintage of every named series, one row per (reference period, release),
so a restatement lands beside the original rather than replacing it.

The report prints **which inputs are vintage-clean** — Spec O section 4.2's
checkpoint requirement — by splitting the requested series into the
never-revised allowlist and the revised set. A ``regime_v1`` input appearing in
the revised column is a bug, and ``test_regime_v1_inputs_unrevised`` fails
before it can happen.

``--since`` is a **release-date** floor: it selects vintages published on or
after that date, which is the bitemporal reading. Writing requires
``PLANE_MACRO_VINTAGE_ENABLED=true``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config.settings import Settings  # noqa: E402
from data.macro_data import MacroDataAdapter  # noqa: E402
from database.db import get_session, init_db  # noqa: E402
from macro import regime_v1  # noqa: E402
from macro import series as series_registry  # noqa: E402
from macro.api import macro_state  # noqa: E402
from macro.vintage import (  # noqa: E402
    FixtureVintageSource,
    FredVintageSource,
    MacroCoverage,
    ingest_series,
)


def fixture_source(directory: Path) -> FixtureVintageSource:
    """Replay recorded ALFRED responses from ``directory``."""
    by_series = {}
    for path in sorted(Path(directory).glob("alfred_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        by_series[payload["series_id"]] = payload["rows"]
    if not by_series:
        raise SystemExit(f"no alfred_*.json files in {directory}")
    return FixtureVintageSource(by_series)


def format_report(coverage: MacroCoverage, state: dict | None) -> str:
    lines = [
        "Macro plane — ALFRED vintages",
        f"  observations written         {coverage.written}",
        f"  observations already present {coverage.duplicates}",
    ]
    if coverage.unavailable:
        lines.append(f"  series unavailable           {', '.join(coverage.unavailable)}")
    lines.append("")
    lines.append("  rows per series")
    for series_id, count in sorted(coverage.series.items()):
        spec = series_registry.spec_for(series_id)
        flag = "never-revised" if spec.never_revised else "REVISED"
        gate = ", vintage-only" if spec.vintage_only else ""
        lines.append(f"    {series_id:<12} {count:>6}   ({flag}{gate})")

    lines.append("")
    lines.append("  vintage-clean inputs (safe for regime_v1)")
    lines.append(f"    {', '.join(coverage.vintage_clean_inputs) or '(none)'}")
    lines.append("  revised inputs (regime_v2 only, once vintages are stored)")
    lines.append(f"    {', '.join(coverage.revised_inputs) or '(none)'}")
    lines.append("")
    lines.append(f"  regime_v1 inputs             {', '.join(regime_v1.INPUT_SERIES)}")
    ok, offenders = regime_v1.input_series_are_unrevised()
    lines.append(f"  all on the allowlist         {ok}{'' if ok else f' — {offenders}'}")

    if state is not None:
        lines.append("")
        lines.append(f"  macro_state as of {state['as_of']}")
        for series_id, value in sorted(state["series"].items()):
            lines.append(
                f"    {series_id:<12} {value['value']:>12.4f}   "
                f"(reference {value['reference_date']})"
            )
        if state["missing"]:
            lines.append(f"    absent at that date: {', '.join(state['missing'])}")
        if state.get("regime"):
            regime = state["regime"]
            lines.append(f"    regime: {regime['label']} ({regime['version']})")
            for reason in regime["reasons"]:
                lines.append(f"      because {reason}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--series", nargs="+")
    group.add_argument(
        "--regime-inputs",
        action="store_true",
        help="the never-revised series regime_v1 reads",
    )
    parser.add_argument("--since", help="YYYY-MM-DD release-date floor")
    parser.add_argument("--fixtures", help="replay recorded vintages from this directory")
    parser.add_argument("--as-of", help="also print macro_state as of this date")
    parser.add_argument("--report", help="write the coverage report as JSON to this path")
    parser.add_argument("--dry-run", action="store_true", help="fetch and report; write nothing")
    args = parser.parse_args(argv)

    series_ids = (
        list(regime_v1.INPUT_SERIES) if args.regime_inputs else list(args.series)
    )
    since = date.fromisoformat(args.since) if args.since else None
    as_of = date.fromisoformat(args.as_of) if args.as_of else None

    settings = Settings()
    init_db(settings.database_url)

    if args.fixtures:
        source = fixture_source(Path(args.fixtures))
    else:
        if not settings.fred_api_key:
            raise SystemExit(
                "FRED_API_KEY is unset. ALFRED uses the same key as FRED; set it "
                "in .env, or pass --fixtures to replay recorded vintages."
            )
        source = FredVintageSource(MacroDataAdapter(settings.fred_api_key))

    with get_session() as session:
        coverage = ingest_series(
            session, source, series_ids, settings=settings, since=since
        )
        state = (
            macro_state(session, as_of=as_of, series_ids=series_ids)
            if as_of
            else None
        )
        if args.dry_run:
            session.rollback()

    print(format_report(coverage, state))
    if args.report:
        payload = {"coverage": asdict(coverage), "macro_state": state}
        Path(args.report).write_text(
            json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
        )
        print(f"\n  report written to {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
