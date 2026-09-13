"""The memory-bounded bulk backfill: staging, streaming, checkpoint/`--resume`.

`SharadarPricePlane.load_bulk_bars` used to materialise a whole-market zip's
rows into a `dict[ticker, list[dict]]` before building a single `DailyBar` —
at least two full in-memory copies. Instrumented in production that hit
3.4 GB RSS 15 seconds in and got the bot container SIGKILLed three times
(`docs/investment-workspace/handoff/OWNER_SETUP_EXECUTION_2026-09-12.md` §4).
This module covers the replacement: `data/prices/bulk_stream.BulkStagingStore`
stages the CSV to disk instead of a dict, `SharadarPricePlane.
open_bulk_bar_stream` drains it one ticker at a time, and
`scripts.price_backfill.backfill_bulk` commits and checkpoints one ticker at a
time so a killed run resumes with `--resume` rather than restarting.

The named tests from the brief's own list:

  * streaming parse equals the old parse, bar-for-bar
    (`test_streaming_load_bulk_bars_matches_the_old_in_memory_parse`)
  * staging survives a simulated kill and `--resume` finishes with identical
    rows (`test_resume_after_a_kill_mid_staging_restages_cleanly`,
    `test_resume_after_the_rss_guard_aborts_finishes_the_remaining_tickers`)
  * `--tickers` never stages other tickers
    (`test_tickers_filter_never_stages_the_other_ticker`)
  * header validation errors still raise `PricePlaneSchemaError`
    (`test_a_missing_expected_column_is_a_schema_error`)
  * the RSS guard aborts with a checkpoint
    (`test_resume_after_the_rss_guard_aborts_finishes_the_remaining_tickers`)
  * a tracemalloc peak bound on the streaming path in the default suite
    (`test_streaming_peak_allocation_is_bounded`), with a real-RSS old-vs-new
    comparison on a much bigger synthetic zip behind `SLOW_TESTS=1`
    (`test_streaming_rss_beats_the_old_whole_zip_parse`)
"""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
import zipfile
from datetime import date
from pathlib import Path

from data.prices import store
from data.prices.base import PricePlaneSchemaError
from data.prices.bulk_stream import BulkStagingStore
from data.prices.derived import check_reconstruction, with_derived_series
from data.prices.sharadar import STOCKS_COLUMNS, SharadarPricePlane
from tests.dbfixture import init_test_db


def _zip_bytes(name: str, csv_text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, csv_text)
    return buffer.getvalue()


class _StubResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


class _StubStream:
    def __init__(self, content=b"", status_code=200):
        self.status_code = status_code
        self._content = content

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def iter_bytes(self):
        yield self._content


class _StubClient:
    """Enough of an `httpx.Client` to drive `bulk_download` + `security_master`."""

    def __init__(self, tables=None, streams=None):
        self.tables = tables or {}
        self.streams = streams or {}

    def get(self, url, params=None, headers=None, timeout=None):
        table = url.rsplit("/", 1)[-1]
        params = dict(params or {})
        entry = self.tables[table]
        limit = params.get("limit")
        offset = params.get("offset", 0) or 0
        page = entry[offset:offset + limit] if limit is not None else entry
        return _StubResponse({"count": len(page), "data": page})

    def stream(self, method, url, params=None, headers=None, timeout=None, follow_redirects=None):
        table = url.rsplit("/", 1)[-1]
        return self.streams[table]


def _tickers_rows():
    return [
        {"table": "stocks", "permaticker": "199059", "ticker": "BBBY",
         "name": "Bed Bath & Beyond Inc", "exchange": "NASDAQ", "isdelisted": "Y",
         "firstpricedate": "1992-07-28", "lastpricedate": "2023-05-02"},
        {"table": "stocks", "permaticker": "320193", "ticker": "AAPL",
         "name": "Apple Inc", "exchange": "NASDAQ", "isdelisted": "N",
         "firstpricedate": "1980-12-12", "lastpricedate": "2023-05-02"},
    ]


