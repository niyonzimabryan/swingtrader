"""The impure half of snapshot construction (Spec Q §6, PR 2 requirement 1).

``strategy_lab/snapshots.py`` states the rules and holds no session. This module
holds the session, reads the tables that already exist, and hands normalized
value objects to those rules. Keeping the split explicit is what makes every
point-in-time rule testable with hand-written numbers instead of a database.

**No second ledger.** The bitemporal source-observation ledger already exists —
``source_observations``, with ``valid_at``, ``known_at_utc``, ``precision``,
``provenance_class`` and ``replay_eligible``, written through
``filings/observations.py``. Snapshots reference those rows' ids, exactly as
``market_snapshots.source_observation_ids_json`` anticipates. Nothing here
writes an observation.

**Prices are archival until a source proves otherwise.** Spec Q §6 is explicit:
"Current cached OHLC and historical earnings rows are not automatically
point-in-time evidence: prices may carry later corporate-action adjustments,
universe membership may contain survivorship bias… Replays using those rows are
labeled ``archival_reconstructed`` and cannot support promotion." No price
source in this repository currently carries availability or revision
provenance, so :data:`REPLAY_ELIGIBLE_PRICE_SOURCES` is **empty** and every
snapshot this builder produces from stored bars is ``archival_reconstructed``
and exploratory. That is not a defect to work around: it is the honest state of
the price plane, and the way to change it is to land a licensed archival source
with availability/revision provenance (or forward-collect the observations) and
add its name to that set — not to relax the flag.

**Membership is filtered on both times.** ``data/prices/store.members_as_of``
applies the ``member_from <= day < member_to`` interval; a snapshot also has to
apply ``known_at_utc <= cutoff``, because the month-end liquidity rank that
produced a membership row could not have been computed before that month-end
closed. Both halves are applied here.

This module cannot import ``data/`` or ``filings/`` — Spec Q §5 keeps the
Strategy Lab's first-party surface to ``utils`` plus, for the two modules that
hold a session, ``database``. The queries it needs are three ``select``
statements, and the alternative — widening the Strategy Lab's import graph to
reach the whole evidence plane — buys nothing and costs the boundary. Where a
definition is shared with those modules it is restated with the source named,
and a test asserts the two agree.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Sequence

from sqlalchemy import or_, select

from database import models
from strategy_lab import registry, snapshots
from strategy_lab.domain import MarketSnapshot, sha256_of
from utils.logger import get_logger

log = get_logger("strategy_lab_snapshot_builder")

__all__ = [
    "REPLAY_ELIGIBLE_PRICE_SOURCES",
    "DEFAULT_SESSION_WINDOW",
    "SnapshotBuildError",
    "build_ticker_snapshot",
    "build_universe_snapshot",
    "record",
]


class SnapshotBuildError(snapshots.SnapshotError):
    """The stored data cannot produce the snapshot that was asked for."""


#: Price sources whose bars carry availability/revision provenance. Empty until
#: one exists; see the module docstring.
REPLAY_ELIGIBLE_PRICE_SOURCES: frozenset[str] = frozenset()

#: 253 bars: ``momentum_v1`` needs a ``T-252`` close to anchor its formation
#: window on, which is the longest window any V1 arm asks for.
DEFAULT_SESSION_WINDOW = 253

#: How far back to look for the earnings components that make up one record.
#: A quarter plus a fortnight: long enough for a 10-Q's EPS to land after its
#: 8-K, short enough that two quarters cannot be spliced together.
EARNINGS_LOOKBACK_DAYS = 105


# --------------------------------------------------------------------------- #
# Prices and membership
# --------------------------------------------------------------------------- #


def _price_provenance(sources: Sequence[str]) -> tuple[str, bool]:
    """``(provenance_class, replay_eligible)`` for a series' source names."""
    distinct = {s for s in sources if s}
    if distinct and distinct <= REPLAY_ELIGIBLE_PRICE_SOURCES:
        return snapshots.PROVENANCE_VENDOR_PIT, True
    return snapshots.PROVENANCE_ARCHIVAL, False


