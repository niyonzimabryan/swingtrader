"""Entity resolution with history — Spec O section 3.2.

The unglamorous part of the filings plane, and the one that silently corrupts
everything downstream when it is skipped.

**Tickers are reused and companies rename.** EDGAR's ``company_tickers.json``
and the ``tickers`` array on a ``submissions`` document are *current*
snapshots. Joining a 2019 event to a current snapshot resolves the symbol to
whoever holds it today — no error, no warning, just the wrong company. Phase 3a
knew this and stamped every row it wrote with ``ticker_from_current_snapshot``
rather than pretend otherwise; this module is what lets those warnings be
answered.

Two bases, and they are not equally good:

``submissions_former_names``
    EDGAR states ``from`` and ``to`` on every entry in the ``formerNames``
    array. A real dated range, from the regulator.
``filing_cover_page``
    The filer wrote its own trading symbol on a filing, and EDGAR dated that
    filing to the second. Form 4's ``issuerTradingSymbol`` is the common case,
    and it is what makes historical ticker coverage possible at all: a
    company that has filed Form 4s for a decade has a decade of dated symbol
    statements from the regulator's own archive.

``snapshot_observed``
    We saw a value in a snapshot on one date and a different one in a later
    snapshot. The interval is bounded by *our observations*, so ``valid_from``
    is an upper bound on when the mapping actually began — the ticker may have
    been in use long before we first looked. The row says so
    (``valid_from_is_first_observation``), and :func:`ticker_at` refuses to
    answer for a date before the first observation rather than extrapolating
    backwards. A wrong ticker is worse than no ticker: the first is a silent
    join to another company's prices, the second is a gap you can count.

Nothing here calls a model, and nothing here places an order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable

from database.models import EntityHistory, SourceObservation
from filings import client as sec_client
from filings.errors import AdapterSchemaError
from utils.logger import get_logger

log = get_logger("filings_entities")

ATTRIBUTE_TICKER = "ticker"
ATTRIBUTE_NAME = "name"
ATTRIBUTES = frozenset({ATTRIBUTE_TICKER, ATTRIBUTE_NAME})

BASIS_FORMER_NAMES = "submissions_former_names"
BASIS_SNAPSHOT = "snapshot_observed"
#: The trading symbol a filer wrote on a filing's cover page, dated by that
#: filing's acceptance. Form 4's ``issuerTradingSymbol`` is the common case.
#: This is a *real* dated observation — the regulator holds a document in which
#: the company stated its own symbol on that day — so it is much stronger than
#: a snapshot, and it is what gives historical ticker coverage at all.
BASIS_FILING_COVER_PAGE = "filing_cover_page"

#: Bases whose intervals are bounded by *observations* rather than stated by
#: the source. An open one is closed when a later observation shows a different
#: value; a dated interval from ``formerNames`` never is, because EDGAR's own
#: dates beat ours.
OBSERVATIONAL_BASES = frozenset({BASIS_SNAPSHOT, BASIS_FILING_COVER_PAGE})

SOURCE_SUBMISSIONS = "sec_submissions"

#: The warning Phase 3a stamps on every row whose ticker came from the current
#: snapshot. :func:`resolve_snapshot_warnings` clears it where history can
#: answer.
WARN_TICKER_SNAPSHOT = "ticker_from_current_snapshot"
#: Set when a resolution was attempted for a date this CIK has no interval for.
WARN_TICKER_UNRESOLVED = "ticker_unresolved_at_event_date"


class EntityResolutionError(RuntimeError):
    """A resolution was asked for something that cannot be answered honestly."""


# --- parsing the submissions document --------------------------------------


@dataclass(frozen=True)
class Interval:
    """One (attribute, value) mapping and the range it applied to."""

    entity_cik: str
    attribute: str
    value: str
    valid_from: date
    valid_to: date | None
    basis: str
    exchange: str | None = None
    valid_from_is_first_observation: bool = False

    def covers(self, when: date) -> bool:
        """Half-open: ``valid_from <= when < valid_to``, open-ended if no end."""
        if when < self.valid_from:
            return False
        return self.valid_to is None or when < self.valid_to


def _parse_edgar_stamp(raw: Any, *, where: str) -> datetime | None:
    """``2019-08-06T00:00:00.000Z`` -> aware UTC datetime, or ``None``."""
    if raw in (None, ""):
        return None
    if not isinstance(raw, str):
        raise AdapterSchemaError(f"{where}: expected an ISO timestamp, got {raw!r}")
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise AdapterSchemaError(f"{where}: unparseable timestamp {raw!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def former_name_intervals(payload: Any) -> list[Interval]:
    """Dated name intervals from ``submissions.formerNames``.

    EDGAR gives ``{"name": ..., "from": ..., "to": ...}`` with a null ``to`` on
    the entry that ran until the current name took over. The current name gets
    an interval starting where the last former name ended.
    """
    if not isinstance(payload, dict):
        raise AdapterSchemaError("submissions: expected an object")
    cik = sec_client.normalise_cik(payload.get("cik", ""))
    raw_former = payload.get("formerNames")
    if raw_former is None:
        raw_former = []
    if not isinstance(raw_former, list):
        raise AdapterSchemaError("submissions: 'formerNames' is not an array")

    out: list[Interval] = []
    latest_end: date | None = None
    for index, entry in enumerate(raw_former):
        where = f"submissions.formerNames[{index}]"
        if not isinstance(entry, dict) or "name" not in entry:
            raise AdapterSchemaError(f"{where}: unexpected entry {entry!r}")
        start = _parse_edgar_stamp(entry.get("from"), where=f"{where}.from")
        end = _parse_edgar_stamp(entry.get("to"), where=f"{where}.to")
        if start is None:
            # A former name with no start date carries no interval we can trust.
            log.warning("former_name_without_start", cik=cik, name=entry.get("name"))
            continue
        out.append(
            Interval(
                entity_cik=cik,
                attribute=ATTRIBUTE_NAME,
                value=str(entry["name"]),
                valid_from=start.date(),
                valid_to=end.date() if end else None,
                basis=BASIS_FORMER_NAMES,
            )
        )
        if end is not None and (latest_end is None or end.date() > latest_end):
            latest_end = end.date()

    current = str(payload.get("name") or "").strip()
    if current and latest_end is not None:
        out.append(
            Interval(
                entity_cik=cik,
                attribute=ATTRIBUTE_NAME,
                value=current,
                valid_from=latest_end,
                valid_to=None,
                basis=BASIS_FORMER_NAMES,
            )
        )
    return sorted(out, key=lambda i: (i.valid_from, i.value))


def snapshot_intervals(payload: Any, *, observed_on: date) -> list[Interval]:
    """The *current* ticker and name, as an interval opening at ``observed_on``.

    This is the honest encoding of a snapshot: "on this date, this CIK's ticker
    was X". Nothing is claimed about before ``observed_on`` — the flag says the
    lower bound is our first sighting, not the mapping's start.
    """
    if not isinstance(payload, dict):
        raise AdapterSchemaError("submissions: expected an object")
    cik = sec_client.normalise_cik(payload.get("cik", ""))
    tickers = payload.get("tickers") or []
    exchanges = payload.get("exchanges") or []
    if not isinstance(tickers, list) or not isinstance(exchanges, list):
        raise AdapterSchemaError("submissions: 'tickers'/'exchanges' is not an array")

    out: list[Interval] = []
    for index, ticker in enumerate(tickers):
        symbol = str(ticker or "").strip().upper()
        if not symbol:
            continue
        out.append(
            Interval(
                entity_cik=cik,
                attribute=ATTRIBUTE_TICKER,
                value=symbol,
                valid_from=observed_on,
                valid_to=None,
                basis=BASIS_SNAPSHOT,
                exchange=str(exchanges[index]) if index < len(exchanges) else None,
                valid_from_is_first_observation=True,
            )
        )
    name = str(payload.get("name") or "").strip()
    if name:
        out.append(
            Interval(
                entity_cik=cik,
                attribute=ATTRIBUTE_NAME,
                value=name,
                valid_from=observed_on,
                valid_to=None,
                basis=BASIS_SNAPSHOT,
                valid_from_is_first_observation=True,
            )
        )
    return out


# --- storing ----------------------------------------------------------------


@dataclass
class HistoryWrite:
    opened: int = 0
    closed: int = 0
    refreshed: int = 0


def record_intervals(
    session,
    intervals: Iterable[Interval],
    *,
    observed_at: datetime,
    source_url: str = "",
) -> HistoryWrite:
    """Merge snapshot and dated intervals into ``entity_history``.

    Observational semantics, and they are the whole reason this function is
    not a plain insert: seeing ticker ``B`` for a CIK whose open interval says
    ``A`` means ``A`` ended somewhere between the two observations. The open
    interval is closed at the new observation date and the new one opens there.
    Re-observing the same value only moves ``last_observed_at``.

    Dated intervals (``formerNames``) are inserted as stated and never closed
    by an observation: EDGAR's own dates beat ours.

    **Order matters.** Intervals must be recorded oldest first, or a later
    value closes an earlier one at the wrong date. Callers that walk a filing
    history sort by acceptance ascending before calling.
    """
    result = HistoryWrite()
    stamp = _naive(observed_at)

    # The session is built with ``autoflush=False``, so a row added earlier in
    # this loop is invisible to the queries below until it is flushed. Without
    # this the interval-closing and still-open checks only ever see the
    # previous *call's* rows, and one batch of cover-page sightings opens one
    # interval per filing.
    session.flush()

    for interval in intervals:
        existing = (
            session.query(EntityHistory)
            .filter(
                EntityHistory.entity_cik == interval.entity_cik,
                EntityHistory.attribute == interval.attribute,
                EntityHistory.value == interval.value,
                EntityHistory.valid_from == interval.valid_from,
            )
            .one_or_none()
        )
        if existing is not None:
            existing.last_observed_at = stamp
            result.refreshed += 1
            continue

        if interval.basis in OBSERVATIONAL_BASES:
            # The same value observed again later is not a new interval: the
            # mapping simply still holds. Without this, a company with ten
            # years of Form 4s gets one interval per filing, all saying the
            # same thing, and the table grows with the archive rather than
            # with the number of actual changes.
            still_open = (
                session.query(EntityHistory)
                .filter(
                    EntityHistory.entity_cik == interval.entity_cik,
                    EntityHistory.attribute == interval.attribute,
                    EntityHistory.value == interval.value,
                    EntityHistory.basis.in_(sorted(OBSERVATIONAL_BASES)),
                    EntityHistory.valid_to.is_(None),
                    EntityHistory.valid_from <= interval.valid_from,
                )
                .first()
            )
            if still_open is not None:
                still_open.last_observed_at = stamp
                # A cover-page sighting upgrades a snapshot interval's basis:
                # the filer stated the symbol on a dated document, which is a
                # stronger claim than "we looked and saw it".
                if (
                    interval.basis == BASIS_FILING_COVER_PAGE
                    and still_open.basis == BASIS_SNAPSHOT
                ):
                    still_open.basis = BASIS_FILING_COVER_PAGE
                    still_open.valid_from = interval.valid_from
                    still_open.valid_from_is_first_observation = False
                result.refreshed += 1
                continue

            open_rows = (
                session.query(EntityHistory)
                .filter(
                    EntityHistory.entity_cik == interval.entity_cik,
                    EntityHistory.attribute == interval.attribute,
                    EntityHistory.basis.in_(sorted(OBSERVATIONAL_BASES)),
                    EntityHistory.valid_to.is_(None),
                    EntityHistory.value != interval.value,
                )
                .all()
            )
            for row in open_rows:
                if row.valid_from <= interval.valid_from:
                    row.valid_to = interval.valid_from
                    result.closed += 1

        session.add(
            EntityHistory(
                entity_cik=interval.entity_cik,  # noqa: E128
                attribute=interval.attribute,
                value=interval.value,
                valid_from=interval.valid_from,
                valid_to=interval.valid_to,
                valid_from_is_first_observation=interval.valid_from_is_first_observation,
                basis=interval.basis,
                exchange=interval.exchange,
                source=SOURCE_SUBMISSIONS,
                source_url=source_url,
                known_at_utc=stamp,
                first_observed_at=stamp,
                last_observed_at=stamp,
            )
        )
        result.opened += 1
        session.flush()

    session.flush()
    return result


def record_submissions(
    session, payload: Any, *, observed_at: datetime, source_url: str = ""
) -> HistoryWrite:
    """Record both bases from one ``submissions`` document."""
    intervals = list(former_name_intervals(payload))
    intervals.extend(snapshot_intervals(payload, observed_on=_naive(observed_at).date()))
    return record_intervals(
        session, intervals, observed_at=observed_at, source_url=source_url
    )


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _as_date(when: date | datetime) -> date:
    return _naive(when).date() if isinstance(when, datetime) else when


# --- resolving --------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    """An answer, or an honest refusal to give one."""

    value: str | None
    basis: str | None = None
    warning: str | None = None
    candidates: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.value is not None


def _rows_for(session, *, attribute: str, when: date, **filters):
    query = session.query(EntityHistory).filter(EntityHistory.attribute == attribute)
    for column, value in filters.items():
        query = query.filter(getattr(EntityHistory, column) == value)
    rows = query.order_by(EntityHistory.valid_from.asc(), EntityHistory.id.asc()).all()
    return [row for row in rows if _covers(row, when)]


def _covers(row: EntityHistory, when: date) -> bool:
    if when < row.valid_from:
        return False
    return row.valid_to is None or when < row.valid_to


def ticker_at(session, entity_cik: str, when: date | datetime) -> Resolution:
    """The ticker this CIK traded under on ``when``.

    Returns an unresolved :class:`Resolution` — never a guess — when no
    interval covers the date. Two CIKs can hold the same symbol in different
    eras, so a "nearest interval" fallback is exactly the bug this table
    exists to prevent.
    """
    cik = sec_client.normalise_cik(entity_cik)
    day = _as_date(when)
    rows = _rows_for(session, attribute=ATTRIBUTE_TICKER, when=day, entity_cik=cik)
    if not rows:
        return Resolution(None, warning=WARN_TICKER_UNRESOLVED)
    # A CIK genuinely can have several tickers at once (share classes). A
    # cover-page interval — the filer's own statement of its symbol on a dated
    # document — beats a snapshot bound by when we happened to look; otherwise
    # the first by valid_from is the primary listing. The rest ride as
    # candidates so a caller that cares can see them.
    rows = sorted(rows, key=lambda r: (r.basis != BASIS_FILING_COVER_PAGE, r.valid_from))
    values = [row.value for row in rows]
    return Resolution(values[0], basis=rows[0].basis, candidates=tuple(values))


def cik_for_ticker(session, ticker: str, when: date | datetime) -> Resolution:
    """Which CIK held ``ticker`` on ``when`` — the reuse question.

    ``test_ticker_reuse_resolved_by_date``: a symbol surrendered by one filer
    and picked up by another resolves to the filer that held it on the event
    date, not to today's holder. Several CIKs covering the same date is a
    genuine ambiguity and is returned as one, unresolved, with every candidate.
    """
    symbol = (ticker or "").strip().upper()
    day = _as_date(when)
    rows = _rows_for(session, attribute=ATTRIBUTE_TICKER, when=day, value=symbol)
    ciks = sorted({row.entity_cik for row in rows})
    if not ciks:
        return Resolution(None, warning="ticker_unknown_at_date")
    if len(ciks) > 1:
        return Resolution(
            None, warning="ticker_ambiguous_at_date", candidates=tuple(ciks)
        )
    return Resolution(ciks[0], basis=rows[0].basis, candidates=tuple(ciks))


def name_at(session, entity_cik: str, when: date | datetime) -> Resolution:
    """The registrant name on ``when``, preferring EDGAR's own dated ranges."""
    cik = sec_client.normalise_cik(entity_cik)
    day = _as_date(when)
    rows = _rows_for(session, attribute=ATTRIBUTE_NAME, when=day, entity_cik=cik)
    if not rows:
        return Resolution(None, warning="name_unknown_at_date")
    stated = [row for row in rows if row.basis == BASIS_FORMER_NAMES]
    chosen = (stated or rows)[0]
    return Resolution(chosen.value, basis=chosen.basis)


