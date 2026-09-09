"""Populate `universe_membership` for the two universes this phase ships.

    python -m scripts.build_universes --universe sp500_wikipedia_v1
    python -m scripts.build_universes --universe liquid_us_equity_v1 --top-n 500

`sp500_wikipedia_v1` loads the committed MIT-licensed `fja05680/sp500` history
(a hand-maintained Wikipedia scrape — provenance class
`archival_reconstructed`, see `data/prices/sp500_history.py`).

`liquid_us_equity_v1` computes membership from `price_bars` alone: top N by
20-session median dollar volume as of each month-end, `known_at_utc` at that
month-end's close. It reads no vendor and needs no share count — market-cap
ranking waits for the Phase 3a SEC feed.

Both rewrite their own slug wholesale, so re-running is how you update.
Requires `PRICE_PLANE_ENABLED=true`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime

from data.prices import config as plane_config
from data.prices import sp500_history, store, universes
from data.prices.base import PricePlaneError

UNIVERSES = (sp500_history.UNIVERSE_SLUG, universes.UNIVERSE_SLUG)


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--universe", required=True, choices=UNIVERSES)
    parser.add_argument("--top-n", type=int, default=None)
    parser.add_argument("--window", type=int, default=None, help="sessions in the liquidity window")
    parser.add_argument("--since", type=parse_date, default=None)
    parser.add_argument("--until", type=parse_date, default=None)
    parser.add_argument("--csv", default=None, help="override the committed S&P 500 CSV path")
    args = parser.parse_args(argv)

    settings = plane_config.get_settings()
    try:
        plane_config.require_enabled(settings)
    except PricePlaneError as exc:
        print(f"universe build refused: {exc}", file=sys.stderr)
        return 2

    from database.db import get_session, init_db

    init_db(settings.database_url)

    with get_session() as session:
        if args.universe == sp500_history.UNIVERSE_SLUG:
            intervals = sp500_history.membership_intervals(args.csv)
            written = store.replace_universe(session, args.universe, intervals)
        else:
            written = universes.rebuild(
                session,
                top_n=args.top_n if args.top_n is not None else settings.liquid_universe_top_n,
                window=(
                    args.window if args.window is not None
                    else settings.liquid_universe_window_sessions
                ),
                start=args.since,
                end=args.until,
            )

    print(f"{written} membership rows for {args.universe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
