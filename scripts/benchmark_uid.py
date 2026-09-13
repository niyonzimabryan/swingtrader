"""Print the `security_uid` of a stored fund, for `COMPARABLE_BENCHMARK_SECURITY_UID`.

    python -m scripts.benchmark_uid SPY
    python -m scripts.benchmark_uid           # every fund in the stored master

`CohortContext` refuses to construct without a benchmark uid
(`comparables/cohort.py`), and the uid is a `permaticker`-derived string such
as `sharadar:118691` — not something anybody can guess from a ticker, and not
printed anywhere except by the backfill that first wrote it. This script reads
it back out of `securities` afterwards, so recovering it does not mean
re-running a backfill or opening a `psql` session.

It reads the **stored master**, never the vendor. If a name is missing here the
answer is to back it up first:

    python -m scripts.price_backfill --source sharadar --since 2016-01-01 \\
        --tickers SPY --asset-class fund

Requires `PRICE_PLANE_ENABLED=true` and `PRICE_PLANE_FUNDS_ENABLED=true`, on
the same "flags gate the entry points" rule as every other price-plane script
(`data/prices/config.py`).
"""

from __future__ import annotations

import argparse
import sys

from data.prices import config as plane_config
from data.prices.base import ASSET_CLASS_FUND, PricePlaneError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "tickers", nargs="*",
        help="fund tickers to look up; omit for every fund in the stored master",
    )
    parser.add_argument(
        "--any-asset-class", action="store_true",
        help="also print equities. Off by default because a benchmark that is "
             "an operating company is a benchmark nobody meant to set.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    settings = plane_config.get_settings()
    try:
        plane_config.require_enabled(settings)
        plane_config.require_funds_enabled(settings)
    except PricePlaneError as exc:
        print(f"benchmark_uid refused: {exc}", file=sys.stderr)
        return 2

    from data.prices import store
    from database.db import get_session, init_db

    init_db(settings.database_url)

    wanted = [t.strip().upper() for t in args.tickers if t.strip()]
    with get_session() as session:
        # Read into plain tuples *inside* the session. `load_securities` hands
        # back ORM instances, and touching one after the session closes raises
        # `DetachedInstanceError` rather than printing a uid.
        rows = tuple(
            (row.ticker, row.asset_class, row.security_uid)
            for row in store.load_securities(session, wanted or None)
            if args.any_asset_class or row.asset_class == ASSET_CLASS_FUND
        )

    if not rows:
        scope = ", ".join(wanted) if wanted else "the stored master"
        print(
            f"no fund rows in {scope}. Back one up first:\n"
            "  python -m scripts.price_backfill --source sharadar "
            "--since 2016-01-01 --tickers SPY --asset-class fund",
            file=sys.stderr,
        )
        return 1

    for ticker, asset_class, uid in rows:
        print(f"{ticker:<8} {asset_class:<7} {uid}")

    print(
        f"\nCOMPARABLE_BENCHMARK_SECURITY_UID={rows[0][2]}\n"
        "then: python -m scripts.cohort_smoke   (docs/ENV_SETUP.md §7)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
