"""The `tickers` bulk snapshot: one request where 470 used to go.

`--bulk years=10` against production on 2026-09-14 got, after #98 made both
`ticker`-parameter limits correct:

    HTTP 429 ... Request count quota exceeded. Slow down or use bulk
    downloads for large extracts.

A whole-market master lookup at 30 tickers a request is ~470 requests, plus
one `actions` slice per delisted name for its reason category.
`SharadarPricePlane.iter_security_master_bulk` reads the `tickers` full
snapshot instead — one request — and the per-request `security_master` stays
exactly as #98 left it for short lists.

The test that matters is `IdenticalRowsTests`: **the same vendor rows must
produce byte-identical `SecurityMasterRow`s down either path.** A bulk path
that classified `asset_class` or built `security_uid` differently would not
fail here; it would write a second, subtly different master row for a name
already in the database and orphan its bars months later.
"""

from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

from data.prices.base import (
    ASSET_CLASS_EQUITY,
    ASSET_CLASS_FUND,
    PricePlaneConfigError,
    PricePlaneSchemaError,
)
from data.prices.sharadar import TICKERS_COLUMNS, SharadarPricePlane

# --------------------------------------------------------------------------- #
# Fixtures: the same rows served two ways — as an HTTP `tickers` page and as
# the `tickers` bulk zip — so the two paths can be compared on one input.
# --------------------------------------------------------------------------- #

#: One delisted equity, one live equity, one fund, and one non-price-plan row
#: (`fundamentals`) that only the auto-detect path ever sees.
_TICKERS_ROWS = [
    {"table": "stocks", "permaticker": "199059", "ticker": "BBBY",
     "name": "Bed Bath & Beyond Inc", "exchange": "NASDAQ", "isdelisted": "Y",
     "firstpricedate": "1992-07-28", "lastpricedate": "2023-05-02"},
    {"table": "stocks", "permaticker": "320193", "ticker": "AAPL",
     "name": "Apple Inc", "exchange": "NASDAQ", "isdelisted": "N",
     "firstpricedate": "1980-12-12", "lastpricedate": "2026-09-11"},
    {"table": "funds", "permaticker": "118691", "ticker": "SPY",
     "name": "SPDR S&P 500 ETF Trust", "exchange": "NYSEARCA", "isdelisted": "N",
     "firstpricedate": "1993-01-29", "lastpricedate": "2026-09-11"},
    {"table": "fundamentals", "permaticker": "320193", "ticker": "AAPL",
     "name": "Apple Inc", "exchange": "NASDAQ", "isdelisted": "N",
     "firstpricedate": "1980-12-12", "lastpricedate": "2026-09-11"},
]

#: BBBY's delisting is a bankruptcy -> `performance`. The dividend row is
#: there to prove the mapping skips unmappable actions rather than stopping at
#: the last row.
_ACTIONS_ROWS = [
    {"date": "2022-01-10", "action": "dividend", "ticker": "BBBY", "value": 0.10},
    {"date": "2023-04-24", "action": "bankruptcy", "ticker": "BBBY", "value": None},
]


def _zip_bytes(name: str, csv_text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, csv_text)
    return buffer.getvalue()


def _csv_text(rows, columns) -> str:
    lines = [",".join(columns)]
    for row in rows:
        lines.append(",".join(
            "" if row.get(c) is None else str(row.get(c)).replace(",", " ")
            for c in columns
        ))
    return "\n".join(lines) + "\n"


def _tickers_zip(path: Path, rows=None) -> Path:
    rows = _TICKERS_ROWS if rows is None else rows
    path.write_bytes(_zip_bytes(
        "SHARADAR_TICKERS.csv", _csv_text(rows, TICKERS_COLUMNS),
    ))
    return path


class _StubResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = ""

    def json(self):
        return self._payload


class _StubStream:
    def __init__(self, content=b""):
        self.status_code = 200
        self._content = content

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def iter_bytes(self):
        yield self._content


