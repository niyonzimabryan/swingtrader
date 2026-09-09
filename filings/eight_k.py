"""The 8-K item index, beyond Item 2.02 — Spec O section 3.1.

Phase 3a took the one item Spec N's event clock needs: 2.02, Results of
Operations, whose ``acceptanceDateTime`` dates an earnings announcement to the
second. That row keeps its own ``fact_type`` and is untouched here — Phase 3b
reads it.

This module indexes **every** item on **every** 8-K, one observation per
(filing, item), because event detection is not only earnings. An auditor
resignation (4.01), a non-reliance restatement (4.02), a CEO departure (5.02),
a material agreement (1.01) and a bankruptcy (1.03) are all events a cohort can
be conditioned on, and all of them carry the same second-resolution acceptance
stamp that makes the timestamp defensible.

Item codes come from SEC's Form 8-K, and an item that is not in the table is
**kept** with its code and flagged rather than dropped: SEC adds items, and a
silently discarded 8-K item is an event that never happened as far as any
cohort is concerned.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable

from filings import client as sec_client
from filings.observations import (
    PRECISION_SECOND,
    PROVENANCE_VENDOR_PIT,
    TRUST_PRIMARY_REGULATOR,
    Observation,
)

SOURCE_EIGHT_K_ITEMS = "sec_8k_items"
FACT_TYPE_EIGHT_K_ITEM = "eight_k_item"

WARN_UNKNOWN_ITEM = "eight_k_item_not_in_table"

#: Item number -> title, from SEC's Form 8-K. Sections 1-9.
EIGHT_K_ITEMS: dict[str, str] = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.04": "Mine Safety — Reporting of Shutdowns and Patterns of Violations",
    "1.05": "Material Cybersecurity Incidents",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation",
    "2.04": "Triggering Events That Accelerate a Direct Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Notice of Delisting or Failure to Satisfy a Listing Rule",
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure or Election of Directors or Certain Officers",
    "5.03": "Amendments to Articles of Incorporation or Bylaws; Change in Fiscal Year",
    "5.04": "Temporary Suspension of Trading Under Registrant's Employee Benefit Plans",
    "5.05": "Amendment to Registrant's Code of Ethics, or Waiver of a Provision",
    "5.06": "Change in Shell Company Status",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "5.08": "Shareholder Director Nominations",
    "6.01": "ABS Informational and Computational Material",
    "6.02": "Change of Servicer or Trustee",
    "6.03": "Change in Credit Enhancement or Other External Support",
    "6.04": "Failure to Make a Required Distribution",
    "6.05": "Securities Act Updating Disclosure",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}

#: Items that carry no information about the business on their own. 9.01 is
#: the exhibit list that rides along with almost every 8-K; conditioning a
#: cohort on it would select for "filed an 8-K", not for an event.
NON_EVENT_ITEMS = frozenset({"9.01"})


def item_title(code: str) -> str | None:
    return EIGHT_K_ITEMS.get((code or "").strip())


def eight_k_item_observations(
    company,
    *,
    since: date | None = None,
    ticker_at_time: str | None = None,
    include_non_event_items: bool = False,
) -> list[Observation]:
    """One observation per item on every 8-K the company has filed.

    ``valid_at`` and ``known_at_utc`` are both the acceptance instant: an
    announcement applies when it is made, and — this is the whole point of the
    plane — it becomes knowable at the same moment, to the second, from the
    regulator.
    """
    out: list[Observation] = []
    for filing in company.filings:
        if not filing.form.upper().startswith("8-K"):
            continue
        if since is not None and filing.acceptance.date() < since:
            continue
        for code in filing.items:
            if code in NON_EVENT_ITEMS and not include_non_event_items:
                continue
            title = item_title(code)
            warnings = () if title else (WARN_UNKNOWN_ITEM,)
            out.append(
                Observation(
                    source=SOURCE_EIGHT_K_ITEMS,
                    entity_cik=company.cik,
                    ticker_at_time=ticker_at_time or company.primary_ticker,
                    fact_type=FACT_TYPE_EIGHT_K_ITEM,
                    valid_at=filing.acceptance,
                    known_at_utc=filing.acceptance,
                    known_at_source="acceptanceDateTime",
                    precision=PRECISION_SECOND,
                    provenance_class=PROVENANCE_VENDOR_PIT,
                    replay_eligible=True,
                    value_text=code,
                    accession=filing.accession,
                    source_url=sec_client.filing_index_url(
                        company.cik, filing.accession
                    ),
                    source_trust=TRUST_PRIMARY_REGULATOR,
                    payload={
                        "item": code,
                        "item_title": title,
                        "form": filing.form,
                        "all_items": list(filing.items),
                        "is_amendment": filing.form.upper().endswith("/A"),
                        "primary_document": filing.primary_document,
                        "report_date": (
                            filing.report_date.isoformat() if filing.report_date else None
                        ),
                        # Audit only; the ledger refuses to let it be a timestamp.
                        "filing_date": (
                            filing.filing_date.isoformat() if filing.filing_date else None
                        ),
                    },
                    quality_warnings=warnings,
                )
            )
    return out


def item_coverage(observations: Iterable[Observation]) -> dict:
    """``{item code: count}`` — logged at each checkpoint."""
    counts: dict[str, int] = {}
    for obs in observations:
        if obs.fact_type != FACT_TYPE_EIGHT_K_ITEM:
            continue
        code = obs.value_text or ""
        counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items()))
