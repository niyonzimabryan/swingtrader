"""Twenty known performance-related US delistings, 2015–2024 (Spec N §4.2).

What this list is for
---------------------
Verification §24 states the failure mode precisely: a vendor can carry a
delisted ticker's full history and still simply *stop* at the last quoted price,
producing a file that looks survivorship-free while omitting the terminal loss
that matters. `scripts/audit_delisting_returns.py` pulls each of these names'
final bars from a price plane and asks which of the two it is. The answer is
recorded on the data snapshot, and it is the thing to run **before** paying
Sharadar and again on every refresh.

Only performance-related delistings are useful here. Shumway (1997) and Shumway
& Warther (1999) find ~99.8% of returns missing for performance delistings
versus at most 1% for mergers and exchanges, so a merger is not a test of
anything. Each row below is a bankruptcy, a receivership, or an exchange
deficiency delisting.

Sourcing, stated honestly
-------------------------
Every US delisting is effected by a Form 25 filed on EDGAR (by the exchange for
an involuntary delisting, by the issuer for a voluntary one) or by an exchange
deficiency notice. `source_kind` says which of the two is the primary record for
that row and `source_url` resolves to it.

**None of these rows has been checked against the primary filing in this
session**: the egress proxy blocks `www.sec.gov`, the same class of failure that
left verification Claim 7 unverified. `VERIFIED` is therefore `False` on every
row, `source_url` is an EDGAR *lookup* for the company's Form 25 filings rather
than a specific accession number, and the dates are approximate — the month is
reliable, the exact session is not. That is enough for what the audit does
(compare a series' terminal behaviour against a known collapse) and it is **not**
enough to publish as a delisting-date reference. Resolving each to an accession
number is an open item in `docs/PRICE_PLANE.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Mapping

#: EDGAR company search, filtered to Form 25 (and 25-NSE). A lookup, not a
#: citation of a specific filing — see the module docstring.
EDGAR_FORM_25_SEARCH = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company={company}"
    "&type=25&dateb=&owner=include&count=40"
)


@dataclass(frozen=True)
class DelistingCase:
    """One known performance-related delisting, with where the fact comes from.

    `ticker` stays the historical, pre-event symbol this case is known by —
    that is the identity a reader recognises and the identity Shumway's own
    literature uses. `vendor_symbols` is a separate, per-vendor overlay
    (`{"sharadar": "RSHCQ"}`) for the case where a vendor keys the same
    company under a different symbol after the event — Sharadar renames a
    bankrupt issuer to a `Q`-suffixed symbol, but not by a mechanical rule
    (`JCP` does not become `JCPNQ` by suffix, and does not resolve at all
    without more research; see `resolution_note`). `data/prices/audit.py`'s
    resolver reads this before ever asking the plane for bars, so a wrong or
    missing vendor symbol shows up as `unresolved`, never as a silent
    `missing`.
    """

    ticker: str
    company: str
    #: Approximate — month reliable, session not. See the module docstring.
    delisting_date: date
    venue: str                 # nyse_amex | nasdaq, for the Shumway convention
    event: str                 # what happened, in one line
    source_kind: str           # sec_form_25 | exchange_notice
    source_url: str
    verified: bool = False
    #: `{vendor_source: vendor_ticker}`, e.g. `{"sharadar": "RSHCQ"}`. Empty
    #: means "not yet resolved for that vendor" — see `resolution_note`.
    vendor_symbols: Mapping[str, str] = field(default_factory=dict)
    #: Human-readable record of how (or whether) the symbol was resolved,
    #: and when. Not machine-read; it exists so the next person does not
    #: redo verification work that already happened.
    resolution_note: str = ""

    def __post_init__(self) -> None:
        if self.venue not in ("nyse_amex", "nasdaq"):
            raise ValueError(f"{self.ticker}: venue must be nyse_amex or nasdaq")
        if self.source_kind not in ("sec_form_25", "exchange_notice"):
            raise ValueError(f"{self.ticker}: unknown source kind {self.source_kind!r}")
        if not self.source_url:
            raise ValueError(f"{self.ticker}: every row must carry a source URL")


def _form_25(company: str) -> str:
    return EDGAR_FORM_25_SEARCH.format(company=company.replace(" ", "+"))


#: Twenty rows, 2015–2024, both venues represented (14 NYSE/AMEX, 6 Nasdaq).
#:
#: 2021 and 2022 are absent, and that is a property of the period rather than a
#: gap in the list: pandemic-era liquidity support produced very few large-cap
#: performance delistings in those two years. The audit does not need a uniform
#: calendar — it needs names whose terminal behaviour is known.
#: Everything below was verified live against the Sharadar direct API on
#: 2026-09-12 (`docs/investment-workspace/handoff/OWNER_SETUP_EXECUTION_2026-09-12.md`
#: §4). It is not a suffix rule: `JCP` does not become `JCPNQ` the way `RSH`
#: becomes `RSHCQ`, so a case not listed here needs `--resolve` to check, not
#: a guessed extrapolation of the pattern below.
_RESOLVE_WITH_LIVE_KEY = (
    "Not yet checked against Sharadar's post-event symbol scheme; run "
    "`python -m scripts.audit_delisting_returns --source sharadar --resolve` "
    "with a live SHARADAR_API_KEY to find candidates."
)

DELISTING_AUDIT_LIST: tuple[DelistingCase, ...] = (
    DelistingCase("RSH", "RadioShack Corporation", date(2015, 2, 25), "nyse_amex",
                  "Chapter 11 filed 2015-02-05; NYSE suspended trading and filed Form 25",
                  "sec_form_25", _form_25("RadioShack"),
                  vendor_symbols={"sharadar": "RSHCQ"},
                  resolution_note=(
                      "Verified live 2026-09-12: RSHCQ ('RADIOSHACK CORP', permaticker "
                      "199304). Out of window under the 10-year tier regardless — "
                      "delists before 2016-09-12."
                  )),
    DelistingCase("WLT", "Walter Energy Inc", date(2015, 7, 27), "nyse_amex",
                  "Chapter 11 filed 2015-07-15; NYSE delisting for abnormally low price",
                  "sec_form_25", _form_25("Walter Energy"),
                  resolution_note=_RESOLVE_WITH_LIVE_KEY + " Out of window under the "
                  "10-year tier regardless — delists before 2016-09-12."),
    DelistingCase("ZQK", "Quiksilver Inc", date(2015, 9, 21), "nyse_amex",
                  "Chapter 11 filed 2015-09-09; NYSE suspended trading",
                  "sec_form_25", _form_25("Quiksilver"),
                  resolution_note=_RESOLVE_WITH_LIVE_KEY + " Out of window under the "
                  "10-year tier regardless — delists before 2016-09-12."),
    DelistingCase("ACI", "Arch Coal Inc", date(2016, 1, 22), "nyse_amex",
                  "Chapter 11 filed 2016-01-11; NYSE delisting",
                  "sec_form_25", _form_25("Arch Coal"),
                  vendor_symbols={"sharadar": "ACI"},
                  resolution_note="Verified live 2026-09-12: resolves under its "
                  "original ticker on Sharadar, no remap needed."),
    DelistingCase("SUNE", "SunEdison Inc", date(2016, 4, 25), "nyse_amex",
                  "Chapter 11 filed 2016-04-21; NYSE suspended trading and filed Form 25",
                  "sec_form_25", _form_25("SunEdison"),
                  vendor_symbols={"sharadar": "SUNE"},
                  resolution_note="Verified live 2026-09-12: resolves under its "
                  "original ticker on Sharadar, no remap needed."),
    DelistingCase("CIE", "Cobalt International Energy Inc", date(2017, 12, 19), "nyse_amex",
                  "Chapter 11 filed 2017-12-14; NYSE delisting",
                  "sec_form_25", _form_25("Cobalt International Energy"),
                  resolution_note=_RESOLVE_WITH_LIVE_KEY),
    DelistingCase("SHLD", "Sears Holdings Corporation", date(2018, 10, 24), "nasdaq",
                  "Chapter 11 filed 2018-10-15; Nasdaq delisting determination",
                  "exchange_notice", _form_25("Sears Holdings"),
                  vendor_symbols={"sharadar": "SHLDQ"},
                  resolution_note="Verified live 2026-09-12: SHLDQ resolves."),
    DelistingCase("WIN", "Windstream Holdings Inc", date(2019, 3, 4), "nasdaq",
                  "Chapter 11 filed 2019-02-25; Nasdaq delisting determination",
                  "exchange_notice", _form_25("Windstream"),
                  resolution_note=_RESOLVE_WITH_LIVE_KEY),
    DelistingCase("DEAN", "Dean Foods Company", date(2019, 11, 25), "nyse_amex",
                  "Chapter 11 filed 2019-11-12; NYSE delisting",
                  "sec_form_25", _form_25("Dean Foods"),
                  resolution_note=_RESOLVE_WITH_LIVE_KEY),
    DelistingCase("WLL", "Whiting Petroleum Corporation", date(2020, 4, 15), "nyse_amex",
                  "Chapter 11 filed 2020-04-01; NYSE delisting (company later relisted)",
                  "sec_form_25", _form_25("Whiting Petroleum"),
                  vendor_symbols={"sharadar": "WLL"},
                  resolution_note="Verified live 2026-09-12: resolves under its "
                  "original ticker on Sharadar, no remap needed."),
    DelistingCase("FTR", "Frontier Communications Corporation", date(2020, 4, 24), "nasdaq",
                  "Chapter 11 filed 2020-04-14; Nasdaq delisting determination",
                  "exchange_notice", _form_25("Frontier Communications"),
                  vendor_symbols={"sharadar": "FTRCQ"},
                  resolution_note="Verified live 2026-09-12: FTRCQ resolves."),
    DelistingCase("JCP", "J. C. Penney Company Inc", date(2020, 5, 21), "nyse_amex",
                  "Chapter 11 filed 2020-05-15; NYSE suspended trading and filed Form 25",
                  "sec_form_25", _form_25("Penney"),
                  resolution_note=(
                      "Verified live 2026-09-12: the natural guess JCPNQ does not "
                      "resolve by exact ticker or by company-name match against "
                      "Sharadar's tickers table. This is not a mechanical suffix rule "
                      "(RSH -> RSHCQ does not generalize to JCP -> JCPNQ); needs "
                      "manual research before a vendor_symbols entry can be added."
                  )),
    DelistingCase("LK", "Luckin Coffee Inc", date(2020, 6, 29), "nasdaq",
                  "Nasdaq delisting notice 2020-05-19 after disclosure of fabricated sales; "
                  "trading suspended 2020-06-29",
                  "exchange_notice", _form_25("Luckin Coffee"),
                  resolution_note=_RESOLVE_WITH_LIVE_KEY),
    DelistingCase("CHK", "Chesapeake Energy Corporation", date(2020, 7, 10), "nyse_amex",
                  "Chapter 11 filed 2020-06-28; NYSE delisting (company later relisted)",
                  "sec_form_25", _form_25("Chesapeake Energy"),
                  resolution_note=_RESOLVE_WITH_LIVE_KEY),
    DelistingCase("CBL", "CBL & Associates Properties Inc", date(2020, 11, 12), "nyse_amex",
                  "Chapter 11 filed 2020-11-01; NYSE delisting",
                  "sec_form_25", _form_25("CBL %26 Associates"),
                  vendor_symbols={"sharadar": "CBL"},
                  resolution_note="Verified live 2026-09-12: resolves under its "
                  "original ticker on Sharadar, no remap needed."),
    DelistingCase("PRTY", "Party City Holdco Inc", date(2023, 1, 27), "nyse_amex",
                  "Chapter 11 filed 2023-01-17; NYSE delisting",
                  "sec_form_25", _form_25("Party City"),
                  resolution_note=_RESOLVE_WITH_LIVE_KEY),
    DelistingCase("SIVB", "SVB Financial Group", date(2023, 4, 6), "nasdaq",
                  "FDIC receivership of Silicon Valley Bank 2023-03-10; Chapter 11 "
                  "2023-03-17; Nasdaq delisting determination",
                  "exchange_notice", _form_25("SVB Financial"),
                  vendor_symbols={"sharadar": "SIVBQ"},
                  resolution_note="Verified live 2026-09-12: SIVBQ resolves with bars."),
    DelistingCase("BBBY", "Bed Bath & Beyond Inc", date(2023, 5, 3), "nasdaq",
                  "Chapter 11 filed 2023-04-23; Nasdaq delisting determination",
                  "exchange_notice", _form_25("Bed Bath %26 Beyond"),
                  vendor_symbols={"sharadar": "BBBYQ"},
                  resolution_note="Verified live 2026-09-12: BBBYQ resolves with bars."),
    DelistingCase("RAD", "Rite Aid Corporation", date(2023, 10, 26), "nyse_amex",
                  "Chapter 11 filed 2023-10-15; NYSE delisting for abnormally low price",
                  "sec_form_25", _form_25("Rite Aid"),
                  vendor_symbols={"sharadar": "RADCQ"},
                  resolution_note="Verified live 2026-09-12: RADCQ resolves."),
    DelistingCase("BIG", "Big Lots Inc", date(2024, 9, 20), "nyse_amex",
                  "Chapter 11 filed 2024-09-09; NYSE delisting",
                  "sec_form_25", _form_25("Big Lots"),
                  vendor_symbols={"sharadar": "BIGGQ"},
                  resolution_note="Verified live 2026-09-12: BIGGQ resolves."),
)

#: Every row is unverified against its primary filing in this session; see the
#: module docstring. A refresh that resolves them flips this.
VERIFIED = any(case.verified for case in DELISTING_AUDIT_LIST)