class _StubClient:
    """Enough `httpx.Client` to drive both master paths. Records every call.

    The `tickers` table honours the `ticker=` filter, unlike the looser stubs
    elsewhere in the suite, because these tests care about *which* names each
    path asked for and how many requests it took to ask.
    """

    def __init__(self, tables=None, streams=None):
        self.tables = tables or {}
        self.streams = streams or {}
        self.calls = []
        self.stream_calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        table = url.rsplit("/", 1)[-1]
        params = dict(params or {})
        self.calls.append((table, params))
        rows = list(self.tables.get(table, []))
        wanted_table = params.get("table")
        if wanted_table is not None:
            rows = [r for r in rows if r.get("table") == wanted_table]
        wanted = params.get("ticker")
        if wanted is not None:
            names = set(str(wanted).split(","))
            rows = [r for r in rows if r.get("ticker") in names]
        limit = params.get("limit")
        offset = params.get("offset", 0) or 0
        page = rows[offset:offset + limit] if limit is not None else rows
        return _StubResponse({"count": len(page), "data": page})

    def stream(self, method, url, params=None, headers=None, timeout=None, follow_redirects=None):
        table = url.rsplit("/", 1)[-1]
        self.stream_calls.append((table, dict(params or {})))
        return self.streams[table]

    def tickers_get_calls(self):
        return [params for table, params in self.calls if table == "tickers"]


def _plane(tickers_rows=None, actions_rows=None):
    rows = _TICKERS_ROWS if tickers_rows is None else tickers_rows
    actions = _ACTIONS_ROWS if actions_rows is None else actions_rows
    client = _StubClient(
        tables={"tickers": rows, "actions": actions},
        streams={"tickers": _StubStream(
            content=_zip_bytes("SHARADAR_TICKERS.csv", _csv_text(rows, TICKERS_COLUMNS)),
        )},
    )
    return SharadarPricePlane(api_key="test-key", client=client), client


def _bulk_actions(plane, actions_rows=None):
    """`load_bulk_actions`' shape, from the same rows the HTTP table serves."""
    rows = _ACTIONS_ROWS if actions_rows is None else actions_rows
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "actions.zip"
        path.write_bytes(_zip_bytes(
            "SHARADAR_ACTIONS.csv",
            _csv_text(rows, ("date", "action", "ticker", "value")),
        ))
        return plane.load_bulk_actions(path)


class IdenticalRowsTests(unittest.TestCase):
    """The two paths must agree row for row, on the same input rows."""

    def _both(self, asset_class):
        per_request_plane, _ = _plane()
        per_request = per_request_plane.security_master(asset_class=asset_class)

        bulk_plane, _ = _plane()
        actions_by_ticker = _bulk_actions(bulk_plane)
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = _tickers_zip(Path(tmp) / "tickers.zip")
            bulk = tuple(sorted(
                bulk_plane.iter_security_master_bulk(
                    zip_path, asset_class=asset_class, actions_by_ticker=actions_by_ticker,
                ),
                key=lambda r: (r.ticker, r.security_uid),
            ))
        return per_request, bulk

    def test_equity_rows_are_identical(self):
        per_request, bulk = self._both(ASSET_CLASS_EQUITY)
        self.assertEqual([r.ticker for r in per_request], ["AAPL", "BBBY"])
        self.assertEqual(bulk, per_request)

    def test_fund_rows_are_identical(self):
        per_request, bulk = self._both(ASSET_CLASS_FUND)
        self.assertEqual([r.ticker for r in per_request], ["SPY"])
        self.assertEqual(bulk, per_request)

    def test_auto_detect_rows_are_identical(self):
        """`asset_class=None`: the `fundamentals` row is dropped by both."""
        per_request, bulk = self._both(None)
        self.assertEqual([r.ticker for r in per_request], ["AAPL", "BBBY", "SPY"])
        self.assertEqual(bulk, per_request)

    def test_the_delisted_row_carries_a_real_reason_down_both_paths(self):
        """Equality above is only meaningful if the field is actually set."""
        per_request, bulk = self._both(ASSET_CLASS_EQUITY)
        by_ticker = {r.ticker: r for r in bulk}
        self.assertEqual(by_ticker["BBBY"].delisting_reason, "performance")
        self.assertEqual(by_ticker["BBBY"].delisting_date, date(2023, 5, 2))
        self.assertEqual(by_ticker["BBBY"].ticker_valid_to, date(2023, 5, 2))
        self.assertIsNone(by_ticker["AAPL"].delisting_date)
        self.assertEqual(by_ticker["AAPL"].delisting_reason, "unknown")
        self.assertEqual({r.ticker: r.delisting_reason for r in per_request},
                         {r.ticker: r.delisting_reason for r in bulk})

    def test_the_bulk_path_seeds_the_resolve_cache_like_the_per_request_one(self):
        plane, _ = _plane()
        actions_by_ticker = _bulk_actions(plane)
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = _tickers_zip(Path(tmp) / "tickers.zip")
            list(plane.iter_security_master_bulk(
                zip_path, asset_class=None, actions_by_ticker=actions_by_ticker,
            ))
        self.assertEqual(plane._resolved["SPY"], ("sharadar:118691", ASSET_CLASS_FUND))
        self.assertEqual(plane._resolved["AAPL"], ("sharadar:320193", ASSET_CLASS_EQUITY))


