#!/usr/bin/env python3
"""Report how ``ensure_schema`` would classify a database. Read-only.

Run this against a *copy* of a database before deploying a change that touches
the schema — in particular against a copy of the production SQLite file before
the first deploy that runs Alembic, to confirm it classifies as ``legacy`` (it
will be stamped and upgraded) rather than ``unknown`` (startup fails closed).

    python -m scripts.schema_status                       # $DATABASE_URL
    python -m scripts.schema_status sqlite:///copy.db
    python -m scripts.schema_status postgresql+psycopg://...

Exit status is 0 for a database Alembic can take over, 1 for one it cannot.
Nothing is written: no migration runs, no revision is stamped.
"""

import os
import sys

from sqlalchemy import create_engine

from database.schema import (
    BASELINE_REVISION,
    recovery_message,
    classify,
    current_revision,
)

ACTIONS = {
    "versioned": "already under Alembic; startup would run `upgrade head`",
    "empty": "no ORM tables; startup would create the schema from the baseline",
    "legacy": f"pre-Alembic schema; startup would stamp {BASELINE_REVISION}, then upgrade",
    "unknown": "cannot be adopted; startup would fail closed",
}


def main(argv: list[str]) -> int:
    url = argv[1] if len(argv) > 1 else os.environ.get("DATABASE_URL")
    if not url:
        print("usage: python -m scripts.schema_status [DATABASE_URL]", file=sys.stderr)
        return 2

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            state = classify(conn)
            print(f"url:      {engine.url.render_as_string(hide_password=True)}")
            print(f"backend:  {engine.dialect.name}")
            print(f"state:    {state}")
            print(f"action:   {ACTIONS[state]}")
            if state == "versioned":
                print(f"revision: {current_revision(conn)}")
            if state == "unknown":
                print()
                print(recovery_message(conn))
                return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
