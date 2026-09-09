"""The Phase 4 filings ingest — Form 4, 13D/G, the 8-K item index, entity history.

One entry point, :func:`ingest_filings`, behind ``PLANE_FILINGS_ENABLED``.
It fetches through the one throttled client Phase 3a built, parses with the
per-form adapters, resolves the ticker from entity history rather than from
today's snapshot, links amendments to what they amend, and writes everything
through the one ledger seam.

**Amendment linking is deliberately conservative.** A ``4/A`` restates a
filing; EDGAR does not put the amended accession in the ownership XML, so the
target has to be found by natural key — same issuer, same reporting owner,
same transaction date, same security. Where that leaves more than one
candidate the amendment is written **unlinked** and counted, because linking an
amendment to the wrong original corrupts a supersession chain silently and an
unlinked amendment is a number in a coverage report.

Nothing here calls a model. Nothing here can reach an order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Sequence

from database.models import SourceObservation
from filings import client as sec_client
from filings import entities, eight_k, form4, ownership
from filings.client import SECClient
from filings.errors import AdapterSchemaError, PlaneDisabled
from filings.observations import Observation, write_observations
from filings.sec_minimal import Filing, fetch_company
from utils.logger import get_logger

log = get_logger("filings_plane")

FORM4_FORMS = frozenset({"4", "4/A"})

WARN_AMENDMENT_UNLINKED = "amendment_target_not_found"
WARN_AMENDMENT_AMBIGUOUS = "amendment_target_ambiguous"


class FilingsPlaneDisabled(PlaneDisabled):
    """``PLANE_FILINGS_ENABLED`` is false."""


# --- amendment resolution ---------------------------------------------------


def _form4_natural_key(payload: dict, valid_at: datetime, entity_cik: str) -> tuple:
    return (
        entity_cik,
        str(payload.get("owner_cik") or ""),
        valid_at.date().isoformat(),
        str(payload.get("security_title") or ""),
    )


def find_form4_amendment_target(
    session, amendment: Observation
) -> tuple[str | None, str | None]:
    """The ``payload_hash`` a ``4/A`` supersedes, or a reason it cannot be found.

    Candidates are the non-amendment Form 4 rows sharing issuer, owner,
    transaction date and security, knowable before the amendment. Where the
    transaction code also matches, that narrows it; where it does not, the code
    may be exactly what the amendment corrects, so the fallback keeps the
    wider set. Two survivors mean no link.
    """
    key = _form4_natural_key(amendment.payload, amendment.valid_at, amendment.entity_cik)
    rows = (
        session.query(SourceObservation)
        .filter(
            SourceObservation.source == form4.SOURCE_FORM4,
            SourceObservation.entity_cik == amendment.entity_cik,
            SourceObservation.valid_at == _naive(amendment.valid_at),
        )
        .order_by(SourceObservation.known_at_utc.asc(), SourceObservation.id.asc())
        .all()
    )
    candidates = [
        row
        for row in rows
        if not row.payload.get("is_amendment")
        and _form4_natural_key(row.payload, row.valid_at, row.entity_cik) == key
    ]
    if not candidates:
        return None, WARN_AMENDMENT_UNLINKED

    code = amendment.payload.get("transaction_code")
    same_code = [row for row in candidates if row.payload.get("transaction_code") == code]
    pool = same_code or candidates
    if len(pool) > 1:
        return None, WARN_AMENDMENT_AMBIGUOUS
    return pool[0].payload_hash, None


def find_schedule13_amendment_target(
    session, amendment: Observation
) -> tuple[str | None, str | None]:
    """The ``payload_hash`` an ``SC 13D/A`` supersedes.

    Matched on issuer plus the reporting person, which is what an amendment to
    a beneficial-ownership schedule restates. Filers are matched by CIK when
    the structured cover page gives one and by name otherwise; an amendment
    whose cover page is unstructured carries neither and stays unlinked.
    """
    persons = amendment.payload.get("reporting_persons") or []
    identity = sorted(
        (p.get("cik") or p.get("name") or "") for p in persons if isinstance(p, dict)
    )
    if not any(identity):
        return None, WARN_AMENDMENT_UNLINKED

    rows = (
        session.query(SourceObservation)
        .filter(
            SourceObservation.source == ownership.SOURCE_SCHEDULE_13,
            SourceObservation.entity_cik == amendment.entity_cik,
            SourceObservation.fact_type == amendment.fact_type,
            SourceObservation.known_at_utc < _naive(amendment.known_at_utc),
        )
        .order_by(SourceObservation.known_at_utc.desc(), SourceObservation.id.desc())
        .all()
    )
    for row in rows:
        row_persons = row.payload.get("reporting_persons") or []
        row_identity = sorted(
            (p.get("cik") or p.get("name") or "")
            for p in row_persons
            if isinstance(p, dict)
        )
        if row_identity and row_identity == identity:
            return row.payload_hash, None
    return None, WARN_AMENDMENT_UNLINKED


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _ticker_for(session, company, when: datetime) -> tuple[str | None, tuple[str, ...]]:
    """The ticker at ``when``, falling back to the snapshot **with the warning**.

    Entity history answers whenever an interval covers the date. When it does
    not — typically because the earliest snapshot we ever took is later than
    the filing, which is true of every historical filing on a first ingest —
    the current snapshot's ticker is used and the row carries Phase 3a's
    ``ticker_from_current_snapshot`` warning.

    That is deliberately not a silent fallback and deliberately not a null.
    A null ticker on every historical row makes the plane unusable by anything
    that joins on symbol; a silent snapshot ticker is the point-in-time bug
    this module exists to prevent. The warning is the third option: usable,
    and impossible to mistake for a resolved mapping. Downstream should still
    join on ``entity_cik``.
    """
    resolution = entities.ticker_at(session, company.cik, when)
    if resolution.resolved:
        return resolution.value, ()
    return company.primary_ticker, (entities.WARN_TICKER_SNAPSHOT,)


def _with_supersession(obs: Observation, target: str | None, warning: str | None):
    """Rebuild an observation with its supersession link (frozen dataclass)."""
    from dataclasses import replace

    warnings = obs.quality_warnings + ((warning,) if warning else ())
    return replace(obs, supersedes_payload_hash=target, quality_warnings=warnings)


def link_amendments(session, observations: Sequence[Observation]) -> list[Observation]:
    """Attach ``supersedes_payload_hash`` to every amendment we can place."""
    out: list[Observation] = []
    for obs in observations:
        if not obs.payload.get("is_amendment"):
            out.append(obs)
            continue
        if obs.source == form4.SOURCE_FORM4:
            target, warning = find_form4_amendment_target(session, obs)
        elif obs.source == ownership.SOURCE_SCHEDULE_13:
            target, warning = find_schedule13_amendment_target(session, obs)
        else:
            target, warning = None, None
        out.append(_with_supersession(obs, target, warning))
    return out


# --- fetching the per-filing documents --------------------------------------


def fetch_form4_document(client: SECClient, cik: str, filing: Filing):
    """Fetch and parse one Form 4's ownership XML, or ``None`` if it is absent."""
    url = sec_client.filing_document_url(cik, filing.accession, filing.primary_document)
    text = client.get_text_or_none(url)
    if text is None:
        return None, url
    return form4.parse_form4(text, form_type=filing.form), url


