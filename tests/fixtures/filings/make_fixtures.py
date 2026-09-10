"""Rebuild the synthetic Phase 4 filings fixtures in this directory.

Run from the repository root::

    python tests/fixtures/filings/make_fixtures.py

See README.md here for why these are synthetic rather than recorded, and for
``scripts/record_filings_fixtures.py``, which replaces them with real responses
on a machine that can reach ``www.sec.gov``.

Everything is deliberately fictional: CIKs in the ``00020000xx`` block, tickers
``RCYC``/``NUCO``/``INSD``/``ACTV``, and people named after the case they
exercise, so no reader can mistake a generated number for a filed one.
"""
import json
import os
from datetime import date

OUT = os.path.dirname(os.path.abspath(__file__))

FIELDS = [
    "accessionNumber", "filingDate", "reportDate", "acceptanceDateTime", "act",
    "form", "fileNumber", "filmNumber", "items", "core_type", "size", "isXBRL",
    "isInlineXBRL", "isXBRLNumeric", "primaryDocument", "primaryDocDescription",
]


class Sub:
    def __init__(self, cik, name, ticker, exchange="Nasdaq", former_names=()):
        self.cik = cik
        self.name = name
        self.ticker = ticker
        self.exchange = exchange
        self.former_names = list(former_names)
        self.rows = []
        self.seq = 0

    def add(self, form, filing_date, acceptance_time, *, items="", doc="doc.htm",
            report_date=None):
        self.seq += 1
        yy = filing_date.strftime("%y")
        accn = f"{self.cik}-{yy}-{self.seq:06d}"
        self.rows.append({
            "accessionNumber": accn,
            "filingDate": filing_date.isoformat(),
            "reportDate": (report_date or filing_date).isoformat(),
            "acceptanceDateTime": f"{filing_date.isoformat()}T{acceptance_time}.000Z",
            "act": "34",
            "form": form,
            "fileNumber": "001-90002",
            "filmNumber": "",
            "items": items,
            "core_type": form,
            "size": 45678,
            "isXBRL": 0,
            "isInlineXBRL": 0,
            "isXBRLNumeric": 0,
            "primaryDocument": doc,
            "primaryDocDescription": form,
        })
        return accn

    def payload(self):
        rows = sorted(self.rows, key=lambda r: r["acceptanceDateTime"], reverse=True)
        recent = {field: [r[field] for r in rows] for field in FIELDS}
        return {
            "cik": self.cik.lstrip("0"),
            "entityType": "operating",
            "sic": "3571",
            "name": self.name,
            "tickers": [self.ticker],
            "exchanges": [self.exchange],
            "formerNames": self.former_names,
            "filings": {"recent": recent, "files": []},
        }