_STOCKS_CSV = (
    "ticker,date,open,high,low,close,volume,closeunadj\n"
    "BBBY,2023-04-28,1.0,1.1,0.9,1.00,1000000,1.00\n"
    "BBBY,2023-05-01,0.8,0.9,0.7,0.80,2000000,0.80\n"
    "BBBY,2023-05-02,0.3,0.4,0.2,0.30,3000000,0.30\n"
    "AAPL,2023-05-01,170.0,171.0,169.0,170.5,5000000,170.5\n"
    "AAPL,2023-05-02,170.5,172.0,170.0,171.8,4500000,171.8\n"
)
_ACTIONS_CSV = (
    "date,action,ticker,value\n"
    "2023-04-24,bankruptcy,BBBY,\n"
    "2022-01-10,dividend,BBBY,0.10\n"
)


def _stub_plane(stocks_csv=_STOCKS_CSV, actions_csv=_ACTIONS_CSV):
    client = _StubClient(
        tables={"tickers": _tickers_rows(), "actions": []},
        streams={
            "stocks": _StubStream(content=_zip_bytes("SHARADAR_STOCKS.csv", stocks_csv)),
            "actions": _StubStream(content=_zip_bytes("SHARADAR_ACTIONS.csv", actions_csv)),
        },
    )
    return SharadarPricePlane(api_key="test-key", client=client)