class AssetClassSplitTests(unittest.TestCase):
    """The `stocks`/`funds` split is preserved client-side, not guessed."""

    def test_equity_default_excludes_the_fund(self):
        plane, _ = _plane()
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = _tickers_zip(Path(tmp) / "tickers.zip")
            rows = list(plane.iter_security_master_bulk(zip_path))
        self.assertEqual(sorted(r.ticker for r in rows), ["AAPL", "BBBY"])
        self.assertTrue(all(r.asset_class == ASSET_CLASS_EQUITY for r in rows))

    def test_fund_class_returns_only_the_fund(self):
        plane, _ = _plane()
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = _tickers_zip(Path(tmp) / "tickers.zip")
            rows = list(plane.iter_security_master_bulk(zip_path, asset_class=ASSET_CLASS_FUND))
        self.assertEqual([(r.ticker, r.asset_class) for r in rows],
                         [("SPY", ASSET_CLASS_FUND)])

    def test_an_unknown_asset_class_is_refused_before_the_zip_is_opened(self):
        plane, _ = _plane()
        with self.assertRaises(PricePlaneConfigError):
            list(plane.iter_security_master_bulk("no-such.zip", asset_class="crypto"))


class BothPriceTablesTests(unittest.TestCase):
    """A name in `stocks` *and* `funds` still raises rather than picking one."""

    _AMBIGUOUS = [
        {"table": "stocks", "permaticker": "1", "ticker": "DUAL", "name": "Dual Inc",
         "exchange": "NASDAQ", "isdelisted": "N",
         "firstpricedate": "2000-01-03", "lastpricedate": "2026-09-11"},
        {"table": "funds", "permaticker": "2", "ticker": "DUAL", "name": "Dual Trust",
         "exchange": "NYSEARCA", "isdelisted": "N",
         "firstpricedate": "2000-01-03", "lastpricedate": "2026-09-11"},
    ]

    def test_the_bulk_path_raises_on_the_auto_detect_path(self):
        plane, _ = _plane(tickers_rows=self._AMBIGUOUS)
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = _tickers_zip(Path(tmp) / "tickers.zip", rows=self._AMBIGUOUS)
            with self.assertRaises(PricePlaneSchemaError) as caught:
                list(plane.iter_security_master_bulk(zip_path, asset_class=None))
        self.assertIn("both", str(caught.exception))
        self.assertIn("DUAL", str(caught.exception))

    def test_the_per_request_path_raises_the_same_way(self):
        """The oracle for the assertion above — unchanged by this branch."""
        plane, _ = _plane(tickers_rows=self._AMBIGUOUS)
        with self.assertRaises(PricePlaneSchemaError) as caught:
            plane.security_master(["DUAL"], asset_class=None)
        self.assertIn("both", str(caught.exception))

    def test_naming_a_class_explicitly_still_resolves_it(self):
        plane, _ = _plane(tickers_rows=self._AMBIGUOUS)
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = _tickers_zip(Path(tmp) / "tickers.zip", rows=self._AMBIGUOUS)
            rows = list(plane.iter_security_master_bulk(zip_path, asset_class=ASSET_CLASS_EQUITY))
        self.assertEqual([r.security_uid for r in rows], ["sharadar:1"])


