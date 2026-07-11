"""Backfill point-in-time EventContext rows for stored HistoricalEvent records."""

from __future__ import annotations

import argparse

from config.settings import Settings
from data.event_outcomes import EventOutcomeEngine, HistoricalMarketCapUnavailable
from database.db import get_session, init_db
from database.models import CompanyProfile, HistoricalEvent


def run(limit: int | None = None) -> tuple[int, int]:
    settings = Settings()
    init_db(settings.database_url)
    engine = EventOutcomeEngine(settings)
    count = 0
    skipped = 0
    with get_session() as session:
        query = session.query(HistoricalEvent).filter(~HistoricalEvent.context.has())
        if limit:
            query = query.limit(limit)
        for event in query.all():
            profile = session.query(CompanyProfile).filter_by(ticker=event.ticker).first()
            sector = profile.sector if profile else ""
            try:
                engine.compute_context(event, session=session, sector=sector)
            except HistoricalMarketCapUnavailable as exc:
                # PIT market cap is plan-gated on FMP (402): skip this event —
                # never fake with current-as-of values, never abort the run
                # (an early raise killed the whole chained runbook, BRY-300 #3).
                skipped += 1
                print(f"context skipped for {event.ticker} {event.event_date}: {exc}")
                continue
            count += 1
            session.commit()
    return count, skipped


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    count, skipped = run(limit=args.limit)
    print(f"Backfilled PIT contexts for {count} events ({skipped} skipped)")


if __name__ == "__main__":
    main()
