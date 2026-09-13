"""`SharadarPricePlane` — Sharadar's direct API (`docs/vendors/sharadar.md`).

Sharadar's "Prices" tier is the one paid feed in the whole stack (README §3):
prices, corporate actions, a ticker master with listing/delisting dates, and
S&P 500 constituent history, for $19/mo at the 10-year tier the owner buys
(Spec N §5.4/§5.5 want two market cycles).

This adapter was first written against Nasdaq Data Link's tables API before a
sharadar.com key existed, was found to target the wrong host, and is now
ported against the real payloads recorded under
`tests/fixtures/sharadar_direct/` (fetched with Sharadar's public
`test-api-key`; see that directory's README for exactly what was observed).
The two hosts do not share a payload shape:

* Nasdaq Data Link wraps rows as `{"datatable": {"columns": [...], "data":
  [[...], ...]}}` — column names and row arrays, zipped by this adapter.
* The direct API returns `{"count": N, "data": [{...}, ...]}` — rows are
  already objects. There is nothing to zip; `_rows` below just validates that
  the expected keys are present and yields the dicts.

Why `httpx` and not a vendor SDK
---------------------------------
`httpx` is already a pinned dependency, so this adds none. A DataFrame (what
`pandas`-based vendor SDKs return) has already coerced the payload — a
renamed column becomes a `KeyError` somewhere downstream, and a null becomes
`NaN` that flows into a stored bar. Reading the raw JSON lets the column list
be compared against the list this code was written against, before a single
value is parsed.

Column names and the mappings that survive the port
-----------------------------------------------------
Confirmed against `tests/fixtures/sharadar_direct/schema_stocks.sql` and a
live slice — the schema is authoritative because it is the vendor's own DDL,
not a search extract:

* `stocks` (legacy code `SEP`) `open`/`high`/`low`/`close` are split-adjusted
  but not dividend-adjusted; `closeunadj` is the fully unadjusted close. The
  **raw** OHLC that Spec N §4.3 wants is reconstructed as
  `field × (closeunadj / close)`, exactly as before, and the raw close is
  `closeunadj` itself. Verified live: `close == closeunadj` for every AAPL
  session in the free sample window (no split falls inside it), and `closeadj`
  differs from both by a small, dividend-sized ratio — consistent with `close`
  being split-only and `closeadj` being Sharadar's fully-adjusted series.
* `closeadj` is Sharadar's own total-return close. This adapter does **not**
  store it. The stored total-return series is derived from the raw closes and
  the stored factors, so that the §4.3 reconstruction identity holds against
  the factors we actually keep (Spec N §12 ruling, Phase 3p).
* `tickers` returns **one row per plan a ticker appears in** (`table: "stocks"
  | "fundamentals" | "insiders"`, otherwise identical) when queried with no
  `table` filter — confirmed live against AAPL, which came back 3×. This
  adapter sends `table=stocks` or `table=funds` (confirmed server-side to
  filter correctly) so `security_master` never double-counts a name; the one
  path that omits the filter, `asset_class=None`, narrows the rows itself to
  the two tables that carry prices.

Equities and funds are two tables, and SPY is only in the second
---------------------------------------------------------------
Sharadar splits the price file: `stocks` (legacy `SEP`) carries operating
companies, `funds` (legacy `SFP`) carries ETFs, CEFs, ETNs and ETDs — about
10,000 tickers per the vendor's own page. Confirmed live on 2026-09-13 with the
public `test-api-key`:

* `GET /data/tickers?ticker=SPY` returns exactly **one** row and its `table` is
  `funds` (permaticker 118691, NYSEARCA, category `ETF`). SPY has no `stocks`
  row at all, so an adapter that only ever sent `table=stocks` — which this one
  did until this change — could not see the benchmark that every abnormal
  return in Spec N §5.2 is measured against.
* `GET /data/funds?ticker=SPY` returns `200` with bars. `GET /data/stocks?ticker=SPY`
  returns `403 "Exceeds free tier"`, as does `GET /data/actions?ticker=SPY`.
* `GET /schema/funds` is **byte-identical to `stocks` in its column names**
  (`ticker, date, open, high, low, close, volume, closeadj, closeunadj,
  lastupdated`), recorded as `tests/fixtures/sharadar_direct/schema_funds.sql`.

So the fund path differs from the equity path in exactly two places: the table
`daily_bars` reads, and where its split and dividend factors come from. The
second is the interesting one and it is documented on
`fund_factors_from_quotes`: a fund's factors are derived from its own `closeadj`
column, because `actions` is documented to cover funds ("all tickers in
fundamentals, stocks and funds tables") but **could not be observed doing so**
from this session, while `closeadj` could be and was. `corporate_actions()`
still reads `actions` for a fund exactly as for an equity.
* `tickers` carries **no delisting-reason field** (confirmed against
  `schema_tickers.sql`). The reason is therefore derived from `actions` where
  one exists and is `unknown` otherwise — unchanged from the original mapping,
  and still the correct answer under §4.4's censoring rule.
* `sp500`'s `action` values observed live are `current` (the latest snapshot
  row) and `historical` (one row per quarter-end membership snapshot) — never
  `added`/`removed`, because the free sample's one ticker (AAPL) never left
  the index in that window. This confirms the snapshot-vs-change-event
  distinction the original code already drew; it does not confirm the
  `added`/`removed` values themselves, which remain the vendor's documented
  terminology for an actual change and are unverified in this session (same
  caveat as before the port — the membership source this repo actually loads
  is `sp500_wikipedia_v1`, and this method exists so the interface is
  complete rather than because it is trusted).

Auth: header, not query string
-------------------------------
The direct API accepts the key as `api_key`/`apiKey` on the query string or as
an `x-api-key` header. This adapter uses the header. A query-string key ends
up in access logs, proxy logs, and `Referer` headers on any redirect (bulk
downloads *are* a redirect); a header does not survive a redirect by default
and is never part of the URL. This is a deliberate change from the pre-port
adapter, which put the key in the query string because that was the only
option Nasdaq Data Link's tables API offered.

Pagination
----------
The direct API's envelope carries no total-row-count or cursor field —
`tests/fixtures/sharadar_direct/stocks_aapl_page1_limit3.json` and
`..._page2_limit3_offset3.json` show `count` is simply `len(data)` for that
page. Pagination is therefore the ordinary offset convention, terminated when
a page comes back shorter than the requested `limit` — confirmed live: an
unpaginated request for AAPL's ~5-year sample window returned every row
(`count: 1254`) in one response, under the default `limit=10000`. This
adapter pages explicitly with a smaller `page_size` so a full table is never
pulled in one call, and stops on the first short page.

Errors
------
Confirmed live (`tests/fixtures/sharadar_direct/error_*.json`):

* `401` — only observed on the bulk-download path with no valid key
  (`{"error": "Unauthorized", "description": "A valid API key is required for
  bulk downloads."}`). Mapped to `PricePlaneAuthError`.
* `403` with `error == "Exceeds free tier"` — a name outside the free sample
  universe. Mapped to `PricePlaneAuthError` with its own message pointing at
  `/subscribe`, distinct from a rejected key.
* Any other non-2xx (e.g. `403 Forbidden: Unknown table`, a generic 5xx) —
  `PricePlaneSchemaError`, same as a malformed payload: the adapter cannot
  tell a bad request from a vendor outage from here, and both mean "do not
  trust this response".
* A non-JSON body, or JSON missing the `data` list, or a row missing an
  expected key — `PricePlaneSchemaError`, unchanged in spirit from the
  pre-port adapter.

Bulk downloads: streamed, not materialised
--------------------------------------------
`bulk_download` fetches the vendor's whole-market zip for `--bulk years=5|10|
full` (`scripts/price_backfill.py`); the 10-year `stocks` zip is the whole US
equity market's daily history, and parsing it with a
`dict[ticker, list[dict]]` accumulator — this adapter's first implementation —
held two full in-memory copies at once. Instrumented in production that hit
3.4 GB RSS 15 seconds in and got the bot container SIGKILLed three times
(`docs/investment-workspace/handoff/OWNER_SETUP_EXECUTION_2026-09-12.md` §4);
`--tickers` did not help, because the old `backfill_bulk` filtered *after* that
full parse.

`open_bulk_bar_stream` (backed by `data/prices/bulk_stream.BulkStagingStore`)
replaces the accumulator with a table on disk: the zip's CSV is streamed in
once, in ~50k-row batches, into a SQLite file indexed on `ticker` —
`--tickers` filters **during** this pass, so a subset run never even writes
another name's rows to disk. A caller (`scripts/price_backfill.py`'s
`backfill_bulk`) then drains one ticker at a time — build bars, apply that
ticker's split/dividend factors, `with_derived_series`, remap the placeholder
`security_uid`, store in one transaction, drop the staged rows — so peak
memory is one ticker's history plus one staging batch, never the whole
market. `load_bulk_bars` still exists and still returns the whole-market
dict, for a caller that genuinely wants it (mainly the test suite); it holds
everything in memory exactly like the pre-streaming version did, and a real
backfill must not call it — see its docstring.

The staging file is also the resumability boundary: `backfill_bulk` commits
and drops one ticker at a time and checkpoints after each one, so a run
killed mid-way — by `--max-rss-mb`'s own guard, or by the same SIGKILL this
was built to survive — restarts with `--resume` from the next undone ticker,
not from the top of the zip. See `scripts/price_backfill.py`'s module
docstring for the full `--checkpoint`/`--resume`/`--max-rss-mb` contract.
"""