# --- answering Phase 3a's snapshot warnings ---------------------------------


@dataclass
class SnapshotResolutionReport:
    """What a pass over the snapshot-stamped rows could and could not answer."""

    examined: int = 0
    resolved: int = 0
    changed: int = 0
    unresolved: int = 0
    unresolved_ciks: set = field(default_factory=set)
    applied: bool = False

    @property
    def coverage(self) -> float:
        return (self.resolved / self.examined) if self.examined else 0.0

    def as_dict(self) -> dict:
        return {
            "examined": self.examined,
            "resolved": self.resolved,
            "changed": self.changed,
            "unresolved": self.unresolved,
            "coverage": round(self.coverage, 4),
            "applied": self.applied,
            "unresolved_ciks": sorted(self.unresolved_ciks)[:20],
        }


def resolve_snapshot_warnings(
    session, *, apply: bool = False, limit: int | None = None
) -> SnapshotResolutionReport:
    """Replace snapshot tickers with the ticker held at the row's ``valid_at``.

    Phase 3a stamps ``ticker_from_current_snapshot`` on every row it writes,
    because at the time there was no history to consult. There is now.

    ``apply=False`` (the default) reports coverage and changes nothing — run it
    first. With ``apply=True`` the row's ``ticker_at_time`` is corrected and the
    warning dropped; ``payload_hash`` is computed from the observation's
    *identity and value*, which does not include the ticker, so rewriting it
    cannot collide with an existing row or change what a re-ingest does. Rows
    whose date no history covers keep their warning: a wrong point-in-time
    ticker is worse than an admitted snapshot.
    """
    report = SnapshotResolutionReport(applied=apply)
    query = session.query(SourceObservation).filter(
        SourceObservation.quality_warnings.like(f"%{WARN_TICKER_SNAPSHOT}%")
    ).order_by(SourceObservation.id.asc())
    if limit is not None:
        query = query.limit(limit)

    for row in query.all():
        report.examined += 1
        resolution = ticker_at(session, row.entity_cik, row.valid_at)
        if not resolution.resolved:
            report.unresolved += 1
            report.unresolved_ciks.add(row.entity_cik)
            continue
        report.resolved += 1
        if resolution.value != row.ticker_at_time:
            report.changed += 1
        if apply:
            row.ticker_at_time = resolution.value
            remaining = [w for w in row.warnings if w != WARN_TICKER_SNAPSHOT]
            row.quality_warnings = json.dumps(remaining)

    if apply:
        session.flush()
    log.info("entity_snapshot_resolution", **report.as_dict())
    return report