class StagingFilterTests(unittest.TestCase):
    """`--tickers` filters during staging, not after (brief item 2)."""

    def test_tickers_filter_never_stages_the_other_ticker(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "stocks.zip"
            zip_path.write_bytes(_zip_bytes("SHARADAR_STOCKS.csv", _STOCKS_CSV))
            db_path = Path(tmp) / "stage.sqlite"

            store_ = BulkStagingStore(db_path)
            staged = store_.stage(zip_path, STOCKS_COLUMNS, tickers=["BBBY"])
            self.assertEqual(staged, 3)  # only BBBY's three rows
            self.assertEqual(store_.distinct_tickers(), ["BBBY"])
            self.assertEqual(store_.rows_for_ticker("AAPL"), [])
            store_.close()

    def test_a_missing_expected_column_is_a_schema_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "stocks.zip"
            zip_path.write_bytes(_zip_bytes(
                "SHARADAR_STOCKS.csv",
                "ticker,date,open,high,low,close,volume\nBBBY,2023-05-02,1,1,1,1,1\n",
            ))
            store_ = BulkStagingStore(Path(tmp) / "stage.sqlite")
            with self.assertRaises(PricePlaneSchemaError) as caught:
                store_.stage(zip_path, STOCKS_COLUMNS)
            self.assertIn("closeunadj", str(caught.exception))
            store_.close()

    def test_a_zip_with_the_wrong_number_of_members_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "empty.zip"
            with zipfile.ZipFile(zip_path, "w"):
                pass
            store_ = BulkStagingStore(Path(tmp) / "stage.sqlite")
            with self.assertRaises(PricePlaneSchemaError):
                store_.stage(zip_path, STOCKS_COLUMNS)
            store_.close()

    def test_restaging_drops_the_previous_table_first(self):
        """Restaging (as a resumed run does after a kill mid-stage) must not
        duplicate rows left over from the interrupted attempt."""
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "stocks.zip"
            zip_path.write_bytes(_zip_bytes("SHARADAR_STOCKS.csv", _STOCKS_CSV))
            db_path = Path(tmp) / "stage.sqlite"

            store_ = BulkStagingStore(db_path)
            store_.stage(zip_path, STOCKS_COLUMNS)
            first_count = len(store_.rows_for_ticker("BBBY"))
            store_.stage(zip_path, STOCKS_COLUMNS)  # simulate a restage
            second_count = len(store_.rows_for_ticker("BBBY"))
            self.assertEqual(first_count, second_count)
            self.assertEqual(second_count, 3)
            store_.close()


class StreamingParseEquivalenceTests(unittest.TestCase):
    """Streaming parse equals the old whole-file parse, bar-for-bar."""

    def test_streaming_load_bulk_bars_matches_the_old_in_memory_parse(self):
        plane = _stub_plane()
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "stocks.zip"
            zip_path.write_bytes(_zip_bytes("SHARADAR_STOCKS.csv", _STOCKS_CSV))

            # The old implementation, verbatim: read the whole zip into a
            # dict, then build every bar with no factors applied. Kept only
            # in this test as the equivalence oracle — `_read_bulk_csv`
            # itself is unchanged and still used for the (small, in-memory)
            # actions parse.
            rows_by_ticker = SharadarPricePlane._read_bulk_csv(zip_path, STOCKS_COLUMNS)
            old = {}
            for ticker, rows in rows_by_ticker.items():
                uid = f"sharadar:bulk:{ticker}"
                bars = [plane._bar_from_row(row, ticker, uid, 1.0, 0.0) for row in rows]
                old[ticker] = with_derived_series(sorted(bars, key=lambda b: b.session_date))

            new = plane.load_bulk_bars(zip_path)

        self.assertEqual(set(old), set(new))
        for ticker in old:
            self.assertEqual(old[ticker], new[ticker])


class FactorApplicationTests(unittest.TestCase):
    """`open_bulk_bar_stream` applies factors; `load_bulk_bars` (no actions
    zip given) does not — a deliberate difference, not a bug (see
    `data/prices/sharadar.py`'s `load_bulk_bars` docstring)."""

    def test_a_dividend_inside_the_window_changes_the_bar_when_factors_are_given(self):
        stocks_csv = (
            "ticker,date,open,high,low,close,volume,closeunadj\n"
            "BBBY,2023-05-01,1.0,1.0,1.0,1.00,1000,1.00\n"
            "BBBY,2023-05-02,1.0,1.0,1.0,1.00,1000,1.00\n"
        )
        actions_csv = "date,action,ticker,value\n2023-05-02,dividend,BBBY,0.10\n"
        plane = _stub_plane(stocks_csv=stocks_csv, actions_csv=actions_csv)

        with tempfile.TemporaryDirectory() as tmp:
            stocks_zip = Path(tmp) / "stocks.zip"
            stocks_zip.write_bytes(_zip_bytes("SHARADAR_STOCKS.csv", stocks_csv))
            actions_zip = Path(tmp) / "actions.zip"
            actions_zip.write_bytes(_zip_bytes("SHARADAR_ACTIONS.csv", actions_csv))
            actions_by_ticker = plane.load_bulk_actions(actions_zip)

            with plane.open_bulk_bar_stream(
                stocks_zip, Path(tmp) / "stage.sqlite", actions_by_ticker=actions_by_ticker,
            ) as stream:
                with_factors = stream.bars_for("BBBY")

            no_factors = plane.load_bulk_bars(stocks_zip)["BBBY"]

        self.assertNotEqual(
            [b.total_return_close for b in with_factors],
            [b.total_return_close for b in no_factors],
        )
        check_reconstruction(with_factors)
        check_reconstruction(no_factors)


class BackfillBulkResumeTests(unittest.TestCase):
    """`backfill_bulk`'s checkpoint: the RSS guard, and `--resume`."""

    def setUp(self):
        self.db = init_test_db(f"bulk_streaming_{self.id().rsplit('.', 1)[-1]}")
        self.addCleanup(self.db.cleanup)

    def test_resume_after_the_rss_guard_aborts_finishes_the_remaining_tickers(self):
        from scripts.price_backfill import backfill_bulk

        plane = _stub_plane()
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_path = Path(tmp) / "checkpoint.json"

            first = backfill_bulk(
                plane, "10", None, since=None, until=None, check=True,
                checkpoint_path=checkpoint_path, max_rss_mb=0,
            )
            self.assertTrue(first["aborted"])
            self.assertIsNotNone(first["aborted_reason"])
            self.assertTrue(checkpoint_path.exists())
            state_after_abort = json.loads(checkpoint_path.read_text())
            self.assertEqual(len(state_after_abort["tickers_done"]), 1)

            second = backfill_bulk(
                plane, None, None, since=None, until=None, check=True,
                checkpoint_path=checkpoint_path, resume=True, max_rss_mb=None,
            )
            self.assertFalse(second["aborted"])

        self.assertEqual(second["tickers_with_bars"], 2)
        self.assertEqual(second["bars_written"], 5)
        self.assertEqual(second["actions_written"], 2)

        with_session = store.load_all_bars
        from database.db import get_session

        with get_session() as session:
            all_bars = with_session(session)
            self.assertEqual(sum(len(b) for b in all_bars.values()), 5)

    def test_resume_after_a_kill_mid_staging_restages_cleanly(self):
        """A kill *before* staging finishes must not leave a `--resume` with
        duplicated or missing rows: staging is simply redone from the zip."""
        from scripts.price_backfill import (
            _new_bulk_checkpoint,
            _save_bulk_checkpoint,
            _staging_db_path_for,
            backfill_bulk,
        )

        plane = _stub_plane()
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_path = Path(tmp) / "checkpoint.json"
            staging_db_path = _staging_db_path_for(checkpoint_path)

            # Simulate a process that staged a prior (different) attempt and
            # then died before flipping `staging_complete`: stage an
            # unrelated "ZZZZ" row at the same path, so this test knows it
            # gets dropped rather than unioned with the real restage.
            leftover_zip = Path(tmp) / "leftover.zip"
            leftover_zip.write_bytes(_zip_bytes(
                "SHARADAR_STOCKS.csv",
                "ticker,date,open,high,low,close,volume,closeunadj\n"
                "ZZZZ,2000-01-01,1,1,1,1,1,1\n",
            ))
            leftover = BulkStagingStore(staging_db_path)
            leftover.stage(leftover_zip, STOCKS_COLUMNS)
            leftover.close()

            state = _new_bulk_checkpoint(
                checkpoint_path=checkpoint_path, staging_db_path=staging_db_path,
                source="sharadar", years="10", tickers=None,
                since=None, until=None, check=True, batch_size=50_000,
            )
            self.assertFalse(state["staging_complete"])
            _save_bulk_checkpoint(state)

            result = backfill_bulk(
                plane, None, None, since=None, until=None, check=True,
                checkpoint_path=checkpoint_path, resume=True,
            )

        self.assertFalse(result["aborted"])
        self.assertEqual(result["tickers_with_bars"], 2)
        self.assertEqual(result["bars_written"], 5)
        self.assertNotIn("ZZZZ", result["tickers_empty"])
        self.assertNotIn("ZZZZ", result["tickers_without_a_security_master_row"])

    def test_resume_with_no_checkpoint_file_refuses(self):
        from scripts.price_backfill import backfill_bulk

        plane = _stub_plane()
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"
            from data.prices.base import PricePlaneError

            with self.assertRaises(PricePlaneError):
                backfill_bulk(
                    plane, None, None, since=None, until=None,
                    checkpoint_path=missing, resume=True,
                )

    def test_default_ephemeral_checkpoint_does_not_collide_across_calls(self):
        """Two calls with no explicit `checkpoint_path` must not share state —
        each is a fresh, independent run (this is what makes it safe for two
        CI shard processes to run the same test concurrently)."""
        from scripts.price_backfill import backfill_bulk

        plane = _stub_plane()
        first = backfill_bulk(plane, "10", None, since=None, until=None, check=True)
        second = backfill_bulk(plane, "10", None, since=None, until=None, check=True)
        self.assertEqual(first["bars_written"], 5)
        self.assertEqual(second["bars_written"], 5)
        self.assertNotEqual(first["checkpoint_path"], second["checkpoint_path"])


def _write_synthetic_stocks_zip(path: Path, num_tickers: int, num_sessions: int) -> None:
    """Write a synthetic bulk `stocks` zip straight to disk, one row at a
    time — the rows are never assembled into a Python list first, matching
    how a real ~5M-row vendor zip must be generated for a memory test to mean
    anything."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        with archive.open("SHARADAR_STOCKS.csv", "w") as raw:
            wrapper = io.TextIOWrapper(raw, encoding="utf-8", newline="")
            writer = csv.writer(wrapper)
            writer.writerow(["ticker", "date", "open", "high", "low", "close", "volume", "closeunadj"])
            base = date(2015, 1, 2)
            for t in range(num_tickers):
                ticker = f"SYN{t:05d}"
                for s in range(num_sessions):
                    day = date.fromordinal(base.toordinal() + s)
                    price = 10.0 + (s % 50) * 0.1
                    writer.writerow([ticker, day.isoformat(), price, price + 0.5,
                                      price - 0.5, price, 100000, price])
            wrapper.flush()


class BulkMemoryBoundTests(unittest.TestCase):
    """The bound the brief asks for directly: streaming stays bounded, the
    old dict-accumulation does not, as ticker count grows."""

    def test_streaming_peak_allocation_is_bounded(self):
        """Default-suite, small-file version: a `tracemalloc` peak bound on
        the streaming path alone (no old-path comparison — that needs a much
        bigger file to show a difference, and lives behind `SLOW_TESTS=1`
        below)."""
        num_tickers, num_sessions = 100, 200  # 20,000 rows

        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "stocks.zip"
            _write_synthetic_stocks_zip(zip_path, num_tickers, num_sessions)
            db_path = Path(tmp) / "stage.sqlite"
            plane = SharadarPricePlane(api_key="k", client=_StubClient())

            tracemalloc.start()
            try:
                with plane.open_bulk_bar_stream(zip_path, db_path, batch_size=5_000) as stream:
                    seen = 0
                    for ticker in stream.tickers():
                        bars = stream.bars_for(ticker)
                        self.assertEqual(len(bars), num_sessions)
                        stream.drop(ticker)
                        seen += 1
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

        self.assertEqual(seen, num_tickers)
        # Holding all 20,000 rows (plus the `DailyBar`s built from them) at
        # once would be several MB of Python objects; one ticker's worth
        # (200 rows) plus a 5,000-row staging batch is well under 1 MB of
        # *traced* allocation. 8 MB leaves generous headroom against
        # allocator/interpreter noise while still failing loudly if this
        # regresses back to whole-file accumulation.
        self.assertLess(peak, 8 * 1024 * 1024, f"peak traced allocation was {peak} bytes")

    @unittest.skipUnless(os.environ.get("SLOW_TESTS") == "1", "generates a large synthetic zip; SLOW_TESTS=1 to run")
    def test_streaming_rss_beats_the_old_whole_zip_parse(self):
        """A real-RSS comparison on a much bigger zip, each path in its own
        subprocess so one path's allocator high-water mark cannot bleed into
        the other's measurement (`ru_maxrss` never decreases within a
        process). Scaled down from the brief's suggested 2,000 tickers x
        2,500 sessions (~5M rows) to keep this opt-in test's runtime
        reasonable; the mechanism being demonstrated does not depend on the
        exact size."""
        num_tickers, num_sessions = 1_000, 1_000  # 1,000,000 rows

        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "stocks.zip"
            _write_synthetic_stocks_zip(zip_path, num_tickers, num_sessions)

            old_rss = self._run_subprocess(f"""
