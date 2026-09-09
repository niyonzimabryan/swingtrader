"""The minimum SEC ingestion contract — Spec N section 4.0, Spec O section 3.4.

Three free, credential-less feeds, turned into ``source_observations`` rows:

1. **XBRL ``companyfacts``** — every reported fact with its accession number.
   Restatements arrive as new facts with later accessions, so ``known_at <= t``
   returns the as-reported figure with no deletion anywhere.
2. **``submissions``** — the per-filing ``acceptanceDateTime``, second
   resolution, explicit UTC. This is the ``known_at_utc`` for feeds 1 and 3;
   without it neither is point-in-time. Filing dates are read and stored in the
   payload for audit, and are structurally barred from becoming a timestamp
   (``filings.observations``).
3. **The 8-K index filtered to Item 2.02** (Results of Operations) — the
   earnings-announcement clock Spec N section 4.0 needs, exact to the second.
   It does not cover every announcement: a company that press-releases before
   filing is announced earlier than its 8-K, and that case is Phase 3b's to
   fall back on, not this module's to guess at.

``dei:EntityCommonStockSharesOutstanding`` rides on feed 1 and is what makes
``market_cap_decile`` computable — the price file carries no share count.

Not in this phase: 13D/G, Form 4, entity history, news. Those are Phase 4 and
build on the same ledger.

No model is called anywhere in this path, and nothing here can reach an order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Sequence

from filings import client as sec_client
from filings.client import SECClient
from filings.errors import AdapterSchemaError, PlaneDisabled
from filings.observations import (
    PRECISION_SECOND,
    PROVENANCE_VENDOR_PIT,
    TRUST_PRIMARY_REGULATOR,
    Observation,
    latest_observation_as_of,
    parse_acceptance_datetime,
    write_observations,
)
from filings.xbrl_aliases import (
    CORE_FACT_TYPES,
    FACT_TYPES,
    CoverageAlert,
    RawFact,
    coverage_alerts,
    facts_for,
)
from utils.logger import get_logger

log = get_logger("sec_minimal")

SOURCE_COMPANYFACTS = "sec_xbrl_companyfacts"
SOURCE_EIGHT_K_INDEX = "sec_8k_item_202"

FACT_TYPE_EARNINGS_RELEASE = "earnings_release_8k_item_202"
ITEM_RESULTS_OF_OPERATIONS = "2.02"

#: Every row's ticker comes from the *current* ``submissions`` snapshot.
#: Ticker history (Spec O section 3.2) is Phase 4 work, so the row says so
#: rather than letting a downstream join assume a point-in-time mapping.
WARN_TICKER_SNAPSHOT = "ticker_from_current_snapshot"
#: companyfacts does not expose the share-class axis; see below.
WARN_SHARES_MULTI_CLASS = "shares_outstanding_summed_across_classes"


# --- submissions ------------------------------------------------------------


@dataclass(frozen=True)
class Filing:
    accession: str
    form: str
    items: tuple[str, ...]
    filing_date: date | None
    report_date: date | None
    acceptance: datetime
    primary_document: str

    @property
    def is_earnings_8k(self) -> bool:
        return self.form.upper().startswith("8-K") and ITEM_RESULTS_OF_OPERATIONS in self.items


@dataclass(frozen=True)
class Company:
    cik: str
    name: str
    tickers: tuple[str, ...]
    filings: tuple[Filing, ...]

    @property
    def primary_ticker(self) -> str | None:
        return self.tickers[0] if self.tickers else None

    def acceptance_index(self) -> dict[str, Filing]:
        return {f.accession: f for f in self.filings}


REQUIRED_FILING_FIELDS = (
    "accessionNumber",
    "acceptanceDateTime",
    "filingDate",
    "form",
    "items",
    "primaryDocument",
    "reportDate",
)


def _optional_date(raw: Any) -> date | None:
    if raw in (None, ""):
        return None
    if not isinstance(raw, str):
        raise AdapterSchemaError(f"submissions: expected an ISO date, got {raw!r}")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise AdapterSchemaError(f"submissions: unparseable date {raw!r}") from exc


def parse_filings_block(block: Any, *, where: str) -> list[Filing]:
    """Parse one parallel-array filings block into :class:`Filing` records.

    ``filings.recent`` in the main submissions document and each older
    ``CIK##########-submissions-NNN.json`` page share this shape: one array per
    field, all the same length, positionally aligned.
    """
    if not isinstance(block, dict):
        raise AdapterSchemaError(f"{where}: expected an object, got {type(block).__name__}")

    missing = [key for key in REQUIRED_FILING_FIELDS if key not in block]
    if missing:
        raise AdapterSchemaError(
            f"{where}: missing field arrays {missing}; keys were {sorted(block)}"
        )

    columns = {key: block[key] for key in REQUIRED_FILING_FIELDS}
    for key, values in columns.items():
        if not isinstance(values, list):
            raise AdapterSchemaError(f"{where}: {key} is not an array")

    lengths = {key: len(values) for key, values in columns.items()}
    if len(set(lengths.values())) > 1:
        raise AdapterSchemaError(
            f"{where}: field arrays are not the same length: {lengths}. "
            "The positional alignment this parser depends on is gone."
        )

    out: list[Filing] = []
    for index in range(next(iter(lengths.values()), 0)):
        accession = columns["accessionNumber"][index]
        if not isinstance(accession, str) or not accession.strip():
            raise AdapterSchemaError(f"{where}[{index}]: accessionNumber is {accession!r}")
        raw_items = columns["items"][index] or ""
        if not isinstance(raw_items, str):
            raise AdapterSchemaError(f"{where}[{index}]: items is {raw_items!r}, expected a string")
        out.append(
            Filing(
                accession=accession.strip(),
                form=str(columns["form"][index] or ""),
                items=tuple(part.strip() for part in raw_items.split(",") if part.strip()),
                filing_date=_optional_date(columns["filingDate"][index]),
                report_date=_optional_date(columns["reportDate"][index]),
                # Raises when the stamp is absent, empty, or has no timezone —
                # this join is the entire point-in-time claim.
                acceptance=parse_acceptance_datetime(columns["acceptanceDateTime"][index]),
                primary_document=str(columns["primaryDocument"][index] or ""),
            )
        )
    return out


def parse_submissions(payload: Any, extra_pages: Sequence[Any] = ()) -> Company:
    """Validate and flatten a ``submissions`` document plus any older pages."""
    if not isinstance(payload, dict):
        raise AdapterSchemaError(
            f"submissions: expected an object, got {type(payload).__name__}"
        )
    for key in ("cik", "name", "filings"):
        if key not in payload:
            raise AdapterSchemaError(
                f"submissions: missing {key!r}; keys were {sorted(payload)}"
            )

    filings_block = payload["filings"]
    if not isinstance(filings_block, dict) or "recent" not in filings_block:
        raise AdapterSchemaError("submissions: 'filings' has no 'recent' block")

    filings = parse_filings_block(filings_block["recent"], where="submissions.filings.recent")
    for page_number, page in enumerate(extra_pages):
        filings.extend(parse_filings_block(page, where=f"submissions page {page_number}"))

    tickers = payload.get("tickers") or []
    if not isinstance(tickers, list):
        raise AdapterSchemaError("submissions: 'tickers' is not an array")

    return Company(
        cik=sec_client.normalise_cik(payload["cik"]),
        name=str(payload["name"] or ""),
        tickers=tuple(str(t) for t in tickers),
        filings=tuple(filings),
    )


def submissions_pages_to_fetch(payload: Any, since: date | None) -> list[str]:
    """Older submissions pages overlapping ``since``, as file names.

    ``filings.recent`` holds roughly the last year. Anything older lives in the
    pages listed under ``filings.files``, each with the date range it covers, so
    a backfill fetches only the ones it needs.
    """
    files = (payload.get("filings") or {}).get("files") or []
    if not isinstance(files, list):
        raise AdapterSchemaError("submissions: 'filings.files' is not an array")
    out = []
    for entry in files:
        if not isinstance(entry, dict) or "name" not in entry:
            raise AdapterSchemaError(f"submissions: unexpected filings.files entry {entry!r}")
        if since is not None:
            covered_to = _optional_date(entry.get("filingTo"))
            if covered_to is not None and covered_to < since:
                continue
        out.append(str(entry["name"]))
    return out


# --- fetching ---------------------------------------------------------------


@dataclass
class CompanyFeeds:
    cik: str
    submissions_payload: Any
    companyfacts_payload: Any | None
    company: Company


def fetch_company(client: SECClient, cik: str, *, since: date | None = None) -> CompanyFeeds:
    """Fetch both documents for one CIK, through the one throttled client."""
    normalised = sec_client.normalise_cik(cik)
    submissions_payload = client.get_json(sec_client.submissions_url(normalised))
    pages = [
        client.get_json(sec_client.submissions_page_url(name))
        for name in submissions_pages_to_fetch(submissions_payload, since)
    ]
    company = parse_submissions(submissions_payload, pages)
    companyfacts = client.get_json_or_none(sec_client.companyfacts_url(normalised))
    return CompanyFeeds(
        cik=normalised,
        submissions_payload=submissions_payload,
        companyfacts_payload=companyfacts,
        company=company,
    )


def load_ticker_map(client: SECClient) -> dict[str, str]:
    """``company_tickers.json`` -> ``{TICKER: zero-padded CIK}``.

    A *current* snapshot, which is why nothing derived from it is treated as
    point-in-time (Spec O section 3.2 defers ticker history to Phase 4).
    """
    payload = client.get_json(sec_client.company_tickers_url())
    if not isinstance(payload, dict):
        raise AdapterSchemaError("company_tickers: expected an object")
    out: dict[str, str] = {}
    for key, entry in payload.items():
        if not isinstance(entry, dict) or "ticker" not in entry or "cik_str" not in entry:
            raise AdapterSchemaError(f"company_tickers[{key}]: unexpected entry {entry!r}")
        out[str(entry["ticker"]).upper()] = sec_client.normalise_cik(entry["cik_str"])
    return out


# --- building observations --------------------------------------------------


def _base_warnings(company: Company) -> tuple[str, ...]:
    return (WARN_TICKER_SNAPSHOT,) if company.primary_ticker else ()


def eight_k_observations(company: Company, *, since: date | None = None) -> list[Observation]:
    """One observation per 8-K carrying Item 2.02, stamped at acceptance."""
    out = []
    for filing in company.filings:
        if not filing.is_earnings_8k:
            continue
        if since is not None and filing.acceptance.date() < since:
            continue
        out.append(
            Observation(
                source=SOURCE_EIGHT_K_INDEX,
                entity_cik=company.cik,
                ticker_at_time=company.primary_ticker,
                fact_type=FACT_TYPE_EARNINGS_RELEASE,
                # The event *is* the acceptance: an announcement applies when it
                # is made, so both times are the same instant here.
                valid_at=filing.acceptance,
                known_at_utc=filing.acceptance,
                known_at_source="acceptanceDateTime",
                precision=PRECISION_SECOND,
                provenance_class=PROVENANCE_VENDOR_PIT,
                replay_eligible=True,
                value_text=",".join(filing.items),
                accession=filing.accession,
                source_url=sec_client.filing_index_url(company.cik, filing.accession),
                source_trust=TRUST_PRIMARY_REGULATOR,
                payload={
                    "form": filing.form,
                    "items": list(filing.items),
                    "primary_document": filing.primary_document,
                    "report_date": filing.report_date.isoformat() if filing.report_date else None,
                    # Stored for audit only. The ledger refuses to let it become
                    # a known_at_utc.
                    "filing_date": filing.filing_date.isoformat() if filing.filing_date else None,
                },
                quality_warnings=_base_warnings(company),
            )
        )
    return out


def _observation_from_fact(
    fact: RawFact,
    filing: Filing,
    company: Company,
    *,
    value: float | None = None,
    extra_payload: dict | None = None,
    extra_warnings: Sequence[str] = (),
) -> Observation:
    payload = {
        "taxonomy": fact.taxonomy,
        "tag": fact.tag,
        "form": fact.form,
        "fy": fact.fiscal_year,
        "fp": fact.fiscal_period,
        "frame": fact.frame,
        "period_end": fact.period_end.isoformat(),
        "filing_date": filing.filing_date.isoformat() if filing.filing_date else None,
    }
    payload.update(extra_payload or {})
    return Observation(
        source=SOURCE_COMPANYFACTS,
        entity_cik=company.cik,
        ticker_at_time=company.primary_ticker,
        fact_type=fact.fact_type,
        # valid_at carries the date resolution XBRL reports: the period end, at
        # midnight UTC. It is never compared against an availability cutoff —
        # known_at_utc is — so it needs no end-of-day convention.
        valid_at=datetime(
            fact.period_end.year, fact.period_end.month, fact.period_end.day,
            tzinfo=timezone.utc,
        ),
        period_start=fact.period_start,
        known_at_utc=filing.acceptance,
        known_at_source="acceptanceDateTime",
        precision=PRECISION_SECOND,
        provenance_class=PROVENANCE_VENDOR_PIT,
        replay_eligible=True,
        value_numeric=fact.value if value is None else value,
        unit=fact.unit,
        accession=fact.accession,
        source_url=sec_client.filing_index_url(company.cik, fact.accession),
        source_trust=TRUST_PRIMARY_REGULATOR,
        payload=payload,
        quality_warnings=tuple(_base_warnings(company)) + tuple(extra_warnings),
    )


@dataclass
class FactBuild:
    observations: list[Observation] = field(default_factory=list)
    unresolved_accessions: set[str] = field(default_factory=set)


def xbrl_observations(
    companyfacts: Any,
    company: Company,
    *,
    fact_types: Sequence[str] = tuple(FACT_TYPES),
    since: date | None = None,
) -> FactBuild:
    """Join companyfacts to the acceptance index and emit observations.

    A fact whose accession is not in the acceptance index gets **no row**. The
    alternative — writing it with a guessed timestamp — is the failure this
    whole plane exists to prevent, and a missing fact is a coverage number we
    can report rather than a lie we cannot detect.
    """
    index = company.acceptance_index()
    build = FactBuild()

    for fact_type in fact_types:
        merged, _ = facts_for(companyfacts, fact_type)
        if fact_type == "shares_outstanding":
            build.observations.extend(
                _share_count_observations(merged, index, company, since, build)
            )
            continue
        for fact in merged:
            filing = index.get(fact.accession)
            if filing is None:
                build.unresolved_accessions.add(fact.accession)
                continue
            if since is not None and filing.acceptance.date() < since:
                continue
            build.observations.append(_observation_from_fact(fact, filing, company))
    return build


def _share_count_observations(
    facts: Iterable[RawFact],
    index: dict[str, Filing],
    company: Company,
    since: date | None,
    build: FactBuild,
) -> list[Observation]:
    """Aggregate ``dei:EntityCommonStockSharesOutstanding`` across share classes.

    Spec N section 4.0 wants the share count "aggregated across share classes by
    CIK". ``companyfacts`` flattens away the share-class axis, so a dual-class
    filer appears as several entries sharing an accession and a date and
    differing only in value. Summing the *distinct* values on that key is the
    only aggregation the payload supports.

    Its one blind spot is stated rather than hidden: two classes with exactly
    equal share counts collapse into one and the total is understated. The row
    carries ``share_class_count`` and a quality warning whenever more than one
    value was summed, so the case is visible downstream.
    """
    grouped: dict[tuple[str, date], list[RawFact]] = {}
    for fact in facts:
        grouped.setdefault((fact.accession, fact.period_end), []).append(fact)

    out: list[Observation] = []
    for (accession, _period_end), group in sorted(grouped.items()):
        filing = index.get(accession)
        if filing is None:
            build.unresolved_accessions.add(accession)
            continue
        if since is not None and filing.acceptance.date() < since:
            continue

        distinct = sorted({fact.value for fact in group})
        total = float(sum(distinct))
        warnings = (WARN_SHARES_MULTI_CLASS,) if len(distinct) > 1 else ()
        out.append(
            _observation_from_fact(
                group[0],
                filing,
                company,
                value=total,
                extra_payload={
                    "share_class_count": len(distinct),
                    "share_class_values": distinct,
                },
                extra_warnings=warnings,
            )
        )
    return out


# --- coverage ---------------------------------------------------------------


@dataclass
class CompanyCoverage:
    """What one company's ingest actually produced. Printed by the backfill."""

    cik: str
    ticker: str | None = None
    name: str = ""
    written: int = 0
    duplicates: int = 0
    fact_type_counts: dict = field(default_factory=dict)
    continuous_series: dict = field(default_factory=dict)
    share_count_rows: int = 0
    latest_share_count: date | None = None
    eight_k_total: int = 0
    eight_k_item_202: int = 0
    unresolved_accessions: int = 0
    alerts: list = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def has_core_series(self) -> bool:
        return all(self.continuous_series.get(name) for name in CORE_FACT_TYPES)