# --- coverage ---------------------------------------------------------------


@dataclass
class EntityCoverage:
    ciks: int = 0
    ticker_intervals: int = 0
    name_intervals: int = 0
    dated_intervals: int = 0
    snapshot_intervals: int = 0
    cover_page_intervals: int = 0

    def as_dict(self) -> dict:
        return {
            "ciks": self.ciks,
            "ticker_intervals": self.ticker_intervals,
            "name_intervals": self.name_intervals,
            "dated_intervals": self.dated_intervals,
            "snapshot_intervals": self.snapshot_intervals,
            "cover_page_intervals": self.cover_page_intervals,
        }


def coverage(session) -> EntityCoverage:
    """What entity history holds — logged at every checkpoint."""
    rows = session.query(EntityHistory).all()
    return EntityCoverage(
        ciks=len({row.entity_cik for row in rows}),
        ticker_intervals=sum(1 for r in rows if r.attribute == ATTRIBUTE_TICKER),
        name_intervals=sum(1 for r in rows if r.attribute == ATTRIBUTE_NAME),
        dated_intervals=sum(1 for r in rows if r.basis == BASIS_FORMER_NAMES),
        snapshot_intervals=sum(1 for r in rows if r.basis == BASIS_SNAPSHOT),
        cover_page_intervals=sum(1 for r in rows if r.basis == BASIS_FILING_COVER_PAGE),
    )
