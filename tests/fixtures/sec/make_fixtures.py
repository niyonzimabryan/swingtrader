"""Rebuild the synthetic SEC fixtures in this directory.

Run from the repository root: ``python tests/fixtures/sec/make_fixtures.py``.

See README.md here for why these are synthetic rather than recorded, and for
``scripts/record_sec_fixtures.py``, which replaces them with real responses on
a machine that can reach ``data.sec.gov``.
"""
import json, os
from datetime import date, timedelta

OUT = "tests/fixtures/sec"
os.makedirs(OUT, exist_ok=True)

FIELDS = ["accessionNumber","filingDate","reportDate","acceptanceDateTime","act","form",
          "fileNumber","filmNumber","items","core_type","size","isXBRL","isInlineXBRL",
          "isXBRLNumeric","primaryDocument","primaryDocDescription"]

class Sub:
    def __init__(self, cik, name, ticker, exchange="Nasdaq"):
        self.cik = cik; self.name = name; self.ticker = ticker; self.exchange = exchange
        self.rows = []
        self.seq = 0
    def add(self, form, filing_date, acceptance_time, *, items="", report_date=None, doc="doc.htm"):
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
            "fileNumber": "001-90001",
            "filmNumber": "",
            "items": items,
            "core_type": form,
            "size": 123456,
            "isXBRL": 1,
            "isInlineXBRL": 1,
            "isXBRLNumeric": 1,
            "primaryDocument": doc,
            "primaryDocDescription": form,
        })
        return accn
    def payload(self):
        # newest first, as EDGAR returns
        rows = sorted(self.rows, key=lambda r: r["acceptanceDateTime"], reverse=True)
        recent = {field: [r[field] for r in rows] for field in FIELDS}
        return {
            "cik": self.cik.lstrip("0"),
            "entityType": "operating",
            "sic": "3571",
            "name": self.name,
            "tickers": [self.ticker],
            "exchanges": [self.exchange],
            "formerNames": [],
            "filings": {"recent": recent, "files": []},
        }

class Facts:
    def __init__(self, cik, name):
        self.cik = int(cik); self.name = name; self.facts = {}
    def add(self, taxonomy, tag, unit, entry, label=None):
        block = self.facts.setdefault(taxonomy, {}).setdefault(
            tag, {"label": label or tag, "description": label or tag, "units": {}}
        )
        block["units"].setdefault(unit, []).append(entry)
    def payload(self):
        return {"cik": self.cik, "entityName": self.name, "facts": self.facts}

def quarters(start_year, n):
    """(start, end) pairs on a ~91-day cadence."""
    end = date(start_year, 3, 31)
    out = []
    for _ in range(n):
        start = end - timedelta(days=90)
        out.append((start, end))
        end = end + timedelta(days=91)
    return out

ACCEPT_TIMES = ["16:31:02", "21:07:31", "13:45:09", "22:30:44"]

def build(cik, name, ticker, *, migrate_at=None, skip_quarter=None, dual_class=False,
          n_quarters=16, start_year=2016):
    sub = Sub(cik, name, ticker)
    facts = Facts(cik, name)
    qs = quarters(start_year, n_quarters)
    eps = 1.00
    revenue = 4_000_000_000
    shares = 1_200_000_000
    for index, (period_start, period_end) in enumerate(qs):
        release_day = period_end + timedelta(days=25)
        filing_day = period_end + timedelta(days=32)
        accept = ACCEPT_TIMES[index % len(ACCEPT_TIMES)]

        # The earnings 8-K: Item 2.02 Results of Operations.
        sub.add("8-K", release_day, ACCEPT_TIMES[(index + 3) % len(ACCEPT_TIMES)],
                items="2.02,9.01", report_date=release_day, doc="ex99-1.htm")
        # An 8-K that is not an earnings release, to prove the filter.
        if index % 4 == 1:
            sub.add("8-K", release_day + timedelta(days=3), "18:02:11",
                    items="5.02,7.01", report_date=release_day + timedelta(days=3))
        # A Form 4 accepted after the close: the verified leak case (claim 13).
        if index % 5 == 0:
            sub.add("4", release_day + timedelta(days=5), "22:30:44",
                    report_date=release_day + timedelta(days=4), doc="xslF345X03/form4.xml")

        annual = period_end.month == 12
        form = "10-K" if annual else "10-Q"
        accn = sub.add(form, filing_day, accept, report_date=period_end,
                       doc=f"{ticker.lower()}-{period_end.isoformat()}.htm")

        fy = period_end.year
        fp = "FY" if annual else f"Q{(period_end.month - 1)//3 + 1}"
        common = {"accn": accn, "fy": fy, "fp": fp, "form": form,
                  "filed": filing_day.isoformat(), "frame": f"CY{fy}Q{(period_end.month-1)//3+1}"}

        revenue += 75_000_000
        eps += 0.03
        shares -= 4_000_000

        if not (skip_quarter is not None and index == skip_quarter):
            tag = "Revenues"
            if migrate_at is not None and index >= migrate_at:
                tag = "RevenueFromContractWithCustomerExcludingAssessedTax"
            facts.add("us-gaap", tag, "USD",
                      {"start": period_start.isoformat(), "end": period_end.isoformat(),
                       "val": revenue, **common},
                      label="Revenues")

        facts.add("us-gaap", "EarningsPerShareDiluted", "USD/shares",
                  {"start": period_start.isoformat(), "end": period_end.isoformat(),
                   "val": round(eps, 2), **common}, label="Earnings Per Share, Diluted")
        facts.add("us-gaap", "NetIncomeLoss", "USD",
                  {"start": period_start.isoformat(), "end": period_end.isoformat(),
                   "val": int(revenue * 0.21), **common}, label="Net Income (Loss)")

        cover_date = filing_day - timedelta(days=2)
        instant = {"end": cover_date.isoformat(), "val": shares, "accn": accn,
                   "fy": fy, "fp": fp, "form": form, "filed": filing_day.isoformat()}
        facts.add("dei", "EntityCommonStockSharesOutstanding", "shares", dict(instant),
                  label="Entity Common Stock, Shares Outstanding")
        if dual_class:
            facts.add("dei", "EntityCommonStockSharesOutstanding", "shares",
                      dict(instant, val=int(shares * 0.15)),
                      label="Entity Common Stock, Shares Outstanding")

    return sub, facts

def write(sub, facts):
    cik = sub.cik
    with open(f"{OUT}/submissions_CIK{cik}.json", "w") as fh:
        json.dump(sub.payload(), fh, indent=1)
        fh.write("\n")
    with open(f"{OUT}/companyfacts_CIK{cik}.json", "w") as fh:
        json.dump(facts.payload(), fh, indent=1)
        fh.write("\n")

write(*build("0001000001", "Tagmigration Industries Inc", "TAGM", migrate_at=8))
write(*build("0001000002", "Gapco Holdings Inc", "GAPC", skip_quarter=6))
write(*build("0001000003", "Dualclass Systems Inc", "DUAL", dual_class=True))

tickers = {
    "0": {"cik_str": 1000001, "ticker": "TAGM", "title": "Tagmigration Industries Inc"},
    "1": {"cik_str": 1000002, "ticker": "GAPC", "title": "Gapco Holdings Inc"},
    "2": {"cik_str": 1000003, "ticker": "DUAL", "title": "Dualclass Systems Inc"},
}
with open(f"{OUT}/company_tickers.json", "w") as fh:
    json.dump(tickers, fh, indent=1)
    fh.write("\n")
print("written")
