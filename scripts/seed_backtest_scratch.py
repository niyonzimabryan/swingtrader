"""Seed a scratch DB with CANNED events + price bars for the Spec J backtester.

Offline / no-network / no-FMP-key demo harness. The FMP key is Railway-only, so
this mirrors Spec H's offline approach: it writes a handful of hand-authored
``HistoricalEvent`` rows and pre-populates the shared price cache
(``PatternProviderCache``) with synthetic OHLC bars, so
``python -m backtest.run_event_replay`` can run end-to-end against a scratch DB
with zero external calls.

The produced artifact is SYNTHETIC — it demonstrates the pipeline shape, not a
real edge. In the prod container (with a warmed library), run the CLI directly
against the real DB; see the PR runbook.

Usage:
    python -m scripts.seed_backtest_scratch --db sqlite:///scratch_backtest.db
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta

from data.event_outcomes import PriceHistoryCache
from database.db import get_session, init_db
from database.models import HistoricalEvent, ScoredCandidate

# (ticker, event_type, polarity, magnitude, source_type, outcome)
# outcome ∈ {"win", "chop", "loss"} controls the synthetic forward path.
CANNED_EVENTS = [
    ("AAPL", "earnings_beat_structured", "bullish", 8.2, "fmp_structured", "win"),
    ("MSFT", "earnings_beat_structured", "bullish", 5.1, "fmp_structured", "win"),
    ("NKE", "earnings_beat_structured", "bullish", 2.0, "fmp_structured", "chop"),
    ("INTC", "earnings_miss_structured", "bearish", 6.4, "fmp_structured", "win"),
    ("F", "earnings_miss_structured", "bearish", 3.1, "fmp_structured", "loss"),
    ("NVDA", "analyst_upgrade_cluster", "bullish", 3.0, "fmp_structured", "win"),
    ("AMD", "analyst_upgrade_cluster", "bullish", 4.0, "fmp_structured", "chop"),
    ("BA", "analyst_downgrade_cluster", "bearish", 2.0, "fmp_structured", "win"),
]

# Canned Spec I shadow-ledger rows (J4): (ticker, direction, cohort, final_score, outcome).
# suggested_stop/target_1 are the ~5%/10% levels; the ledger has a single target.
CANNED_SCORED = [
    ("SCA", "long", "memo", 0.82, "win"),
    ("SCB", "long", "exploration", 0.66, "chop"),
    ("SCC", "short", "exploration", 0.55, "win"),
    ("SCD", "long", "below", 0.35, "loss"),
]


def _forward(outcome: str, direction: str) -> list[tuple[float, float, float, float]]:
    """Synthetic forward OHLC (T+1 onward). Entry ~100.1/99.9; stop ~5%; T1/T2 at 10%/15%."""
    if direction == "long":
        return {
            "win": [(100, 105, 99, 104), (104, 111, 103, 110), (110, 116, 109, 115)],
            "chop": [(100, 103, 98, 101), (101, 104, 99, 102), (102, 105, 100, 103)],
            "loss": [(100, 101, 99, 100), (99, 100, 94, 95)],
        }[outcome]
    # short: falling paths hit the below-entry targets; rising path hits the stop
    return {
        "win": [(100, 101, 95, 96), (96, 97, 89, 90), (90, 91, 84, 85)],
        "chop": [(100, 102, 97, 99), (99, 101, 96, 98), (98, 100, 95, 97)],
        "loss": [(100, 106, 99, 105), (105, 107, 104, 106)],
    }[outcome]


def _bars_payload(signal_date: date, outcome: str, direction: str) -> list[dict]:
    rows = []
    start = signal_date - timedelta(days=20)
    for i in range(20):  # flat warmup incl. the signal bar
        d = start + timedelta(days=i)
        rows.append({"date": d.isoformat(), "open": 100, "high": 100.5, "low": 99.5, "close": 100, "volume": 1000})
    for j, (o, h, l, c) in enumerate(_forward(outcome, direction), start=1):
        d = signal_date + timedelta(days=j)
        rows.append({"date": d.isoformat(), "open": o, "high": h, "low": l, "close": c, "volume": 1000})
    return rows


def seed(database_url: str, signal_date: date) -> dict:
    init_db(database_url)
    cache = PriceHistoryCache(None)  # source defaults to "fmp"; we only WRITE the cache
    max_hold = 20
    counts = {"events": 0, "bars_cached": 0}
    with get_session() as session:
        for ticker, event_type, polarity, magnitude, source_type, outcome in CANNED_EVENTS:
            direction = "long" if polarity == "bullish" else "short"
            session.add(HistoricalEvent(
                ticker=ticker, event_type=event_type, event_date=signal_date, polarity=polarity,
                magnitude=magnitude, source_type=source_type, provider="canned_offline",
                dedupe_key=f"canned:{ticker}:{event_type}:{signal_date}",
                headline=f"[CANNED] {ticker} {event_type}", summary="synthetic offline seed",
            ))
            counts["events"] += 1
            # Pre-populate the exact cache key the replay's _fetch_bars will request.
            start = signal_date - timedelta(days=90)
            end = signal_date + timedelta(days=max_hold * 2 + 21)
            key = f"price:{cache.source}:{ticker}:{start.isoformat()}:{end.isoformat()}"
            payload = _bars_payload(signal_date, outcome, direction)
            cache._write_cache(key, "price_history", ticker, {"start": str(start), "end": str(end)}, payload, session)
            counts["bars_cached"] += 1

        # Spec I shadow-ledger rows (J4). Their fetch window starts scored_at-15.
        counts["scored_candidates"] = 0
        for ticker, direction, cohort, score, outcome in CANNED_SCORED:
            stop = 95.1 if direction == "long" else 105.0
            target_1 = 110.1 if direction == "long" else 90.0
            session.add(ScoredCandidate(
                ticker=ticker, scored_at=datetime(signal_date.year, signal_date.month, signal_date.day),
                direction=direction, suggested_stop=stop, target_1=target_1,
                cohort=cohort, final_score=score, source="canned_offline",
            ))
            start = signal_date - timedelta(days=15)
            end = signal_date + timedelta(days=max_hold * 2 + 21)
            key = f"price:{cache.source}:{ticker}:{start.isoformat()}:{end.isoformat()}"
            payload = _bars_payload(signal_date, outcome, direction)
            cache._write_cache(key, "price_history", ticker, {"start": str(start), "end": str(end)}, payload, session)
            counts["scored_candidates"] += 1
    return counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Seed a scratch DB for the Spec J backtester (offline/canned)")
    parser.add_argument("--db", default="sqlite:///scratch_backtest.db", help="DATABASE_URL for the scratch DB")
    parser.add_argument("--signal-date", default="2024-06-03", help="signal date for all canned events (ISO)")
    args = parser.parse_args(argv)
    counts = seed(args.db, date.fromisoformat(args.signal_date))
    print(json.dumps({"db": args.db, "signal_date": args.signal_date, **counts}, indent=2))
    print("\nNow run:  python -m backtest.run_event_replay --classes upgrades,earnings --sweep --json artifact.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
