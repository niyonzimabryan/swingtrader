"""swingtrader eval CLI (report-only; touches nothing in prod).

    # scoring parity: Opus vs a candidate, over the shadow-logged corpus
    python -m evals.run_evals scoring --corpus evals/corpus/scoring.jsonl \
        --candidate claude-sonnet-5 --candidate-out evals/corpus/sonnet5.jsonl

    # P&L rollback monitor around a swap date (reads the SQLite outcomes DB)
    python -m evals.run_evals pnl --db /data/swing_trader.db --swap-date 2026-08-01

Scoring is UNDERPOWERED until the corpus clears N_min=150 (design §3) — expected
today (~19 historical scoring calls). The command still runs and reports directionally.
"""
from __future__ import annotations

import argparse
import json
import sys

from evals import _bootstrap  # noqa: F401
from evals import build_dataset, pnl_monitor, tasks
from model_evals.catalog import Catalog
from model_evals.report import render
from model_evals.run import evaluate


def _candidate_map(path):
    """JSONL of {"item_id": ..., "decision": "act"|"skip"} → lookup closure."""
    m = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                d = json.loads(line)
                m[d["item_id"]] = {"decision": d["decision"], "_usage": d.get("_usage")}
    return lambda r: m.get(r.item_id, {"decision": "skip"})


def _scoring(args) -> int:
    if args.from_traces:
        records = build_dataset.from_traces(args.from_traces)
        print(f"pulled {len(records)} scoring records from Langfuse traces "
              f"since {args.from_traces}", file=sys.stderr)
    else:
        records = build_dataset.from_jsonl(args.corpus)
    spec = tasks.scoring_spec()
    if args.candidate_out:
        cof = _candidate_map(args.candidate_out)
    else:
        print("no --candidate-out given: using incumbent-as-candidate sanity (agreement≈1.0).",
              file=sys.stderr)
        cof = lambda r: {"decision": r.incumbent_output.get("decision")}  # noqa: E731
    res = evaluate(records, spec, args.candidate, cof, Catalog.load())
    if res.verdict == "UNDERPOWERED":
        print(f"note: {res.n} scoring items < N_min={spec.min_n}; directional only.", file=sys.stderr)
    print(render([res], Catalog.load()))
    return 0


def _pnl(args) -> int:
    outcomes = pnl_monitor.realized_pnl(args.db)
    print(f"closed trades with P&L: {len(outcomes)}", file=sys.stderr)
    print(json.dumps(pnl_monitor.regression_check(outcomes, args.swap_date), indent=2, default=str))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="swingtrader model-eval CLI (report-only).")
    sub = p.add_subparsers(dest="mode", required=True)
    s = sub.add_parser("scoring")
    s.add_argument("--corpus", help="JSONL snapshot of shadow-logged records")
    s.add_argument("--from-traces", metavar="ISO_TS",
                   help="pull scoring corpus live from Langfuse traces since this timestamp "
                        "(e.g. 2026-04-01T00:00:00Z); needs LANGFUSE_* env")
    s.add_argument("--candidate", required=True)
    s.add_argument("--candidate-out", help="JSONL of candidate decisions; else sanity mode")
    s.set_defaults(func=_scoring)
    m = sub.add_parser("pnl")
    m.add_argument("--db", required=True)
    m.add_argument("--swap-date", required=True)
    m.set_defaults(func=_pnl)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
