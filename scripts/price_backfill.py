"""Backfill the price plane from a source into `price_bars` and friends.

    python -m scripts.price_backfill --source fixture --since 2023-01-01
    python -m scripts.price_backfill --source sharadar --since 2015-01-01 --tickers AAPL,MSFT
    python -m scripts.price_backfill --source sharadar --bulk years=10
    python -m scripts.price_backfill --source sharadar --since 2016-01-01 \
        --tickers SPY --asset-class fund

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

`--asset-class equity|fund|auto` says which Sharadar price table to read.
`equity` is the default and sends `table=stocks`, exactly as this script did
before funds existed. `fund` sends `table=funds` (legacy SFP) — the only table
SPY is in, and therefore the only way to load the benchmark that every abnormal
return in Spec N §5.2 is measured against. `auto` asks the vendor's `tickers`
master which table each name is in and uses that, which is the right answer for
a mixed `--tickers` list and the wrong answer to guess from a symbol.

`fund` and `auto` additionally require `PRICE_PLANE_FUNDS_ENABLED=true`; the
default `equity` does not, so this script behaves identically to before with no
new variable set.

A fund run prints the resulting `security_uid` for each name, because that is
the value the owner has to paste into `COMPARABLE_BENCHMARK_SECURITY_UID`
(`docs/ENV_SETUP.md` §7). `scripts/benchmark_uid.py` prints the same value from
the stored master afterwards, without re-running a backfill.

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
from data.prices.base import (
    ASSET_CLASS_EQUITY,
    ASSET_CLASS_FUND,
    PricePlane,
    PricePlaneError,
)
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
    asset_class: str | None = ASSET_CLASS_EQUITY,
) -> dict:
    """Pull master, actions and bars for `tickers` and store them.

    `asset_class` is passed straight to `plane.security_master`: a class name
    to look up one table, or `None` to let the vendor's master say which table
    each name is in. It is **not** passed to `daily_bars`, which resolves the
    table itself through the same master — one source of truth for "what kind
    of instrument is this", and no way for the two calls to disagree.

    `security_uid_by_ticker` is in the summary because a fund run exists to
    produce exactly that value: `COMPARABLE_BENCHMARK_SECURITY_UID` is a uid,
    not a ticker, and it is otherwise only discoverable by querying the
    database by hand. `main()` prints it and then drops it before recording the
    snapshot — see the comment there.
    """
    from database.db import get_session

    summary = {
        "source": plane.source,
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "asset_class": asset_class or "auto",
        "tickers_requested": len(tickers),
        "tickers_with_bars": 0,
        "bars_written": 0,
        "actions_written": 0,
        "securities_written": 0,
        "tickers_empty": [],
        "security_uid_by_ticker": {},
        "asset_class_by_ticker": {},
        "reconstruction_checked": check,
    }

    with get_session() as session:
        master_rows = plane.security_master(tickers, asset_class=asset_class)
        summary["securities_written"] = store.upsert_securities(session, master_rows)
        summary["security_uid_by_ticker"] = {
            row.ticker: row.security_uid for row in master_rows
        }
        summary["asset_class_by_ticker"] = {
            row.ticker: row.asset_class for row in master_rows
        }
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
    parser.add_argument(
        "--asset-class", default=ASSET_CLASS_EQUITY,
        choices=(ASSET_CLASS_EQUITY, ASSET_CLASS_FUND, "auto"),
        help="which Sharadar price table to read: equity -> stocks (default, "
        "unchanged behaviour), fund -> funds/SFP (SPY lives here), auto -> ask "
        "the vendor master per ticker. fund and auto need "
        "PRICE_PLANE_FUNDS_ENABLED=true.",
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

    asset_class = None if args.asset_class == "auto" else args.asset_class
    if asset_class != ASSET_CLASS_EQUITY:
        try:
            plane_config.require_funds_enabled(settings)
        except PricePlaneError as exc:
            print(f"price backfill refused: {exc}", file=sys.stderr)
            return 2
    if args.bulk and asset_class != ASSET_CLASS_EQUITY:
        # The bulk zips this script knows how to parse are `stocks` and
        # `actions`. A `funds` bulk zip is a separate brief; refusing is the
        # honest answer, because `--bulk --asset-class fund` would otherwise
        # silently load equities and report success.
        print(
            "--bulk covers the `stocks` zip only; run a fund with the slice "
            "path (--tickers SPY --asset-class fund).",
            file=sys.stderr,
        )
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
            plane, tickers, args.since, args.until,
            check=not args.skip_reconstruction_check, asset_class=asset_class,
        )

    # The two per-ticker maps are for the printout below, not for the stored
    # snapshot: `coverage_summary_json` is one text column, and a 5,000-name
    # equity backfill would put a 5,000-entry uid map in it on every run. The
    # scalar `asset_class` stays, because "which table was this snapshot
    # loaded from" is exactly the kind of thing a snapshot should record.
    uids = summary.pop("security_uid_by_ticker", None) or {}
    classes = summary.pop("asset_class_by_ticker", None) or {}

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

    # The uid printout. A fund run exists to produce it: the benchmark variable
    # is a `security_uid`, and nothing else in the pipeline ever shows one to a
    # human. Printed for every non-default asset class, including `auto`, since
    # `auto` is how an operator finds out a name was a fund at all.
    funds = sorted(t for t, k in classes.items() if k == ASSET_CLASS_FUND)
    if funds:
        print("\nfunds loaded — these are the security_uids:")
        for ticker in funds:
            print(f"  {ticker:<8} {uids.get(ticker, '?')}")
        print(
            "\nSet the cohort benchmark to one of them, e.g.\n"
            f"  COMPARABLE_BENCHMARK_SECURITY_UID={uids.get(funds[0], '?')}\n"
            "then run `python -m scripts.cohort_smoke` (docs/ENV_SETUP.md §7)."
        )
    elif asset_class != ASSET_CLASS_EQUITY:
        print(
            f"\nno fund rows came back for {', '.join(tickers) or 'the requested names'}; "
            "the vendor master put every one of them in the equity table."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