def ingest_company(
    session,
    client: SECClient,
    cik: str,
    *,
    settings,
    since: date | None = None,
    fact_types: Sequence[str] = tuple(FACT_TYPES),
) -> CompanyCoverage:
    """Fetch, normalise and write one company's three feeds. Idempotent.

    ``since`` is a *knowledge* cutoff, not a period cutoff: it selects filings
    **accepted** on or after that date, which is the bitemporal reading. The
    coverage alerts are computed over the company's whole reported history,
    because "did this filer migrate a tag" is not a question about the window.
    """
    if not getattr(settings, "plane_sec_minimal_enabled", False):
        raise PlaneDisabled(
            "PLANE_SEC_MINIMAL_ENABLED is false. Set it to true to let the SEC "
            "plane write to source_observations."
        )

    feeds = fetch_company(client, cik, since=since)
    company = feeds.company
    coverage = CompanyCoverage(
        cik=company.cik, ticker=company.primary_ticker, name=company.name
    )

    coverage.eight_k_total = sum(
        1 for f in company.filings if f.form.upper().startswith("8-K")
    )
    observations = eight_k_observations(company, since=since)
    coverage.eight_k_item_202 = len(observations)

    if feeds.companyfacts_payload is not None:
        build = xbrl_observations(
            feeds.companyfacts_payload, company, fact_types=fact_types, since=since
        )
        observations.extend(build.observations)
        coverage.unresolved_accessions = len(build.unresolved_accessions)
        for fact_type in fact_types:
            alerts = coverage_alerts(feeds.companyfacts_payload, fact_type, company.cik)
            coverage.alerts.extend(alerts)
            coverage.continuous_series[fact_type] = not any(
                alert.kind in ("series_gap", "no_coverage") for alert in alerts
            )
    else:
        coverage.alerts.append(
            CoverageAlert(
                kind="no_companyfacts",
                fact_type="*",
                entity_cik=company.cik,
                detail="SEC has no companyfacts document for this CIK",
            )
        )
        for fact_type in fact_types:
            coverage.continuous_series[fact_type] = False

    for obs in observations:
        coverage.fact_type_counts[obs.fact_type] = (
            coverage.fact_type_counts.get(obs.fact_type, 0) + 1
        )
        if obs.fact_type == "shares_outstanding":
            coverage.share_count_rows += 1
            seen = obs.valid_at.date()
            if coverage.latest_share_count is None or seen > coverage.latest_share_count:
                coverage.latest_share_count = seen

    result = write_observations(session, observations)
    coverage.written = result.inserted
    coverage.duplicates = result.duplicates

    log.info(
        "sec_minimal_ingested",
        cik=company.cik,
        ticker=company.primary_ticker,
        written=result.inserted,
        duplicates=result.duplicates,
        alerts=len(coverage.alerts),
    )
    return coverage


