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

**Funds are not in the universe.** `liquid_us_equity_v1` means equities, and it
has to mean it: SPY is the most liquid instrument on the tape, so a rule that
ranked ETFs by dollar volume would put the *benchmark* in the top ten every
month — and a cohort whose members include the security its abnormal returns
are measured against is a number measured against itself (Spec N §5.2).
`rebuild` therefore drops every security the master calls a fund
(`data/prices/base.py::ASSET_CLASSES`) before ranking anything, and
`compute_membership` refuses outright to emit an interval for a uid it was told
is ineligible. That second part is a belt on top of braces on purpose: the
filter is one line and the failure it prevents is silent and arithmetic.

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
from typing import Collection, Mapping, Sequence

from sqlalchemy.orm import Session

from data.prices import store
from data.prices.base import DailyBar, MembershipInterval
from data.prices.derived import DEFAULT_WINDOW_SESSIONS, SeriesError, median_dollar_volume
from data.prices.fixture_plane import session_close_utc

UNIVERSE_SLUG = "liquid_us_equity_v1"
SOURCE = "rule:liquid_us_equity_v1"

DEFAULT_TOP_N = 500


class UniverseRuleError(RuntimeError):
    """The membership the rule produced violates the rule. Never written."""


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
    eligible_uids: Collection[str] | None = None,
) -> tuple[tuple[str, float], ...]:
    """`((security_uid, median_dollar_volume), ...)` for the top `top_n`.

    Descending by liquidity, ties broken ascending on `security_uid` so two runs
    over the same bars produce the same list in the same order.

    `eligible_uids`, when given, is the set of securities allowed into the
    ranking at all — `rebuild` passes the equities. `None` means "rank whatever
    you were handed", which is what the pure-data callers that have already
    chosen their inputs want; it is not a way to opt out of the rule, because
    the stored rule runs through `rebuild`.
    """
    measured: list[tuple[str, float]] = []
    for uid, bars in bars_by_uid.items():
        if eligible_uids is not None and uid not in eligible_uids:
            continue
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
    eligible_uids: Collection[str] | None = None,
) -> tuple[MembershipInterval, ...]:
    """The whole rule, as pure data in and data out.

    Consecutive months in which a name stays a member are **not** merged: one
    interval per month-end keeps the natural key stable and makes a month's
    ranking individually auditable, which is worth more than a shorter table.

    `eligible_uids` narrows what may be ranked (see `rank_as_of`) and is then
    asserted over the result: if an ineligible uid reached an interval anyway,
    this raises rather than writing it, because the whole cost of a fund in
    `liquid_us_equity_v1` is paid silently, months later, inside a cohort
    statistic.
    """
    ends = month_end_sessions(sessions)
    ticker_by_uid = {
        uid: (bars[-1].ticker if bars else uid) for uid, bars in bars_by_uid.items()
    }

    intervals: list[MembershipInterval] = []
    for index, month_end in enumerate(ends):
        member_to = ends[index + 1] if index + 1 < len(ends) else None
        known_at = session_close_utc(month_end)
        for uid, _liquidity in rank_as_of(
            bars_by_uid, month_end, top_n, window, eligible_uids=eligible_uids
        ):
            intervals.append(MembershipInterval(
                universe_slug=UNIVERSE_SLUG,
                security_uid=uid,
                ticker=ticker_by_uid.get(uid, uid),
                member_from=month_end,
                member_to=member_to,
                source=SOURCE,
                known_at_utc=known_at,
            ))

    if eligible_uids is not None:
        intruders = sorted({
            i.security_uid for i in intervals if i.security_uid not in eligible_uids
        })
        if intruders:
            raise UniverseRuleError(
                f"{UNIVERSE_SLUG} would have admitted {len(intruders)} security "
                f"uid(s) the rule excludes: {intruders[:10]}. A benchmark ETF "
                f"inside the universe it benchmarks is a cohort measured against "
                f"itself (Spec N §5.2), so this refuses rather than writing it."
            )
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
    # The enforcement point for "funds are never universe members". Read from
    # `securities`, not guessed from the ticker: `SPY` is three letters like
    # any other. Phrased as "everything with bars, minus what the master calls
    # a fund" rather than "everything the master calls an equity" — see
    # `store.non_equity_security_uids` for why an allow-list would be the wrong
    # shape here.
    excluded = store.non_equity_security_uids(session)
    intervals = compute_membership(
        bars_by_uid, sessions, top_n=top_n, window=window,
        eligible_uids=frozenset(bars_by_uid) - excluded,
    )
    return store.replace_universe(session, UNIVERSE_SLUG, intervals)


def last_calendar_day(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])
