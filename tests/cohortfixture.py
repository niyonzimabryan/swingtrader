"""A stored world for the Phase 3c cohort tests.

Phase 3b's fixtures are in-memory dataclasses; Phase 3c reads *rows*, so its
tests need a database with bars, a security master, a point-in-time universe, a
price snapshot carrying a delisting audit, and a bitemporal fact ledger. This
module builds one, deterministically, from a seeded PRNG and nothing else.

Everything here is **synthetic**. No vendor series and no real filing is
committed anywhere in this repo (Spec K §3.3), and the tickers are `SY00`..`SY29`
so nobody mistakes one for a real name.

The world is shaped to exercise the rules, not to look realistic:

* one Nasdaq **performance delisting** mid-window, so the §4.2 terminal return
  and the §4.4 matured-vs-censored split both have something to bite on;
* one **merger** with no stored deal terms, which is *censored*, not matured;
* a **gap** injected on chosen sessions for chosen names, so `gap_and_go_v1`
  clears the §8 floors of 20 distinct dates and 30 matured events;
* quarterly **XBRL EPS** with a seasonal jump for a subset of names, tagged on
  the same 8-K accession as the announcement, so `earnings_sue_seasonal_v1`'s
  SUE is knowable at the announcement instant rather than weeks later;
* one company whose EPS is tagged **only on the later 10-Q**, so the
  `sue_known_after_announcement` path is exercised;
* one company with **no share count at all**, so `market_cap_decile` refuses it
  by name rather than computing a market cap from a later count.

The name is deliberately not ``test_*``: ``unittest discover -p "test_*.py"``
would otherwise import it as a test module.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from data.prices import store, universes
from data.prices.base import DailyBar, MembershipInterval, SecurityMasterRow
from filings.observations import (
    PRECISION_DAY,
    PRECISION_SECOND,
    PROVENANCE_VENDOR_PIT,
    TRUST_PRIMARY_REGULATOR,
    Observation,
    write_observations,
)

from comparables.cohort import CohortContext

SOURCE = "fixture"
BENCH_UID = "fx-bench"
BENCH_TICKER = "BENCHX"
SNAPSHOT_SLUG = "test_snapshot"
UNIVERSE_SLUG = universes.UNIVERSE_SLUG

#: Enough names that a decile is a decile and enough sessions that a 50-session
#: moving average exists well before the first event.
N_SECURITIES = 30
N_SESSIONS = 420

#: How many sessions carry an injected gap, and how many names gap on each.
N_GAP_DATES = 26
NAMES_PER_GAP = 3

#: The delisted and merged names, by index.
DELISTED_INDEX = 27
MERGED_INDEX = 28
#: The company whose EPS only ever appears on the later 10-Q.
LATE_EPS_INDEX = 3
#: The company that never tagged a cover-page share count.
NO_SHARES_INDEX = 2

#: Quarterly EPS: a small deterministic wobble so the seasonal differences have
#: a spread at all, and a permanent step up at a name-specific quarter so the
#: four quarters that follow it carry a real year-on-year surprise.
EPS_WOBBLE = 0.02
EPS_STEP = 0.30
FIRST_STEP_QUARTER = 11

SESSION_CLOSE = datetime.min.time().replace(hour=21, tzinfo=timezone.utc)


def ticker_for(index: int) -> str:
    return f"SY{index:02d}"


def uid_for(index: int) -> str:
    return f"fx-{index:04d}"


def cik_for(index: int) -> str:
    return f"{1_500_000 + index:010d}"


@dataclass(frozen=True)
class World:
    """What a test needs to know about the world it was just handed."""

    as_of: date
    context: CohortContext
    sessions: tuple[date, ...]
    tickers: tuple[str, ...]
    gap_sessions: tuple[date, ...]
    earnings_dates: tuple[date, ...]
    delisted_ticker: str
    delisting_date: date
    merged_ticker: str
    late_eps_ticker: str
    no_shares_ticker: str
    cik_by_ticker: tuple[tuple[str, str], ...]


def business_days(start: date, count: int) -> list[date]:
    out: list[date] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def session_close_utc(day: date) -> datetime:
    return datetime.combine(day, SESSION_CLOSE)


# --------------------------------------------------------------------------- #
# Bars
# --------------------------------------------------------------------------- #


def _paths(sessions: list[date]) -> tuple[dict[str, list[float]], list[date]]:
    """A deterministic close path per security, with gaps injected on schedule.

    Upward drift, because `gap_and_go_v1` requires the close to sit above its
    50-session average and a fixture that never satisfies its own setup tests
    only the refusal path.
    """
    rng = random.Random(20260909)
    n = len(sessions)
    first_gap = 60
    span = n - 25 - first_gap
    step = max(span // N_GAP_DATES, 1)
    gap_indices = [first_gap + i * step for i in range(N_GAP_DATES)]

    opens: dict[str, list[float]] = {}
    closes: dict[str, list[float]] = {}
    for index in range(N_SECURITIES):
        ticker = ticker_for(index)
        level = 20.0 + index * 3.5
        o: list[float] = []
        c: list[float] = []
        gapping = {
            gi for k, gi in enumerate(gap_indices)
            if (k + index) % N_SECURITIES < NAMES_PER_GAP
        }
        for i in range(n):
            if i in gapping:
                open_ = level * 1.05
            else:
                open_ = level * (1.0 + rng.uniform(-0.002, 0.002))
            close = open_ * (1.0 + 0.0016 + rng.uniform(-0.006, 0.006))
            o.append(round(open_, 4))
            c.append(round(close, 4))
            level = close
        opens[ticker] = o
        closes[ticker] = c
    return {"open": opens, "close": closes}, [sessions[i] for i in gap_indices]


def _benchmark_path(sessions: list[date]) -> tuple[list[float], list[float]]:
    rng = random.Random(4242)
    level = 400.0
    o, c = [], []
    for _ in sessions:
        open_ = level
        close = open_ * (1.0 + 0.0004 + rng.uniform(-0.004, 0.004))
        o.append(round(open_, 4))
        c.append(round(close, 4))
        level = close
    return o, c


def _bar(uid: str, ticker: str, day: date, open_: float, close: float,
         volume: float) -> DailyBar:
    high = round(max(open_, close) * 1.004, 6)
    low = round(min(open_, close) * 0.996, 6)
    return DailyBar(
        security_uid=uid,
        ticker=ticker,
        session_date=day,
        raw_open=open_,
        raw_high=high,
        raw_low=low,
        raw_close=close,
        volume=volume,
        split_factor=1.0,
        dividend_cash=0.0,
        # No corporate actions in the window, so all three series coincide and
        # the §4.3 reconstruction identity holds by construction rather than by
        # rounding luck.
        split_adjusted_close=close,
        total_return_close=close,
        source=SOURCE,
    )


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #


def seed_prices(session, *, sessions: list[date], delisting_date: date,
                merger_date: date) -> tuple[dict, list[date]]:
    paths, gap_sessions = _paths(sessions)
    opens, closes = paths["open"], paths["close"]

    securities: list[SecurityMasterRow] = []
    bars: list[DailyBar] = []

    b_open, b_close = _benchmark_path(sessions)
    securities.append(SecurityMasterRow(
        security_uid=BENCH_UID, ticker=BENCH_TICKER, source=SOURCE,
        name="Fixture total-return benchmark", exchange="INDEX", venue="other",
        ticker_valid_from=sessions[0], listing_date=sessions[0],
    ))
    for i, day in enumerate(sessions):
        bars.append(_bar(BENCH_UID, BENCH_TICKER, day, b_open[i], b_close[i], 0.0))

    for index in range(N_SECURITIES):
        ticker, uid = ticker_for(index), uid_for(index)
        venue = "nasdaq" if index % 2 else "nyse_amex"
        delisting, reason = None, "unknown"
        if index == DELISTED_INDEX:
            delisting, reason, venue = delisting_date, "performance", "nasdaq"
        elif index == MERGED_INDEX:
            delisting, reason = merger_date, "merger_acquisition"

        securities.append(SecurityMasterRow(
            security_uid=uid, ticker=ticker, source=SOURCE,
            name=f"{ticker} Synthetic Corp", exchange=venue.upper(), venue=venue,
            ticker_valid_from=sessions[0], listing_date=sessions[0],
            delisting_date=delisting, delisting_reason=reason,
        ))
        # A spread of dollar volumes, so liquidity deciles are not all ties and
        # the §5.3 half-spread table is actually exercised across its range.
        volume = 200_000.0 + index * 90_000.0
        for i, day in enumerate(sessions):
            if delisting is not None and day >= delisting:
                break
            bars.append(_bar(uid, ticker, day, opens[ticker][i], closes[ticker][i], volume))

    store.upsert_securities(session, securities)
    store.upsert_bars(session, bars)
    return paths, gap_sessions


def seed_universe(session, sessions: list[date]) -> int:
    """`liquid_us_equity_v1`, computed from the stored bars by the real rule."""
    return universes.rebuild(session, top_n=N_SECURITIES + 1)


def seed_membership_subset(session, sessions: list[date], tickers) -> int:
    """A hand-written membership table, for the tests that need a small one."""
    intervals = [
        MembershipInterval(
            universe_slug=UNIVERSE_SLUG,
            security_uid=uid_for(int(t[2:])),
            ticker=t,
            member_from=sessions[0],
            member_to=None,
            source=universes.SOURCE,
            known_at_utc=session_close_utc(sessions[0]),
        )
        for t in tickers
    ]
    return store.replace_universe(session, UNIVERSE_SLUG, intervals)


def audit_blob(*, synthesised: bool = True) -> dict:
    """A delisting-audit result of the shape `data/prices/audit.py` writes."""
    collapse, stop = (4, 16) if synthesised else (16, 4)
    return {
        "spec": "N-4.2-delisting-returns",
        "source": SOURCE,
        "run_at_utc": "2026-09-09T00:00:00+00:00",
        "window_sessions": 10,
        "collapse_threshold": -0.60,
        "n_cases": 20,
        "counts": {"collapse": collapse, "stop": stop, "missing": 0, "too_short": 0},
        "collapse_rate_of_classified": collapse / (collapse + stop),
        "terminal_returns_must_be_synthesised": stop > collapse,
        "sources_verified_against_primary_filing": False,
        "cases": [],
    }


def seed_snapshot(session, *, slug: str = SNAPSHOT_SLUG, audit: dict | None = None):
    return store.record_snapshot(
        session, slug, SOURCE,
        {"n_securities": N_SECURITIES, "synthetic": True},
        audit,
    )


# --------------------------------------------------------------------------- #
# The fact ledger
# --------------------------------------------------------------------------- #


def _acceptance(day: date, minute: int) -> datetime:
    """After the close, which is when 8-Ks and 10-Qs actually land."""
    return datetime.combine(day, datetime.min.time()).replace(
        hour=21, minute=minute, second=13, tzinfo=timezone.utc
    )


def seed_filings(session, *, sessions: list[date]) -> list[date]:
    """Quarterly EPS plus one 8-K Item 2.02 per company per quarter.

    The EPS fact is tagged on the **same accession** as the 8-K for every
    company but `LATE_EPS_INDEX`, so its SUE is knowable at the announcement
    instant. That one company's EPS is tagged on a 10-Q accepted three weeks
    later, which is the ordinary case in real data and the reason
    `comparables/cohort.py` carries the `sue_known_after_announcement` path.
    """
    observations: list[Observation] = []
    announcement_days: set[date] = set()

    # Twenty quarters of history so the seasonal-difference standard deviation
    # is computed from something, ending inside the price window.
    first_session, last_session = sessions[0], sessions[-1]
    quarters = [
        date(2019, 3, 31) + timedelta(days=91 * q) for q in range(28)
    ]

    for index in range(N_SECURITIES):
        ticker, cik = ticker_for(index), cik_for(index)
        rng = random.Random(9000 + index)
        wobble = [rng.uniform(-1.0, 1.0) for _ in quarters]
        # Most names step, at a name-specific quarter, so the qualifying events
        # spread across the window instead of piling onto four reporting dates.
        steps = index % 7 != 0
        step_quarter = FIRST_STEP_QUARTER + (index % 8)
        base = 1.00 + index * 0.01
        for q, period_end in enumerate(quarters):
            announce_day = period_end + timedelta(days=21 + (index % 5))
            while announce_day.weekday() >= 5:
                announce_day += timedelta(days=1)
            accession = f"9999999999-{index:02d}-{q:06d}"
            acceptance = _acceptance(announce_day, 5 + (index % 40))

            # A slow trend, a deterministic wobble so the seasonal differences
            # have a standard deviation to be scaled by at all, and — for the
            # names that step — a permanent level shift at `step_quarter`. The
            # four quarters after that shift carry a large year-on-year
            # difference against a history of small ones, which is precisely
            # what a seasonal-random-walk SUE is built to notice; from the
            # fifth quarter on, the year-ago figure carries the step too and
            # the surprise disappears, which is also correct.
            eps = base + 0.004 * q + wobble[q] * EPS_WOBBLE
            if steps and q >= step_quarter:
                eps += EPS_STEP

            eps_accession, eps_acceptance = accession, acceptance
            if index == LATE_EPS_INDEX:
                later = announce_day + timedelta(days=21)
                while later.weekday() >= 5:
                    later += timedelta(days=1)
                eps_accession = f"8888888888-{index:02d}-{q:06d}"
                eps_acceptance = _acceptance(later, 30)

            observations.append(Observation(
                source="sec_xbrl_companyfacts",
                entity_cik=cik,
                ticker_at_time=ticker,
                fact_type="eps_diluted",
                valid_at=datetime.combine(period_end, datetime.min.time(),
                                          tzinfo=timezone.utc),
                known_at_utc=eps_acceptance,
                known_at_source="acceptanceDateTime",
                precision=PRECISION_SECOND,
                provenance_class=PROVENANCE_VENDOR_PIT,
                replay_eligible=True,
                value_numeric=round(eps, 4),
                unit="USD/shares",
                accession=eps_accession,
                source_url=f"https://example.invalid/{eps_accession}",
                source_trust=TRUST_PRIMARY_REGULATOR,
            ))

            if not (first_session <= announce_day <= last_session):
                continue
            announcement_days.add(announce_day)
            observations.append(Observation(
                source="sec_8k_item_202",
                entity_cik=cik,
                ticker_at_time=ticker,
                fact_type="earnings_release_8k_item_202",
                valid_at=acceptance,
                known_at_utc=acceptance,
                known_at_source="acceptanceDateTime",
                precision=PRECISION_SECOND,
                provenance_class=PROVENANCE_VENDOR_PIT,
                replay_eligible=True,
                value_text="2.02",
                accession=accession,
                source_url=f"https://example.invalid/{accession}",
                source_trust=TRUST_PRIMARY_REGULATOR,
                payload={"form": "8-K", "items": ["2.02"]},
            ))

    write_observations(session, observations)
    return sorted(announcement_days)


def seed_share_counts(session, *, sessions: list[date]) -> int:
    """Cover-page share counts, except for one company that never tagged one."""
    observations: list[Observation] = []
    for index in range(N_SECURITIES):
        if index == NO_SHARES_INDEX:
            continue
        cik = cik_for(index)
        for year in range(2019, sessions[-1].year + 1):
            period_end = date(year, 3, 31)
            accession = f"7777777777-{index:02d}-{year:06d}"
            observations.append(Observation(
                source="sec_xbrl_companyfacts",
                entity_cik=cik,
                ticker_at_time=ticker_for(index),
                fact_type="shares_outstanding",
                valid_at=datetime.combine(period_end, datetime.min.time(),
                                          tzinfo=timezone.utc),
                known_at_utc=_acceptance(period_end + timedelta(days=25), 11),
                known_at_source="acceptanceDateTime",
                precision=PRECISION_SECOND,
                provenance_class=PROVENANCE_VENDOR_PIT,
                replay_eligible=True,
                value_numeric=float(10_000_000 + index * 3_100_000),
                unit="shares",
                accession=accession,
                source_url=f"https://example.invalid/{accession}",
                source_trust=TRUST_PRIMARY_REGULATOR,
            ))
    write_observations(session, observations)
    return len(observations)


# --------------------------------------------------------------------------- #
# The whole world, split for cheap per-test reseeding
# --------------------------------------------------------------------------- #
#
# `seed_prices` writes on the order of 13,000 rows and dominates `seed_world`'s
# cost (~90% of it); nothing in this file's test suites ever mutates the
# securities or bars it writes, so `seed_base_world` seeds prices and share
# counts once and never again. Filings stay in the cheap, per-test bucket
# despite being seeded alongside prices in `seed_world`, because
# `test_sue_from_xbrl_only` (tests/test_comparables_cohort.py) deletes every
# `eps_diluted` observation to test the "no XBRL EPS" path — a base seeded
# once would stay deleted for every test after it. The universe and the price
# snapshot are the other two things a test rewrites (Spec N §10's "without
# universe"/"without audit" cases). `seed_mutable_world` (re)seeds all three
# every test, which also *undoes* whatever the previous test did to them —
# `replace_universe`/`record_snapshot`/`write_observations` are keyed by slug
# or by (source, entity, fact_type, valid_at, accession), so reseeding is
# equivalent to starting over, not a merge.
#
# A test module that seeds the base once per process (`setUpModule`) and calls
# `seed_mutable_world` at the top of every test gets exactly the isolation
# `seed_world` per test gave, at a fraction of the cost. `seed_world` itself is
# unchanged and still builds everything fresh in one call, for suites that
# only seed a world once (or need the with_* flags on the whole world, though
# nothing in this repo currently calls it with a non-default flag).


def seed_base_world(
    session, *, start: date = date(2022, 1, 3), n_sessions: int = N_SESSIONS,
) -> dict:
    """Seed the expensive, never-mutated part of the world: prices and shares.

    Returns the raw materials `seed_mutable_world` needs to assemble a full
    `World` around freshly (re)seeded filings, universe, and snapshot.
    """
    sessions = business_days(start, n_sessions)
    delisting_date = sessions[int(n_sessions * 0.72)]
    merger_date = sessions[int(n_sessions * 0.80)]

    _paths_unused, gap_sessions = seed_prices(
        session, sessions=sessions, delisting_date=delisting_date,
        merger_date=merger_date,
    )
    seed_share_counts(session, sessions=sessions)

    return {
        "sessions": tuple(sessions),
        "gap_sessions": tuple(gap_sessions),
        "delisting_date": delisting_date,
        "merger_date": merger_date,
        "cik_by_ticker": tuple((ticker_for(i), cik_for(i)) for i in range(N_SECURITIES)),
    }


def seed_mutable_world(
    session,
    base: dict,
    *,
    with_universe: bool = True,
    with_audit: bool = True,
    audit_synthesised: bool = True,
    with_filings: bool = True,
    snapshot_slug: str = SNAPSHOT_SLUG,
) -> World:
    """(Re)seed the filings, the universe, and the snapshot; assemble a `World`.

    Call this at the top of every test that shares a `seed_base_world` base: it
    rebuilds `liquid_us_equity_v1`, the named snapshot, and every filings
    observation from scratch, which is a clean slate regardless of what the
    previous test deleted or replaced. Filings are cheap to reseed
    (`write_observations` is idempotent by payload hash, so re-seeding the same
    facts a previous test did not touch is close to a no-op).
    """
    sessions = list(base["sessions"])
    if with_universe:
        seed_universe(session, sessions)
    seed_snapshot(
        session, slug=snapshot_slug,
        audit=audit_blob(synthesised=audit_synthesised) if with_audit else None,
    )
    earnings_dates = seed_filings(session, sessions=sessions) if with_filings else []
    return World(
        as_of=sessions[-1],
        context=CohortContext(
            universe_slug=UNIVERSE_SLUG,
            price_snapshot_slug=snapshot_slug,
            benchmark_security_uid=BENCH_UID,
            cik_by_ticker=base["cik_by_ticker"],
        ),
        sessions=tuple(sessions),
        tickers=tuple(ticker_for(i) for i in range(N_SECURITIES)),
        gap_sessions=base["gap_sessions"],
        earnings_dates=tuple(earnings_dates),
        delisted_ticker=ticker_for(DELISTED_INDEX),
        delisting_date=base["delisting_date"],
        merged_ticker=ticker_for(MERGED_INDEX),
        late_eps_ticker=ticker_for(LATE_EPS_INDEX),
        no_shares_ticker=ticker_for(NO_SHARES_INDEX),
        cik_by_ticker=base["cik_by_ticker"],
    )


def seed_world(
    session,
    *,
    start: date = date(2022, 1, 3),
    n_sessions: int = N_SESSIONS,
    with_universe: bool = True,
    with_audit: bool = True,
    audit_synthesised: bool = True,
    with_filings: bool = True,
    with_share_counts: bool = True,
    snapshot_slug: str = SNAPSHOT_SLUG,
) -> World:
    """Build the stored world and return the handles a test needs.

    Each `with_*` flag exists because a §10 test needs the world *without* that
    piece: no membership rows caps the tier (`test_universe_is_stored_not_computed`),
    no recorded audit caps it too (`test_delisting_audit_recorded`), and no share
    count refuses `market_cap_decile` (`test_market_cap_has_share_source`).
    """
    sessions = business_days(start, n_sessions)
    delisting_date = sessions[int(n_sessions * 0.72)]
    merger_date = sessions[int(n_sessions * 0.80)]

    _paths_unused, gap_sessions = seed_prices(
        session, sessions=sessions, delisting_date=delisting_date,
        merger_date=merger_date,
    )
    if with_universe:
        seed_universe(session, sessions)
    seed_snapshot(
        session, slug=snapshot_slug,
        audit=audit_blob(synthesised=audit_synthesised) if with_audit else None,
    )
    earnings_dates = seed_filings(session, sessions=sessions) if with_filings else []
    if with_share_counts:
        seed_share_counts(session, sessions=sessions)

    cik_by_ticker = tuple(
        (ticker_for(i), cik_for(i)) for i in range(N_SECURITIES)
    )
    return World(
        as_of=sessions[-1],
        context=CohortContext(
            universe_slug=UNIVERSE_SLUG,
            price_snapshot_slug=snapshot_slug,
            benchmark_security_uid=BENCH_UID,
            cik_by_ticker=cik_by_ticker,
        ),
        sessions=tuple(sessions),
        tickers=tuple(ticker_for(i) for i in range(N_SECURITIES)),
        gap_sessions=tuple(gap_sessions),
        earnings_dates=tuple(earnings_dates),
        delisted_ticker=ticker_for(DELISTED_INDEX),
        delisting_date=delisting_date,
        merged_ticker=ticker_for(MERGED_INDEX),
        late_eps_ticker=ticker_for(LATE_EPS_INDEX),
        no_shares_ticker=ticker_for(NO_SHARES_INDEX),
        cik_by_ticker=cik_by_ticker,
    )


def settings_for(world: World, database_url: str, **overrides):
    """Workspace settings pointed at this world, with the Spec N flag on."""
    from config.settings import Settings

    settings = Settings()
    settings.database_url = database_url
    settings.workspace_api_enabled = True
    settings.comparable_setups_enabled = True
    settings.comparable_universe_slug = world.context.universe_slug
    settings.comparable_price_snapshot = world.context.price_snapshot_slug
    settings.comparable_benchmark_security_uid = world.context.benchmark_security_uid
    settings.comparable_cik_map = ",".join(
        f"{t}:{c}" for t, c in world.cik_by_ticker
    )
    settings.comparable_quick_bootstrap_reps = 200
    settings.comparable_full_bootstrap_reps = 200
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def dump(obj) -> str:
    return json.dumps(obj, sort_keys=True, indent=2, default=str)
