"""Run the pre-registered roster against whatever `DATABASE_URL` names.

Spec N §11's definition of done asks for *"at least one `insufficient` and one
`clean_pit` answer on real data, both hand-verified"*. The warmed event library
that would supply it — `historical_events` from the FMP backfill, plus a real
price file and a real SEC ingest — lives only in the production database, which
the session that built this phase could not reach. So this script exists to be
run by someone who can:

    DATABASE_URL=postgresql+psycopg://... python -m scripts.cohort_smoke

It prints, per roster setup, the status, the evidence tier, the composition and
the reason — and it deliberately prints the `insufficient` answers in full,
because `insufficient` is the expected outcome at a floor of twenty distinct
event dates and it is the one an operator most needs to be able to read.

**What it was exercised against here: fixtures only.** Every number this script
has ever printed in CI came from `tests/cohortfixture.py`, which is synthetic.
Nothing in this repository has been run against a vendor price file or a real
SEC ingest.

It writes: each query is logged in `comparable_queries` and each answer cached
in `cohort_answers`, exactly as an MCP call would be. That is the point — the
smoke run should leave the same trail a real question does. `--dry-run` builds
the cohorts and prints their composition without storing anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Spec N roster against the configured database."
    )
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    parser.add_argument("--setup", action="append", default=None)
    parser.add_argument(
        "--depth", choices=("quick", "full"), default="quick",
        help="`quick` by default; `full` is what a citable answer needs.",
    )
    parser.add_argument("--universe", default=None)
    parser.add_argument("--snapshot", default=None)
    parser.add_argument("--benchmark", default=None)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Build and describe the cohorts without storing a query or answer.",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def _cohort_delisting_rate(build):
    if build is None:
        return None
    from comparables.outcomes import delisting_rate

    return delisting_rate(build.events)


def _describe_build(build) -> dict:
    reasons: dict[str, int] = {}
    for item in build.excluded:
        key = item.reason.split(":")[0]
        reasons[key] = reasons.get(key, 0) + 1
    return {
        "n_candidates": build.n_candidates,
        "n_events": len(build.events),
        "n_excluded": len(build.excluded),
        "excluded_by_reason": reasons,
        "evidence_cap": build.evidence_cap,
        "provenance_mix": build.provenance_mix,
        "universe_point_in_time": build.universe.point_in_time,
        "universe_sources": list(build.universe.sources),
        "universe_delisting_rate": build.universe_delisting_rate,
        "delisting_audit_recorded": build.snapshot.audit_recorded,
        "terminal_returns_synthesised": build.snapshot.terminal_returns_synthesised,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from config.settings import Settings
    from database.db import get_session, init_db

    from comparables import cohort as cohort_mod
    from comparables import query as query_mod
    from comparables import setups as roster
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
    slugs = args.setup or list(roster.slugs())

    print(
        f"cohort smoke: as_of={as_of} depth={args.depth} "
        f"universe={context.universe_slug} snapshot={context.price_snapshot_slug}"
    )
    print(
        "  reminder: `insufficient` is a valid, expected and frequently-correct "
        "answer at a floor of 20 distinct event dates. Read it as a refusal."
    )

    results: list[dict] = []
    statuses: list[str] = []
    tiers: list[str] = []

    with get_session() as session:
        for slug in slugs:
            entry = roster.get(slug)
            row: dict = {"setup": slug, "setup_hash": entry.setup_hash,
                         "family_slug": entry.family_slug}
            if not entry.available:
                row.update(status=roster.STATUS_PENDING_PLANE,
                           refusal_reason=entry.unavailable_reason)
                results.append(row)
                statuses.append(roster.STATUS_PENDING_PLANE)
                continue

            try:
                if args.dry_run:
                    build = cohort_mod.build_cohort(
                        session, entry, as_of=as_of, context=context
                    )
                    row.update(status="dry_run", build=_describe_build(build))
                else:
                    outcome = query_mod.answer_query(
                        session, context=context, as_of=as_of, depth=args.depth,
                        setup=slug, requester_label="cohort_smoke",
                        reps=(
                            settings.comparable_quick_bootstrap_reps
                            if args.depth == "quick"
                            else settings.comparable_full_bootstrap_reps
                        ),
                    )
                    answer = outcome.payload()["answer"]
                    row.update(
                        status=outcome.status,
                        evidence_tier=answer.get("evidence_tier"),
                        n_matured=answer.get("n_matured"),
                        n_distinct_dates=answer.get("n_distinct_dates"),
                        # `quick` carries no delisting rate (the §8 schema puts
                        # it on `full` only), so fall back to the build's — the
                        # composition is the thing an operator is reading for.
                        delisting_rate=answer.get(
                            "delisting_rate",
                            _cohort_delisting_rate(outcome.build),
                        ),
                        trials=answer.get("trials_against_this_pattern"),
                        refusal_reason=answer.get("refusal_reason"),
                        query_id=outcome.query_id,
                        citation_id=outcome.citation_id,
                        cached=outcome.cached,
                        provenance=outcome.provenance,
                        build=_describe_build(outcome.build) if outcome.build else None,
                    )
                    statuses.append(outcome.status)
                    if answer.get("evidence_tier"):
                        tiers.append(answer["evidence_tier"])
            except query_mod.QueryRefused as exc:
                row.update(status=exc.status, refusal_reason=exc.message)
                statuses.append(exc.status)
            except cohort_mod.CohortConstructionError as exc:
                row.update(status="unavailable", refusal_reason=str(exc))
                statuses.append("unavailable")
            results.append(row)

        if not args.dry_run:
            session.commit()

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        for row in results:
            print(f"\n{row['setup']}  [{row['setup_hash'][:12]}]  {row['status']}")
            print(f"  family: {row.get('family_slug')}")
            if row.get("evidence_tier"):
                print(
                    f"  tier: {row['evidence_tier']}   n_matured="
                    f"{row.get('n_matured')}   distinct_dates="
                    f"{row.get('n_distinct_dates')}   delisting_rate="
                    f"{row.get('delisting_rate')}"
                )
                print(f"  trials against this pattern: {row.get('trials')}")
                print(f"  query_id={row.get('query_id')}  "
                      f"citation_id={row.get('citation_id')}  cached={row.get('cached')}")
            if row.get("refusal_reason"):
                print(f"  REFUSED: {row['refusal_reason']}")
            if row.get("build"):
                print(f"  build: {json.dumps(row['build'], default=str)}")

    print("\n--- what this run demonstrated ---")
    print(f"  statuses seen: {sorted(set(statuses)) or ['none']}")
    print(f"  evidence tiers seen: {sorted(set(tiers)) or ['none']}")
    if "insufficient" not in statuses:
        print(
            "  no `insufficient` answer: Spec N §11 wants one on real data, and "
            "getting one is easy — tighten a threshold or move `--as-of` earlier."
        )
    if not ({"clean_pit", "vendor_pit"} & set(tiers)):
        print(
            "  no point-in-time tier: check that the universe has membership "
            "rows for the period and that the price snapshot carries a "
            "delisting-audit result (scripts/audit_delisting_returns.py)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
