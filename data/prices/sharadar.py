"""`SharadarPricePlane` — Sharadar via Nasdaq Data Link's tables API.

Sharadar's "Prices" tier is the one paid feed in the whole stack (README §3):
prices, corporate actions, a ticker master with listing/delisting dates, and
S&P 500 constituent history, for $9/mo "from". Nothing here spends money and
nothing here has been run against a live key — none exists yet.

Why `httpx` and not `nasdaq-data-link`
--------------------------------------
`httpx` is already a pinned dependency, so this adds none. The vendor SDK
returns a pandas DataFrame, which is exactly the wrong shape for the one thing
this adapter must do: fail loudly when the payload changes. A DataFrame has
already coerced the payload — a renamed column becomes a `KeyError` somewhere
downstream, and a null becomes `NaN` that flows into a stored bar. Reading the
raw JSON lets the column list be compared against the list this code was
written against, before a single value is parsed.

Table and column names
----------------------
The names below are what this adapter is written against. **They were verified
from search extracts, not from a fetched vendor page**: this session's egress
proxy blocks `data.nasdaq.com`, `sharadar.com` and `quantrocket.com`, which is
the same failure the original research pass hit (verification Claim 7, still
`UNVERIFIED` across the board). Sources are listed in `docs/PRICE_PLANE.md`.
The adapter therefore treats its column list as a *hypothesis it checks*, not as
a fact: `_rows` raises `PricePlaneSchemaError` if any expected column is absent.

Two consequences of the verified column semantics are load-bearing:

* SEP's `open`/`high`/`low`/`close` are adjusted for splits and stock dividends;
  `closeunadj` is the unadjusted close. So the **raw** OHLC that Spec N §4.3
  wants is reconstructed as `field x (closeunadj / close)`, exactly, and the raw
  close is `closeunadj` itself.
* `closeadj` is Sharadar's own total-return close. This adapter does **not**
  store it. The stored total-return series is derived from the raw closes and
  the stored factors, so that the §4.3 reconstruction identity holds against the
  factors we actually keep; Sharadar's anchoring convention and its spinoff
  handling are both unverified, and a series that cannot be reproduced from the
  stored factors is not inspectable. Comparing the two once a key exists is a
  cheap cross-check and an open item in `docs/PRICE_PLANE.md`.

TICKERS carries **no delisting-reason field** (verified negative from the same
extracts). The reason is therefore derived from the ACTIONS `action` value where
one exists and is `unknown` otherwise — which is the correct answer, and which
`§4.4` treats as censored rather than matured. `scripts/audit_delisting_returns.py`
is what turns that into a number before anyone pays.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
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

# PORT PENDING (docs/vendors/sharadar.md): a sharadar.com key does not work here.
# The direct API is https://api.sharadar.com/v1.0/data/{table} with `x-api-key`
# and `ticker`/`from`/`to`/`fields` — see the handoff. This constant stays until
# the port lands so the fixture path and tests are unchanged.
BASE_URL = "https://data.nasdaq.com/api/v3/datatables"

#: Environment variable carrying the key. Never hardcoded, never logged.
API_KEY_ENV = "NASDAQ_DATA_LINK_API_KEY"

#: Columns each table must contain. Extra columns are tolerated (a vendor adding
#: a field should not stop ingest); a missing or renamed one raises.
SEP_COLUMNS = ("ticker", "date", "open", "high", "low", "close", "volume", "closeunadj")
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
#: **Unverified.** The set of values Sharadar emits is not documented anywhere
#: this session could reach; only `split`, `dividend` and a generic delisting
#: marker are attested. Anything not listed maps to `other`, and a security with
#: no delisting action at all keeps `unknown`. Widening this map is a one-line
#: change once a key exists and the distinct values can be listed.
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
#: also carries quarterly snapshot rows (`current` / `historical`), and treating
#: a snapshot as a join would date every current constituent's membership to the
#: snapshot. The whole SP500 reconstruction here is unverified; the membership
#: source this repo actually loads is the free `sp500_wikipedia_v1`
#: (`data/prices/sp500_history.py`), and this exists so the interface is
#: complete rather than because it is trusted.
SP500_ADD_ACTIONS = ("added",)
SP500_REMOVE_ACTIONS = ("removed",)

DEFAULT_TIMEOUT_S = 30.0
MAX_PAGES = 500


def api_key_from_env(explicit: str | None = None) -> str:
    key = (explicit or os.environ.get(API_KEY_ENV) or "").strip()
    if not key:
        raise PricePlaneConfigError(
            f"{API_KEY_ENV} is not set. The Sharadar plane needs a Nasdaq Data Link "
            "key; run against FixturePricePlane if you are testing."
        )
    return key


class SharadarPricePlane(PricePlane):
    """Sharadar SEP / ACTIONS / TICKERS / SP500 behind the `PricePlane` contract.

    `client` is any object with a `get(url, params=..., timeout=...)` returning
    something with `.status_code`, `.json()` and `.text` — an `httpx.Client` in
    production, a stub in tests. It is injected rather than constructed so that
    the suite can exercise the parsing and the schema guard with no network at
    all (`test_no_network_in_tests`).
    """

    source = SOURCE

    def __init__(self, api_key: str | None = None, client: Any = None, timeout: float = DEFAULT_TIMEOUT_S):
        self._api_key = api_key_from_env(api_key)
        self._client = client
        self._timeout = timeout
        self._uid_cache: dict[str, str] = {}

    # -- transport -------------------------------------------------------- #

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _rows(self, table: str, params: Mapping[str, Any], expected: Sequence[str]) -> Iterator[dict]:
        """Yield rows of a Sharadar table as dicts, paging through the cursor.

        Raises `PricePlaneSchemaError` on anything that is not the payload shape
        this adapter was written against: a non-JSON body, a missing `datatable`,
        a missing expected column, or a row whose width does not match the
        column list. Silence here is the failure mode that matters — a cohort
        built on quietly-empty bars looks exactly like a cohort built on real
        ones.
        """
        cursor: str | None = None
        for _page in range(MAX_PAGES):
            query = dict(params)
            query["api_key"] = self._api_key
            if cursor:
                query["qopts.cursor_id"] = cursor
            response = self._http().get(f"{BASE_URL}/SHARADAR/{table}.json", params=query)
            status = getattr(response, "status_code", None)
            if status != 200:
                body = (getattr(response, "text", "") or "")[:400]
                raise PricePlaneSchemaError(
                    f"SHARADAR/{table}: HTTP {status} from Nasdaq Data Link: {body}"
                )
            try:
                payload = response.json()
            except Exception as exc:
                raise PricePlaneSchemaError(f"SHARADAR/{table}: response is not JSON") from exc

            datatable = payload.get("datatable") if isinstance(payload, dict) else None
            if not isinstance(datatable, dict) or "data" not in datatable or "columns" not in datatable:
                raise PricePlaneSchemaError(
                    f"SHARADAR/{table}: payload has no datatable/data/columns; "
                    f"top-level keys were {sorted(payload) if isinstance(payload, dict) else type(payload)}"
                )

            names = [column.get("name") for column in datatable["columns"]]
            missing = [name for name in expected if name not in names]
            if missing:
                raise PricePlaneSchemaError(
                    f"SHARADAR/{table}: expected columns {missing} are absent. "
                    f"The table returned {names}. The adapter was written against "
                    f"{list(expected)}; a rename upstream must be handled here, not "
                    "worked around at the call site."
                )

            for row in datatable["data"]:
                if len(row) != len(names):
                    raise PricePlaneSchemaError(
                        f"SHARADAR/{table}: row has {len(row)} values for {len(names)} columns"
                    )
                yield dict(zip(names, row))

            cursor = (payload.get("meta") or {}).get("next_cursor_id")
            if not cursor:
                return
        raise PricePlaneSchemaError(f"SHARADAR/{table}: cursor did not terminate in {MAX_PAGES} pages")

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

    # -- interface -------------------------------------------------------- #

    def daily_bars(
        self, ticker: str, start: date | None = None, end: date | None = None
    ) -> tuple[DailyBar, ...]:
        params: dict[str, Any] = {"ticker": ticker}
        if start:
            params["date.gte"] = start.isoformat()
        if end:
            params["date.lte"] = end.isoformat()

        uid = self._uid_for(ticker)
        factors = self._factors(ticker, start, end)

        bars: list[DailyBar] = []
        for row in self._rows("SEP", params, SEP_COLUMNS):
            session = self._need_date(row, "date", f"SEP {ticker}")
            context = f"SEP {ticker} {session}"
            adjusted_close = self._need_float(row, "close", context)
            raw_close = self._need_float(row, "closeunadj", context)
            if adjusted_close <= 0:
                raise PricePlaneSchemaError(f"{context}: close is {adjusted_close}")
            # SEP's OHLC are split-adjusted; closeunadj is not. The ratio is the
            # cumulative split adjustment as of this session, exactly.
            ratio = raw_close / adjusted_close
            split_factor, dividend_cash = factors.get(session, (1.0, 0.0))
            bars.append(DailyBar(
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
            ))

        if not bars:
            return ()
        return with_derived_series(sorted(bars, key=lambda b: b.session_date))

    def _factors(
        self, ticker: str, start: date | None, end: date | None
    ) -> dict[date, tuple[float, float]]:
        """`{ex_date: (split_factor, dividend_cash)}` from ACTIONS."""
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
            params["date.gte"] = start.isoformat()
        if end:
            params["date.lte"] = end.isoformat()

        uid = self._uid_for(ticker)
        records = []
        for row in self._rows("ACTIONS", params, ACTIONS_COLUMNS):
            ex_date = self._need_date(row, "date", f"ACTIONS {ticker}")
            action = str(self._need(row, "action", f"ACTIONS {ticker} {ex_date}")).strip().lower()
            value = row.get("value")
            records.append(CorporateActionRecord(
                security_uid=uid,
                ticker=ticker,
                ex_date=ex_date,
                action_type=action,
                value=float(value) if value not in (None, "") else None,
                source=self.source,
            ))
        return tuple(sorted(records, key=lambda r: (r.ex_date, r.action_type)))

    def security_master(
        self, tickers: Sequence[str] | None = None
    ) -> tuple[SecurityMasterRow, ...]:
        params: dict[str, Any] = {"table": "SEP"}
        if tickers is not None:
            params["ticker"] = ",".join(tickers)

        rows = []
        for row in self._rows("TICKERS", params, TICKERS_COLUMNS):
            ticker = str(self._need(row, "ticker", "TICKERS"))
            context = f"TICKERS {ticker}"
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
                # observation is the best available proxy and is labelled as one
                # in docs/PRICE_PLANE.md.
                delisting_date=last_price if delisted else None,
                delisting_reason=self.delisting_reason(ticker) if delisted else "unknown",
            ))
        return tuple(sorted(rows, key=lambda r: (r.ticker, r.security_uid)))

    def delisting_reason(self, ticker: str) -> str:
        """Reason category from the ACTIONS row that marks the delisting.

        TICKERS has no reason field, so this is the only vendor-side signal, and
        it is unverified. `unknown` is the honest default and the one Spec N
        §4.4 treats as censored.
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
            self._rows("SP500", {}, SP500_COLUMNS),
            key=lambda r: (str(r.get("date")), str(r.get("ticker"))),
        )
        for row in rows:
            effective = self._need_date(row, "date", "SP500")
            ticker = str(self._need(row, "ticker", f"SP500 {effective}"))
            action = str(self._need(row, "action", f"SP500 {effective}")).strip().lower()
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
                    f"TICKERS has no row for {ticker!r}; refusing to invent a security id"
                )
            self._uid_cache[ticker] = rows[0].security_uid
        return self._uid_cache[ticker]