def _load_bars(
    session, security_uid: str, cutoff: datetime, window: int
) -> tuple[tuple[snapshots.SnapshotBar, ...], tuple[str, ...]]:
    """The last ``window`` *completed* sessions at or before the cutoff.

    "Completed" is the part that needs saying. A build running at 20:30 UTC
    would otherwise pick up a bar for a session whose close
    (``snapshots.SESSION_CLOSE_UTC``, 21:00) had not arrived, and the pure rules
    would — correctly — refuse the whole snapshot. So the still-forming session
    is dropped here rather than raised on, and one extra row is fetched so
    dropping it still leaves a full window.

    That close instant is a fixed 21:00 UTC, mirroring the price plane. Under
    US daylight saving the real close is 20:00 UTC, so a build between 20:00 and
    21:00 in summer sees the previous session rather than the one that has just
    closed. That is the conservative direction — it can only withhold
    information, never grant it early — and it is the price of not carrying a
    second exchange calendar. A snapshot built after 21:00 UTC, which is when
    every scheduled evaluation runs, is unaffected.
    """
    rows = session.execute(
        select(models.PriceBar)
        .where(
            models.PriceBar.security_uid == security_uid,
            models.PriceBar.session_date <= cutoff.date(),
        )
        .order_by(models.PriceBar.session_date.desc())
        .limit(window + 1)
    ).scalars().all()
    complete = [
        row for row in rows if snapshots.session_close_utc(row.session_date) <= cutoff
    ]
    ordered = sorted(complete, key=lambda row: row.session_date)[-window:]
    bars = tuple(
        snapshots.SnapshotBar(
            session_date=row.session_date,
            raw_open=row.raw_open,
            raw_high=row.raw_high,
            raw_low=row.raw_low,
            raw_close=row.raw_close,
            volume=row.volume,
            split_adjusted_close=row.split_adjusted_close,
            total_return_close=row.total_return_close,
        )
        for row in ordered
    )
    return bars, tuple(row.source for row in ordered)


def _security_uid_for(session, ticker: str, cutoff_day: date) -> str | None:
    """The security uid a ticker referred to at the cutoff, not today.

    ``securities`` carries a validity interval per ticker so a rename does not
    silently split a history. The most recent interval that had started by the
    cutoff and had not ended wins; a ticker with no security master row falls
    back to the uid on its own bars, which is what a fixture plane produces.
    """
    rows = session.execute(
        select(models.Security)
        .where(models.Security.ticker == ticker)
        .order_by(models.Security.ticker_valid_from)
    ).scalars().all()
    for row in reversed(rows):
        starts_ok = row.ticker_valid_from is None or row.ticker_valid_from <= cutoff_day
        ends_ok = row.ticker_valid_to is None or row.ticker_valid_to > cutoff_day
        if starts_ok and ends_ok:
            return row.security_uid
    fallback = session.execute(
        select(models.PriceBar.security_uid)
        .where(models.PriceBar.ticker == ticker)
        .limit(1)
    ).scalars().first()
    return fallback


def _members_as_of(session, universe_slug: str, day: date, cutoff: datetime):
    """Membership under *both* times: the interval and ``known_at_utc``."""
    return session.execute(
        select(models.UniverseMembership)
        .where(
            models.UniverseMembership.universe_slug == universe_slug,
            models.UniverseMembership.member_from <= day,
            or_(
                models.UniverseMembership.member_to.is_(None),
                models.UniverseMembership.member_to > day,
            ),
            models.UniverseMembership.known_at_utc <= cutoff,
        )
        .order_by(models.UniverseMembership.ticker, models.UniverseMembership.security_uid)
    ).scalars().all()


# --------------------------------------------------------------------------- #
# Observations
# --------------------------------------------------------------------------- #


def _to_fact(row) -> snapshots.ObservationFact:
    try:
        payload = json.loads(row.payload_json or "{}")
    except (TypeError, ValueError):
        payload = {}
    return snapshots.ObservationFact(
        observation_id=row.id,
        fact_type=row.fact_type,
        ticker=row.ticker_at_time,
        valid_at=row.valid_at,
        known_at_utc=row.known_at_utc,
        precision=row.precision,
        provenance_class=row.provenance_class,
        replay_eligible=bool(row.replay_eligible),
        source=row.source,
        source_trust=row.source_trust,
        value_numeric=row.value_numeric,
        value_text=row.value_text,
        unit=row.unit,
        payload=payload,
    )


