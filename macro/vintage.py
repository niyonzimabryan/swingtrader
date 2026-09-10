"""ALFRED vintages into ``source_observations`` — Spec O section 4.1.

The rule this module exists to enforce, stated as plainly as it can be:

> ``macro_state(as_of=2024-03-15)`` returns the values a person could have seen
> on 2024-03-15, **including the release lag**: if February CPI had not yet
> been published, it is *absent*, not back-filled.

Two facts about ALFRED the code respects rather than works around:

**Vintages are dated, not timestamped.** ``realtime_start`` is the date a value
became the current value. So every macro observation is ``precision='day'``
and — Spec O section 2 — is known at the **close** of that date, via
``filings.observations.day_precision_known_at``. A cohort cutting off at 10:00
cannot inherit a print that landed at 08:30 that morning; that is a real
half-day of conservatism and it is the right side to err on, because the
alternative errs in the direction that flatters.

**``USREC`` is vintage-only.** ``macro.series.require_vintage`` raises for it on
the current-value path (``test_usrec_only_via_vintage``).

The provenance class is ``vendor_pit``: the release date is the publisher's
record of when it published, which is defensible and is not the same as having
been there. ``replay_eligible=True`` — the date is *measured*, not guessed. A
series row with no release date at all would be a guess, and there is no code
path here that writes one.

The source is deliberately **not** ``sec_``-prefixed: the ledger's EDGAR
acceptance-timestamp rule applies to EDGAR, and a FRED release date is a
different (and honestly named) kind of stamp.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Protocol, Sequence

from filings.observations import (
    PRECISION_DAY,
    PROVENANCE_VENDOR_PIT,
    TRUST_STATISTICAL_AGENCY,
    Observation,
    day_precision_known_at,
    observations_known_at,
    write_observations,
)
from macro import series as series_registry
from utils.logger import get_logger

log = get_logger("macro_vintage")

SOURCE_ALFRED = "fred_alfred_vintage"
FACT_TYPE_PREFIX = "macro_series"

#: ``source_observations.entity_cik`` is not nullable and macro facts are not
#: about a registrant. A reserved sentinel is more honest than borrowing some
#: company's CIK, and it keeps every entity-scoped query from matching them.
MACRO_ENTITY = "MACRO"

FRED_SERIES_URL = "https://fred.stlouisfed.org/series/{series_id}"


class MacroPlaneDisabled(RuntimeError):
    """``PLANE_MACRO_VINTAGE_ENABLED`` is false, so nothing may be ingested."""


class VintageUnavailable(RuntimeError):
    """A vintage was asked for and the source holds none for that series."""


def fact_type_for(series_id: str) -> str:
    return f"{FACT_TYPE_PREFIX}:{series_id.strip().upper()}"


@dataclass(frozen=True)
class VintageRow:
    """One (reference period, release date, value) triple.

    ``release_date`` is when the value became public — the ``known_at`` — and
    ``reference_date`` is the period it describes — the ``valid_at``. Keeping
    both is the entire difference between this plane and ``get_series``.
    """

    reference_date: date
    release_date: date
    value: float | None


class VintageSource(Protocol):
    """Where vintages come from. Live ALFRED, or a recorded fixture."""

    def all_releases(self, series_id: str) -> list[VintageRow]:  # pragma: no cover
        ...


def _to_date(raw: Any, *, where: str) -> date:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError as exc:
        raise ValueError(f"{where}: unparseable date {raw!r}") from exc


def rows_from_dicts(rows: Iterable[dict], *, where: str = "vintage") -> list[VintageRow]:
    out = [
        VintageRow(
            reference_date=_to_date(row.get("reference_date"), where=f"{where}.reference_date"),
            release_date=_to_date(row.get("release_date"), where=f"{where}.release_date"),
            value=None if row.get("value") is None else float(row["value"]),
        )
        for row in rows
    ]
    return sorted(out, key=lambda r: (r.reference_date, r.release_date))


class FredVintageSource:
    """Live ALFRED, through the one wrapper in ``data/macro_data.py``."""

    def __init__(self, adapter):
        self._adapter = adapter

    def all_releases(self, series_id: str) -> list[VintageRow]:
        raw = self._adapter.get_series_all_releases(series_id)
        return rows_from_dicts(raw, where=f"alfred:{series_id}")


class FixtureVintageSource:
    """A recorded set of vintages — what the offline suite runs against.

    ``scripts/record_macro_fixtures.py`` writes these from live ALFRED on any
    machine that can reach ``api.stlouisfed.org``.
    """

    def __init__(self, by_series: dict):
        self._by_series = {
            key.upper(): rows_from_dicts(rows, where=f"fixture:{key}")
            for key, rows in by_series.items()
        }

    def all_releases(self, series_id: str) -> list[VintageRow]:
        key = (series_id or "").strip().upper()
        if key not in self._by_series:
            raise VintageUnavailable(f"no recorded vintages for {series_id!r}")
        return list(self._by_series[key])


# --- the point-in-time collapse ---------------------------------------------


def series_as_of(
    source: VintageSource, series_id: str, as_of: date
) -> dict[date, float]:
    """The series as it stood on ``as_of``: ``{reference period: value}``.

    For each reference period, the **latest release on or before ``as_of``**
    wins; a period whose first release is later than ``as_of`` is **absent**
    from the result, which is what ``test_macro_vintage_absent_before_release``
    checks. A period released as ALFRED's "." (no observation) is also absent —
    the publisher said there was no value, and inventing one is the same error
    as back-filling.
    """
    cutoff = _to_date(as_of, where="as_of")
    chosen: dict[date, tuple[date, float | None]] = {}
    for row in source.all_releases(series_id):
        if row.release_date > cutoff:
            continue
        current = chosen.get(row.reference_date)
        if current is None or row.release_date >= current[0]:
            chosen[row.reference_date] = (row.release_date, row.value)
    return {
        reference: value
        for reference, (_release, value) in sorted(chosen.items())
        if value is not None
    }


def latest_as_of(
    source: VintageSource, series_id: str, as_of: date
) -> tuple[date, float] | None:
    """The most recent *published* observation as of ``as_of``, or ``None``."""
    values = series_as_of(source, series_id, as_of)
    if not values:
        return None
    reference = max(values)
    return reference, values[reference]


def release_date_for(
    source: VintageSource, series_id: str, reference_date: date, as_of: date
) -> date | None:
    """The **operative** release for ``reference_date`` as of ``as_of``.

    The latest release on or before ``as_of`` — the value that was on the
    screen then. For "when did this period first appear at all", which is a
    different question, use :func:`first_release_date_for`.
    """
    cutoff = _to_date(as_of, where="as_of")
    target = _to_date(reference_date, where="reference_date")
    releases = [
        row.release_date
        for row in source.all_releases(series_id)
        if row.reference_date == target and row.release_date <= cutoff
    ]
    return max(releases) if releases else None


def first_release_date_for(
    source: VintageSource, series_id: str, reference_date: date
) -> date | None:
    """When ``reference_date`` was **first** published — the release lag."""
    target = _to_date(reference_date, where="reference_date")
    releases = [
        row.release_date
        for row in source.all_releases(series_id)
        if row.reference_date == target
    ]
    return min(releases) if releases else None


# --- observations -----------------------------------------------------------


def vintage_observations(
    source: VintageSource,
    series_id: str,
    *,
    since: date | None = None,
    first_release_only: bool = False,
) -> list[Observation]:
    """One observation per (reference period, release) — restatements included.

    A restatement is a *new row* with a later ``known_at_utc``, never an
    overwrite: reading with ``known_at_utc <= t`` then returns the number that
    was on the screen at ``t``, and reading with ``t=now`` returns today's.
    That is the whole bitemporal contract, and it is why the release date is
    part of the row rather than metadata about it.
    """
    spec = series_registry.spec_for(series_id)
    out: list[Observation] = []
    seen_reference: set[date] = set()

    for row in source.all_releases(spec.series_id):
        if row.value is None:
            continue
        if since is not None and row.release_date < since:
            continue
        if first_release_only:
            if row.reference_date in seen_reference:
                continue
            seen_reference.add(row.reference_date)

        out.append(
            Observation(
                source=SOURCE_ALFRED,
                entity_cik=MACRO_ENTITY,
                fact_type=fact_type_for(spec.series_id),
                valid_at=datetime(
                    row.reference_date.year,
                    row.reference_date.month,
                    row.reference_date.day,
                    tzinfo=timezone.utc,
                ),
                # Dated, not timestamped: known at the close of the release day.
                known_at_utc=day_precision_known_at(row.release_date),
                known_at_source="realtime_start",
                precision=PRECISION_DAY,
                provenance_class=PROVENANCE_VENDOR_PIT,
                replay_eligible=True,
                value_numeric=float(row.value),
                unit=spec.units or None,
                source_url=FRED_SERIES_URL.format(series_id=spec.series_id),
                source_trust=TRUST_STATISTICAL_AGENCY,
                payload={
                    "series_id": spec.series_id,
                    "series_label": spec.label,
                    "reference_date": row.reference_date.isoformat(),
                    "release_date": row.release_date.isoformat(),
                    "never_revised": spec.never_revised,
                    "vintage_only": spec.vintage_only,
                },
            )
        )
    return out


@dataclass
class MacroCoverage:
    series: dict = field(default_factory=dict)
    written: int = 0
    duplicates: int = 0
    unavailable: list = field(default_factory=list)
    vintage_clean_inputs: list = field(default_factory=list)
    revised_inputs: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "series": self.series,
            "written": self.written,
            "duplicates": self.duplicates,
            "unavailable": self.unavailable,
            "vintage_clean_inputs": self.vintage_clean_inputs,
            "revised_inputs": self.revised_inputs,
        }


def ingest_series(
    session,
    source: VintageSource,
    series_ids: Sequence[str],
    *,
    settings,
    since: date | None = None,
) -> MacroCoverage:
    """Write every vintage of every named series. Idempotent."""
    if not getattr(settings, "plane_macro_vintage_enabled", False):
        raise MacroPlaneDisabled(
            "PLANE_MACRO_VINTAGE_ENABLED is false. Set it to true to let the "
            "macro plane write to source_observations."
        )

    coverage = MacroCoverage()
    batch: list[Observation] = []
    for series_id in series_ids:
        spec = series_registry.spec_for(series_id)
        try:
            rows = vintage_observations(source, spec.series_id, since=since)
        except VintageUnavailable as exc:
            coverage.unavailable.append(spec.series_id)
            log.warning("macro_vintage_unavailable", series=spec.series_id, detail=str(exc))
            continue
        coverage.series[spec.series_id] = len(rows)
        (coverage.vintage_clean_inputs if spec.never_revised else coverage.revised_inputs).append(
            spec.series_id
        )
        batch.extend(rows)

    result = write_observations(session, batch)
    coverage.written = result.inserted
    coverage.duplicates = result.duplicates
    log.info("macro_vintage_ingested", **coverage.as_dict())
    return coverage


# --- reading back -----------------------------------------------------------


def stored_series_as_of(
    session, series_id: str, *, as_of: datetime
) -> dict[date, float]:
    """The stored vintages of one series, collapsed at ``as_of``.

    The ledger read that mirrors :func:`series_as_of`. Because every row was
    written with ``known_at_utc`` at the close of its release date, the single
    ``known_at_utc <= as_of`` filter in ``observations_known_at`` is the whole
    of the point-in-time logic; this function only picks the latest release per
    reference period out of what comes back.
    """
    rows = observations_known_at(
        session, fact_type=fact_type_for(series_id), cutoff=as_of
    )
    out: dict[date, float] = {}
    for row in rows:                     # ordered by valid_at, then known_at
        if row.value_numeric is None:
            continue
        out[row.valid_at.date()] = float(row.value_numeric)
    return out
