"""`liquid_us_equity_v1` — a universe computed from `price_bars` alone.

Spec N §4.2 is blunt about why this exists: no affordable source has Russell
membership history, S&P history is a hand-maintained Wikipedia scrape, and a
universe defined as "liquid US equities" computed from *today's* liquidity is
survivorship bias wearing a disguise — the names that died were illiquid on the
way down. So the primary universe is self-defined, reproducible, and applied
point-in-time to a delisting-complete price file.

The rule, in full
-----------------
At each **month-end** (the last session in `price_bars` on or before the last
calendar day of the month):

1. For every security with a full `window` of sessions on or before that date,
   compute the median dollar volume over those sessions (`raw_close x volume`).
2. Rank descending; take the top `top_n`. Ties break on `security_uid`, so the
   answer is a pure function of the stored bars.
3. Membership runs from that month-end (inclusive) to the next month-end
   (exclusive); the final month's interval is left open.
4. `known_at_utc` is the month-end **close**, because that is the first moment
   the ranking could have been computed.

Point 1 is the survivorship-critical part: a name that delisted mid-month keeps
whatever membership it had, and a name whose history has not yet reached
`window` sessions is simply absent rather than defaulted in.

**Market cap is not in the rule.** Spec N §4.2 names "rank-by-market-cap and
dollar volume"; market cap needs a share count, the price file does not carry
one, and the SEC `dei:EntityCommonStockSharesOutstanding` feed that supplies it
lands in Phase 3a. `liquid_us_equity_v1` is therefore liquidity-only and is
versioned as `_v1` precisely so that adding the cap leg later is a new universe
slug rather than a silent redefinition of an existing one — the same
immutability rule the setup specs get.
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Mapping, Sequence

from sqlalchemy.orm import Session

from data.prices import store
from data.prices.base import DailyBar, MembershipInterval
from data.prices.derived import DEFAULT_WINDOW_SESSIONS, SeriesError, median_dollar_volume
from data.prices.fixture_plane import session_close_utc

UNIVERSE_SLUG = "liquid_us_equity_v1"
SOURCE = "rule:liquid_us_equity_v1"

DEFAULT_TOP_N = 500


def month_end_sessions(sessions: Sequence[date]) -> tuple[date, ...]:
    """The last session of each calendar month present in `sessions`."""
    latest: dict[tuple[int, int], date] = {}
    for day in sorted(sessions):
        latest[(day.year, day.month)] = day
    return tuple(latest[key] for key in sorted(latest))


def rank_as_of(
    bars_by_uid: Mapping[str, Sequence[DailyBar]],
    as_of: date,
    top_n: int = DEFAULT_TOP_N,
    window: int = DEFAULT_WINDOW_SESSIONS,
) -> tuple[tuple[str, float], ...]:
    """`((security_uid, median_dollar_volume), ...)` for the top `top_n`.

    Descending by liquidity, ties broken ascending on `security_uid` so two runs
    over the same bars produce the same list in the same order.
    """
    measured: list[tuple[str, float]] = []
    for uid, bars in bars_by_uid.items():
        try:
            measured.append((uid, median_dollar_volume(bars, as_of, window)))
        except SeriesError:
            continue
    measured.sort(key=lambda pair: (-pair[1], pair[0]))
    return tuple(measured[:top_n])


def compute_membership(
    bars_by_uid: Mapping[str, Sequence[DailyBar]],
    sessions: Sequence[date],
    top_n: int = DEFAULT_TOP_N,
    window: int = DEFAULT_WINDOW_SESSIONS,
) -> tuple[MembershipInterval, ...]:
    """The whole rule, as pure data in and data out.

    Consecutive months in which a name stays a member are **not** merged: one
    interval per month-end keeps the natural key stable and makes a month's
    ranking individually auditable, which is worth more than a shorter table.
    """
    ends = month_end_sessions(sessions)
    ticker_by_uid = {
        uid: (bars[-1].ticker if bars else uid) for uid, bars in bars_by_uid.items()
    }

    intervals: list[MembershipInterval] = []
    for index, month_end in enumerate(ends):
        member_to = ends[index + 1] if index + 1 < len(ends) else None
        known_at = session_close_utc(month_end)
        for uid, _liquidity in rank_as_of(bars_by_uid, month_end, top_n, window):
            intervals.append(MembershipInterval(
                universe_slug=UNIVERSE_SLUG,
                security_uid=uid,
                ticker=ticker_by_uid.get(uid, uid),
                member_from=month_end,
                member_to=member_to,
                source=SOURCE,
                known_at_utc=known_at,
            ))
    return tuple(intervals)


def rebuild(
    session: Session,
    top_n: int = DEFAULT_TOP_N,
    window: int = DEFAULT_WINDOW_SESSIONS,
    start: date | None = None,
    end: date | None = None,
) -> int:
    """Recompute `liquid_us_equity_v1` from stored bars and rewrite the table.

    Reads `price_bars` and nothing else — no vendor call, no share count, no
    current-vintage list. Returns the number of membership rows written.
    """
    bars_by_uid = store.load_all_bars(session, end=end)
    sessions = [d for d in store.session_dates(session, start=start, end=end)]
    intervals = compute_membership(bars_by_uid, sessions, top_n=top_n, window=window)
    return store.replace_universe(session, UNIVERSE_SLUG, intervals)


def last_calendar_day(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])