from __future__ import annotations

import csv
import io
import os
import tempfile
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from data.prices.base import (
    ASSET_CLASS_EQUITY,
    ASSET_CLASS_FUND,
    ASSET_CLASSES,
    CorporateActionRecord,
    DailyBar,
    MembershipInterval,
    PricePlane,
    PricePlaneConfigError,
    PricePlaneSchemaError,
    SecurityMasterRow,
)
from data.prices.bulk_stream import BulkStagingStore
from data.prices.derived import with_derived_series

SOURCE = "sharadar"

#: The direct API. Not Nasdaq Data Link — see the module docstring and
#: `docs/vendors/sharadar.md`.
BASE_URL = "https://api.sharadar.com/v1.0"

#: Modern table names the direct API uses in `/data/{table}`. Legacy codes
#: (`SEP`, `TICKERS`, `ACTIONS`, `SP500`) also work per the vendor docs but the
#: modern names are what this adapter sends.
TABLE_STOCKS = "stocks"
TABLE_FUNDS = "funds"
TABLE_ACTIONS = "actions"
TABLE_TICKERS = "tickers"
TABLE_SP500 = "sp500"

#: Sharadar splits its price file in two and a name lives in exactly one half:
#: `stocks` (legacy `SEP`) for operating companies, `funds` (legacy `SFP`) for
#: ETFs, CEFs, ETNs and ETDs. SPY is in `funds` and **not** in `stocks` —
#: confirmed live, see the module docstring. These two maps are the only place
#: the correspondence is written down; nothing anywhere guesses a table from a
#: symbol.
TABLE_BY_ASSET_CLASS = {
    ASSET_CLASS_EQUITY: TABLE_STOCKS,
    ASSET_CLASS_FUND: TABLE_FUNDS,
}
ASSET_CLASS_BY_TABLE = {table: klass for klass, table in TABLE_BY_ASSET_CLASS.items()}

#: Header carrying the key. Chosen over the `api_key` query param so the key
#: never ends up in a URL, a log line, or a `Referer` on the bulk redirect.
AUTH_HEADER = "x-api-key"

#: Environment variable carrying the key. Never hardcoded, never logged.
#: `LEGACY_API_KEY_ENV` is read too, for a deployment whose `Settings` still
#: only wires through the pre-port name — see `data/prices/config.py`.
API_KEY_ENV = "SHARADAR_API_KEY"
LEGACY_API_KEY_ENV = "NASDAQ_DATA_LINK_API_KEY"

#: Columns each table must contain. Extra columns are tolerated (a vendor
#: adding a field should not stop ingest); a missing or renamed one raises.
STOCKS_COLUMNS = ("ticker", "date", "open", "high", "low", "close", "volume", "closeunadj")
#: `funds` has byte-identical column names to `stocks` (verified against
#: `tests/fixtures/sharadar_direct/schema_funds.sql`, which is the vendor's own
#: DDL). `closeadj` is in this list and not in `STOCKS_COLUMNS` because the fund
#: path *needs* it: it is where a fund's distributions are, and it is the only
#: place this adapter could verify they are. See `_fund_factors`.
FUNDS_COLUMNS = (
    "ticker", "date", "open", "high", "low", "close", "volume", "closeadj", "closeunadj",
)
ACTIONS_COLUMNS = ("date", "action", "ticker", "value")
TICKERS_COLUMNS = (
    # `table` is first in the vendor's own primary key and is what says which
    # price table a name's bars are in. It is validated rather than read
    # best-effort because the auto-detect path in `security_master` keys off
    # it: a `tickers` payload without it would otherwise resolve every name to
    # nothing at all, quietly.
    "table",
    "permaticker", "ticker", "name", "exchange", "isdelisted",
    "firstpricedate", "lastpricedate",
)
SP500_COLUMNS = ("date", "action", "ticker")

#: `exchange` -> the Shumway venue the delisting convention is keyed on.
VENUE_BY_EXCHANGE = {
    "NYSE": "nyse_amex",
    "NYSEMKT": "nyse_amex",
    "NYSEARCA": "nyse_amex",
    "AMEX": "nyse_amex",
    "BATS": "other",
    "NASDAQ": "nasdaq",
    "OTC": "other",
}

