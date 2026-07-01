"""swingtrader adapter tests. Run from repo root: `python -m pytest evals`.

Uses a fabricated in-memory-style SQLite (prod data lives on the Railway volume,
unreachable here) and synthetic scoring corpora."""
import json
import os
import sqlite3

import pytest

from evals import _bootstrap  # noqa: F401
from evals import pnl_monitor, shadow_log, tasks
from model_evals import decide as D
from model_evals.catalog import Catalog
from model_evals.run import evaluate
from model_evals.schema import ReplayRecord


# ── P&L monitor ──────────────────────────────────────────────────────────────
def _fab_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE tickers(id INTEGER PRIMARY KEY, symbol TEXT, ticker_id INT);
        CREATE TABLE memos(id INTEGER PRIMARY KEY, ticker_id INT, direction TEXT,
                           composite_score REAL, classification TEXT, opus_critique TEXT);
        CREATE TABLE trades(id INTEGER PRIMARY KEY, memo_id INT, status TEXT,
                            pnl_pct REAL, pnl_absolute REAL, entry_date TEXT, exit_date TEXT,
                            exit_reason TEXT);
    """)
    con.execute("INSERT INTO tickers VALUES (1,'AAPL',1)")
    con.execute("INSERT INTO memos VALUES (1,1,'long',0.8,'high_conviction',?)",
                (json.dumps({"opus_evaluation": {"conviction": "high"}}),))
    # 20 closed trades before swap (+0.05 avg), 20 after (−0.05 avg)
    tid = 1
    for i in range(20):
        con.execute("INSERT INTO trades VALUES (?,1,'closed',?,100,?,?,'target_1')",
                    (tid, 0.05, "2026-05-01", "2026-05-10")); tid += 1
    for i in range(20):
        con.execute("INSERT INTO trades VALUES (?,1,'closed',?,-100,?,?,'stop_loss')",
                    (tid, -0.05, "2026-09-01", "2026-09-10")); tid += 1
    con.commit(); con.close()


def test_realized_pnl_and_regression(tmp_path):
    db = os.path.join(str(tmp_path), "t.db")
    _fab_db(db)
    outcomes = pnl_monitor.realized_pnl(db)
    assert len(outcomes) == 40
    assert outcomes[0].conviction == "high"           # parsed from opus_critique JSON
    chk = pnl_monitor.regression_check(outcomes, swap_date="2026-08-01")
    assert chk["n_before"] == 20 and chk["n_after"] == 20
    assert chk["rollback_suggested"] is True           # −0.05 after vs +0.05 before, well-sampled


# ── shadow-log decision proxy ────────────────────────────────────────────────
def test_decision_of():
    assert shadow_log.decision_of({"opus_evaluation": {"recommendation": "proceed"}}) == "act"
    assert shadow_log.decision_of({"opus_evaluation": {"recommendation": "pass"}}) == "skip"
    assert shadow_log.decision_of({"opus_evaluation": {"recommendation": "watchlist"}}) == "skip"


# ── scoring parity ───────────────────────────────────────────────────────────
def _scoring_corpus(n, agree_frac):
    recs, cand = [], {}
    for i in range(n):
        inc = "act" if i % 3 == 0 else "skip"
        iid = f"TCK{i}:2026-06-01"
        recs.append(ReplayRecord(task="swingtrader.scoring", item_id=iid,
                                 input={"ticker": f"TCK{i}", "ctx": "x" * 200},
                                 incumbent_model="claude-opus-4-6",
                                 incumbent_output={"decision": inc},
                                 incumbent_usage={"in": 1200, "out": 200}))
        c = inc if (i / n) < agree_frac else ("skip" if inc == "act" else "act")
        cand[iid] = {"decision": c, "_usage": {"in": 1200, "out": 210}}
    return recs, cand


def test_scoring_underpowered_below_floor():
    recs, cand = _scoring_corpus(40, agree_frac=1.0)     # n < 150
    spec = tasks.scoring_spec(incumbent_model="claude-opus-4-6")
    res = evaluate(recs, spec, "claude-sonnet-5", lambda r: cand[r.item_id], Catalog.load())
    assert res.verdict == D.UNDERPOWERED


def test_scoring_promotes_cheaper_at_parity():
    recs, cand = _scoring_corpus(180, agree_frac=0.95)   # n ≥ 150, agreement high
    spec = tasks.scoring_spec(incumbent_model="claude-opus-4-6")
    res = evaluate(recs, spec, "claude-sonnet-5", lambda r: cand[r.item_id], Catalog.load())
    assert res.n == 180
    assert res.verdict == D.PROMOTE
    assert res.cost_candidate < res.cost_incumbent       # sonnet-5 < opus