def _observations(
    session, ticker: str, fact_types: Sequence[str], cutoff: datetime, since: datetime | None
):
    """``known_at_utc <= cutoff``, the only sanctioned bitemporal filter.

    Mirrors ``filings/observations.py::observations_known_at`` — same predicate,
    same ordering — restated here for the import boundary in the module
    docstring, with a test asserting the two return the same rows.
    """
    statement = select(models.SourceObservation).where(
        models.SourceObservation.known_at_utc <= cutoff,
        models.SourceObservation.ticker_at_time == ticker,
        models.SourceObservation.fact_type.in_(list(fact_types)),
    )
    if since is not None:
        statement = statement.where(models.SourceObservation.valid_at >= since)
    return session.execute(
        statement.order_by(
            models.SourceObservation.valid_at.asc(),
            models.SourceObservation.known_at_utc.asc(),
            models.SourceObservation.id.asc(),
        )
    ).scalars().all()


def _earnings_record(session, ticker: str, cutoff: datetime) -> snapshots.EarningsRecord | None:
    """Assemble the Spec Q §7B structured record, or return ``None``.

    Three components, all of which must be knowable at the cutoff: the 8-K
    Item 2.02 acceptance that dates the event, a reported EPS fact, and a
    consensus EPS fact. Missing any one of them means there is no record — the
    strategy must never see a half-built one and never impute the third value.

    The record's ``known_at_utc`` is the **latest** of the three, because that
    is when it first became actionable as a whole.
    """
    since = cutoff - timedelta(days=EARNINGS_LOOKBACK_DAYS)
    events = _observations(session, ticker, [snapshots.FACT_EARNINGS_RELEASE], cutoff, since)
    if not events:
        return None
    event = events[-1]

    reported_rows = _observations(
        session, ticker, [snapshots.FACT_EPS_DILUTED, snapshots.FACT_EPS_BASIC], cutoff, since
    )
    reported = next(
        (r for r in reversed(reported_rows) if r.value_numeric is not None), None
    )
    consensus_rows = _observations(
        session, ticker, [snapshots.FACT_CONSENSUS_EPS], cutoff, since
    )
    consensus = next(
        (r for r in reversed(consensus_rows) if r.value_numeric is not None), None
    )
    if reported is None or consensus is None:
        return None

    components = (event, reported, consensus)
    return snapshots.EarningsRecord(
        ticker=ticker,
        event_date=event.valid_at.date(),
        event_known_at_utc=event.known_at_utc,
        reported_eps=float(reported.value_numeric),
        consensus_eps=float(consensus.value_numeric),
        known_at_utc=max(row.known_at_utc for row in components),
        observation_ids=tuple(sorted(row.id for row in components)),
        provenance_classes=tuple(sorted({row.provenance_class for row in components})),
        replay_eligible=all(bool(row.replay_eligible) for row in components),
        sources=tuple(sorted({row.source for row in components})),
    )


# --------------------------------------------------------------------------- #
# The frozen composite result
# --------------------------------------------------------------------------- #


