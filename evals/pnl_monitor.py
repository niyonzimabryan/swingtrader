"""Realized-P&L monitor — the rollback signal (design §2), NOT a swap gate.

Joins closed trades → memos → tickers from the SQLite outcomes DB
(prod: /data/swing_trader.db). P&L can never *promote* a model (too slow, too
noisy, market-beta-dominated), but a P&L drop after a swap is a rollback trigger.
So the monitor's job is: split realized P&L before vs after a swap date and flag
a regression.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from statistics import mean

from evals import _bootstrap  # noqa: F401

_JOIN = """
SELECT tk.symbol, m.direction, m.composite_score, m.classification, m.opus_critique,
       t.pnl_pct, t.pnl_absolute, t.entry_date, t.exit_date, t.exit_reason
FROM trades t
JOIN memos   m  ON t.memo_id  = m.id
JOIN tickers tk ON m.ticker_id = tk.id
WHERE t.status = 'closed' AND t.pnl_pct IS NOT NULL
"""


@dataclass
class Outcome:
    symbol: str
    direction: str
    composite_score: float
    conviction: str
    pnl_pct: float
    entry_date: str
    exit_date: str
    exit_reason: str


def _conviction(opus_critique) -> str:
    if not opus_critique:
        return "unknown"
    try:
        d = json.loads(opus_critique) if isinstance(opus_critique, str) else opus_critique
        return (d.get("opus_evaluation") or d).get("conviction", "unknown")
    except Exception:
        return "unknown"


def realized_pnl(db_path: str) -> list[Outcome]:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(_JOIN).fetchall()
    finally:
        con.close()
    out = []
    for symbol, direction, score, _cls, crit, pnl_pct, _abs, ed, xd, reason in rows:
        out.append(Outcome(symbol, direction, score or 0.0, _conviction(crit),
                           float(pnl_pct), ed or "", xd or "", reason or ""))
    return out


def regression_check(outcomes: list[Outcome], swap_date: str) -> dict:
    """Compare mean realized P&L of trades ENTERED before vs on/after swap_date.
    Returns a dict the operator reads; `rollback_suggested` is advisory only."""
    before = [o.pnl_pct for o in outcomes if o.entry_date and o.entry_date < swap_date]
    after = [o.pnl_pct for o in outcomes if o.entry_date and o.entry_date >= swap_date]
    mb = mean(before) if before else None
    ma = mean(after) if after else None
    # Deliberately weak: only a large, well-sampled drop suggests rollback. P&L is
    # noisy — this is a tripwire, not a verdict.
    rollback = (mb is not None and ma is not None and len(after) >= 15 and ma < mb - 0.03)
    return {
        "n_before": len(before), "n_after": len(after),
        "mean_pnl_before": mb, "mean_pnl_after": ma,
        "swap_date": swap_date, "rollback_suggested": rollback,
        "note": "monitor only — P&L never gates a swap (design §2); needs ≥15 post-swap "
                "closed trades before the tripwire can fire.",
    }