def form4(*, issuer_cik, issuer_name, symbol, owner_cik, owner_name, period,
          transactions, form_type="4", is_director=1, is_officer=1,
          officer_title="Chief Executive Officer", footnotes=(),
          document_10b5_1=None):
    """Build an ownership document to the shape EDGAR publishes.

    ``transactions`` entries are dicts with ``code``, ``date``, ``shares``,
    ``price``, ``acquired_disposed``, ``owned_after``, optional ``security``,
    ``footnote`` and ``flag_10b5_1`` (which emits the per-transaction element).
    """
    parts = [
        '<?xml version="1.0"?>',
        "<ownershipDocument>",
        "  <schemaVersion>X0508</schemaVersion>",
        f"  <documentType>{form_type}</documentType>",
        f"  <periodOfReport>{period}</periodOfReport>",
    ]
    if document_10b5_1 is not None:
        parts.append(f"  <aff10b5One>{1 if document_10b5_1 else 0}</aff10b5One>")
    parts += [
        "  <issuer>",
        f"    <issuerCik>{issuer_cik}</issuerCik>",
        f"    <issuerName>{issuer_name}</issuerName>",
        f"    <issuerTradingSymbol>{symbol}</issuerTradingSymbol>",
        "  </issuer>",
        "  <reportingOwner>",
        "    <reportingOwnerId>",
        f"      <rptOwnerCik>{owner_cik}</rptOwnerCik>",
        f"      <rptOwnerName>{owner_name}</rptOwnerName>",
        "    </reportingOwnerId>",
        "    <reportingOwnerRelationship>",
        f"      <isDirector>{is_director}</isDirector>",
        f"      <isOfficer>{is_officer}</isOfficer>",
        "      <isTenPercentOwner>0</isTenPercentOwner>",
        f"      <officerTitle>{officer_title}</officerTitle>",
        "    </reportingOwnerRelationship>",
        "  </reportingOwner>",
        "  <nonDerivativeTable>",
    ]
    for t in transactions:
        parts += [
            "    <nonDerivativeTransaction>",
            "      <securityTitle>",
            f"        <value>{t.get('security', 'Common Stock')}</value>",
            "      </securityTitle>",
            "      <transactionDate>",
            f"        <value>{t['date']}</value>",
            "      </transactionDate>",
            "      <transactionCoding>",
            "        <transactionFormType>4</transactionFormType>",
            f"        <transactionCode>{t['code']}</transactionCode>",
            "        <equitySwapInvolved>0</equitySwapInvolved>",
        ]
        if t.get("flag_10b5_1") is not None:
            parts.append(
                f"        <rule10b5-1Flag><value>{1 if t['flag_10b5_1'] else 0}"
                "</value></rule10b5-1Flag>"
            )
        parts += [
            "      </transactionCoding>",
            "      <transactionAmounts>",
            f"        <transactionShares><value>{t['shares']}</value></transactionShares>",
        ]
        if t.get("price") is not None:
            parts.append(
                "        <transactionPricePerShare>"
                f"<value>{t['price']}</value></transactionPricePerShare>"
            )
        parts += [
            "        <transactionAcquiredDisposedCode>"
            f"<value>{t['acquired_disposed']}</value>"
            "</transactionAcquiredDisposedCode>",
            "      </transactionAmounts>",
            "      <postTransactionAmounts>",
            "        <sharesOwnedFollowingTransaction>"
            f"<value>{t['owned_after']}</value>"
            "</sharesOwnedFollowingTransaction>",
            "      </postTransactionAmounts>",
            "      <ownershipNature>",
            "        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>",
            "      </ownershipNature>",
        ]
        if t.get("footnote"):
            parts.append(f'      <footnoteId id="{t["footnote"]}"/>')
        parts.append("    </nonDerivativeTransaction>")
    parts.append("  </nonDerivativeTable>")

    if footnotes:
        parts.append("  <footnotes>")
        for fid, text in footnotes:
            parts.append(f'    <footnote id="{fid}">{text}</footnote>')
        parts.append("  </footnotes>")
    parts.append("</ownershipDocument>")
    return "\n".join(parts) + "\n"


def schedule_13(*, issuer_name, cusip, event_date, persons):
    parts = [
        '<?xml version="1.0"?>',
        "<edgarSubmission>",
        "  <coverPageHeader>",
        f"    <issuerName>{issuer_name}</issuerName>",
        f"    <issuerCUSIP>{cusip}</issuerCUSIP>",
        f"    <dateOfEvent>{event_date}</dateOfEvent>",
        "  </coverPageHeader>",
    ]
    for person in persons:
        parts += [
            "  <reportingPerson>",
            f"    <reportingPersonName>{person['name']}</reportingPersonName>",
            f"    <reportingPersonCik>{person['cik']}</reportingPersonCik>",
            f"    <aggregateAmountOwned>{person['shares']}</aggregateAmountOwned>",
            f"    <percentOfClass>{person['percent']}</percentOfClass>",
            f"    <soleVotingPower>{person['shares']}</soleVotingPower>",
            "    <sharedVotingPower>0</sharedVotingPower>",
            f"    <soleDispositivePower>{person['shares']}</soleDispositivePower>",
            "    <sharedDispositivePower>0</sharedDispositivePower>",
            "  </reportingPerson>",
        ]
    parts.append("</edgarSubmission>")
    return "\n".join(parts) + "\n"


def write(name, text):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as handle:
        handle.write(text)
    print(f"  wrote {name}")


# --- CIK 0002000001 — the insider-code company ------------------------------
#
# One issuer, four insiders, every code category, an after-the-close acceptance
# and a 4/A that amends one of them.

INSIDER_CIK = "0002000001"
insider = Sub(INSIDER_CIK, "Insider Codes Test Corp", "INSD", former_names=[])

FORM4_DOCS = {}