import sys
sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
from data.prices.sharadar import SharadarPricePlane, STOCKS_COLUMNS
from utils.memory import max_rss_mb
rows_by_ticker = SharadarPricePlane._read_bulk_csv({str(zip_path)!r}, STOCKS_COLUMNS)
assert len(rows_by_ticker) == {num_tickers}
print(max_rss_mb())
""")
            new_rss = self._run_subprocess(f"""
import sys
sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
import tempfile
from pathlib import Path
from data.prices.sharadar import SharadarPricePlane
from utils.memory import max_rss_mb
plane = SharadarPricePlane(api_key="k", client=object())
with tempfile.TemporaryDirectory() as tmp:
    db_path = Path(tmp) / "stage.sqlite"
    with plane.open_bulk_bar_stream({str(zip_path)!r}, db_path) as stream:
        n = 0
        for ticker in stream.tickers():
            stream.bars_for(ticker)
            stream.drop(ticker)
            n += 1
assert n == {num_tickers}
print(max_rss_mb())
""")

        self.assertLess(
            new_rss, old_rss * 0.5,
            f"streaming RSS {new_rss:.0f}MB was not well under the old path's {old_rss:.0f}MB",
        )

    @staticmethod
    def _run_subprocess(script: str) -> float:
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise AssertionError(f"subprocess failed:\n{result.stdout}\n{result.stderr}")
        return float(result.stdout.strip().splitlines()[-1])


if __name__ == "__main__":
    unittest.main()
