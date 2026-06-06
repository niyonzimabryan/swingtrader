"""Small-sample-aware signal attribution analysis."""

from __future__ import annotations

import json
from collections import defaultdict

from database.db import get_session
from database.models import Memo, Trade
from utils.logger import get_logger

log = get_logger("attribution")


def get_signal_attribution() -> dict:
    """Analyze which signals contributed to winners vs losers."""
    with get_session() as session:
        closed = session.query(Trade).filter(Trade.status == "closed").all()
        memos = session.query(Memo).all()

        memo_counts = {
            "total": len(memos),
            "approved": sum(1 for m in memos if m.status == "approved"),
            "rejected": sum(1 for m in memos if m.status == "rejected"),
            "watchlisted": sum(1 for m in memos if m.status == "watchlisted"),
            "dismissed": sum(1 for m in memos if m.status == "dismissed"),
        }

        if not closed:
            return {
                "status": "no_closed_trades",
                "sample_warning": "No closed trades yet. Approval conversion is still tracked.",
                "closed_trade_count": 0,
                "memo_counts": memo_counts,
                "overall": {},
                "groups": {},
                "agents": {},
            }

        enriched = []
        for trade in closed:
            scores = _json_dict(trade.signal_scores)
            memo = trade.memo
            enriched.append(
                {
                    "trade": trade,
                    "symbol": trade.ticker.symbol if trade.ticker else "?",
                    "pnl": trade.pnl_absolute or 0.0,
                    "r": _r_multiple(trade),
                    "setup_type": trade.setup_type or "unknown",
                    "regime": trade.regime_at_entry or "unknown",
                    "direction": trade.direction or "unknown",
                    "score_bucket": _score_bucket(memo.composite_score if memo else None),
                    "scores": scores,
                }
            )

        return {
            "status": "ok",
            "sample_warning": _sample_warning(len(enriched)),
            "closed_trade_count": len(enriched),
            "memo_counts": memo_counts,
            "overall": _summarize(enriched),
            "groups": {
                "setup_type": _group(enriched, "setup_type"),
                "regime": _group(enriched, "regime"),
                "direction": _group(enriched, "direction"),
                "score_bucket": _group(enriched, "score_bucket"),
            },
            "agents": _agent_correlations(enriched),
        }


def _summarize(rows: list[dict]) -> dict:
    wins = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] <= 0]
    total_pnl = sum(r["pnl"] for r in rows)
    avg_r = sum(r["r"] for r in rows) / len(rows) if rows else 0.0
    return {
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(rows) * 100, 1) if rows else 0.0,
        "total_pnl": round(total_pnl, 2),
        "avg_r": round(avg_r, 2),
    }


def _group(rows: list[dict], key: str) -> dict:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        buckets[str(row.get(key) or "unknown")].append(row)
    return {name: _summarize(items) for name, items in sorted(buckets.items())}


def _agent_correlations(rows: list[dict]) -> dict:
    agent_values: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        outcome = row["r"]
        for agent, score in row["scores"].items():
            value = _score_value(score)
            if value is not None:
                agent_values[agent].append((value, outcome))
    out = {}
    for agent, pairs in agent_values.items():
        scores = [p[0] for p in pairs]
        outcomes = [p[1] for p in pairs]
        out[agent] = {
            "n": len(pairs),
            "avg_score": round(sum(scores) / len(scores), 3) if scores else 0.0,
            "avg_r": round(sum(outcomes) / len(outcomes), 2) if outcomes else 0.0,
            "correlation": round(_pearson(scores, outcomes), 3) if len(pairs) >= 3 else None,
        }
    return out


def _r_multiple(trade: Trade) -> float:
    if not trade.entry_price or not trade.stop_loss or not trade.shares:
        return 0.0
    risk_per_share = abs(trade.entry_price - trade.stop_loss)
    risk = risk_per_share * trade.shares
    if risk <= 0:
        return 0.0
    return round((trade.pnl_absolute or 0.0) / risk, 3)


def _score_bucket(score: float | None) -> str:
    if score is None:
        return "unknown"
    if score >= 0.75:
        return "0.75+"
    if score >= 0.60:
        return "0.60-0.74"
    if score >= 0.45:
        return "0.45-0.59"
    return "<0.45"


def _score_value(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("score", "final_score", "composite_score"):
            if key in value:
                try:
                    return float(value[key])
                except (TypeError, ValueError):
                    return None
    return None


def _pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x = sum((x - mean_x) ** 2 for x in xs) ** 0.5
    den_y = sum((y - mean_y) ** 2 for y in ys) ** 0.5
    if den_x == 0 or den_y == 0:
        return 0.0
    return num / (den_x * den_y)


def _json_dict(raw: str | None) -> dict:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _sample_warning(n: int) -> str:
    if n < 10:
        return "Very small sample. Treat attribution as directional only."
    if n < 30:
        return "Small sample. Avoid weight changes until at least 30 closed trades."
    return ""