def add_form4(filing_date, acceptance, doc_name, **kwargs):
    accn = insider.add("4", filing_date, acceptance, doc=f"xslF345X05/{doc_name}")
    FORM4_DOCS[doc_name] = form4(
        issuer_cik=INSIDER_CIK, issuer_name="Insider Codes Test Corp",
        symbol="INSD", **kwargs
    )
    return accn


# An open-market purchase, accepted at 22:30 UTC — after the close on its own
# filing date. This is the verified claim-13 case that makes filingDate a leak.
add_form4(
    date(2026, 3, 3), "22:30:44", "form4_purchase.xml",
    owner_cik="0002100001", owner_name="Purchaser, Pat", period="2026-03-02",
    transactions=[dict(code="P", date="2026-03-02", shares=12000, price=41.50,
                       acquired_disposed="A", owned_after=112000)],
)
# A second, different insider buying two days later: makes a cluster of two.
add_form4(
    date(2026, 3, 5), "21:05:10", "form4_purchase_second_insider.xml",
    owner_cik="0002100002", owner_name="Buyer, Bev", period="2026-03-04",
    is_officer=0, officer_title="",
    transactions=[dict(code="P", date="2026-03-04", shares=5000, price=42.10,
                       acquired_disposed="A", owned_after=45000)],
)
# A grant. Never pooled with the two above.
add_form4(
    date(2026, 3, 6), "20:15:00", "form4_award.xml",
    owner_cik="0002100003", owner_name="Grantee, Gil", period="2026-03-05",
    transactions=[dict(code="A", date="2026-03-05", shares=30000, price=0.0,
                       acquired_disposed="A", owned_after=130000)],
)
# An option exercise plus the tax-withholding sale that follows it, on one form.
add_form4(
    date(2026, 3, 9), "19:40:00", "form4_exercise_and_withholding.xml",
    owner_cik="0002100003", owner_name="Grantee, Gil", period="2026-03-06",
    transactions=[
        dict(code="M", date="2026-03-06", shares=10000, price=12.00,
             acquired_disposed="A", owned_after=140000),
        dict(code="F", date="2026-03-06", shares=4200, price=43.00,
             acquired_disposed="D", owned_after=135800),
    ],
)
# A gift.
add_form4(
    date(2026, 3, 10), "18:00:00", "form4_gift.xml",
    owner_cik="0002100002", owner_name="Buyer, Bev", period="2026-03-09",
    is_officer=0, officer_title="",
    transactions=[dict(code="G", date="2026-03-09", shares=1000, price=None,
                       acquired_disposed="D", owned_after=44000)],
)
# A 10b5-1 sale, flagged by the per-transaction element.
add_form4(
    date(2026, 3, 11), "21:55:00", "form4_planned_sale.xml",
    owner_cik="0002100004", owner_name="Seller, Sam", period="2026-03-10",
    transactions=[dict(code="S", date="2026-03-10", shares=8000, price=44.25,
                       acquired_disposed="D", owned_after=92000,
                       flag_10b5_1=True)],
)
# A 10b5-1 sale flagged only in a footnote — the fallback path.
add_form4(
    date(2026, 3, 12), "21:58:00", "form4_planned_sale_footnote.xml",
    owner_cik="0002100005", owner_name="Planner, Pia", period="2026-03-11",
    transactions=[dict(code="S", date="2026-03-11", shares=3000, price=44.90,
                       acquired_disposed="D", owned_after=57000, footnote="F1")],
    footnotes=[("F1", "This sale was made pursuant to a Rule 10b5-1 trading "
                      "plan adopted on 2025-11-14.")],
)
# A discretionary sale with no plan indication anywhere: the flag is unknown,
# and unknown must never be stored as "not a plan".
add_form4(
    date(2026, 3, 13), "22:02:00", "form4_unflagged_sale.xml",
    owner_cik="0002100006", owner_name="Unknown, Uma", period="2026-03-12",
    transactions=[dict(code="S", date="2026-03-12", shares=2000, price=45.10,
                       acquired_disposed="D", owned_after=18000)],
)
# The amendment: restates the first purchase's share count, 12000 -> 11500.
amendment_accn = insider.add(
    "4/A", date(2026, 3, 17), "20:11:00", doc="xslF345X05/form4_purchase_amended.xml"
)
FORM4_DOCS["form4_purchase_amended.xml"] = form4(
    issuer_cik=INSIDER_CIK, issuer_name="Insider Codes Test Corp", symbol="INSD",
    owner_cik="0002100001", owner_name="Purchaser, Pat", period="2026-03-02",
    form_type="4/A",
    transactions=[dict(code="P", date="2026-03-02", shares=11500, price=41.50,
                       acquired_disposed="A", owned_after=111500)],
)

