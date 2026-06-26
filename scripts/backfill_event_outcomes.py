"""Backfill EventOutcome rows for stored HistoricalEvent records."""

from __future__ import annotations

import argparse

from config.settings import Settings
from data.event_outcomes import EventOutcomeEngine
from database.db import get_session, init_db
from database.models import HistoricalEvent


def run(limit: int | None = None) -> int:
    settings = Settings()
    init_db(settings.database_url)
    engine = EventOutcomeEngine(settings)
    count = 0
    with get_session() as session:
        query = session.query(HistoricalEvent).filter(~HistoricalEvent.outcome.has())
        if limit:
            query = query.limit(limit)
        for event in query.all():
            engine.compute_outcome(event, session=session)
            count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    count = run(limit=args.limit)
    print(f"Backfilled outcomes for {count} events")


if __name__ == "__main__":
    main()