#: ACTIONS `action` values -> our four reason categories.
#:
#: **Unverified.** The set of values Sharadar emits for an actual delisting is
#: not observable from the free sample (no delisted name is in it); only
#: `split` and `dividend` are attested live. Anything not listed maps to
#: `other`, and a security with no delisting action at all keeps `unknown`.
#: Widening this map is a one-line change once a paid key can list the
#: distinct values on a real delisted name.
DELISTING_REASON_BY_ACTION = {
    "bankruptcy": "performance",
    "liquidation": "performance",
    "regulatorydelisting": "performance",
    "delisted": "unknown",
    "acquisitionbypubliccompany": "merger_acquisition",
    "acquisitionbyprivatecompany": "merger_acquisition",
    "mergerfrom": "merger_acquisition",
    "mergerto": "merger_acquisition",
}

ACTION_SPLIT_VALUES = ("split",)
ACTION_DIVIDEND_VALUES = ("dividend",)

#: SP500 constituent-change actions. Only the change rows are used: the table
#: also carries quarterly snapshot rows (`current` / `historical`, confirmed
#: live), and treating a snapshot as a join would date every current
#: constituent's membership to the snapshot. The whole SP500 reconstruction
#: here is unverified for an actual change event (see module docstring); the
#: membership source this repo actually loads is the free
#: `sp500_wikipedia_v1` (`data/prices/sp500_history.py`), and this exists so
#: the interface is complete rather than because it is trusted.
SP500_ADD_ACTIONS = ("added",)
SP500_REMOVE_ACTIONS = ("removed",)

#: Half of the last decimal Sharadar quotes `close` and `closeadj` at. Every
#: value observed live is at most three decimal places (`462.612`, `460.01`),
#: so a quoted value carries at most 0.0005 of rounding error.
FUND_QUOTE_HALF_ULP = 0.0005

#: Four quoted values enter one implied distribution (`close` and `closeadj` on
#: this session and the previous one), so the propagated rounding bound is
#: `4 x FUND_QUOTE_HALF_ULP`, scaled from adjusted units into raw ones.
FUND_QUOTE_TERMS = 4

#: How far above that rounding bound an implied distribution has to be before
#: this adapter believes it is a distribution rather than arithmetic noise.
#:
#: **Measured, not guessed.** Against SPY's live `funds` rows for 2023-12-01 ..
#: 2025-01-31 (292 sessions, free `test-api-key`), the implied distribution was
#: above 2 bps of price on exactly five sessions — 2023-12-15, 2024-03-15,
#: 2024-06-21, 2024-09-20 and 2024-12-20, SPY's four quarterly ex-dividend
#: dates in that window plus the one that opens it — and the amounts came out
#: at 1.9063 / 1.6000 / 1.7588 / 1.7453 / 1.9699 dollars per share. On the
#: other 286 sessions the implied value never exceeded **0.019 bps** of price
#: (stdev 0.0074 bps), which is $0.0009 a share and is exactly what 3-decimal
#: rounding predicts. The floor this constant sets for SPY is about $0.01 —
#: roughly ten times the largest noise ever observed and about a hundred and
#: fifty times smaller than the smallest real distribution.
#:
#: **Where the margin narrows.** The rounding bound is absolute in `closeadj`
#: units, so the floor in *price* terms scales as
#: `closeunadj / closeadj` — a fund's cumulative distribution adjustment — and
#: inversely with its price level. SPY, quoted near $500 with a ratio of about
#: 1.03, gets a floor around 2 bps. A long-lived bond ETF quoted near $8 with a
#: ratio of 2.7 gets one nearer 30 bps, which is the same order as its own
#: monthly distribution — so for such a fund a real distribution could be
#: zeroed. That is a known limit, recorded in `docs/PRICE_PLANE.md`, not a
#: property anyone should discover from a wrong number: the only fund this
#: system uses is the benchmark, and lowering the constant to widen the margin
#: would trade a missed distribution for a fabricated one on every ordinary
#: session, which is the worse of the two.
FUND_DISTRIBUTION_SAFETY = 5.0

DEFAULT_TIMEOUT_S = 30.0
#: Rows requested per page. Deliberately well under the vendor's own
#: `limit=10000` default so a single ticker's full history is never pulled in
#: one call, and so the "stop on a short page" rule actually exercises more
#: than one page in normal use.
DEFAULT_PAGE_SIZE = 2000
MAX_PAGES = 500

#: `years=` values the bulk endpoint accepts (Spec N §5.4/§5.5; the owner buys
#: the 10-year Prices tier).
BULK_YEARS = ("5", "10", "full")

#: Rows staged per batch when a bulk `stocks` zip is streamed to disk
#: (`open_bulk_bar_stream`) — see `data/prices/bulk_stream.py`.
DEFAULT_BULK_BATCH_SIZE = 50_000


class PricePlaneAuthError(PricePlaneConfigError):
    """The vendor rejected the key, or the key's plan does not cover this call.

    A subclass of `PricePlaneConfigError` rather than `PricePlaneSchemaError`:
    a bad payload shape means "the adapter and the vendor disagree", while a
    rejected key or an exceeded plan means "this deployment is misconfigured",
    which is exactly what `PricePlaneConfigError` already means for a missing
    key. Callers that only catch `PricePlaneConfigError` still catch this.
    """


#: A split factor this close to 1.0 is rounding in `closeunadj / close`, not a
#: split. `F = closeunadj / close` is a ratio of two 3-decimal quotes, so its
#: relative error is about `0.0005 x (1/closeunadj + 1/close)` — of order 1e-6
#: at SPY's price level and still under 1e-4 for a one-dollar fund. Snapping is
#: not cosmetic: an unsnapped 1.0000001 on every ordinary session would compound
#: through `derived.forward_split_factors` and put a fictional split on every
#: stored bar.
FUND_SPLIT_SNAP_RTOL = 1e-4