def _composite_result(
    session, ticker: str, cutoff: datetime
) -> snapshots.CompositeScoringResult | None:
    """Freeze the pipeline's already-produced output for the compatibility arm.

    The score, cohort, direction and ``memo_generated`` come from the
    ``scored_candidates`` row — the shadow calibration ledger the pipeline
    writes for *every* ticker that reaches scoring. The classification, trade
    parameters and signal breakdown come from the ``memos`` row when the
    pipeline produced one, because that is where the full trade plan lives.

    ``portfolio_context_hash`` is a hash of the sizing context the scorer
    actually recorded — regime multiplier, conviction multiplier, volatility
    adjustment and position percentage. The pipeline does not persist a
    complete portfolio context, so this is what is available; the gap is named
    in the provenance block rather than papered over with a fabricated hash.
    """
    scored = session.execute(
        select(models.ScoredCandidate)
        .where(
            models.ScoredCandidate.ticker == ticker,
            models.ScoredCandidate.scored_at <= cutoff,
        )
        .order_by(models.ScoredCandidate.scored_at.desc(), models.ScoredCandidate.id.desc())
        .limit(1)
    ).scalars().first()
    if scored is None:
        return None

    memo = session.execute(
        select(models.Memo)
        .join(models.Ticker, models.Memo.ticker_id == models.Ticker.id)
        .where(
            models.Ticker.symbol == ticker,
            models.Memo.created_at <= cutoff,
            models.Memo.created_at >= scored.scored_at,
        )
        .order_by(models.Memo.created_at.desc(), models.Memo.id.desc())
        .limit(1)
    ).scalars().first()

    trade_params = memo.trade_params_dict if memo is not None else {}
    signal_breakdown = memo.signal_breakdown_dict if memo is not None else {
        "catalyst": scored.catalyst_score,
        "fundamental": scored.fundamental_score,
        "pattern": scored.pattern_score,
        "web_research": scored.web_research_score,
    }
    sizing_context = {
        key: trade_params.get(key)
        for key in (
            "regime_multiplier", "conviction_multiplier", "vol_adjustment", "position_pct",
        )
    }
    return snapshots.CompositeScoringResult(
        ticker=ticker,
        scored_at=scored.scored_at,
        run_id=scored.run_id or "",
        final_score=float(scored.final_score or 0.0),
        classification=(memo.classification if memo is not None else "") or "",
        direction=scored.direction or "",
        cohort=scored.cohort or "",
        memo_generated=bool(scored.memo_generated),
        signal_breakdown=signal_breakdown,
        trade_params=trade_params,
        model_provenance={
            "memo_id": memo.id if memo is not None else None,
            "run_id": scored.run_id or "",
            "scored_candidate_id": scored.id,
            "source": scored.source or "",
            "regime": scored.regime or "",
        },
        portfolio_context_hash=sha256_of(sizing_context),
    )


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #


def _ticker_inputs(
    session,
    ticker: str,
    *,
    cutoff: datetime,
    window: int,
    security_uid: str | None = None,
    membership_known_at: datetime | None = None,
    with_earnings: bool = True,
    with_composite: bool = False,
) -> snapshots.TickerInputs:
    symbol = ticker.strip().upper()
    uid = security_uid or _security_uid_for(session, symbol, cutoff.date())
    if uid is None:
        bars, sources = (), ()
    else:
        bars, sources = _load_bars(session, uid, cutoff, window)
    provenance_class, replay_eligible = _price_provenance(sources)
    return snapshots.TickerInputs(
        ticker=symbol,
        bars=bars,
        facts=(),
        earnings=_earnings_record(session, symbol, cutoff) if with_earnings else None,
        composite=_composite_result(session, symbol, cutoff) if with_composite else None,
        price_source=",".join(sorted({s for s in sources if s})) or "unknown",
        price_provenance_class=provenance_class,
        price_replay_eligible=replay_eligible,
        membership_known_at_utc=membership_known_at,
        # No corporate-action feed carries a delisting flag for a live cutoff,
        # so the honest answer is "unknown" and the snapshot says so.
        delisting_known=False,
    )


def build_ticker_snapshot(
    session,
    ticker: str,
    *,
    cutoff: datetime,
    window: int = DEFAULT_SESSION_WINDOW,
    with_earnings: bool = True,
    with_composite: bool = False,
) -> MarketSnapshot:
    """One name at one cutoff, from the tables that already hold the data."""
    inputs = _ticker_inputs(
        session, ticker,
        cutoff=cutoff, window=window,
        with_earnings=with_earnings, with_composite=with_composite,
    )
    if not inputs.bars and inputs.composite is None and inputs.earnings is None:
        raise SnapshotBuildError(
            f"{ticker}: no bars, no earnings record and no scored result at or "
            f"before {cutoff.isoformat()}; there is nothing to snapshot"
        )
    return snapshots.build_ticker_snapshot(
        as_of_utc=cutoff,
        data_cutoff_utc=cutoff,
        inputs=inputs,
        provenance=_provenance(
            scope="ticker",
            cutoff=cutoff,
            window=window,
            price_sources=(inputs.price_source,),
            universe_slug=None,
        ),
    )