# --- reads that Phase 3b depends on ----------------------------------------


@dataclass(frozen=True)
class MarketCap:
    """A market cap that can name the share count it was computed from."""

    value: float
    price: float
    shares_outstanding: float
    shares_known_at_utc: datetime
    shares_accession: str | None
    shares_observation_id: int | None


def shares_outstanding_as_of(session, *, entity_cik: str, as_of: datetime):
    """The latest share count knowable at ``as_of``, or ``None``.

    ``None`` is a real answer: before a company's first cover-page tagging
    there is no share count, and inventing one is how a market cap becomes
    fiction.
    """
    return latest_observation_as_of(
        session,
        fact_type="shares_outstanding",
        cutoff=as_of,
        entity_cik=entity_cik,
    )


def market_cap_as_of(
    session, *, entity_cik: str, price: float, as_of: datetime
) -> MarketCap | None:
    """``price x shares_outstanding`` using the latest share count known then.

    Returns ``None`` when no share count was knowable at ``as_of``. Spec N
    section 4.0: market cap needs a share count and the price file does not
    carry one, so a name without one has no market cap — not a market cap
    computed from a share count it could not have had.
    """
    row = shares_outstanding_as_of(session, entity_cik=entity_cik, as_of=as_of)
    if row is None or row.value_numeric is None or row.value_numeric <= 0:
        return None
    return MarketCap(
        value=float(price) * float(row.value_numeric),
        price=float(price),
        shares_outstanding=float(row.value_numeric),
        shares_known_at_utc=row.known_at_utc,
        shares_accession=row.accession,
        shares_observation_id=row.id,
    )
