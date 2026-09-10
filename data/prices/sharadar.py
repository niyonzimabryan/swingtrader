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
  adapter always sends `table=stocks` (confirmed server-side to filter
  correctly) so `security_master` never double-counts a name.
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
"""

from __future__ import annotations

import csv
import io
import os
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from data.prices.base import (
    CorporateActionRecord,
    DailyBar,
    MembershipInterval,
    PricePlane,
    PricePlaneConfigError,
    PricePlaneSchemaError,
    SecurityMasterRow,
)
from data.prices.derived import with_derived_series

SOURCE = "sharadar"

#: The direct API. Not Nasdaq Data Link — see the module docstring and
#: `docs/vendors/sharadar.md`.
BASE_URL = "https://api.sharadar.com/v1.0"

#: Modern table names the direct API uses in `/data/{table}`. Legacy codes
#: (`SEP`, `TICKERS`, `ACTIONS`, `SP500`) also work per the vendor docs but the
#: modern names are what this adapter sends.
TABLE_STOCKS = "stocks"
TABLE_ACTIONS = "actions"
TABLE_TICKERS = "tickers"
TABLE_SP500 = "sp500"

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
ACTIONS_COLUMNS = ("date", "action", "ticker", "value")
TICKERS_COLUMNS = (
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


class PricePlaneAuthError(PricePlaneConfigError):
    """The vendor rejected the key, or the key's plan does not cover this call.

    A subclass of `PricePlaneConfigError` rather than `PricePlaneSchemaError`:
    a bad payload shape means "the adapter and the vendor disagree", while a
    rejected key or an exceeded plan means "this deployment is misconfigured",
    which is exactly what `PricePlaneConfigError` already means for a missing
    key. Callers that only catch `PricePlaneConfigError` still catch this.
    """


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
        self._uid_cache: dict[str, str] = {}

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
        """Parse a `stocks` bulk zip into bars per ticker, factors applied.

        The zip's single CSV member is read by header name rather than a
        hardcoded filename — the exact name inside the zip is unverified
        (bulk needs a paid key; see `bulk_download`'s docstring), but its
        column headers are the same `STOCKS_COLUMNS` names the slice path
        already validates against, per the CSV slice recorded in
        `tests/fixtures/sharadar_direct/stocks_aapl.csv`.

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
        rows_by_ticker = self._read_bulk_csv(zip_path, STOCKS_COLUMNS)
        out: dict[str, tuple[DailyBar, ...]] = {}
        for ticker, rows in rows_by_ticker.items():
            uid = f"{SOURCE}:bulk:{ticker}"
            bars = [self._bar_from_row(row, ticker, uid, split_factor=1.0, dividend_cash=0.0)
                    for row in rows]
            out[ticker] = with_derived_series(sorted(bars, key=lambda b: b.session_date))
        return out

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
    ) -> DailyBar:
        session = self._need_date(row, "date", f"stocks {ticker}")
        context = f"stocks {ticker} {session}"
        adjusted_close = self._need_float(row, "close", context)
        raw_close = self._need_float(row, "closeunadj", context)
        if adjusted_close <= 0:
            raise PricePlaneSchemaError(f"{context}: close is {adjusted_close}")
        # `stocks`' OHLC are split-adjusted; closeunadj is not. The ratio is
        # the cumulative split adjustment as of this session, exactly.
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
        params: dict[str, Any] = {"ticker": ticker}
        if start:
            params["from"] = start.isoformat()
        if end:
            params["to"] = end.isoformat()

        uid = self._uid_for(ticker)
        factors = self._factors(ticker, start, end)

        bars: list[DailyBar] = []
        for row in self._rows(TABLE_STOCKS, params, STOCKS_COLUMNS):
            session = self._need_date(row, "date", f"stocks {ticker}")
            split_factor, dividend_cash = factors.get(session, (1.0, 0.0))
            bars.append(self._bar_from_row(row, ticker, uid, split_factor, dividend_cash))

        if not bars:
            return ()
        return with_derived_series(sorted(bars, key=lambda b: b.session_date))

    def _factors(
        self, ticker: str, start: date | None, end: date | None
    ) -> dict[date, tuple[float, float]]:
        """`{ex_date: (split_factor, dividend_cash)}` from actions."""
        out: dict[date, tuple[float, float]] = {}
        for action in self.corporate_actions(ticker, start, end):
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

        uid = self._uid_for(ticker)
        records = [
            self._action_from_row(row, ticker, uid)
            for row in self._rows(TABLE_ACTIONS, params, ACTIONS_COLUMNS)
        ]
        return tuple(sorted(records, key=lambda r: (r.ex_date, r.action_type)))

    def security_master(
        self, tickers: Sequence[str] | None = None
    ) -> tuple[SecurityMasterRow, ...]:
        # `table=stocks` filters the direct API's per-plan duplication:
        # `tickers` returns one row per plan a name appears in (`stocks`,
        # `fundamentals`, `insiders`) unless filtered — confirmed live, see
        # the module docstring and `tests/fixtures/sharadar_direct/README.md`.
        params: dict[str, Any] = {"table": TABLE_STOCKS}
        if tickers is not None:
            params["ticker"] = ",".join(tickers)

        rows = []
        for row in self._rows(TABLE_TICKERS, params, TICKERS_COLUMNS):
            ticker = str(self._need(row, "ticker", "tickers"))
            context = f"tickers {ticker}"
            uid = f"{SOURCE}:{self._need(row, 'permaticker', context)}"
            exchange = str(row.get("exchange") or "").strip().upper()
            delisted = str(self._need(row, "isdelisted", context)).strip().upper() == "Y"
            last_price = self._optional_date(row, "lastpricedate")
            # Seed the cache before `delisting_reason` reaches back through
            # `corporate_actions` for a uid: that round trip is what would
            # otherwise recurse into this method.
            self._uid_cache[ticker] = uid
            rows.append(SecurityMasterRow(
                security_uid=uid,
                ticker=ticker,
                source=self.source,
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

    def _uid_for(self, ticker: str) -> str:
        """A stable id for a ticker, from TICKERS' `permaticker`.

        Cached per instance: a backfill over 5,000 names would otherwise fetch
        the master 5,000 times.
        """
        if ticker not in self._uid_cache:
            rows = self.security_master([ticker])
            if not rows:
                raise PricePlaneSchemaError(
                    f"tickers has no row for {ticker!r}; refusing to invent a security id"
                )
            self._uid_cache[ticker] = rows[0].security_uid
        return self._uid_cache[ticker]
