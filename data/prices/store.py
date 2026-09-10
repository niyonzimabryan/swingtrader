"""Persistence for the price plane: the five Phase 3p tables, idempotently.

Every writer here is safe to re-run. A backfill that cannot be re-run is a
backfill nobody dares re-run, and the whole point of a stored price file is that
the same `--since` produces the same rows.

Idempotency is by natural key, not by truncate-and-reload:

  * `securities`          `(security_uid, ticker, ticker_valid_from)`
  * `price_bars`          `(security_uid, session_date)`
  * `corporate_actions`   `(security_uid, ex_date, action_type, source)`
  * `universe_membership` `(universe_slug, security_uid, member_from)`
  * `price_snapshots`     `(snapshot_slug)`

Read helpers live here too, so that everything downstream — the covariates, the
universe job, the audit — works from *stored rows*, which is the only way to know
the stored rows are enough.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Iterable, Sequence

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from data.prices.base import (
    CorporateActionRecord,
    DailyBar,
    MembershipInterval,
    SecurityMasterRow,
)
from database.models import (
    CorporateActionRow,
    PriceBar,
    PriceSnapshot,
    Security,
    UniverseMembership,
)
from utils.timeutils import utcnow_naive


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #


def upsert_securities(session: Session, rows: Iterable[SecurityMasterRow]) -> int:
    written = 0
    for row in rows:
        existing = session.execute(
            select(Security).where(
                Security.security_uid == row.security_uid,
                Security.ticker == row.ticker,
                Security.ticker_valid_from == row.ticker_valid_from,
            )
        ).scalars().first()
        target = existing or Security(
            security_uid=row.security_uid,
            ticker=row.ticker,
            ticker_valid_from=row.ticker_valid_from,
        )
        target.ticker_valid_to = row.ticker_valid_to
        target.name = row.name
        target.exchange = row.exchange
        target.venue = row.venue
        target.listing_date = row.listing_date
        target.delisting_date = row.delisting_date
        target.delisting_reason = row.delisting_reason
        target.source = row.source
        target.ingested_at = utcnow_naive()
        if existing is None:
            session.add(target)
        written += 1
    session.flush()
    return written


def upsert_bars(session: Session, bars: Iterable[DailyBar]) -> int:
    written = 0
    for bar in bars:
        existing = session.execute(
            select(PriceBar).where(
                PriceBar.security_uid == bar.security_uid,
                PriceBar.session_date == bar.session_date,
            )
        ).scalars().first()
        target = existing or PriceBar(
            security_uid=bar.security_uid, session_date=bar.session_date
        )
        target.ticker = bar.ticker
        target.raw_open = bar.raw_open
        target.raw_high = bar.raw_high
        target.raw_low = bar.raw_low
        target.raw_close = bar.raw_close
        target.volume = bar.volume
        target.split_factor = bar.split_factor
        target.dividend_cash = bar.dividend_cash
        target.split_adjusted_close = bar.split_adjusted_close
        target.total_return_close = bar.total_return_close
        target.source = bar.source
        target.ingested_at = utcnow_naive()
        if existing is None:
            session.add(target)
        written += 1
    session.flush()
    return written


def upsert_corporate_actions(session: Session, actions: Iterable[CorporateActionRecord]) -> int:
    written = 0
    for action in actions:
        existing = session.execute(
            select(CorporateActionRow).where(
                CorporateActionRow.security_uid == action.security_uid,
                CorporateActionRow.ex_date == action.ex_date,
                CorporateActionRow.action_type == action.action_type,
                CorporateActionRow.source == action.source,
            )
        ).scalars().first()
        target = existing or CorporateActionRow(
            security_uid=action.security_uid,
            ex_date=action.ex_date,
            action_type=action.action_type,
            source=action.source,
        )
        target.ticker = action.ticker
        target.value = action.value
        target.ingested_at = utcnow_naive()
        if existing is None:
            session.add(target)
        written += 1
    session.flush()
    return written


def replace_universe(
    session: Session, universe_slug: str, intervals: Sequence[MembershipInterval]
) -> int:
    """Rewrite one universe's membership.

    A universe is a *rule applied to a snapshot*, so a partial update makes no
    sense: recomputing `liquid_us_equity_v1` from a longer price history should
    leave the table holding the new answer, not the union of two. Rows for other
    slugs are untouched.
    """
    for row in session.execute(
        select(UniverseMembership).where(UniverseMembership.universe_slug == universe_slug)
    ).scalars():
        session.delete(row)
    session.flush()

    for interval in intervals:
        session.add(UniverseMembership(
            universe_slug=interval.universe_slug,
            security_uid=interval.security_uid,
            ticker=interval.ticker,
            member_from=interval.member_from,
            member_to=interval.member_to,
            source=interval.source,
            known_at_utc=_naive_utc(interval.known_at_utc),
        ))
    session.flush()
    return len(intervals)


def record_snapshot(
    session: Session,
    snapshot_slug: str,
    source: str,
    coverage_summary: dict,
    delisting_audit: dict | None = None,
) -> PriceSnapshot:
    """Create or update a named snapshot row.

    `delisting_audit=None` leaves whatever audit is already recorded alone, so
    re-running a backfill does not wipe the audit that justified the file.
    """
    snapshot = session.execute(
        select(PriceSnapshot).where(PriceSnapshot.snapshot_slug == snapshot_slug)
    ).scalars().first()
    if snapshot is None:
        snapshot = PriceSnapshot(snapshot_slug=snapshot_slug, created_at=utcnow_naive())
        session.add(snapshot)
    snapshot.source = source
    snapshot.coverage_summary_json = json.dumps(coverage_summary, sort_keys=True, default=str)
    if delisting_audit is not None:
        snapshot.delisting_audit_json = json.dumps(delisting_audit, sort_keys=True, default=str)
    elif snapshot.delisting_audit_json is None:
        snapshot.delisting_audit_json = "{}"
    if snapshot.coverage_summary_json is None:
        snapshot.coverage_summary_json = "{}"
    session.flush()
    return snapshot


# --------------------------------------------------------------------------- #
# Readers
# --------------------------------------------------------------------------- #


def load_bars(
    session: Session,
    security_uid: str,
    start: date | None = None,
    end: date | None = None,
) -> tuple[DailyBar, ...]:
    statement = select(PriceBar).where(PriceBar.security_uid == security_uid)
    if start is not None:
        statement = statement.where(PriceBar.session_date >= start)
    if end is not None:
        statement = statement.where(PriceBar.session_date <= end)
    rows = session.execute(statement.order_by(PriceBar.session_date)).scalars().all()
    return tuple(_to_bar(row) for row in rows)


def load_all_bars(
    session: Session, start: date | None = None, end: date | None = None
) -> dict[str, tuple[DailyBar, ...]]:
    statement = select(PriceBar)
    if start is not None:
        statement = statement.where(PriceBar.session_date >= start)
    if end is not None:
        statement = statement.where(PriceBar.session_date <= end)
    grouped: dict[str, list[DailyBar]] = {}
    for row in session.execute(
        statement.order_by(PriceBar.security_uid, PriceBar.session_date)
    ).scalars():
        grouped.setdefault(row.security_uid, []).append(_to_bar(row))
    return {uid: tuple(bars) for uid, bars in grouped.items()}


def _to_bar(row: PriceBar) -> DailyBar:
    return DailyBar(
        security_uid=row.security_uid,
        ticker=row.ticker,
        session_date=row.session_date,
        raw_open=row.raw_open,
        raw_high=row.raw_high,
        raw_low=row.raw_low,
        raw_close=row.raw_close,
        volume=row.volume,
        split_factor=row.split_factor,
        dividend_cash=row.dividend_cash,
        split_adjusted_close=row.split_adjusted_close,
        total_return_close=row.total_return_close,
        source=row.source,
    )


def session_dates(session: Session, start: date | None = None, end: date | None = None) -> tuple[date, ...]:
    """Distinct session dates present in `price_bars`, ascending."""
    statement = select(PriceBar.session_date).distinct()
    if start is not None:
        statement = statement.where(PriceBar.session_date >= start)
    if end is not None:
        statement = statement.where(PriceBar.session_date <= end)
    return tuple(sorted(session.execute(statement).scalars().all()))


def members_as_of(session: Session, universe_slug: str, day: date) -> tuple[UniverseMembership, ...]:
    """The stored point-in-time filter: `member_from <= day < member_to`.

    Both halves are in SQL, on `ix_universe_membership_slug_window`. The open
    interval is `member_to IS NULL`, and it has to be spelled that way rather
    than left to a comparison: `NULL > day` is NULL, not true, on both engines,
    so a still-current member would silently vanish from every query.
    """
    return tuple(session.execute(
        select(UniverseMembership).where(
            UniverseMembership.universe_slug == universe_slug,
            UniverseMembership.member_from <= day,
            or_(UniverseMembership.member_to.is_(None), UniverseMembership.member_to > day),
        ).order_by(UniverseMembership.ticker, UniverseMembership.security_uid)
    ).scalars().all())


def load_securities(session: Session, tickers: Sequence[str] | None = None) -> tuple[Security, ...]:
    statement = select(Security)
    if tickers is not None:
        statement = statement.where(Security.ticker.in_(list(tickers)))
    return tuple(session.execute(
        statement.order_by(Security.ticker, Security.ticker_valid_from)
    ).scalars().all())


def get_snapshot(session: Session, snapshot_slug: str) -> PriceSnapshot | None:
    return session.execute(
        select(PriceSnapshot).where(PriceSnapshot.snapshot_slug == snapshot_slug)
    ).scalars().first()


# --------------------------------------------------------------------------- #
# Truncation, for the Spec N §10 lookahead harness
# --------------------------------------------------------------------------- #


def delete_bars_after(session: Session, day: date) -> int:
    """Remove every bar for a session later than `day`.

    A price bar is a fact like any other, and its `known_at` is its session
    date. `comparables/lookahead.py` deletes the future ones and rebuilds the
    cohort to prove no covariate reached forward for them. **Call it inside a
    savepoint you intend to roll back**; nothing else should call it.
    """
    deleted = session.query(PriceBar).filter(PriceBar.session_date > day).delete(
        synchronize_session=False
    )
    session.flush()
    return int(deleted or 0)


def delete_membership_after(session: Session, cutoff: datetime) -> int:
    """Remove membership rows that were not knowable at `cutoff`.

    Same contract as `delete_bars_after`: harness only, inside a savepoint.
    A universe reconstruction published after the event is exactly the kind of
    lookahead §4.2 exists to catch, so the harness deletes it and looks.
    """
    deleted = session.query(UniverseMembership).filter(
        UniverseMembership.known_at_utc > _naive_utc(cutoff)
    ).delete(synchronize_session=False)
    session.flush()
    return int(deleted or 0)
