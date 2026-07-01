"""Runnable proof that Problem B works end-to-end — no API keys, no app needed.

    python -m model_evals.demo

Builds two synthetic corpora (a PARITY buy/skip swap and a DISCOVERY free-upgrade),
runs the real harness (score → bootstrap CI → cost → verdict), and prints the same
report format the app adapters emit. Deterministic (seeded), so it doubles as a
smoke test.
"""
from __future__ import annotations

import random

from .catalog import Catalog
from .report import render
from .run import evaluate
from .schema import ReplayRecord
from .scorers import agree, judge_delta
from .spec import TaskSpec


def _parity_corpus(n=180, agreement=0.93, seed=1):
    rng = random.Random(seed)
    records, cand = [], {}
    for i in range(n):
        iid = f"p{i}"
        inc = "buy" if rng.random() < 0.4 else "skip"
        records.append(ReplayRecord(
            task="demo.scoring", item_id=iid, input={"ticker": f"T{i}", "ctx": "x" * 400},
            incumbent_model="claude-opus-4-6", incumbent_output={"decision": inc},
            incumbent_usage={"in": 900, "out": 110},   # both sides exact → fair cost basis
        ))
        # candidate agrees `agreement` of the time
        c = inc if rng.random() < agreement else ("skip" if inc == "buy" else "buy")
        cand[iid] = {"decision": c, "_usage": {"in": 900, "out": 120}}
    return records, cand


def _discovery_corpus(n=160, cand_win=0.35, inc_win=0.15, seed=2):
    rng = random.Random(seed)
    records, cand = [], {}
    for i in range(n):
        iid = f"d{i}"
        records.append(ReplayRecord(
            task="demo.pick", item_id=iid, input={"items": list(range(28))},
            incumbent_model="claude-opus-4-6", incumbent_output={"top5": [0, 1, 2, 3, 4]},
        ))
        r = rng.random()
        if r < cand_win:          # candidate wins both orderings
            va, vb = "B", "A"
        elif r < cand_win + inc_win:
            va, vb = "A", "B"     # incumbent wins both
        else:
            va, vb = "A", "A"     # order-biased → judge_delta = 0
        cand[iid] = {"vote_ab": va, "vote_ba": vb, "_usage": {"in": 1500, "out": 300}}
    return records, cand


def main() -> int:
    catalog = Catalog.load()

    p_recs, p_cand = _parity_corpus()
    parity = TaskSpec(
        name="demo.scoring", mode="parity", primary_metric="buy_skip_agreement",
        threshold=0.90, incumbent_model="claude-opus-4-6", min_n=100,
        score_item=lambda r, c: agree(r.incumbent_output["decision"], c["decision"]),
    )
    r_parity = evaluate(p_recs, parity, "claude-sonnet-5", lambda r: p_cand[r.item_id], catalog)

    d_recs, d_cand = _discovery_corpus()
    disc = TaskSpec(
        name="demo.pick", mode="discovery", primary_metric="judge_pref_delta",
        threshold=0.05, incumbent_model="claude-opus-4-6", min_n=100,
        score_item=lambda r, c: judge_delta(c["vote_ab"], c["vote_ba"]),
    )
    r_disc = evaluate(d_recs, disc, "claude-opus-4-8", lambda r: d_cand[r.item_id], catalog)

    print(render([r_parity, r_disc], catalog, eval_commit="demo", date="2026-07-01"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
