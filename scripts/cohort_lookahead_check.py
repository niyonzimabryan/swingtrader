"""Run the Spec N §10 lookahead harness against whatever `DATABASE_URL` names.

The same function `tests/test_comparables_lookahead.py` calls
(`comparables.lookahead.check_cohort`), pointed at a real database instead of a
fixture, so a green CI run and a green owner run mean the same thing.

    python -m scripts.cohort_lookahead_check --as-of 2026-09-01
    python -m scripts.cohort_lookahead_check --setup gap_and_go_v1 --max-cutoffs 20

Every deletion the harness performs happens inside a savepoint that is always
rolled back, and the script opens no transaction of its own that could commit
one. It is still a read-only operation on a database you care about only
because that rollback holds, so it prints what it is doing.

Exit status is 1 when any cohort's answer moved — which is the point. A leak is
a defect with a failing check attached, not a warning to read past.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Spec N §10 lookahead harness over stored cohorts."
    )
    parser.add_argument(
        "--as-of", type=date.fromisoformat, default=None,
        help="Query date (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--setup", action="append", default=None,
        help="Roster slug; repeatable. Defaults to every available roster entry.",
    )
    parser.add_argument(
        "--max-cutoffs", type=int, default=None,
        help=(
            "Check this many event cutoffs instead of every one. The first and "
            "the last are always kept. Omit for the full check, which is what "
            "CI runs."
        ),
    )
    parser.add_argument("--universe", default=None, help="Override the universe slug.")
    parser.add_argument("--snapshot", default=None, help="Override the price snapshot.")
    parser.add_argument("--benchmark", default=None, help="Override the benchmark uid.")
    parser.add_argument(
        "--json", action="store_true", help="Print the reports as JSON."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from config.settings import Settings
    from database.db import get_session, init_db

    from comparables import lookahead as lookahead_mod
    from comparables.cohort import CohortContext
    from workspace.tools import parse_cik_map

    settings = Settings()
    init_db(settings.database_url)

    context = CohortContext(
        universe_slug=args.universe or settings.comparable_universe_slug,
        price_snapshot_slug=args.snapshot or settings.comparable_price_snapshot,
        benchmark_security_uid=(
            args.benchmark or settings.comparable_benchmark_security_uid
        ),
        cik_by_ticker=parse_cik_map(settings.comparable_cik_map),
    )
    as_of = args.as_of or date.today()

    print(
        f"lookahead check: as_of={as_of} universe={context.universe_slug} "
        f"snapshot={context.price_snapshot_slug}\n"
        f"  every deletion runs inside a savepoint that is always rolled back"
    )

    with get_session() as session:
        reports = lookahead_mod.check_roster(
            session, as_of=as_of, context=context,
            slugs=args.setup, max_cutoffs=args.max_cutoffs,
        )

    if args.json:
        print(json.dumps([
            {
                "setup": r.setup_slug,
                "setup_hash": r.setup_hash,
                "as_of": r.as_of.isoformat(),
                "n_events": r.n_events,
                "cutoffs_checked": r.n_cutoffs_checked,
                "cutoffs_total": r.n_cutoffs_total,
                "answer_identical": r.answer_identical,
                "clean": r.clean,
                "findings": [
                    {"cutoff": f.cutoff, "kind": f.kind, "summary": f.summary()}
                    for f in r.findings
                ],
            }
            for r in reports
        ], indent=2))

    failed = False
    for report in reports:
        state = "clean" if report.clean else "LEAK"
        print(
            f"  {report.setup_slug:<28} {state:<6} "
            f"n={report.n_events:<5} cutoffs={report.n_cutoffs_checked}"
            f"/{report.n_cutoffs_total} answer_identical={report.answer_identical}"
        )
        for finding in report.findings:
            failed = True
            print(f"      {finding.summary()}")
        if not report.clean:
            failed = True
        if report.n_events == 0:
            print(
                "      note: this cohort is empty, so the check proves nothing "
                "about it — green here means there was nothing to look at"
            )

    if not reports:
        print("  no available roster setups to check")
    print("FAILED: a cohort moved" if failed else "OK: no cohort moved")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