def build_universe_snapshot(
    session,
    *,
    cutoff: datetime,
    universe_slug: str,
    window: int = DEFAULT_SESSION_WINDOW,
    with_earnings: bool = False,
) -> MarketSnapshot:
    """The whole point-in-time constituent set under one cutoff.

    Membership comes from ``universe_membership`` as of the cutoff — never from
    today's list, which is survivorship bias wearing a disguise. A member with
    no bars at or before the cutoff is recorded as an *exclusion* with its
    reason rather than dropped: Spec Q §6 requires the exclusions to travel with
    the snapshot, and a constituent that silently disappears is a constituent
    nobody can audit.
    """
    members = _members_as_of(session, universe_slug, cutoff.date(), cutoff)
    if not members:
        raise SnapshotBuildError(
            f"{universe_slug}: no membership rows knowable at "
            f"{cutoff.isoformat()}; a universe snapshot cannot be built from an "
            "empty constituent set"
        )

    inputs: list[snapshots.TickerInputs] = []
    exclusions: list[tuple[str, str]] = []
    seen: set[str] = set()
    for member in members:
        symbol = (member.ticker or "").strip().upper()
        if symbol in seen:
            # Two securities carrying one ticker at the same cutoff. The first
            # by (ticker, security_uid) wins and the other is recorded, because
            # a constituent that silently disappears is one nobody can audit.
            exclusions.append((symbol, f"duplicate_ticker_for_{member.security_uid}"))
            continue
        seen.add(symbol)
        entry = _ticker_inputs(
            session, symbol,
            cutoff=cutoff, window=window,
            security_uid=member.security_uid,
            membership_known_at=member.known_at_utc,
            with_earnings=with_earnings,
            with_composite=False,
        )
        if not entry.bars:
            exclusions.append((symbol, "no_bars_at_or_before_cutoff"))
            continue
        inputs.append(entry)

    if not inputs:
        raise SnapshotBuildError(
            f"{universe_slug}: every constituent was excluded at "
            f"{cutoff.isoformat()} ({len(exclusions)} with no bars)"
        )

    membership_as_of = max(
        (m.known_at_utc for m in members if m.known_at_utc is not None),
        default=None,
    )
    version = f"{universe_slug}@{cutoff.date().isoformat()}"
    return snapshots.build_universe_snapshot(
        as_of_utc=cutoff,
        data_cutoff_utc=cutoff,
        universe_version=version,
        inputs=inputs,
        provenance=_provenance(
            scope="universe",
            cutoff=cutoff,
            window=window,
            price_sources=tuple(sorted({e.price_source for e in inputs})),
            universe_slug=universe_slug,
            membership_known_at=membership_as_of,
        ),
        exclusions=exclusions,
    )


def _provenance(
    *,
    scope: str,
    cutoff: datetime,
    window: int,
    price_sources: Sequence[str],
    universe_slug: str | None,
    membership_known_at: datetime | None = None,
) -> dict:
    return {
        "builder": "strategy_lab.snapshot_builder",
        "scope": scope,
        "cutoff_utc": cutoff.isoformat(),
        "session_window": window,
        "price_sources": sorted({s for s in price_sources if s}),
        "price_replay_eligible_sources": sorted(REPLAY_ELIGIBLE_PRICE_SOURCES),
        "universe_slug": universe_slug,
        "membership_known_at_utc": (
            membership_known_at.isoformat() if membership_known_at else None
        ),
        "observation_ledger": "source_observations",
        "known_gaps": [
            "price_bars carry no availability or revision provenance, so every "
            "snapshot built from them is archival_reconstructed (Spec Q §6)",
            "the scoring pipeline does not persist a full portfolio context; the "
            "composite arm's portfolio_context_hash covers the recorded sizing "
            "context only",
        ],
    }


def record(session, snapshot: MarketSnapshot):
    """Persist through the registry, which owns every experiment-table write."""
    return registry.record_snapshot(session, snapshot)