def fund_factors_from_quotes(
    quotes: Sequence[tuple[date, float, float, float]],
    ticker: str = "",
) -> tuple[tuple[float, float], ...]:
    """`(split_factor, dividend_cash)` per session, from the `funds` table alone.

    `quotes` is `(session_date, close, closeadj, closeunadj)` ascending. The
    return is one pair per input session; the first is always `(1.0, 0.0)`
    because a one-session return needs a previous close.

    Why the fund path derives its factors from prices and the equity path does
    not
    -------------------------------------------------------------------------
    Sharadar documents `actions` as covering "all tickers in fundamentals,
    stocks and funds tables", so reading a fund's distributions there would be
    the symmetrical thing to do. This adapter does not, for one reason:
    **it could not be verified.** On the public `test-api-key`,
    `GET /data/funds?ticker=SPY` returns `200` with bars while
    `GET /data/actions?ticker=SPY` returns `403 "Exceeds free tier"` — so the
    one table that could be observed carrying SPY's distributions is `funds`,
    through `closeadj`, and it was (see `FUND_DISTRIBUTION_SAFETY` for the
    measurement). Spec N §5.2 measures every abnormal return against the
    benchmark's **total return**; a benchmark whose dividend factors came from
    a table this session never saw a fund row in would be a number nobody had
    checked. `corporate_actions()` still reads `actions` for a fund, exactly as
    for an equity — it is the vendor's action log and the interface promises
    it — but the *bars* carry factors derived here.

    The arithmetic
    --------------
    Sharadar's `close` is split-adjusted, `closeunadj` is raw, and `closeadj`
    is "adjusted for splits, dividends and spinoffs" (vendor docs). With
    `F(i) = closeunadj(i) / close(i)` the cumulative forward split factor of
    `derived.py`:

        split_factor(i) = F(i-1) / F(i)
        close(i)/close(i-1)        = raw(i) x split_factor(i) / raw(i-1)
        closeadj(i)/closeadj(i-1)  = (raw(i) + div(i)) x split_factor(i) / raw(i-1)

    Subtracting the two relatives isolates the distribution:

        div(i) = [closeadj(i)/closeadj(i-1) - close(i)/close(i-1)]
                 x raw(i-1) / split_factor(i)

    which is exactly the `dividend_cash` `derived.total_return_closes` wants,
    so the stored total-return series reproduces the vendor's own adjusted
    series in returns (it differs in level: ours is anchored at the last raw
    close by the §4.3 convention, Sharadar's at its own).

    What is deliberately *not* believed
    -----------------------------------
    Below `FUND_DISTRIBUTION_SAFETY x` the propagated rounding bound the implied
    value is zeroed: three-decimal quotes cannot resolve a tenth of a cent, and
    storing 0.0007 of "dividend" on 250 ordinary sessions a year would be a
    fabricated fact on every one of them. A **negative** implied distribution
    larger than that bound is not zeroed and not stored either — it raises,
    because it means the vendor's adjusted series cannot be expressed in the
    three-series model at all, and a price plane that degrades quietly is worse
    than one that is down (module rule 1).

    One caveat worth stating where it will be read: `closeadj` folds spinoffs in
    alongside cash. A fund that spun off value would have that arrive here as
    `dividend_cash`. That is right for total return and imprecise as a label,
    and it is why `docs/PRICE_PLANE.md` calls a fund's factor provenance
    `closeadj_derived` rather than `dividend`.
    """
    out: list[tuple[float, float]] = [(1.0, 0.0)]
    for index in range(1, len(quotes)):
        session, close, adj, unadj = quotes[index]
        _, prev_close, prev_adj, prev_unadj = quotes[index - 1]
        context = f"funds {ticker} {session}".strip()
        for label, value in (
            ("close", close), ("closeadj", adj), ("closeunadj", unadj),
            ("previous close", prev_close), ("previous closeadj", prev_adj),
            ("previous closeunadj", prev_unadj),
        ):
            if value <= 0:
                raise PricePlaneSchemaError(
                    f"{context}: {label} is {value!r}; a fund's factors are a "
                    f"ratio of quotes and a non-positive quote has no ratio"
                )

        split = (prev_unadj / prev_close) / (unadj / close)
        if abs(split - 1.0) <= FUND_SPLIT_SNAP_RTOL:
            split = 1.0

        implied = ((adj / prev_adj) - (close / prev_close)) * prev_unadj / split
        bound = FUND_QUOTE_TERMS * FUND_QUOTE_HALF_ULP * (prev_unadj / prev_adj)
        floor = FUND_DISTRIBUTION_SAFETY * bound
        if implied < -floor:
            raise PricePlaneSchemaError(
                f"{context}: closeadj implies a distribution of {implied:.6f} per "
                f"share — negative, and {abs(implied) / bound:.1f}x the "
                f"{bound:.6f} this quote precision can explain as rounding. A "
                f"negative distribution cannot be expressed as `dividend_cash`, "
                f"so the three-series contract (Spec N §4.3) cannot hold for this "
                f"session and this adapter will not store a series that pretends "
                f"it does."
            )
        out.append((split, implied if implied > floor else 0.0))
    return tuple(out)


def api_key_from_env(explicit: str | None = None) -> str:
    key = (
        explicit
        or os.environ.get(API_KEY_ENV)
        or os.environ.get(LEGACY_API_KEY_ENV)
        or ""
    ).strip()
    if not key:
        raise PricePlaneConfigError(
            f"{API_KEY_ENV} is not set. The Sharadar plane needs a key from "
            "https://sharadar.com/account; run against FixturePricePlane if "
            "you are testing."
        )
    return key


