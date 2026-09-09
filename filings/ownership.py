"""Schedules 13D and 13G — beneficial ownership above the threshold.

Spec O section 3.1:

| Form | Content | Honest use |
|---|---|---|
| **13D** | Above-threshold ownership *with intent to influence* | Activist situations; the intent narrative matters more than the stake |
| **13G** | Passive above-threshold ownership | Ownership structure, float, index/passive share |

**13F is out of scope for this phase** and the reason is in the spec: at a
1–20 session horizon a quarterly snapshot filed up to 45 days late is nearly
useless, and it is the most work of the three planes. Nothing here reads it.

Two levels of fact come out of a 13D/G, and the difference is worth being
explicit about because only one of them is guaranteed:

**The filing event** — "a 13D was accepted against this issuer at this instant,
by this filer". Available for every 13D/G ever filed, straight from the
submissions index, with the acceptance stamp that makes it point-in-time. This
always lands.

**The structured cover-page facts** — percent of class, shares beneficially
owned, sole/shared voting and dispositive power. SEC moved Schedules 13D/G to
a structured ``primary_doc.xml`` with the 2023 beneficial-ownership amendments;
older filings are free-text cover pages with no machine-readable form at all.
So the parser reads the structured document **when it exists** and the ingest
records coverage rather than pretending: a filing with no structured document
produces the event row and a ``no_structured_cover_page`` warning, never a
regex guess at a percentage. Extracting numbers from a free-text cover page
with a regex is how a 4.9% passive stake becomes a 49% control position.

The structured element names below are read from the document rather than
assumed: every one of them is looked up case-insensitively across a small set
of documented spellings, and a document that yields no recognised field raises
``AdapterSchemaError`` instead of writing nulls.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from filings import client as sec_client
from filings.errors import AdapterSchemaError
from filings.observations import (
    PRECISION_SECOND,
    PROVENANCE_VENDOR_PIT,
    TRUST_PRIMARY_REGULATOR,
    Observation,
)
from utils.logger import get_logger

log = get_logger("ownership")

SOURCE_SCHEDULE_13 = "sec_schedule_13"

FACT_TYPE_13D_EVENT = "beneficial_ownership_13d_event"
FACT_TYPE_13G_EVENT = "beneficial_ownership_13g_event"
FACT_TYPE_PERCENT_OF_CLASS = "beneficial_ownership_percent_of_class"
FACT_TYPE_SHARES_OWNED = "beneficial_ownership_shares"

WARN_NO_STRUCTURED_COVER_PAGE = "no_structured_cover_page"
WARN_MULTIPLE_FILERS = "multiple_reporting_persons"

MAX_DOCUMENT_BYTES = 8 * 1024 * 1024

#: Form -> (fact type, whether the filer asserts intent to influence).
SCHEDULE_FORMS: dict[str, tuple[str, bool]] = {
    "SC 13D": (FACT_TYPE_13D_EVENT, True),
    "SC 13D/A": (FACT_TYPE_13D_EVENT, True),
    "SC 13G": (FACT_TYPE_13G_EVENT, False),
    "SC 13G/A": (FACT_TYPE_13G_EVENT, False),
}


def is_schedule_13(form: str) -> bool:
    return (form or "").strip().upper() in SCHEDULE_FORMS


def schedule_kind(form: str) -> tuple[str, bool]:
    key = (form or "").strip().upper()
    if key not in SCHEDULE_FORMS:
        raise AdapterSchemaError(f"not a Schedule 13D/G form: {form!r}")
    return SCHEDULE_FORMS[key]


# --- the structured cover page ---------------------------------------------


def _find_first(root: ET.Element, names: Sequence[str]) -> str:
    """First non-empty text under any of ``names``, matched case-insensitively."""
    wanted = {name.lower() for name in names}
    for node in root.iter():
        if node.tag.split("}")[-1].lower() not in wanted:
            continue
        inner = node.find("value")
        text = "".join((inner if inner is not None else node).itertext()).strip()
        if text:
            return text
    return ""


def _number(raw: str, *, where: str) -> float | None:
    value = (raw or "").strip().replace(",", "").replace("%", "")
    if not value:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise AdapterSchemaError(f"{where}: expected a number, got {raw!r}") from exc


@dataclass(frozen=True)
class ReportingPerson:
    name: str
    cik: str | None
    percent_of_class: float | None
    shares_beneficially_owned: float | None
    sole_voting_power: float | None = None
    shared_voting_power: float | None = None
    sole_dispositive_power: float | None = None
    shared_dispositive_power: float | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "cik": self.cik,
            "percent_of_class": self.percent_of_class,
            "shares_beneficially_owned": self.shares_beneficially_owned,
            "sole_voting_power": self.sole_voting_power,
            "shared_voting_power": self.shared_voting_power,
            "sole_dispositive_power": self.sole_dispositive_power,
            "shared_dispositive_power": self.shared_dispositive_power,
        }


@dataclass(frozen=True)
class ScheduleCoverPage:
    issuer_name: str
    issuer_cusip: str
    event_date: date | None
    reporting_persons: tuple[ReportingPerson, ...]


def parse_schedule_13(xml_text: str | bytes) -> ScheduleCoverPage:
    """Parse a Schedule 13D/G ``primary_doc.xml`` cover page."""
    raw = xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise AdapterSchemaError(
            f"schedule13: document is {len(raw)} bytes, over the "
            f"{MAX_DOCUMENT_BYTES}-byte ceiling."
        )
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise AdapterSchemaError(f"schedule13: not well-formed XML: {exc}") from exc

    issuer_name = _find_first(root, ("issuerName", "nameOfIssuer", "companyConformedName"))
    cusip = _find_first(root, ("issuerCUSIP", "cusip", "cusipNumber"))
    event_raw = _find_first(root, ("dateOfEvent", "eventDate", "dateOfEventRequiringStatement"))

    persons: list[ReportingPerson] = []
    person_nodes = [
        node
        for node in root.iter()
        if node.tag.split("}")[-1].lower()
        in {"reportingperson", "coverpageheaderreportingpersondetails", "filerinfo"}
    ]
    for node in person_nodes:
        name = _find_first(node, ("reportingPersonName", "filerName", "name", "companyConformedName"))
        if not name:
            continue
        cik_raw = _find_first(node, ("reportingPersonCik", "filerCik", "cik"))
        persons.append(
            ReportingPerson(
                name=name,
                cik=sec_client.normalise_cik(cik_raw) if cik_raw.isdigit() else None,
                percent_of_class=_number(
                    _find_first(node, ("percentOfClass", "aggregateAmountPercentOfClass")),
                    where="schedule13.percentOfClass",
                ),
                shares_beneficially_owned=_number(
                    _find_first(
                        node,
                        ("aggregateAmountOwned", "aggregateAmountBeneficiallyOwned",
                         "amountBeneficiallyOwned"),
                    ),
                    where="schedule13.aggregateAmountOwned",
                ),
                sole_voting_power=_number(
                    _find_first(node, ("soleVotingPower",)), where="schedule13.soleVotingPower"
                ),
                shared_voting_power=_number(
                    _find_first(node, ("sharedVotingPower",)),
                    where="schedule13.sharedVotingPower",
                ),
                sole_dispositive_power=_number(
                    _find_first(node, ("soleDispositivePower",)),
                    where="schedule13.soleDispositivePower",
                ),
                shared_dispositive_power=_number(
                    _find_first(node, ("sharedDispositivePower",)),
                    where="schedule13.sharedDispositivePower",
                ),
            )
        )

    if not issuer_name and not persons:
        raise AdapterSchemaError(
            "schedule13: the document carries neither an issuer name nor a "
            "reporting person. Either it is not a structured cover page or the "
            "element names changed; both are things to look at, not to absorb."
        )

    event_date = None
    if event_raw:
        try:
            event_date = date.fromisoformat(event_raw[:10])
        except ValueError:
            # Cover pages carry dates in several styles; an unparseable one is
            # dropped rather than guessed, and the filing event still lands.
            log.warning("schedule13_unparseable_event_date", raw=event_raw)

    return ScheduleCoverPage(
        issuer_name=issuer_name,
        issuer_cusip=cusip.strip().upper(),
        event_date=event_date,
        reporting_persons=tuple(persons),
    )


# --- observations -----------------------------------------------------------


def schedule_13_observations(
    filing,
    *,
    entity_cik: str,
    cover_page: ScheduleCoverPage | None,
    ticker_at_time: str | None = None,
) -> list[Observation]:
    """The filing event, plus the cover-page facts when they are machine-readable."""
    fact_type, asserts_intent = schedule_kind(filing.form)
    source_url = sec_client.filing_index_url(entity_cik, filing.accession)
    warnings: list[str] = []
    if cover_page is None:
        warnings.append(WARN_NO_STRUCTURED_COVER_PAGE)
    elif len(cover_page.reporting_persons) > 1:
        warnings.append(WARN_MULTIPLE_FILERS)

    persons = list(cover_page.reporting_persons) if cover_page else []
    base_payload = {
        "form": filing.form,
        "is_amendment": filing.form.upper().endswith("/A"),
        "asserts_intent_to_influence": asserts_intent,
        "issuer_name": cover_page.issuer_name if cover_page else None,
        "issuer_cusip": cover_page.issuer_cusip if cover_page else None,
        "event_date": (
            cover_page.event_date.isoformat()
            if cover_page and cover_page.event_date
            else None
        ),
        "reporting_persons": [p.as_dict() for p in persons],
        "structured_cover_page": cover_page is not None,
        "filing_date": filing.filing_date.isoformat() if filing.filing_date else None,
    }

    out = [
        Observation(
            source=SOURCE_SCHEDULE_13,
            entity_cik=entity_cik,
            ticker_at_time=ticker_at_time,
            fact_type=fact_type,
            valid_at=filing.acceptance,
            known_at_utc=filing.acceptance,
            known_at_source="acceptanceDateTime",
            precision=PRECISION_SECOND,
            provenance_class=PROVENANCE_VENDOR_PIT,
            replay_eligible=True,
            value_text=filing.form,
            accession=filing.accession,
            source_url=source_url,
            source_trust=TRUST_PRIMARY_REGULATOR,
            payload=dict(base_payload),
            quality_warnings=tuple(warnings),
        )
    ]

    for person in persons:
        for value, fact, unit in (
            (person.percent_of_class, FACT_TYPE_PERCENT_OF_CLASS, "percent"),
            (person.shares_beneficially_owned, FACT_TYPE_SHARES_OWNED, "shares"),
        ):
            if value is None:
                continue
            payload = dict(base_payload)
            payload["reporting_person"] = person.as_dict()
            out.append(
                Observation(
                    source=SOURCE_SCHEDULE_13,
                    entity_cik=entity_cik,
                    ticker_at_time=ticker_at_time,
                    fact_type=fact,
                    valid_at=filing.acceptance,
                    known_at_utc=filing.acceptance,
                    known_at_source="acceptanceDateTime",
                    precision=PRECISION_SECOND,
                    provenance_class=PROVENANCE_VENDOR_PIT,
                    replay_eligible=True,
                    value_numeric=value,
                    unit=unit,
                    accession=filing.accession,
                    source_url=source_url,
                    source_trust=TRUST_PRIMARY_REGULATOR,
                    payload=payload,
                    quality_warnings=tuple(warnings),
                )
            )
    return out
