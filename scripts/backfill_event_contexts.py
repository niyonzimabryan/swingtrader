"""Backfill point-in-time EventContext rows for stored HistoricalEvent records."""

from __future__ import annotations

import argparse

from config.settings import Settings
from data.event_outcomes import EventOutcomeEngine, HistoricalMarketCapUnavailable
from database.db import get_session, init_db
from database.models import CompanyProfile, HistoricalEvent


def run(limit: int | None = None) -> int:
    settings = Settings()
    init_db(settings.database_url)
    engine = EventOutcomeEngine(settings)
    count = 0
    with get_session() as session:
        query = session.query(HistoricalEvent).filter(~HistoricalEvent.context.has())
        if limit:
            query = query.limit(limit)
        for event in query.all():
            profile = session.query(CompanyProfile).filter_by(ticker=event.ticker).first()
            sector = profile.sector if profile else ""
            try:
                engine.compute_context(event, session=session, sector=sector)
            except HistoricalMarketCapUnavailable:
                raise
            count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    count = run(limit=args.limit)
    print(f"Backfilled PIT contexts for {count} events")


if __name__ == "__main__":
    main()