class SharadarPricePlane(PricePlane):
    """Sharadar stocks / actions / tickers / sp500 behind the `PricePlane` contract.

    `client` is any object with:

    * `get(url, params=..., headers=..., timeout=...)` returning something
      with `.status_code`, `.json()` and `.text` — for the paginated slice
      path;
    * `stream(method, url, params=..., headers=..., timeout=..., \
follow_redirects=...)` returning a context manager whose `__enter__` gives
      an object with `.status_code`, `.json()` (for an error body), and
      `.iter_bytes()` — for `bulk_download`.

    `httpx.Client` satisfies both in production; a stub satisfies both in
    tests, so the suite can exercise the parsing, the pagination boundary and
    the schema guard with no network at all (`test_no_network_in_tests`).
    """

    source = SOURCE

    def __init__(
        self,
        api_key: str | None = None,
        client: Any = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        page_size: int = DEFAULT_PAGE_SIZE,
    ):
        self._api_key = api_key_from_env(api_key)
        self._client = client
        self._timeout = timeout
        self._page_size = page_size
        #: `{ticker: (security_uid, asset_class)}`. Both halves are cached
        #: together because both come from the same `tickers` row, and the
        #: class is what decides which price table `daily_bars` reads.
        self._resolved: dict[str, tuple[str, str]] = {}

    # -- transport -------------------------------------------------------- #

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _headers(self) -> dict[str, str]:
        return {AUTH_HEADER: self._api_key}

    @staticmethod
    def _error_detail(response: Any) -> tuple[str | None, str]:
        """`(error_code, human_detail)` from a non-2xx body, best-effort."""
        try:
            body = response.json()
        except Exception:
            return None, (getattr(response, "text", "") or "")[:400]
        if isinstance(body, dict) and "error" in body:
            description = body.get("description") or ""
            return str(body["error"]), f"{body['error']}: {description}".strip(": ")
        return None, str(body)[:400]

    def _raise_for_status(self, response: Any, table: str, *, context: str = "") -> None:
        status = getattr(response, "status_code", None)
        if status == 200:
            return
        where = f"{table} {context}".strip()
        error_code, detail = self._error_detail(response)
        if status == 401:
            raise PricePlaneAuthError(
                f"Sharadar rejected the API key for {where}: {detail}. "
                f"Set {API_KEY_ENV} to a key from https://sharadar.com/account."
            )
        if status == 403 and error_code and "free tier" in error_code.lower():
            raise PricePlaneAuthError(
                f"Sharadar free-tier limit hit for {where}: {detail}. "
                "This key is running as free-tier, not a paid key — see "
                "https://sharadar.com/subscribe."
            )
        raise PricePlaneSchemaError(f"{where}: HTTP {status} from Sharadar: {detail}")

    def _get(self, table: str, params: Mapping[str, Any]) -> Any:
        return self._http().get(
            f"{BASE_URL}/data/{table}", params=dict(params), headers=self._headers(),
            timeout=self._timeout,
        )

    def _rows(self, table: str, params: Mapping[str, Any], expected: Sequence[str]) -> Iterator[dict]:
        """Yield rows of a Sharadar table as dicts, paging by offset.

        Raises `PricePlaneSchemaError` on anything that is not the payload
        shape this adapter was written against: a non-JSON body, a missing
        `data` list, or a row missing an expected key. Silence here is the
        failure mode that matters — a cohort built on quietly-empty bars looks
        exactly like a cohort built on real ones.
        """
        offset = 0
        for _page in range(MAX_PAGES):
            query = dict(params)
            query["limit"] = self._page_size
            query["offset"] = offset
            query.setdefault("format", "json")
            response = self._get(table, query)
            self._raise_for_status(response, table)
            try:
                payload = response.json()
            except Exception as exc:
                raise PricePlaneSchemaError(f"{table}: response is not JSON") from exc

            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                raise PricePlaneSchemaError(
                    f"{table}: payload has no 'data' list; top-level keys were "
                    f"{sorted(payload) if isinstance(payload, dict) else type(payload)}"
                )

            for row in data:
                missing = [name for name in expected if name not in row]
                if missing:
                    raise PricePlaneSchemaError(
                        f"{table}: expected columns {missing} are absent. The row "
                        f"had {sorted(row)}. The adapter was written against "
                        f"{list(expected)}; a rename upstream must be handled "
                        "here, not worked around at the call site."
                    )
                yield row

            if len(data) < self._page_size:
                return
            offset += len(data)
        raise PricePlaneSchemaError(f"{table}: pagination did not terminate in {MAX_PAGES} pages")

    # -- bulk --------------------------------------------------------------#

    def bulk_download(self, table: str, years: str | int, dest: str | Path) -> Path:
        """Follow the `years=` 302 and stream the zip to `dest`.

        Subscription-gated: `tests/fixtures/sharadar_direct/error_401_bulk_no_key.json`
        shows the free sample key rejected outright for this path (`401`, not
        a free-tier `403`), so this could not be exercised end-to-end against
        a live redirect in this session — only against a stubbed transport
        (`test_bulk_download_streams_the_zip_to_disk`). `years` is one of
        `BULK_YEARS`; the API 302s to a pre-signed zip URL regardless of
        `format`, and `client.stream` is asked to follow that redirect.
        """
        years_str = str(years)
        if years_str not in BULK_YEARS:
            raise PricePlaneConfigError(f"years must be one of {BULK_YEARS}, got {years!r}")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._http().stream(
            "GET", f"{BASE_URL}/data/{table}",
            params={"years": years_str}, headers=self._headers(),
            timeout=self._timeout, follow_redirects=True,
        ) as response:
            self._raise_for_status(response, table, context="bulk")
            with open(dest, "wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
        return dest

    def load_bulk_bars(self, zip_path: str | Path) -> dict[str, tuple[DailyBar, ...]]:
        """Parse a `stocks` bulk zip into bars per ticker, no factors applied.

        A thin wrapper over `open_bulk_bar_stream` (the streaming stager under
        `data/prices/bulk_stream.py`) for a caller that actually wants the
        whole-market dict back — the test suite, mainly. **This still holds
        every ticker's bars in memory at once**, exactly like the pre-streaming
        implementation did; a real bulk backfill must not call this (and
        `scripts/price_backfill.py`'s `backfill_bulk` does not) — it drives
        `open_bulk_bar_stream` directly instead, storing and dropping one
        ticker at a time so peak memory never holds this method's return
        value.

        The zip's single CSV member is read by header name rather than a
        hardcoded filename — the exact name inside the zip is unverified
        (bulk needs a paid key; see `bulk_download`'s docstring), but its
        column headers are the same `STOCKS_COLUMNS` names the slice path
        already validates against, per the CSV slice recorded in
        `tests/fixtures/sharadar_direct/stocks_aapl.csv`.

        No split/dividend factors are applied here — this method takes no
        actions zip, so every bar gets `split_factor=1.0, dividend_cash=0.0`,
        same as before streaming. `open_bulk_bar_stream`'s `actions_by_ticker`
        parameter is what lets a caller (`backfill_bulk`) apply real factors.

        **`security_uid` is a placeholder** (`sharadar:bulk:<ticker>`): the
        bulk `stocks` CSV carries no `permaticker`, only `ticker`, so there is
        nothing here to derive the real uid from. A caller that stores these
        bars must first resolve the real, permaticker-derived uid per ticker
        (`security_master`, which the bulk `tickers` zip is also a
        full-snapshot source for) and replace this field — `store.py` joins
        `price_bars` to `securities` on `security_uid`, so storing the
        placeholder orphans the bar. `scripts/price_backfill.py`'s
        `backfill_bulk` does this remap before writing anything.
        """
        with tempfile.TemporaryDirectory(prefix="sharadar_bulk_stage_") as tmp:
            staging_db_path = Path(tmp) / "stage.sqlite"
            with self.open_bulk_bar_stream(zip_path, staging_db_path) as stream:
                out = {ticker: stream.bars_for(ticker) for ticker in stream.tickers()}
        return out

    def open_bulk_bar_stream(
        self,
        zip_path: str | Path | None,
        staging_db_path: str | Path,
        tickers: Sequence[str] | None = None,
        batch_size: int = DEFAULT_BULK_BATCH_SIZE,
        actions_by_ticker: Mapping[str, Sequence[CorporateActionRecord]] | None = None,
        resume_staging: bool = False,
    ) -> "BulkBarStream":
        """Open a memory-bounded, one-ticker-at-a-time view of a bulk zip.

        Stages `zip_path`'s `stocks` CSV into a SQLite file at
        `staging_db_path` (`data/prices/bulk_stream.BulkStagingStore`),
        filtering to `tickers` **during** staging when given, then returns a
        `BulkBarStream` a caller drains one ticker at a time —
        `BulkBarStream.bars_for` is the only step that costs more than one
        ticker's worth of memory, and `BulkBarStream.drop` is what a caller
        calls once that ticker's bars are safely stored, keeping both peak
        memory and peak staging-disk usage to one ticker's history.

        `actions_by_ticker`, if given (`load_bulk_actions`'s return value),
        is turned into per-ticker `{ex_date: (split_factor, dividend_cash)}`
        factors applied while building each ticker's bars — the same
        factors the per-ticker slice path (`daily_bars`) applies via
        `_factors`, just computed from an already-parsed bulk actions zip
        instead of a fresh `corporate_actions` call per ticker.

        `resume_staging=True` skips `stage()` and reopens an existing,
        already-staged file at `staging_db_path` instead — for `--resume`,
        where a prior process staged the zip and this one only needs to
        finish draining it. Raises `PricePlaneSchemaError` if that file has
        no staged table to reopen.
        """
        store = BulkStagingStore(staging_db_path)
        if resume_staging:
            if not store.has_data():
                store.close()
                raise PricePlaneSchemaError(
                    f"{staging_db_path}: --resume needs an already-staged file; "
                    "found no staged table"
                )
        else:
            store.stage(zip_path, STOCKS_COLUMNS, tickers=tickers, batch_size=batch_size)
        factors_by_ticker = {
            ticker: self._factors_from_actions(actions)
            for ticker, actions in (actions_by_ticker or {}).items()
        }
        return BulkBarStream(self, store, factors_by_ticker)

    def load_bulk_actions(self, zip_path: str | Path) -> dict[str, tuple[CorporateActionRecord, ...]]:
        """Parse an `actions` bulk zip into corporate actions per ticker.

        Same placeholder-`security_uid` caveat as `load_bulk_bars`.
        """
        rows_by_ticker = self._read_bulk_csv(zip_path, ACTIONS_COLUMNS)
        out: dict[str, tuple[CorporateActionRecord, ...]] = {}
        for ticker, rows in rows_by_ticker.items():
            uid = f"{SOURCE}:bulk:{ticker}"
            records = [self._action_from_row(row, ticker, uid) for row in rows]
            out[ticker] = tuple(sorted(records, key=lambda r: (r.ex_date, r.action_type)))
        return out

    @staticmethod
    def _read_bulk_csv(zip_path: str | Path, expected: Sequence[str]) -> dict[str, list[dict]]:
        with zipfile.ZipFile(zip_path) as archive:
            members = [n for n in archive.namelist() if n.lower().endswith(".csv")]
            if len(members) != 1:
                raise PricePlaneSchemaError(
                    f"{zip_path}: expected exactly one CSV member, found {members}"
                )
            with archive.open(members[0]) as handle:
                text = io.TextIOWrapper(handle, encoding="utf-8")
                reader = csv.DictReader(text)
                fieldnames = reader.fieldnames or ()
                missing = [name for name in expected if name not in fieldnames]
                if missing:
                    raise PricePlaneSchemaError(
                        f"{zip_path}: expected columns {missing} are absent. "
                        f"The CSV header was {list(fieldnames)}."
                    )
                grouped: dict[str, list[dict]] = {}
                for row in reader:
                    grouped.setdefault(row["ticker"], []).append(row)
        return grouped

    # -- parsing ---------------------------------------------------------- #

    @staticmethod
    def _need(row: Mapping[str, Any], column: str, context: str) -> Any:
        value = row.get(column)
        if value is None or value == "":
            raise PricePlaneSchemaError(
                f"{context}: {column} is empty. This plane never stores a null; "
                "a vendor row missing a required field is an ingest failure."
            )
        return value

    @classmethod
    def _need_float(cls, row: Mapping[str, Any], column: str, context: str) -> float:
        value = cls._need(row, column, context)
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise PricePlaneSchemaError(f"{context}: {column}={value!r} is not a number") from exc

    @classmethod
    def _need_date(cls, row: Mapping[str, Any], column: str, context: str) -> date:
        value = str(cls._need(row, column, context))
        try:
            return date.fromisoformat(value[:10])
        except ValueError as exc:
            raise PricePlaneSchemaError(f"{context}: {column}={value!r} is not an ISO date") from exc

    @staticmethod
    def _optional_date(row: Mapping[str, Any], column: str) -> date | None:
        value = row.get(column)
        if value in (None, ""):
            return None
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError as exc:
            raise PricePlaneSchemaError(f"{column}={value!r} is not an ISO date") from exc

    def _bar_from_row(
        self, row: Mapping[str, Any], ticker: str, uid: str,
        split_factor: float, dividend_cash: float,
        table: str = TABLE_STOCKS,
    ) -> DailyBar:
        session = self._need_date(row, "date", f"{table} {ticker}")
        context = f"{table} {ticker} {session}"
        adjusted_close = self._need_float(row, "close", context)
        raw_close = self._need_float(row, "closeunadj", context)
        if adjusted_close <= 0:
            raise PricePlaneSchemaError(f"{context}: close is {adjusted_close}")
        # `stocks`' and `funds`' OHLC are split-adjusted; closeunadj is not.
        # The ratio is the cumulative split adjustment as of this session,
        # exactly — the two tables carry byte-identical column names and
        # semantics (`schema_stocks.sql` / `schema_funds.sql`).
        ratio = raw_close / adjusted_close
        return DailyBar(
            security_uid=uid,
            ticker=ticker,
            session_date=session,
            raw_open=self._need_float(row, "open", context) * ratio,
            raw_high=self._need_float(row, "high", context) * ratio,
            raw_low=self._need_float(row, "low", context) * ratio,
            raw_close=raw_close,
            volume=self._need_float(row, "volume", context),
            split_factor=split_factor,
            dividend_cash=dividend_cash,
            split_adjusted_close=raw_close,
            total_return_close=raw_close,
            source=self.source,
        )

    def _action_from_row(self, row: Mapping[str, Any], ticker: str, uid: str) -> CorporateActionRecord:
        ex_date = self._need_date(row, "date", f"actions {ticker}")
        action = str(self._need(row, "action", f"actions {ticker} {ex_date}")).strip().lower()
        value = row.get("value")
        return CorporateActionRecord(
            security_uid=uid,
            ticker=ticker,
            ex_date=ex_date,
            action_type=action,
            value=float(value) if value not in (None, "") else None,
            source=self.source,
        )

    # -- interface -------------------------------------------------------- #

    def daily_bars(
        self, ticker: str, start: date | None = None, end: date | None = None
    ) -> tuple[DailyBar, ...]:
        """Bars from whichever price table the master says this name lives in.

        The table is resolved through `tickers`, never inferred from the symbol
        — `SPY`, `GLD` and `TLT` look like every other three-letter ticker, and
        a wrong guess here is silently empty bars rather than an error.
        """
        uid, asset_class = self._resolve(ticker)
        if asset_class == ASSET_CLASS_FUND:
            return self._fund_bars(ticker, uid, start, end)

        params: dict[str, Any] = {"ticker": ticker}
        if start:
            params["from"] = start.isoformat()
        if end:
            params["to"] = end.isoformat()

        factors = self._factors(ticker, start, end)

        bars: list[DailyBar] = []
        for row in self._rows(TABLE_STOCKS, params, STOCKS_COLUMNS):
            session = self._need_date(row, "date", f"stocks {ticker}")
            split_factor, dividend_cash = factors.get(session, (1.0, 0.0))
            bars.append(self._bar_from_row(row, ticker, uid, split_factor, dividend_cash))

        if not bars:
            return ()
        return with_derived_series(sorted(bars, key=lambda b: b.session_date))

    def _fund_bars(
        self, ticker: str, uid: str, start: date | None, end: date | None
    ) -> tuple[DailyBar, ...]:
        """The fund path: one call to `funds`, factors from its own `closeadj`.

        Deliberately **not** a call to `actions`. See `fund_factors_from_quotes`
        for why, and `docs/PRICE_PLANE.md` for what the resulting provenance is
        called. One consequence worth knowing at the call site: a fund's bars
        cost a single request, where an equity's cost two.

        The window matters to the arithmetic in a way it does not for an equity.
        Every factor here is a ratio against the **previous session in the slice
        returned**, so the first bar of any window carries `(1.0, 0.0)` — if a
        distribution went ex on `start`, that window cannot see it. `--since`
        therefore wants a session of slack, and `store.upsert_bars` overwrites
        by `(security_uid, session_date)`, so a re-run with an earlier `--since`
        corrects it.
        """
        params: dict[str, Any] = {"ticker": ticker}
        if start:
            params["from"] = start.isoformat()
        if end:
            params["to"] = end.isoformat()

        rows = list(self._rows(TABLE_FUNDS, params, FUNDS_COLUMNS))
        if not rows:
            return ()
        rows.sort(key=lambda r: str(r.get("date")))

        quotes: list[tuple[date, float, float, float]] = []
        for row in rows:
            session = self._need_date(row, "date", f"funds {ticker}")
            context = f"funds {ticker} {session}"
            quotes.append((
                session,
                self._need_float(row, "close", context),
                self._need_float(row, "closeadj", context),
                self._need_float(row, "closeunadj", context),
            ))

        factors = fund_factors_from_quotes(quotes, ticker)
        bars = [
            self._bar_from_row(row, ticker, uid, split, dividend, table=TABLE_FUNDS)
            for row, (split, dividend) in zip(rows, factors)
        ]
        return with_derived_series(bars)

    def _factors(
        self, ticker: str, start: date | None, end: date | None
    ) -> dict[date, tuple[float, float]]:
        """`{ex_date: (split_factor, dividend_cash)}` from actions."""
        return self._factors_from_actions(self.corporate_actions(ticker, start, end))

    @staticmethod
    def _factors_from_actions(
        actions: Sequence[CorporateActionRecord],
    ) -> dict[date, tuple[float, float]]:
        """`{ex_date: (split_factor, dividend_cash)}` from already-parsed actions.

        The shared core of `_factors` (per-ticker slice path, one
        `corporate_actions` call) and `open_bulk_bar_stream` (bulk path, an
        already-parsed `load_bulk_actions` result) — same fold, different
        source of `CorporateActionRecord`s.
        """
        out: dict[date, tuple[float, float]] = {}
        for action in actions:
            split, dividend = out.get(action.ex_date, (1.0, 0.0))
            if action.action_type in ACTION_SPLIT_VALUES and action.value:
                split *= action.value
            elif action.action_type in ACTION_DIVIDEND_VALUES and action.value:
                dividend += action.value
            out[action.ex_date] = (split, dividend)
        return out

    def corporate_actions(
        self, ticker: str, start: date | None = None, end: date | None = None
    ) -> tuple[CorporateActionRecord, ...]:
        params: dict[str, Any] = {"ticker": ticker}
        if start:
            params["from"] = start.isoformat()
        if end:
            params["to"] = end.isoformat()

        uid, _asset_class = self._resolve(ticker)
        records = [
            self._action_from_row(row, ticker, uid)
            for row in self._rows(TABLE_ACTIONS, params, ACTIONS_COLUMNS)
        ]
        return tuple(sorted(records, key=lambda r: (r.ex_date, r.action_type)))

    def security_master(
        self,
        tickers: Sequence[str] | None = None,
        *,
        asset_class: str | None = ASSET_CLASS_EQUITY,
    ) -> tuple[SecurityMasterRow, ...]:
        """Master rows for one instrument class, or for whichever class each name is.

        `asset_class="equity"` (the default) sends `table=stocks` and
        `asset_class="fund"` sends `table=funds`. That filter is not an
        optimisation: `tickers` returns **one row per plan a name appears in**
        (`stocks`, `fundamentals`, `insiders` for an equity) when queried
        unfiltered, confirmed live against AAPL, so an unfiltered read
        triple-counts every operating company.

        `asset_class=None` is the auto-detect path — no `table` filter, and the
        rows that come back are narrowed to the two price tables, one per name.
        It is what `daily_bars` resolves through and what
        `scripts/price_backfill.py --asset-class auto` runs, because "is SPY an
        equity or a fund" is a question for the vendor's master and not for a
        guess about a three-letter symbol. A name that somehow appears in both
        price tables raises rather than resolving to one of them.

        The default is `equity` rather than `None` so that every caller written
        before funds existed keeps the behaviour it was written against, and so
        that the universe job cannot start ranking ETFs by forgetting an
        argument.
        """
        if asset_class is not None and asset_class not in ASSET_CLASSES:
            raise PricePlaneConfigError(
                f"asset_class must be one of {ASSET_CLASSES} or None for "
                f"auto-detect, got {asset_class!r}"
            )
        params: dict[str, Any] = {}
        if asset_class is not None:
            params["table"] = TABLE_BY_ASSET_CLASS[asset_class]
        if tickers is not None:
            params["ticker"] = ",".join(tickers)

        rows = []
        seen_class_by_ticker: dict[str, str] = {}
        for row in self._rows(TABLE_TICKERS, params, TICKERS_COLUMNS):
            table = str(row.get("table") or "").strip().lower()
            row_class = ASSET_CLASS_BY_TABLE.get(table)
            if row_class is None:
                # Only reachable on the auto-detect path: `fundamentals` and
                # `insiders` rows describe the same name in a plan that carries
                # no prices, so they are not security-master rows here.
                continue
            ticker = str(self._need(row, "ticker", "tickers"))
            previous = seen_class_by_ticker.setdefault(ticker, row_class)
            if previous != row_class:
                raise PricePlaneSchemaError(
                    f"tickers has {ticker!r} in both the {previous!r} and "
                    f"{row_class!r} price tables; this adapter will not pick one. "
                    f"Pass asset_class= explicitly to say which you mean."
                )
            context = f"tickers {ticker}"
            uid = f"{SOURCE}:{self._need(row, 'permaticker', context)}"
            exchange = str(row.get("exchange") or "").strip().upper()
            delisted = str(self._need(row, "isdelisted", context)).strip().upper() == "Y"
            last_price = self._optional_date(row, "lastpricedate")
            # Seed the cache before `delisting_reason` reaches back through
            # `corporate_actions` for a uid: that round trip is what would
            # otherwise recurse into this method.
            self._resolved[ticker] = (uid, row_class)
            rows.append(SecurityMasterRow(
                security_uid=uid,
                ticker=ticker,
                source=self.source,
                asset_class=row_class,
                name=str(row.get("name") or "") or None,
                exchange=exchange or None,
                venue=VENUE_BY_EXCHANGE.get(exchange, "other" if exchange else "unknown"),
                ticker_valid_from=self._optional_date(row, "firstpricedate"),
                ticker_valid_to=last_price if delisted else None,
                listing_date=self._optional_date(row, "firstpricedate"),
                # Sharadar has no delisting-date column; the last price
                # observation is the best available proxy and is labelled as
                # one in docs/PRICE_PLANE.md.
                delisting_date=last_price if delisted else None,
                delisting_reason=self.delisting_reason(ticker) if delisted else "unknown",
            ))
        return tuple(sorted(rows, key=lambda r: (r.ticker, r.security_uid)))

    def delisting_reason(self, ticker: str) -> str:
        """Reason category from the ACTIONS row that marks the delisting.

        TICKERS has no reason field, so this is the only vendor-side signal,
        and it is unverified (see module docstring). `unknown` is the honest
        default and the one Spec N §4.4 treats as censored.
        """
        for action in reversed(self.corporate_actions(ticker)):
            mapped = DELISTING_REASON_BY_ACTION.get(action.action_type)
            if mapped is not None:
                return mapped
        return "unknown"

    def index_membership(self, universe_slug: str) -> tuple[MembershipInterval, ...]:
        if universe_slug != "sharadar_sp500_v1":
            raise PricePlaneConfigError(
                f"{self.source} carries no membership history for {universe_slug!r}; "
                f"known universes: {self.known_universes()}"
            )
        opened: dict[str, tuple[date, str]] = {}
        intervals: list[MembershipInterval] = []
        rows = sorted(
            self._rows(TABLE_SP500, {}, SP500_COLUMNS),
            key=lambda r: (str(r.get("date")), str(r.get("ticker"))),
        )
        for row in rows:
            effective = self._need_date(row, "date", "sp500")
            ticker = str(self._need(row, "ticker", f"sp500 {effective}"))
            action = str(self._need(row, "action", f"sp500 {effective}")).strip().lower()
            uid = f"{SOURCE}:sp500:{ticker}"
            if action in SP500_ADD_ACTIONS:
                opened.setdefault(ticker, (effective, uid))
            elif action in SP500_REMOVE_ACTIONS and ticker in opened:
                member_from, uid = opened.pop(ticker)
                intervals.append(MembershipInterval(
                    universe_slug=universe_slug, security_uid=uid, ticker=ticker,
                    member_from=member_from, member_to=effective, source=self.source,
                    known_at_utc=datetime.combine(member_from, datetime.min.time(), timezone.utc),
                ))
        for ticker, (member_from, uid) in sorted(opened.items()):
            intervals.append(MembershipInterval(
                universe_slug=universe_slug, security_uid=uid, ticker=ticker,
                member_from=member_from, member_to=None, source=self.source,
                known_at_utc=datetime.combine(member_from, datetime.min.time(), timezone.utc),
            ))
        return tuple(sorted(intervals, key=lambda i: (i.member_from, i.ticker)))

    def known_universes(self) -> tuple[str, ...]:
        return ("sharadar_sp500_v1",)

    def _resolve(self, ticker: str) -> tuple[str, str]:
        """`(security_uid, asset_class)` for a ticker, from the `tickers` master.

        The uid is `permaticker`-derived; the class says which price table the
        name's bars are in. Both are resolved in one auto-detecting master call
        (`asset_class=None`) rather than two, and cached per instance: a
        backfill over 5,000 names would otherwise fetch the master 5,000 times.

        Raises rather than defaulting to `equity` for a name `tickers` does not
        carry. Defaulting would send the request to `stocks`, get an empty list
        back, and report "no bars" for a name the vendor has — which is exactly
        the failure this whole change exists to undo.
        """
        if ticker not in self._resolved:
            rows = self.security_master([ticker], asset_class=None)
            if not rows:
                raise PricePlaneSchemaError(
                    f"tickers has no row for {ticker!r} in either the "
                    f"{TABLE_STOCKS!r} or the {TABLE_FUNDS!r} price table; "
                    f"refusing to invent a security id"
                )
            self._resolved[ticker] = (rows[0].security_uid, rows[0].asset_class)
        return self._resolved[ticker]

    def _uid_for(self, ticker: str) -> str:
        """Back-compat alias: the uid half of `_resolve`."""
        return self._resolve(ticker)[0]


class BulkBarStream:
    """One ticker's `DailyBar`s at a time from a staged bulk `stocks` zip.

    Returned by `SharadarPricePlane.open_bulk_bar_stream`. The intended loop
    (`scripts/price_backfill.py`'s `backfill_bulk`):

        for ticker in stream.tickers():
            bars = stream.bars_for(ticker)   # the only per-ticker-sized step
            ... remap uid, store in one transaction ...
            stream.drop(ticker)              # free the staged rows

    `tickers()` is cheap (one indexed `SELECT DISTINCT`) and safe to call
    speculatively; `bars_for` is the step that costs one ticker's worth of
    memory, never the whole zip's.
    """

    def __init__(
        self,
        plane: "SharadarPricePlane",
        store: BulkStagingStore,
        factors_by_ticker: Mapping[str, Mapping[date, tuple[float, float]]] | None = None,
    ):
        self._plane = plane
        self._store = store
        self._factors_by_ticker = factors_by_ticker or {}

    def tickers(self) -> list[str]:
        """Every ticker still staged, ascending. Shrinks as `drop` is called."""
        return self._store.distinct_tickers()

    def bars_for(self, ticker: str) -> tuple[DailyBar, ...]:
        """Build `ticker`'s bars from its staged rows, factors applied.

        `security_uid` is still the `sharadar:bulk:<ticker>` placeholder — the
        caller remaps it through `security_master` before storing, exactly as
        it always has (see `load_bulk_bars`'s docstring).
        """
        uid = f"{SOURCE}:bulk:{ticker}"
        factors = self._factors_by_ticker.get(ticker, {})
        bars = []
        for row in self._store.rows_for_ticker(ticker):
            session = self._plane._need_date(row, "date", f"stocks {ticker}")
            split_factor, dividend_cash = factors.get(session, (1.0, 0.0))
            bars.append(self._plane._bar_from_row(row, ticker, uid, split_factor, dividend_cash))
        return with_derived_series(sorted(bars, key=lambda b: b.session_date))

    def drop(self, ticker: str) -> None:
        """Free `ticker`'s staged rows. Call only after they are safely stored."""
        self._store.drop_ticker(ticker)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "BulkBarStream":
        return self

    def __exit__(self, *exc_info) -> bool:
        self.close()
        return False