def fetch_schedule13_cover_page(client: SECClient, cik: str, filing: Filing):
    """Fetch the structured cover page, or ``None`` for a pre-2024 free-text one."""
    url = sec_client.filing_document_url(cik, filing.accession, "primary_doc.xml")
    text = client.get_text_or_none(url)
    if text is None:
        return None, url
    try:
        return ownership.parse_schedule_13(text), url
    except AdapterSchemaError as exc:
        # A document that exists but is not a structured cover page is the
        # pre-amendment free-text case, not a parser bug. The event row still
        # lands; the cover-page facts do not.
        log.info("schedule13_unstructured", cik=cik, accession=filing.accession, detail=str(exc))
        return None, url


# --- coverage ---------------------------------------------------------------


@dataclass
class FilingsCoverage:
    cik: str
    ticker: str | None = None
    name: str = ""
    written: int = 0
    duplicates: int = 0
    form4_filings: int = 0
    form4_parsed: int = 0
    form4_missing_document: int = 0
    form4_transactions: dict = field(default_factory=dict)
    form4_amendments: int = 0
    amendments_linked: int = 0
    amendments_unlinked: int = 0
    schedule13_filings: int = 0
    schedule13_structured: int = 0
    eight_k_items: dict = field(default_factory=dict)
    entity_intervals_opened: int = 0
    entity_intervals_closed: int = 0
    ticker_resolved: bool = False
    errors: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "cik": self.cik,
            "ticker": self.ticker,
            "written": self.written,
            "duplicates": self.duplicates,
            "form4_filings": self.form4_filings,
            "form4_parsed": self.form4_parsed,
            "form4_missing_document": self.form4_missing_document,
            "form4_transactions": self.form4_transactions,
            "form4_amendments": self.form4_amendments,
            "amendments_linked": self.amendments_linked,
            "amendments_unlinked": self.amendments_unlinked,
            "schedule13_filings": self.schedule13_filings,
            "schedule13_structured": self.schedule13_structured,
            "eight_k_items": self.eight_k_items,
            "entity_intervals_opened": self.entity_intervals_opened,
            "entity_intervals_closed": self.entity_intervals_closed,
            "ticker_resolved": self.ticker_resolved,
            "errors": self.errors,
        }


# --- the ingest -------------------------------------------------------------


