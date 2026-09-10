"""Writing to and reading from ``source_observations``.

Everything a plane produces goes through :func:`write_observations`, and
everything a cohort reads comes back through :func:`observations_known_at`.
Putting both on one seam is what makes the point-in-time rules checkable
instead of aspirational.

Three rules are enforced here.

**``known_at_utc`` is the acceptance timestamp, never the filing date.**
Every observation names the field its timestamp came from
(``known_at_source``). For an EDGAR source that field must be
``acceptanceDateTime``; ``filed``/``filingDate``/``reportDate`` are refused
outright, and a second guard refuses an EDGAR timestamp that lands on exact
midnight UTC, because that is what a date silently promoted to a datetime looks
like. Apple's 2026-09-03 Form 4 was accepted at 22:30 UTC — after the close
(verification claim 13). Keying on the filing date would make post-close
information available intraday, systematically, in the direction that flatters
a backtest.

**A day-precision fact is known at the close of its day, not its open.**
:func:`day_precision_known_at` maps a date to the last instant of that UTC day,
so ``known_at_utc <= cutoff`` needs no special case at read time and a cohort
cutting off at 10:00 cannot inherit the rest of the day.

**Nothing is written half-formed.** An observation with neither a numeric nor a
text value, an unknown precision, an unknown provenance class, an unknown
source trust, or a missing source URL raises. An adapter that meets an
unexpected payload shape is expected to raise before it ever gets here
(``filings.sec_minimal``); this is the backstop that keeps a null out of the
ledger if one does not.

Phase 4 adds two markers that ride on the same seam:

**``mirror_allowed``** (Spec O section 5). Alpaca's terms bar sharing the data
"or any derived products", so nothing news-derived may reach the ``research/``
mirror. Every news-derived row carries ``mirror_allowed=False``; the guard that
acts on it is ``news.mirror_guard``.

**``supersedes_payload_hash``** (Spec O section 3.1 rule 4). An amendment — a
``4/A``, a ``13F-HR/A`` — names the observation it replaces by hash, and
:func:`write_observations` resolves that to ``superseded_observation_id``. The
original is never deleted: ``known_at_utc <= t`` must still return what was
known at ``t``, and what was known at ``t`` included the un-amended filing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from typing import Iterable, Sequence

from database.models import SourceObservation
from filings.errors import AdapterSchemaError  # noqa: F401  (re-exported for adapters)

# --- vocabularies (Spec O section 2) ---------------------------------------

PRECISION_SECOND = "second"
PRECISION_DAY = "day"
PRECISIONS = frozenset({PRECISION_SECOND, PRECISION_DAY})

PROVENANCE_OBSERVED_LIVE = "observed_live"
PROVENANCE_VENDOR_PIT = "vendor_pit"
PROVENANCE_ARCHIVAL = "archival_reconstructed"
PROVENANCE_CLASSES = frozenset(
    {PROVENANCE_OBSERVED_LIVE, PROVENANCE_VENDOR_PIT, PROVENANCE_ARCHIVAL}
)

TRUST_PRIMARY_REGULATOR = "primary_regulator"
#: A statistical agency publishing its own series (FRED/ALFRED, Treasury).
TRUST_STATISTICAL_AGENCY = "statistical_agency"
#: Spec O section 5.2 source tiers, as they travel on an observation.
TRUST_PRIMARY_ISSUER = "primary_issuer"
TRUST_ESTABLISHED_PUBLISHER = "established_publisher"
TRUST_AGGREGATOR = "aggregator"
TRUST_UNATTRIBUTED = "unattributed"

SOURCE_TRUSTS = frozenset({
    TRUST_PRIMARY_REGULATOR,
    TRUST_STATISTICAL_AGENCY,
    TRUST_PRIMARY_ISSUER,
    TRUST_ESTABLISHED_PUBLISHER,
    TRUST_AGGREGATOR,
    TRUST_UNATTRIBUTED,
})

#: Payload key carrying the mirror marker (Spec O section 5).
#:
#: Alpaca's terms bar sharing the data "or any derived products", so nothing
#: news-derived may reach the ``research/`` mirror. ``source_observations``
#: predates the rule and has no column for it, and the marker cannot be added
#: as one from a migration that branches from ``0001_baseline`` (the table does
#: not exist there). It therefore rides in the payload, where the mirror guard
#: — ``news.mirror_guard`` — reads it. Absent means allowed, so Phase 3a's SEC
#: rows keep their meaning without a rewrite.
MIRROR_ALLOWED_KEY = "mirror_allowed"

#: Sources whose timestamps come from EDGAR and are therefore held to the
#: acceptance-timestamp rule.
EDGAR_SOURCE_PREFIX = "sec_"

#: The only EDGAR field that may ever become a ``known_at_utc``.
EDGAR_KNOWN_AT_FIELD = "acceptanceDateTime"

#: Fields that name *when a filing was dated*, not when it became public.
FORBIDDEN_KNOWN_AT_FIELDS = frozenset(
    {"filed", "filingdate", "filing_date", "reportdate", "report_date", "period"}
)


class LedgerWriteRejected(ValueError):
    """An observation broke a point-in-time rule and was not written."""


# --- time helpers -----------------------------------------------------------


def day_precision_known_at(day: date) -> datetime:
    """The instant a ``precision='day'`` fact becomes known: end of that day.

    Spec O section 2: a dated (not timestamped) fact is treated as known at the
    close of its date, never at its open. The last microsecond of the UTC day
    is the conservative reading — it is never earlier than a market close, so
    no cohort can gain lookahead from the choice, and it keeps a single
    ``known_at_utc <= t`` comparison working for both precisions.
    """
    return datetime.combine(day, time(23, 59, 59, 999999), tzinfo=timezone.utc)


def parse_acceptance_datetime(raw: str) -> datetime:
    """Parse EDGAR's ``2026-09-03T22:30:44.000Z`` into an aware UTC datetime."""
    if not isinstance(raw, str) or not raw.strip():
        raise LedgerWriteRejected(f"empty acceptanceDateTime: {raw!r}")
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise LedgerWriteRejected(f"unparseable acceptanceDateTime: {raw!r}") from exc
    if parsed.tzinfo is None:
        # EDGAR stamps an explicit UTC Z; a value without one is a shape change.
        raise LedgerWriteRejected(f"acceptanceDateTime carries no timezone: {raw!r}")
    return parsed.astimezone(timezone.utc)


