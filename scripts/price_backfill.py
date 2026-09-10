"""Backfill the price plane from a source into `price_bars` and friends.

    python -m scripts.price_backfill --source fixture --since 2023-01-01
    python -m scripts.price_backfill --source sharadar --since 2015-01-01 --tickers AAPL,MSFT
    python -m scripts.price_backfill --source sharadar --bulk years=10

Idempotent: the natural keys in `data/prices/store.py` mean re-running the same
`--since` rewrites the same rows rather than duplicating them, so a run that
dies half way is resumed by running it again.

Every name is checked against the Spec N §4.3 reconstruction identity before it
is stored (`derived.check_reconstruction`). A vendor whose adjusted closes do not
agree with its own factors is caught here, at ingest, and not six weeks later in
a cohort. `--skip-reconstruction-check` exists for triage only and prints a
warning that says so.

`--bulk years=5|10|full` (Sharadar only) downloads the vendor's pre-built zip
for `stocks` and `actions` instead of paging `daily_bars`/`corporate_actions`
once per ticker — the right mode for the 10-year Prices tier the owner buys,
where paging thousands of names one at a time would take hours. Security
master rows are still fetched through the ordinary slice path, batched to keep
the `ticker=` query string a sane length, since `tickers` is a full-snapshot
table either way (Sharadar re-publishes it whole regardless of `years`).
`--tickers` still narrows a bulk run to a subset after the zip is parsed;
omitted, every ticker in the zip is loaded.

Requires `PRICE_PLANE_ENABLED=true`, and for `--source sharadar`,
`SHARADAR_API_KEY`.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

from data.prices import config as plane_config
from data.prices import store
from data.prices.base import PricePlane, PricePlaneError
from data.prices.derived import check_reconstruction

#: Tickers per `security_master` call in bulk mode, so the comma-joined
#: `ticker=` query string stays well under any sane URL-length limit even for
#: a `years=full` run over the whole market.
MASTER_BATCH_SIZE = 200


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


def backfill_bulk(
    plane: PricePlane,
    years: str,
    tickers: list[str] | None,
    since: date | None,
    until: date | None = None,
    check: bool = True,
) -> dict:
    """Bulk-load `stocks` + `actions` from Sharadar's zip download.

    `plane` must be a `SharadarPricePlane` (bulk is not part of the generic
    `PricePlane` interface — `FixturePricePlane` has no vendor zip to fetch).
    `tickers`, if given, narrows the parsed zip to a subset; otherwise every
    ticker in the zip is loaded.

    The bulk `stocks`/`actions` CSVs carry no `permaticker` (only `ticker`),
    so `load_bulk_bars`/`load_bulk_actions` hand back a placeholder
    `security_uid`. That placeholder is replaced here with the real
    permaticker-derived uid from `security_master` before anything is stored
    — `price_bars` and `securities` are joined on `security_uid`
    (`data/prices/store.py`), so storing the placeholder would silently orphan
    every bulk-loaded bar from its security-master row.
    """
    from dataclasses import replace as _replace

    from database.db import get_session

    if not hasattr(plane, "bulk_download"):
        raise PricePlaneError(f"{plane.source} has no bulk download path")

    summary = {
        "source": plane.source,
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "bulk_years": years,
        "tickers_requested": len(tickers) if tickers else None,
        "tickers_with_bars": 0,
        "bars_written": 0,
        "actions_written": 0,
        "securities_written": 0,
        "tickers_empty": [],
        "tickers_without_a_security_master_row": [],
        "reconstruction_checked": check,
    }

    with tempfile.TemporaryDirectory(prefix="sharadar_bulk_") as tmp:
        stocks_zip = plane.bulk_download("stocks", years, Path(tmp) / "stocks.zip")
        actions_zip = plane.bulk_download("actions", years, Path(tmp) / "actions.zip")
        bars_by_ticker = plane.load_bulk_bars(stocks_zip)
        actions_by_ticker = plane.load_bulk_actions(actions_zip)

    wanted = set(tickers) if tickers else set(bars_by_ticker)

    with get_session() as session:
        ticker_list = sorted(wanted)
        uid_by_ticker: dict[str, str] = {}
        for start in range(0, len(ticker_list), MASTER_BATCH_SIZE):
            batch = ticker_list[start:start + MASTER_BATCH_SIZE]
            master_rows = plane.security_master(batch)
            summary["securities_written"] += store.upsert_securities(session, master_rows)
            uid_by_ticker.update({row.ticker: row.security_uid for row in master_rows})

        for ticker in ticker_list:
            uid = uid_by_ticker.get(ticker)
            if uid is None:
                # `tickers` has no row for this name — refuse to store bars
                # under the bulk parser's placeholder uid, same principle as
                # `_uid_for` refusing to invent one on the slice path.
                summary["tickers_without_a_security_master_row"].append(ticker)
                continue
            bars = tuple(
                _replace(bar, security_uid=uid)
                for bar in bars_by_ticker.get(ticker, ())
                if (since is None or bar.session_date >= since)
                and (until is None or bar.session_date <= until)
            )
            if not bars:
                summary["tickers_empty"].append(ticker)
                continue
            if check:
                check_reconstruction(bars)
            summary["bars_written"] += store.upsert_bars(session, bars)
            actions = tuple(
                _replace(action, security_uid=uid)
                for action in actions_by_ticker.get(ticker, ())
                if (since is None or action.ex_date >= since)
                and (until is None or action.ex_date <= until)
            )
            summary["actions_written"] += store.upsert_corporate_actions(session, actions)
            summary["tickers_with_bars"] += 1

    return summary


def parse_bulk_years(value: str) -> str:
    """`years=10` or bare `10` -> `"10"`, validated against `BULK_YEARS`."""
    from data.prices.sharadar import BULK_YEARS

    years = value.split("=", 1)[1] if "=" in value else value
    years = years.strip()
    if years not in BULK_YEARS:
        raise argparse.ArgumentTypeError(f"--bulk must name years in {BULK_YEARS}, got {value!r}")
    return years


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default=None, help="fixture | sharadar")
    parser.add_argument("--since", type=parse_date, default=None, help="YYYY-MM-DD")
    parser.add_argument("--until", type=parse_date, default=None, help="YYYY-MM-DD")
    parser.add_argument(
        "--tickers", default="",
        help="comma-separated; default is every ticker the source knows (fixture), "
        "or every ticker in the zip (--bulk)",
    )
    parser.add_argument(
        "--bulk", default=None, type=parse_bulk_years, metavar="years=5|10|full",
        help="Sharadar only: load stocks+actions from the vendor's bulk zip "
        "instead of paging per ticker",
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

    if args.bulk and plane.source != "sharadar":
        print(f"--bulk is Sharadar-only; --source resolved to {plane.source!r}", file=sys.stderr)
        return 2

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if not tickers and not args.bulk:
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

    if args.bulk:
        summary = backfill_bulk(
            plane, args.bulk, tickers or None, args.since, args.until,
            check=not args.skip_reconstruction_check,
        )
    else:
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
