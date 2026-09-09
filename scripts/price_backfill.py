"""Backfill the price plane from a source into `price_bars` and friends.

    python -m scripts.price_backfill --source fixture --since 2023-01-01
    python -m scripts.price_backfill --source sharadar --since 2015-01-01 --tickers AAPL,MSFT

Idempotent: the natural keys in `data/prices/store.py` mean re-running the same
`--since` rewrites the same rows rather than duplicating them, so a run that
dies half way is resumed by running it again.

Every name is checked against the Spec N §4.3 reconstruction identity before it
is stored (`derived.check_reconstruction`). A vendor whose adjusted closes do not
agree with its own factors is caught here, at ingest, and not six weeks later in
a cohort. `--skip-reconstruction-check` exists for triage only and prints a
warning that says so.

Requires `PRICE_PLANE_ENABLED=true`, and for `--source sharadar`,
`NASDAQ_DATA_LINK_API_KEY`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime

from data.prices import config as plane_config
from data.prices import store
from data.prices.base import PricePlane, PricePlaneError
from data.prices.derived import check_reconstruction


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def backfill(
    plane: PricePlane,
    tickers: list[str],
    since: date | None,
    until: date | None = None,
    check: bool = True,
) -> dict:
    """Pull master, actions and bars for `tickers` and store them."""
    from database.db import get_session

    summary = {
        "source": plane.source,
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "tickers_requested": len(tickers),
        "tickers_with_bars": 0,
        "bars_written": 0,
        "actions_written": 0,
        "securities_written": 0,
        "tickers_empty": [],
        "reconstruction_checked": check,
    }

    with get_session() as session:
        summary["securities_written"] = store.upsert_securities(
            session, plane.security_master(tickers)
        )
        for ticker in tickers:
            bars = plane.daily_bars(ticker, since, until)
            if not bars:
                summary["tickers_empty"].append(ticker)
                continue
            if check:
                check_reconstruction(bars)
            summary["bars_written"] += store.upsert_bars(session, bars)
            summary["actions_written"] += store.upsert_corporate_actions(
                session, plane.corporate_actions(ticker, since, until)
            )
            summary["tickers_with_bars"] += 1

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default=None, help="fixture | sharadar")
    parser.add_argument("--since", type=parse_date, default=None, help="YYYY-MM-DD")
    parser.add_argument("--until", type=parse_date, default=None, help="YYYY-MM-DD")
    parser.add_argument(
        "--tickers", default="",
        help="comma-separated; default is every ticker the source knows (fixture only)",
    )
    parser.add_argument("--snapshot", default=None, help="snapshot slug to record coverage on")
    parser.add_argument("--skip-reconstruction-check", action="store_true")
    args = parser.parse_args(argv)

    settings = plane_config.get_settings()
    try:
        plane_config.require_enabled(settings)
        plane = plane_config.build_plane(args.source, settings)
    except PricePlaneError as exc:
        print(f"price backfill refused: {exc}", file=sys.stderr)
        return 2

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if not tickers:
        listed = getattr(plane, "tickers", None)
        if listed is None:
            print(
                "--tickers is required for this source: it has no enumerable universe.",
                file=sys.stderr,
            )
            return 2
        tickers = list(listed())

    from database.db import get_session, init_db

    init_db(settings.database_url)

    if args.skip_reconstruction_check:
        print(
            "WARNING: storing series without the §4.3 reconstruction check. "
            "The stored bars may not reproduce from their own factors.",
            file=sys.stderr,
        )

    summary = backfill(
        plane, tickers, args.since, args.until, check=not args.skip_reconstruction_check
    )

    snapshot = args.snapshot or settings.price_plane_snapshot
    with get_session() as session:
        store.record_snapshot(session, snapshot, plane.source, summary)

    print(
        f"{summary['bars_written']} bars, {summary['actions_written']} actions, "
        f"{summary['securities_written']} securities from {plane.source} "
        f"into snapshot {snapshot!r}"
    )
    if summary["tickers_empty"]:
        print(f"no bars for: {', '.join(summary['tickers_empty'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