class SchemaValidationTests(unittest.TestCase):
    def test_a_missing_expected_column_is_a_schema_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "tickers.zip"
            zip_path.write_bytes(_zip_bytes(
                "SHARADAR_TICKERS.csv",
                "permaticker,ticker,name\n320193,AAPL,Apple Inc\n",
            ))
            plane, _ = _plane()
            with self.assertRaises(PricePlaneSchemaError) as caught:
                list(plane.iter_security_master_bulk(zip_path))
        self.assertIn("table", str(caught.exception))

    def test_a_zip_with_no_csv_member_is_a_schema_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "tickers.zip"
            with zipfile.ZipFile(zip_path, "w"):
                pass
            plane, _ = _plane()
            with self.assertRaises(PricePlaneSchemaError):
                list(plane.iter_security_master_bulk(zip_path))


class RecordedVendorHeaderTests(unittest.TestCase):
    """The one piece of this that is not a synthetic fixture.

    The *bulk* `tickers` zip could not be fetched here (bulk is
    subscription-gated and the daily quota was exhausted), so what the zip's
    CSV header looks like is an assumption — the same assumption the `stocks`
    bulk path already makes. `tests/fixtures/sharadar_direct/
    tickers_aapl_stocks.csv` is a real recorded `format=csv` response from the
    same table, and the parser is driven over it here so that assumption is at
    least anchored to vendor bytes. If the bulk file's header differs, every
    column name in `TICKERS_COLUMNS` is validated up front and the run stops
    with a `PricePlaneSchemaError` naming the missing column — it does not
    silently classify anything wrong.
    """

    FIXTURE = Path(__file__).parent / "fixtures" / "sharadar_direct" / "tickers_aapl_stocks.csv"

    def test_the_recorded_vendor_csv_parses_into_a_master_row(self):
        plane, client = _plane()
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "tickers.zip"
            zip_path.write_bytes(_zip_bytes(
                "SHARADAR_TICKERS.csv", self.FIXTURE.read_text(encoding="utf-8"),
            ))
            rows = list(plane.iter_security_master_bulk(zip_path, actions_by_ticker={}))

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.ticker, "AAPL")
        self.assertEqual(row.security_uid, "sharadar:199059")
        self.assertEqual(row.asset_class, ASSET_CLASS_EQUITY)
        self.assertEqual(row.venue, "nasdaq")
        self.assertEqual(row.ticker_valid_from, date(1986, 1, 1))
        self.assertIsNone(row.delisting_date)

    def test_the_columns_the_parser_needs_are_all_in_the_recorded_header(self):
        header = self.FIXTURE.read_text(encoding="utf-8").splitlines()[0].split(",")
        self.assertEqual([c for c in TICKERS_COLUMNS if c not in header], [])


class DelistingReasonSourceTests(unittest.TestCase):
    """Same mapping, two sources for the actions rows behind it."""

    def test_the_bulk_actions_table_gives_the_same_reason_as_a_slice_request(self):
        plane, client = _plane()
        actions_by_ticker = _bulk_actions(plane)
        self.assertEqual(
            plane._delisting_reason_from_actions(actions_by_ticker["BBBY"]),
            plane.delisting_reason("BBBY"),
        )
        self.assertEqual(plane._delisting_reason_from_actions(actions_by_ticker["BBBY"]),
                         "performance")

    def test_supplying_the_actions_table_costs_no_actions_request(self):
        plane, client = _plane()
        actions_by_ticker = _bulk_actions(plane)
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = _tickers_zip(Path(tmp) / "tickers.zip")
            list(plane.iter_security_master_bulk(zip_path, actions_by_ticker=actions_by_ticker))
        self.assertEqual([t for t, _ in client.calls], [])

    def test_without_the_actions_table_it_falls_back_to_one_request_per_delisted_name(self):
        """Backward compatible: the fallback is the old behaviour, not a hole."""
        plane, client = _plane()
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = _tickers_zip(Path(tmp) / "tickers.zip")
            rows = list(plane.iter_security_master_bulk(zip_path))
        self.assertEqual([t for t, _ in client.calls], ["actions"])  # BBBY only
        self.assertEqual({r.ticker: r.delisting_reason for r in rows},
                         {"AAPL": "unknown", "BBBY": "performance"})