def _to_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _to_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# --- the observation --------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """One fact, with both of its times and the provenance of the later one."""

    source: str
    entity_cik: str
    fact_type: str
    valid_at: datetime
    known_at_utc: datetime
    known_at_source: str
    precision: str
    provenance_class: str
    source_url: str
    source_trust: str
    replay_eligible: bool
    value_numeric: float | None = None
    value_text: str | None = None
    unit: str | None = None
    ticker_at_time: str | None = None
    period_start: date | None = None
    accession: str | None = None
    payload: dict = field(default_factory=dict)
    quality_warnings: tuple[str, ...] = ()
    #: False marks the row as un-mirrorable (Spec O section 5). It is a
    #: *marker*, not the enforcement: ``news.mirror_guard`` is what refuses.
    mirror_allowed: bool = True
    #: The ``payload_hash`` of the observation this one amends. Resolved to
    #: ``superseded_observation_id`` at write time; the original is never
    #: deleted and stays queryable.
    supersedes_payload_hash: str | None = None

    def __post_init__(self) -> None:
        _validate(self)

    # -- identity ---------------------------------------------------------
    def identity(self) -> dict:
        """The tuple that makes a re-ingest a no-op and a restatement a new row.

        The value is part of it deliberately: the same period reported again
        with the same number is the same fact, and the same period reported
        again with a different number is a restatement that must land beside
        the original rather than replace it.
        """
        return {
            "source": self.source,
            "entity_cik": self.entity_cik,
            "fact_type": self.fact_type,
            "valid_at": _to_aware_utc(self.valid_at).isoformat(),
            "known_at_utc": _to_aware_utc(self.known_at_utc).isoformat(),
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "accession": self.accession,
            "unit": self.unit,
            "value_numeric": self.value_numeric,
            "value_text": self.value_text,
        }

    def payload_hash(self) -> str:
        blob = json.dumps(self.identity(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def stored_payload(self) -> dict:
        merged = dict(self.payload)
        merged["known_at_source"] = self.known_at_source
        merged[MIRROR_ALLOWED_KEY] = bool(self.mirror_allowed)
        if self.supersedes_payload_hash:
            merged["supersedes_payload_hash"] = self.supersedes_payload_hash
        return merged

    def to_row(self) -> SourceObservation:
        return SourceObservation(
            source=self.source,
            source_url=self.source_url,
            source_trust=self.source_trust,
            accession=self.accession,
            entity_cik=self.entity_cik,
            ticker_at_time=self.ticker_at_time,
            fact_type=self.fact_type,
            value_numeric=self.value_numeric,
            value_text=self.value_text,
            unit=self.unit,
            period_start=self.period_start,
            valid_at=_to_naive_utc(self.valid_at),
            known_at_utc=_to_naive_utc(self.known_at_utc),
            precision=self.precision,
            provenance_class=self.provenance_class,
            replay_eligible=self.replay_eligible,
            payload_json=json.dumps(self.stored_payload(), sort_keys=True),
            payload_hash=self.payload_hash(),
            quality_warnings=json.dumps(list(self.quality_warnings)),
        )


def _validate(obs: Observation) -> None:
    if not obs.source:
        raise LedgerWriteRejected("source is required")
    if not obs.fact_type:
        raise LedgerWriteRejected("fact_type is required")
    if not obs.entity_cik:
        raise LedgerWriteRejected("entity_cik is required")
    if not obs.source_url:
        raise LedgerWriteRejected(f"{obs.fact_type}: source_url is required")
    if not obs.source_trust:
        raise LedgerWriteRejected(f"{obs.fact_type}: source_trust is required")
    if obs.source_trust not in SOURCE_TRUSTS:
        raise LedgerWriteRejected(
            f"{obs.fact_type}: source_trust must be one of {sorted(SOURCE_TRUSTS)}, "
            f"got {obs.source_trust!r}. The tier travels with the fact "
            "(Spec O section 5.2), so it cannot be free text."
        )
    if obs.precision not in PRECISIONS:
        raise LedgerWriteRejected(
            f"{obs.fact_type}: precision must be one of {sorted(PRECISIONS)}, "
            f"got {obs.precision!r}"
        )
    if obs.provenance_class not in PROVENANCE_CLASSES:
        raise LedgerWriteRejected(
            f"{obs.fact_type}: provenance_class must be one of "
            f"{sorted(PROVENANCE_CLASSES)}, got {obs.provenance_class!r}"
        )
    if obs.value_numeric is None and not (obs.value_text or "").strip():
        raise LedgerWriteRejected(
            f"{obs.fact_type}: an observation needs a numeric or a text value. "
            "A row with neither is a null the ledger will not store."
        )
    if not isinstance(obs.valid_at, datetime) or not isinstance(obs.known_at_utc, datetime):
        raise LedgerWriteRejected(f"{obs.fact_type}: valid_at and known_at_utc must be datetimes")
    if not obs.known_at_source:
        raise LedgerWriteRejected(
            f"{obs.fact_type}: known_at_source must name the field the timestamp "
            "came from, so the filing-date rule can be checked rather than trusted."
        )
    _reject_filing_date_as_known_at(obs)


def _reject_filing_date_as_known_at(obs: Observation) -> None:
    """Spec O section 2: for EDGAR, ``known_at_utc`` is ``acceptanceDateTime``."""
    if obs.known_at_source.strip().lower() in FORBIDDEN_KNOWN_AT_FIELDS:
        raise LedgerWriteRejected(
            f"{obs.source}/{obs.fact_type}: known_at_utc was derived from "
            f"{obs.known_at_source!r}. A filing date is when the filer dated the "
            "document, not when it became public; EDGAR accepts filings after the "
            "close, so this is a one-session lookahead leak. Use "
            f"{EDGAR_KNOWN_AT_FIELD}."
        )

    if not obs.source.startswith(EDGAR_SOURCE_PREFIX):
        return

    if obs.known_at_source != EDGAR_KNOWN_AT_FIELD:
        raise LedgerWriteRejected(
            f"{obs.source}/{obs.fact_type}: an EDGAR observation's known_at_utc "
            f"must come from {EDGAR_KNOWN_AT_FIELD}, got {obs.known_at_source!r}."
        )

    stamp = _to_aware_utc(obs.known_at_utc)
    if (stamp.hour, stamp.minute, stamp.second, stamp.microsecond) == (0, 0, 0, 0):
        raise LedgerWriteRejected(
            f"{obs.source}/{obs.fact_type}: known_at_utc is exact midnight UTC "
            f"({stamp.isoformat()}). EDGAR acceptance timestamps carry a real "
            "time of day; a midnight value is a filing date that has been "
            "promoted to a datetime."
        )


# --- writing ----------------------------------------------------------------


@dataclass
class WriteResult:
    inserted: int = 0
    duplicates: int = 0
    #: Amendments whose target was not in the ledger. Reported, never guessed
    #: at: linking an amendment to the wrong original is worse than not
    #: linking it, and the amendment row itself is written either way.
    unresolved_supersessions: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return self.inserted + self.duplicates


def write_observations(session, observations: Iterable[Observation]) -> WriteResult:
    """Insert observations that are not already in the ledger.

    Idempotent by ``payload_hash``: re-running an ingest over the same feed
    writes nothing new. Existing hashes are looked up in one query per batch
    rather than relying on integrity errors, so a duplicate never poisons the
    surrounding transaction on Postgres.
    """
    batch = list(observations)
    if not batch:
        return WriteResult()

    hashes = {obs.payload_hash(): obs for obs in batch}
    existing = {
        row[0]
        for row in session.query(SourceObservation.payload_hash)
        .filter(SourceObservation.payload_hash.in_(list(hashes)))
        .all()
    }

    result = WriteResult(duplicates=len(existing))
    pending: list[SourceObservation] = []
    for digest, obs in hashes.items():
        if digest in existing:
            continue
        row = obs.to_row()
        session.add(row)
        pending.append(row)
        result.inserted += 1

    # Two identical observations inside one batch collapse to one hash key.
    result.duplicates += len(batch) - len(hashes)
    session.flush()
    result.unresolved_supersessions = _link_supersessions(session, hashes, pending)
    return result


def _link_supersessions(session, hashes, pending) -> tuple[str, ...]:
    """Point each amendment at the row it replaces, by ``payload_hash``.

    Runs after the flush so an amendment can supersede an original written in
    the same batch. A target that is not in the ledger leaves the link null and
    is returned: the amendment is still a fact, and inventing a parent for it
    would be worse than an honest gap.
    """
    wanted = {
        obs.supersedes_payload_hash
        for obs in hashes.values()
        if obs.supersedes_payload_hash
    }
    if not wanted:
        return ()

    found = {
        row.payload_hash: row.id
        for row in session.query(SourceObservation)
        .filter(SourceObservation.payload_hash.in_(sorted(wanted)))
        .all()
    }
    by_hash = {row.payload_hash: row for row in pending}
    unresolved = []
    for digest, obs in hashes.items():
        target = obs.supersedes_payload_hash
        if not target:
            continue
        row = by_hash.get(digest)
        if row is None:      # already present from an earlier run; link stands
            continue
        if target in found and found[target] != row.id:
            row.superseded_observation_id = found[target]
        else:
            unresolved.append(target)
    session.flush()
    return tuple(sorted(set(unresolved)))


def mirror_allowed(row: SourceObservation) -> bool:
    """Whether a stored row may be copied into the ``research/`` mirror.

    Absent means allowed: Phase 3a's SEC rows predate the marker and are
    regulator data with no redistribution restriction. Only a row that says
    ``False`` is barred, which is what every news-derived row says.
    """
    value = (row.payload or {}).get(MIRROR_ALLOWED_KEY, True)
    return bool(value)


# --- reading ----------------------------------------------------------------


def observations_known_at(
    session,
    *,
    fact_type: str | Sequence[str] | None = None,
    cutoff: datetime,
    entity_cik: str | None = None,
    ticker_at_time: str | None = None,
    source: str | None = None,
    replay_eligible_only: bool = False,
):
    """Every observation knowable at ``cutoff``, newest ``valid_at`` last.

    This is the only sanctioned read. A day-precision row was stored at the
    close of its day (:func:`day_precision_known_at`), so the single
    ``known_at_utc <= cutoff`` comparison is correct for both precisions.
    """
    query = session.query(SourceObservation).filter(
        SourceObservation.known_at_utc <= _to_naive_utc(cutoff)
    )
    if fact_type is not None:
        if isinstance(fact_type, str):
            query = query.filter(SourceObservation.fact_type == fact_type)
        else:
            query = query.filter(SourceObservation.fact_type.in_(list(fact_type)))
    if entity_cik is not None:
        query = query.filter(SourceObservation.entity_cik == entity_cik)
    if ticker_at_time is not None:
        query = query.filter(SourceObservation.ticker_at_time == ticker_at_time)
    if source is not None:
        query = query.filter(SourceObservation.source == source)
    if replay_eligible_only:
        query = query.filter(SourceObservation.replay_eligible.is_(True))
    return (
        query.order_by(
            SourceObservation.valid_at.asc(),
            SourceObservation.known_at_utc.asc(),
            SourceObservation.id.asc(),
        ).all()
    )


def current_view(rows: Sequence[SourceObservation]) -> list[SourceObservation]:
    """Drop rows an amendment in the same set supersedes.

    :func:`observations_known_at` returns **everything** knowable at the
    cutoff, amendments and originals alike, because that is the bitemporal
    answer: before the ``4/A`` landed, the original was the truth. An
    *aggregate* over that set double-counts the amended filing, so any sum
    filters through here first.

    A row superseded by an amendment that is not in the set — because the
    cutoff predates it — correctly stays: at that cutoff it had not been
    amended yet.
    """
    superseded = {
        row.superseded_observation_id for row in rows if row.superseded_observation_id
    }
    return [row for row in rows if row.id not in superseded]


def latest_observation_as_of(session, **kwargs) -> SourceObservation | None:
    """The most recent applicable fact that was knowable at the cutoff.

    Latest ``valid_at`` wins; among rows for the same period the latest
    ``known_at_utc`` wins, which makes a restatement supersede the original
    without either row being deleted.
    """
    rows = observations_known_at(session, **kwargs)
    return rows[-1] if rows else None


# --- truncation, for the lookahead harness ----------------------------------


def delete_observations_after(session, cutoff: datetime) -> int:
    """Remove every observation that was **not** knowable at ``cutoff``.

    This exists for one caller: the Spec N §10 lookahead harness
    (``comparables/lookahead.py``), which re-runs a cohort against a ledger
    physically truncated at each event's cutoff and asserts the answer does not
    move. Borrowed from freqtrade's ``lookahead-analysis``: a filter can be
    written correctly and still be bypassed by a join somewhere downstream, and
    the only way to know is to delete the rows and see whether anything
    changes.

    It lives here rather than in ``comparables/`` on purpose. The engine reads
    the ledger through :func:`observations_known_at` and writes nothing; the
    one place that knows how to remove a row from ``source_observations`` is
    the module that knows how to add one. **Call it inside a savepoint you
    intend to roll back** — the harness does, and nothing else should call it
    at all.
    """
    deleted = (
        session.query(SourceObservation)
        .filter(SourceObservation.known_at_utc > _to_naive_utc(cutoff))
        .delete(synchronize_session=False)
    )
    session.flush()
    return int(deleted or 0)
