"""Seed a scratch DB with canned structured events for the J backtest artifact.

FMP is Railway-only, so this mirrors Spec H's offline approach: it feeds *canned*
FMP-shaped rows (earnings surprises + analyst-grade clusters) through the real
``StructuredEventLoader`` candidate/validation/dedupe path — the exact code the
prod backfill uses — so the scratch DB holds genuine HistoricalEvent rows without
any FMP key. Prices for the replay itself come from real yfinance bars.

The events are curated on real, liquid tickers with realistic quarterly cadence;
the artifact is labeled accordingly (canned events, real prices). This exists so
`python -m backtest.run_event_replay` can be exercised end-to-end locally.

    python -m scripts.seed_backtest_scratch --db sqlite:///backtest_scratch.db

Outcomes/context are intentionally skipped (the replay recomputes over bars).
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

from database.db import get_session, init_db
from scripts.bulk_load_structured_events import StructuredEventLoader

# Real, liquid tickers with long yfinance history.
TICKERS = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD", "JPM", "NFLX"]

# Realistic quarterly report windows, all strictly in the past with room for a
# full forward holding window before "now".
EARNINGS_DATES = [
    date(2024, 8, 1), date(2024, 10, 30), date(2025, 1, 29),
    date(2025, 4, 30), date(2025, 7, 30), date(2025, 10, 29), date(2026, 1, 28),
]

GRADE_DATES = [date(2024, 9, 10), date(2025, 3, 12), date(2025, 11, 5)]


def _deterministic_sign(ticker: str, d: date) -> int:
    """Reproducible pseudo-random beat(+1)/miss(-1) with no RNG (resume-safe)."""
    h = (sum(ord(c) for c in ticker) * 31 + d.month * 7 + d.day) % 5
    return 1 if h >= 2 else -1  # ~60% beats — a realistic, not-rigged mix


def canned_earnings_rows(ticker: str) -> list[dict]:
    rows = []
    for i, d in enumerate(EARNINGS_DATES):
        sign = _deterministic_sign(ticker, d)
        estimate = 1.50 + (i % 3) * 0.20
        # surprise magnitude 1-9% depending on the date, signed by beat/miss.
        pct = (2 + ((d.day + len(ticker)) % 8)) / 100.0
        actual = round(estimate * (1 + sign * pct), 2)
        rows.append({
            "symbol": ticker,
            "date": d.isoformat(),
            "epsActual": actual,
            "epsEstimated": estimate,
            "revenueActual": 1_000_000_000,
            "revenueEstimated": 990_000_000,
        })
    return rows


def canned_grade_rows(ticker: str) -> list[dict]:
    firms = ["JPM", "Morgan Stanley", "Goldman Sachs", "BofA"]
    rows = []
    for gi, gd in enumerate(GRADE_DATES):
        # Cluster of 2-3 same-direction actions within a few days.
        direction = "upgrade" if (gi + len(ticker)) % 2 == 0 else "downgrade"
        count = 2 + ((gd.day + gi) % 2)
        for k in range(count):
            rows.append({
                "symbol": ticker,
                "date": (gd + timedelta(days=k)).isoformat(),
                "action": direction,
                "gradingCompany": firms[(gi + k) % len(firms)],
                "newGrade": "Buy" if direction == "upgrade" else "Hold",
                "previousGrade": "Hold" if direction == "upgrade" else "Buy",
            })
    return rows


class _NoOutcome:
    """No-op outcome engine — keeps seeding fully offline (no yfinance)."""

    def compute_outcome(self, event, session=None):
        return None

    def compute_context(self, event, session=None, sector=""):
        return None


def _canned_fetch(ticker: str):
    earnings = canned_earnings_rows(ticker)
    grades = canned_grade_rows(ticker)

    def fetch(endpoint, params):
        if endpoint == "/earnings":
            return earnings
        if endpoint == "/grades":
            return grades
        return []

    return fetch


def seed(db_url: str, tickers=None, years: int = 3, today: date | None = None) -> dict:
    init_db(db_url)
    tickers = tickers or TICKERS
    today = today or date.today()
    total = {"tickers": 0, "events_stored": 0, "skipped_dupes": 0}
    with get_session() as session:
        for ticker in tickers:
            loader = StructuredEventLoader(
                settings=None, outcome_engine=_NoOutcome(), fetch=_canned_fetch(ticker)
            )
            summary = loader.load(
                session, [ticker], classes=("earnings", "upgrades"),
                years=years, today=today,
            )
            total["tickers"] += 1
            total["events_stored"] += summary["events_stored"]
            total["skipped_dupes"] += summary["skipped_dupes"]
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="sqlite:///backtest_scratch.db")
    parser.add_argument("--years", type=int, default=3)
    args = parser.parse_args()
    result = seed(args.db, years=args.years)
    print(f"Seeded {result['events_stored']} events across {result['tickers']} tickers "
          f"({result['skipped_dupes']} dupes skipped) into {args.db}")


if __name__ == "__main__":
    main()