class RequestCountTests(unittest.TestCase):
    """The 429 is a request-count problem; this is the count."""

    @staticmethod
    def _many_rows(n):
        return [
            {"table": "stocks", "permaticker": str(1000 + i), "ticker": f"T{i:04d}",
             "name": f"Name {i}", "exchange": "NASDAQ", "isdelisted": "N",
             "firstpricedate": "2000-01-03", "lastpricedate": "2026-09-11"}
            for i in range(n)
        ]

    def test_one_zip_replaces_the_batched_requests(self):
        from scripts.price_backfill import _master_batches

        rows = self._many_rows(900)
        tickers = [r["ticker"] for r in rows]
        plane, client = _plane(tickers_rows=rows, actions_rows=[])

        # What the per-request path costs for the same universe.
        for batch in _master_batches(tickers):
            plane.security_master(batch)
        per_request_calls = len(client.tickers_get_calls())
        self.assertEqual(per_request_calls, 30)  # 900 / 30-per-request

        bulk_plane, bulk_client = _plane(tickers_rows=rows, actions_rows=[])
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = _tickers_zip(Path(tmp) / "tickers.zip", rows=rows)
            bulk_rows = list(bulk_plane.iter_security_master_bulk(
                zip_path, actions_by_ticker={},
            ))
        self.assertEqual(len(bulk_rows), 900)
        self.assertEqual(bulk_client.tickers_get_calls(), [])

        # One bulk_download is one request; prove that too, through the
        # method the backfill actually calls.
        with tempfile.TemporaryDirectory() as tmp:
            bulk_plane.bulk_download("tickers", "10", Path(tmp) / "tickers.zip")
        self.assertEqual([t for t, _ in bulk_client.stream_calls], ["tickers"])


class SmallListPathsStillSliceTests(unittest.TestCase):
    """`--tickers`, the delisting audit and `daily_bars` keep the 470-beats-1
    path: a whole-market download to resolve three names is the wrong trade."""

    def test_daily_bars_resolution_uses_a_single_ticker_request(self):
        plane, client = _plane()
        uid, asset_class = plane._resolve("SPY")
        self.assertEqual((uid, asset_class), ("sharadar:118691", ASSET_CLASS_FUND))
        self.assertEqual(client.tickers_get_calls()[0]["ticker"], "SPY")
        self.assertEqual(client.stream_calls, [])

    def test_the_audit_resolver_uses_the_per_request_path(self):
        from data.prices import audit
        from data.prices.delisting_audit_list import DelistingCase

        plane, client = _plane()
        case = DelistingCase(
            ticker="BBBY", company="Bed Bath & Beyond Inc",
            delisting_date=date(2023, 5, 2), venue="nasdaq", event="Chapter 11",
            source_kind="sec_form_25", source_url="https://www.sec.gov/example",
        )
        resolution = audit.resolve_case(plane, case)
        self.assertEqual(resolution.resolution, "security_master")
        self.assertEqual(client.tickers_get_calls()[0]["ticker"], "BBBY")
        self.assertEqual(client.stream_calls, [])

    def test_backfill_batches_an_explicit_ticker_list_and_downloads_nothing(self):
        from scripts.price_backfill import (
            MASTER_TICKER_PARAM_MAX_CHARS,
            MASTER_TICKER_PARAM_MAX_COUNT,
            _master_batches,
        )

        rows = RequestCountTests._many_rows(100)
        tickers = [r["ticker"] for r in rows]
        plane, client = _plane(tickers_rows=rows, actions_rows=[])
        for batch in _master_batches(tickers):
            plane.security_master(batch)

        sent = [params["ticker"] for params in client.tickers_get_calls()]
        self.assertEqual(len(sent), 4)  # 100 names, 30 per request
        for parameter in sent:
            self.assertLessEqual(len(parameter), MASTER_TICKER_PARAM_MAX_CHARS)
            self.assertLessEqual(len(parameter.split(",")), MASTER_TICKER_PARAM_MAX_COUNT)
        self.assertEqual(client.stream_calls, [])
        self.assertEqual(
            [t for batch in _master_batches(tickers) for t in batch], tickers,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