# 8-K items beyond 2.02, so the item index has something to index.
insider.add("8-K", date(2026, 2, 4), "21:31:00", items="2.02,9.01")
insider.add("8-K", date(2026, 2, 18), "13:02:00", items="5.02")
insider.add("8-K", date(2026, 2, 25), "12:45:00", items="1.01,7.01")
insider.add("8-K", date(2026, 3, 2), "11:15:00", items="4.02")
insider.add("8-K", date(2026, 3, 4), "14:00:00", items="1.05")
insider.add("8-K", date(2026, 3, 16), "17:20:00", items="9.99")   # not in the table

# --- CIK 0002000002 — the activist target -----------------------------------

TARGET_CIK = "0002000002"
target = Sub(TARGET_CIK, "Schedule Thirteen Target Inc", "ACTV")
sc13d = target.add("SC 13D", date(2026, 4, 6), "16:40:00", doc="primary_doc.xml")
sc13d_a = target.add("SC 13D/A", date(2026, 5, 11), "16:44:00", doc="primary_doc.xml")
sc13g = target.add("SC 13G", date(2026, 2, 13), "15:12:00", doc="primary_doc.xml")

SCHEDULE_DOCS = {
    f"schedule13d_{sc13d}.xml": schedule_13(
        issuer_name="Schedule Thirteen Target Inc", cusip="00020000A",
        event_date="2026-03-27",
        persons=[dict(name="Fictional Activist Partners LP", cik="0002200001",
                      shares=6_400_000, percent=7.4)],
    ),
    f"schedule13d_{sc13d_a}.xml": schedule_13(
        issuer_name="Schedule Thirteen Target Inc", cusip="00020000A",
        event_date="2026-05-04",
        persons=[dict(name="Fictional Activist Partners LP", cik="0002200001",
                      shares=8_100_000, percent=9.3)],
    ),
    f"schedule13g_{sc13g}.xml": schedule_13(
        issuer_name="Schedule Thirteen Target Inc", cusip="00020000A",
        event_date="2025-12-31",
        persons=[dict(name="Fictional Index Trust", cik="0002200002",
                      shares=9_900_000, percent=11.2)],
    ),
}

# --- Ticker reuse: two CIKs, one symbol, different eras ----------------------
#
# ``RCYC`` was surrendered by the first filer in 2019 and picked up by the
# second in 2021. Resolving it against a current snapshot answers "the second"
# for a 2018 event, silently.

old_holder = Sub(
    "0002000003", "Recycled Ticker Holdings Inc", "OLDR",
    former_names=[
        {"name": "Recycled Ticker Corp",
         "from": "2012-01-04T00:00:00.000Z", "to": "2019-06-28T00:00:00.000Z"},
    ],
)
old_holder.add("8-K", date(2018, 5, 9), "20:02:00", items="8.01")
old_holder.add("8-K", date(2021, 5, 10), "20:02:00", items="8.01")

new_holder = Sub("0002000004", "Newco Under Reused Ticker Inc", "RCYC")
new_holder.add("8-K", date(2022, 7, 14), "20:02:00", items="8.01")

# --- write ------------------------------------------------------------------

if __name__ == "__main__":
    print(f"writing Phase 4 filings fixtures to {OUT}")
    for sub in (insider, target, old_holder, new_holder):
        write(f"submissions_CIK{sub.cik}.json",
              json.dumps(sub.payload(), indent=1) + "\n")
    for name, text in FORM4_DOCS.items():
        write(name, text)
    for name, text in SCHEDULE_DOCS.items():
        write(name, text)
    # The 13D/G documents are all called primary_doc.xml on EDGAR, so the
    # accession-keyed names above are what the test transport maps them from.
    write("schedule13_index.json", json.dumps({
        "sc_13d": sc13d, "sc_13d_a": sc13d_a, "sc_13g": sc13g,
        "form4_amendment": amendment_accn,
    }, indent=1) + "\n")