def ingest_filings(
    session,
    client: SECClient,
    cik: str,
    *,
    settings,
    since: date | None = None,
    observed_at: datetime | None = None,
) -> FilingsCoverage:
    """Fetch, parse and write one company's Phase 4 filings. Idempotent.

    ``since`` is a *knowledge* cutoff exactly as in Phase 3a: it selects
    filings **accepted** on or after that date, which is the bitemporal
    reading.
    """
    if not getattr(settings, "plane_filings_enabled", False):
        raise FilingsPlaneDisabled(
            "PLANE_FILINGS_ENABLED is false. Set it to true to let the Phase 4 "
            "filings plane write to source_observations."
        )

    stamp = observed_at or datetime.now(timezone.utc)
    feeds = fetch_company(client, cik, since=since)
    company = feeds.company
    coverage = FilingsCoverage(
        cik=company.cik, ticker=company.primary_ticker, name=company.name
    )

    history = entities.record_submissions(
        session,
        feeds.submissions_payload,
        observed_at=stamp,
        source_url=sec_client.submissions_url(company.cik),
    )
    coverage.entity_intervals_opened = history.opened
    coverage.entity_intervals_closed = history.closed

    in_window = [
        filing
        for filing in sorted(company.filings, key=lambda f: f.acceptance)
        if since is None or filing.acceptance.date() >= since
    ]

    # --- pass one: parse the Form 4s and learn the ticker history from them.
    #
    # A Form 4 states the issuer's own trading symbol on a document EDGAR dated
    # to the second. That is a far better ticker source than the current
    # snapshot, and it is the only one that reaches backwards — so it is
    # recorded *before* anything is stamped with a ticker. Oldest first, or a
    # later symbol would close an earlier interval at the wrong date.
    parsed: dict[str, Any] = {}
    cover_page_intervals: list[entities.Interval] = []
    for filing in in_window:
        if filing.form.strip().upper() not in FORM4_FORMS:
            continue
        coverage.form4_filings += 1
        document, _url = fetch_form4_document(client, company.cik, filing)
        if document is None:
            coverage.form4_missing_document += 1
            continue
        coverage.form4_parsed += 1
        parsed[filing.accession] = document
        if document.is_amendment:
            coverage.form4_amendments += 1
        if document.issuer_symbol:
            cover_page_intervals.append(
                entities.Interval(
                    entity_cik=company.cik,
                    attribute=entities.ATTRIBUTE_TICKER,
                    value=document.issuer_symbol,
                    valid_from=filing.acceptance.date(),
                    valid_to=None,
                    basis=entities.BASIS_FILING_COVER_PAGE,
                )
            )

    if cover_page_intervals:
        cover = entities.record_intervals(
            session,
            cover_page_intervals,
            observed_at=stamp,
            source_url=sec_client.submissions_url(company.cik),
        )
        coverage.entity_intervals_opened += cover.opened
        coverage.entity_intervals_closed += cover.closed

    # --- pass two: build the observations, with the ticker history in place.
    observations: list[Observation] = []
    for filing in in_window:
        form = filing.form.strip().upper()
        ticker, extra = _ticker_for(session, company, filing.acceptance)
        if not extra:
            coverage.ticker_resolved = True

        if form in FORM4_FORMS:
            document = parsed.get(filing.accession)
            if document is None:
                continue
            rows = form4.form4_observations(
                document,
                accession=filing.accession,
                acceptance=filing.acceptance,
                source_url=sec_client.filing_index_url(company.cik, filing.accession),
                ticker_at_time=ticker,
                extra_warnings=extra,
            )
            for row in rows:
                code = row.payload.get("transaction_code", "?")
                coverage.form4_transactions[code] = (
                    coverage.form4_transactions.get(code, 0) + 1
                )
            observations.extend(rows)
            continue

        if ownership.is_schedule_13(form):
            coverage.schedule13_filings += 1
            cover_page, _url = fetch_schedule13_cover_page(client, company.cik, filing)
            if cover_page is not None:
                coverage.schedule13_structured += 1
            observations.extend(
                ownership.schedule_13_observations(
                    filing,
                    entity_cik=company.cik,
                    cover_page=cover_page,
                    ticker_at_time=ticker,
                )
            )

    item_rows = eight_k.eight_k_item_observations(
        company, since=since, ticker_at_time=_ticker_for(session, company, stamp)[0]
    )
    observations.extend(item_rows)
    coverage.eight_k_items = eight_k.item_coverage(item_rows)

    # Two passes, and the order matters: an amendment can only be linked to a
    # row that is already in the ledger, and a company's original filing and
    # its amendment routinely arrive in the same ingest window.
    originals = [obs for obs in observations if not obs.payload.get("is_amendment")]
    amendments = [obs for obs in observations if obs.payload.get("is_amendment")]

    result = write_observations(session, originals)
    coverage.written = result.inserted
    coverage.duplicates = result.duplicates

    if amendments:
        linked = link_amendments(session, amendments)
        coverage.amendments_unlinked = sum(
            1
            for obs in linked
            if obs.supersedes_payload_hash is None
            and obs.source in (form4.SOURCE_FORM4, ownership.SOURCE_SCHEDULE_13)
        )
        coverage.amendments_linked = sum(
            1 for obs in linked if obs.supersedes_payload_hash is not None
        )
        amended = write_observations(session, linked)
        coverage.written += amended.inserted
        coverage.duplicates += amended.duplicates

    log.info("filings_plane_ingested", **coverage.as_dict())
    return coverage
