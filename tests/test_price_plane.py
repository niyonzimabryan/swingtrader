"""Phase 3p: the price plane, offline.

Everything here runs against `tests/fixtures/prices/` — committed, synthetic,
and no network. The tests that touch a database use `tests/dbfixture.py`, so
they run on whichever engine the matrix entry selects.

The named tests from the phase's stop condition:

  * `test_three_price_series_stored`
  * `test_series_reconstruct_from_factors`
  * `test_universe_membership_is_point_in_time`
  * `test_delisted_security_retained_with_reason`
  * `test_delisting_audit_classifies_collapse_vs_stop`
  * `test_snapshot_records_audit`
  * `test_adapter_schema_change_fails_loudly`
  * `test_liquid_universe_rule_reproducible`
  * `test_no_network_in_tests`
"""

from __future__ import annotations

import io
import socket
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from data.prices import store, universes
from data.prices.audit import run_audit
from data.prices.base import (
    DailyBar,
    MembershipInterval,
    PricePlaneConfigError,
    PricePlaneSchemaError,
    SecurityMasterRow,
    members_as_of,
)
from data.prices.derived import (
    check_reconstruction,
    deciles,
    liquidity_deciles,
    median_dollar_volume,
    price_level_bucket,
    raw_closes_from_split_adjusted,
    raw_closes_from_total_return,
    realized_volatility,
    realized_vol_deciles,
    split_adjusted_closes,
    total_return_closes,
)
from data.prices.fixture_plane import FixturePricePlane, session_close_utc
from data.prices.sharadar import PricePlaneAuthError, SharadarPricePlane
from tests.dbfixture import init_test_db


def plane() -> FixturePricePlane:
    return FixturePricePlane()


class ThreeSeriesTests(unittest.TestCase):
    """Spec N §4.3: three series per name, plus the factors between them."""

    def setUp(self):
        self.plane = plane()

    def test_three_price_series_stored(self):
        db = init_test_db("price_series")
        self.addCleanup(db.cleanup)

        bars = self.plane.daily_bars("ALPH")
        from database.db import get_session

        with get_session() as session:
            store.upsert_bars(session, bars)

        with get_session() as session:
            stored = store.load_bars(session, bars[0].security_uid)

        self.assertEqual(len(stored), len(bars))

        # All three series are present and they are genuinely different series:
        # a fixture where they coincided would prove nothing.
        split_dates = [b.session_date for b in stored if b.split_factor != 1.0]
        dividend_dates = [b.session_date for b in stored if b.dividend_cash != 0.0]
        self.assertTrue(split_dates, "the ALPH fixture must contain a split")
        self.assertTrue(dividend_dates, "the ALPH fixture must contain a dividend")

        first = stored[0]
        self.assertNotAlmostEqual(first.raw_close, first.split_adjusted_close)
        self.assertNotAlmostEqual(first.split_adjusted_close, first.total_return_close)

        # The split is invisible in the adjusted series and visible in the raw one.
        index = next(i for i, b in enumerate(stored) if b.split_factor != 1.0)
        before, after = stored[index - 1], stored[index]
        self.assertLess(after.raw_close / before.raw_close, 0.7)
        self.assertGreater(after.split_adjusted_close / before.split_adjusted_close, 0.9)

        # The factors are stored beside the bar, with their ex-dates.
        with get_session() as session:
            store.upsert_corporate_actions(session, self.plane.corporate_actions("ALPH"))
        self.assertEqual(
            [a.ex_date for a in self.plane.corporate_actions("ALPH") if a.action_type == "split"],
            split_dates,
        )

    def test_series_reconstruct_from_factors(self):
        """Any one series from the other two plus the factors, to 1e-9."""
        for ticker in self.plane.tickers():
            with self.subTest(ticker=ticker):
                bars = self.plane.daily_bars(ticker)
                check_reconstruction(bars, rtol=1e-9)

                stored_adjusted = [b.split_adjusted_close for b in bars]
                stored_total = [b.total_return_close for b in bars]
                stored_raw = [b.raw_close for b in bars]

                for want, got in zip(stored_adjusted, split_adjusted_closes(bars)):
                    self.assertAlmostEqual(want, got, delta=abs(want) * 1e-9 + 1e-9)
                for want, got in zip(stored_total, total_return_closes(bars)):
                    self.assertAlmostEqual(want, got, delta=abs(want) * 1e-9 + 1e-9)
                for want, got in zip(stored_raw, raw_closes_from_split_adjusted(stored_adjusted, bars)):
                    self.assertAlmostEqual(want, got, delta=abs(want) * 1e-9 + 1e-9)
                for want, got in zip(stored_raw, raw_closes_from_total_return(stored_total, bars)):
                    self.assertAlmostEqual(want, got, delta=abs(want) * 1e-9 + 1e-9)

    def test_reconstruction_failure_is_loud(self):
        """A doctored series must not pass silently."""
        from dataclasses import replace
        from data.prices.derived import SeriesError

        bars = list(self.plane.daily_bars("BETA"))
        bars[10] = replace(bars[10], total_return_close=bars[10].total_return_close * 1.05)
        with self.assertRaises(SeriesError):
            check_reconstruction(tuple(bars))

    def test_total_return_exceeds_price_return_when_dividends_are_paid(self):
        """The yield gap is the whole reason the third series exists."""
        bars = self.plane.daily_bars("GAMM")
        price_return = bars[-1].split_adjusted_close / bars[0].split_adjusted_close - 1
        total_return = bars[-1].total_return_close / bars[0].total_return_close - 1
        self.assertGreater(total_return, price_return)


class CovariateTests(unittest.TestCase):
    """Spec N §4.5 covariates, computable from stored bars alone."""

    def setUp(self):
        self.plane = plane()

    def test_median_dollar_volume_uses_raw_units(self):
        bars = self.plane.daily_bars("ALPH")
        as_of = bars[-1].session_date
        value = median_dollar_volume(bars, as_of, 20)
        window = bars[-20:]
        expected = sorted(b.raw_close * b.volume for b in window)
        self.assertAlmostEqual(value, (expected[9] + expected[10]) / 2)

    def test_realized_volatility_is_annualised_and_positive(self):
        bars = self.plane.daily_bars("GAMM")
        vol = realized_volatility(bars, bars[-1].session_date, 20)
        raw = realized_volatility(bars, bars[-1].session_date, 20, annualise=False)
        self.assertGreater(vol, 0)
        self.assertAlmostEqual(vol / raw, 252 ** 0.5)

    def test_deciles_are_a_pure_function_of_the_input(self):
        values = {f"uid-{i}": float(i % 7) for i in range(40)}
        self.assertEqual(deciles(values), deciles(dict(reversed(list(values.items())))))
        self.assertEqual(min(deciles(values).values()), 1)
        self.assertEqual(max(deciles(values).values()), 10)

    def test_deciles_as_of_a_date_skip_names_without_a_full_window(self):
        bars_by_uid = {
            b[0].security_uid: b
            for b in (self.plane.daily_bars(t) for t in self.plane.tickers())
        }
        early = self.plane.daily_bars("ALPH")[5].session_date
        self.assertEqual(liquidity_deciles(bars_by_uid, early, 20), {})

        late = self.plane.daily_bars("ALPH")[-1].session_date
        liquidity = liquidity_deciles(bars_by_uid, late, 20)
        self.assertIn("fx-0001", liquidity)
        self.assertNotIn("fx-0007", liquidity, "RAD's history ends before this date")
        self.assertTrue(all(1 <= d <= 10 for d in liquidity.values()))
        self.assertTrue(realized_vol_deciles(bars_by_uid, late, 20))

    def test_price_level_buckets_are_monotone(self):
        levels = [0.5, 3.0, 7.0, 15.0, 30.0, 75.0, 150.0, 900.0]
        buckets = [price_level_bucket(x) for x in levels]
        self.assertEqual(buckets, sorted(buckets))
        self.assertEqual(len(set(buckets)), len(buckets))


class SecurityMasterTests(unittest.TestCase):
    def setUp(self):
        self.plane = plane()

    def test_delisted_security_retained_with_reason(self):
        db = init_test_db("delisted")
        self.addCleanup(db.cleanup)
        from database.db import get_session

        with get_session() as session:
            store.upsert_securities(session, self.plane.security_master())
            rows = {
                row.ticker: {
                    "security_uid": row.security_uid,
                    "delisting_date": row.delisting_date,
                    "delisting_reason": row.delisting_reason,
                    "venue": row.venue,
                }
                for row in store.load_securities(session)
            }

        # The delisted names are still in the master, with a date and a reason.
        for ticker in ("BBBY", "RAD"):
            with self.subTest(ticker=ticker):
                row = rows[ticker]
                self.assertIsNotNone(row["delisting_date"])
                self.assertEqual(row["delisting_reason"], "performance")
                self.assertIn(row["venue"], ("nyse_amex", "nasdaq"))

        # And their bars are still there — dropping them is the survivorship bug.
        with get_session() as session:
            store.upsert_bars(session, self.plane.daily_bars("BBBY"))
        with get_session() as session:
            self.assertTrue(store.load_bars(session, rows["BBBY"]["security_uid"]))

        self.assertIsNone(rows["ALPH"]["delisting_date"])
        self.assertEqual(rows["ALPH"]["delisting_reason"], "unknown")

    def test_an_unknown_reason_category_is_rejected(self):
        with self.assertRaises(PricePlaneSchemaError):
            SecurityMasterRow(
                security_uid="x", ticker="X", source="fixture",
                delisting_reason="went_away",
            )


class MembershipTests(unittest.TestCase):
    def setUp(self):
        self.plane = plane()

    def test_universe_membership_is_point_in_time(self):
        """A name that joined after `d` is not a member as of `d`."""
        intervals = self.plane.index_membership("sp500_fixture_v1")
        joined = next(i for i in intervals if i.ticker == "GAMM")
        before = self.plane.daily_bars("GAMM")[0].session_date

        as_of_before = {i.ticker for i in members_as_of(intervals, before)}
        self.assertNotIn("GAMM", as_of_before)

        as_of_join = {i.ticker for i in members_as_of(intervals, joined.member_from)}
        self.assertIn("GAMM", as_of_join)

        # And a name that left is not a member after it left.
        left = next(i for i in intervals if i.ticker == "BETA")
        self.assertIn("BETA", {i.ticker for i in members_as_of(intervals, left.member_from)})
        self.assertNotIn("BETA", {i.ticker for i in members_as_of(intervals, left.member_to)})

    def test_stored_membership_is_point_in_time_too(self):
        """The same rule, through the database, on whichever engine runs."""
        db = init_test_db("membership")
        self.addCleanup(db.cleanup)
        from database.db import get_session

        intervals = self.plane.index_membership("sp500_fixture_v1")
        with get_session() as session:
            store.replace_universe(session, "sp500_fixture_v1", intervals)

        joined = next(i for i in intervals if i.ticker == "GAMM")
        with get_session() as session:
            before = {
                row.ticker for row in store.members_as_of(
                    session, "sp500_fixture_v1", joined.member_from - _one_day()
                )
            }
            on_join = {
                row.ticker
                for row in store.members_as_of(session, "sp500_fixture_v1", joined.member_from)
            }
        self.assertNotIn("GAMM", before)
        self.assertIn("GAMM", on_join)

    def test_replacing_a_universe_leaves_other_slugs_alone(self):
        db = init_test_db("slugs")
        self.addCleanup(db.cleanup)
        from database.db import get_session

        other = MembershipInterval(
            universe_slug="other_v1", security_uid="fx-0001", ticker="ALPH",
            member_from=date(2023, 1, 3), member_to=None, source="test",
            known_at_utc=datetime(2023, 1, 3, 21, tzinfo=timezone.utc),
        )
        with get_session() as session:
            store.replace_universe(session, "other_v1", [other])
            store.replace_universe(
                session, "sp500_fixture_v1", self.plane.index_membership("sp500_fixture_v1")
            )
        with get_session() as session:
            self.assertEqual(
                len(store.members_as_of(session, "other_v1", date(2023, 6, 1))), 1
            )

    def test_an_unknown_universe_raises_rather_than_returning_empty(self):
        with self.assertRaises(PricePlaneConfigError):
            self.plane.index_membership("russell_2000_v1")


class Sp500HistoryTests(unittest.TestCase):
    """The free MIT membership source, from the committed CSV."""

    def test_committed_csv_parses_into_intervals(self):
        from data.prices import sp500_history

        intervals = sp500_history.membership_intervals()
        self.assertGreater(len(intervals), 1000)
        self.assertTrue(all(i.universe_slug == "sp500_wikipedia_v1" for i in intervals))
        self.assertTrue(all(i.source.startswith("fja05680/sp500@") for i in intervals))

        # An index of roughly the right size at a date well inside the history.
        members = members_as_of(intervals, date(2015, 6, 30))
        self.assertGreater(len(members), 450)
        self.assertLess(len(members), 560)

    def test_membership_intervals_are_point_in_time(self):
        from data.prices import sp500_history

        intervals = sp500_history.membership_intervals()
        # Tesla joined the S&P 500 in December 2020; it must not be a member in 2015.
        early = {i.ticker for i in members_as_of(intervals, date(2015, 6, 30))}
        later = {i.ticker for i in members_as_of(intervals, date(2024, 6, 28))}
        self.assertNotIn("TSLA", early)
        self.assertIn("TSLA", later)

    def test_a_bad_header_is_rejected(self):
        import tempfile
        from pathlib import Path

        from data.prices import sp500_history

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.csv"
            bad.write_text("effective,members\n2020-01-02,\"AAPL\"\n", encoding="utf-8")
            with self.assertRaises(PricePlaneSchemaError):
                sp500_history.read_snapshots(bad)


class LiquidUniverseTests(unittest.TestCase):
    def setUp(self):
        self.plane = plane()
        self.bars_by_uid = {
            self.plane.daily_bars(t)[0].security_uid: self.plane.daily_bars(t)
            for t in self.plane.tickers()
        }
        self.sessions = sorted({
            b.session_date for bars in self.bars_by_uid.values() for b in bars
        })

    def test_liquid_universe_rule_reproducible(self):
        """Same bars in, identical membership out — twice, and in any order."""
        first = universes.compute_membership(self.bars_by_uid, self.sessions, top_n=3, window=20)
        shuffled = dict(reversed(list(self.bars_by_uid.items())))
        second = universes.compute_membership(shuffled, self.sessions, top_n=3, window=20)
        self.assertEqual(first, second)
        self.assertTrue(first)

    def test_membership_is_top_n_by_median_dollar_volume(self):
        month_end = universes.month_end_sessions(self.sessions)[-1]
        ranked = universes.rank_as_of(self.bars_by_uid, month_end, top_n=3, window=20)
        self.assertEqual(len(ranked), 3)
        self.assertEqual(
            [uid for uid, _ in ranked],
            sorted(
                [uid for uid, _ in ranked],
                key=lambda u: -median_dollar_volume(self.bars_by_uid[u], month_end, 20),
            ),
        )
        # GAMM is the deepest name in the fixture; it must be in the top three.
        self.assertIn("fx-0003", [uid for uid, _ in ranked])

    def test_known_at_utc_is_the_month_end_close(self):
        intervals = universes.compute_membership(
            self.bars_by_uid, self.sessions, top_n=3, window=20
        )
        for interval in intervals:
            self.assertEqual(interval.known_at_utc, session_close_utc(interval.member_from))

    def test_a_name_is_not_a_member_before_it_has_a_full_window(self):
        too_early = self.sessions[10]
        self.assertEqual(
            universes.rank_as_of(self.bars_by_uid, too_early, top_n=10, window=20),
            (),
            "no name has 20 sessions of history yet, so none can be ranked",
        )

    def test_a_delisted_name_drops_out_rather_than_carrying_a_stale_rank(self):
        """RAD's history ends first; it must not stay ranked after that."""
        rad_last = self.bars_by_uid["fx-0007"][-1].session_date
        later = [d for d in self.sessions if d > rad_last][20]
        self.assertIn(
            "fx-0007", [uid for uid, _ in universes.rank_as_of(
                self.bars_by_uid, rad_last, top_n=10, window=20)],
        )
        self.assertNotIn(
            "fx-0007", [uid for uid, _ in universes.rank_as_of(
                self.bars_by_uid, later, top_n=10, window=20)],
        )

    def test_rebuild_reads_only_stored_bars(self):
        db = init_test_db("liquid")
        self.addCleanup(db.cleanup)
        from database.db import get_session

        with get_session() as session:
            for bars in self.bars_by_uid.values():
                store.upsert_bars(session, bars)

        with get_session() as session:
            written = universes.rebuild(session, top_n=3, window=20)
        self.assertGreater(written, 0)

        with get_session() as session:
            rerun = universes.rebuild(session, top_n=3, window=20)
            members = store.members_as_of(
                session, universes.UNIVERSE_SLUG, self.sessions[-1]
            )
        self.assertEqual(written, rerun, "rebuild must be idempotent")
        self.assertEqual(len(members), 3)


class DelistingAuditTests(unittest.TestCase):
    def setUp(self):
        self.plane = plane()

    def test_delisting_audit_classifies_collapse_vs_stop(self):
        result = run_audit(self.plane)
        by_ticker = {case["ticker"]: case for case in result["cases"]}

        self.assertEqual(by_ticker["BBBY"]["classification"], "collapse")
        self.assertLess(by_ticker["BBBY"]["terminal_return"], -0.6)

        self.assertEqual(by_ticker["RAD"]["classification"], "stop")
        self.assertGreater(by_ticker["RAD"]["terminal_return"], -0.6)

        self.assertEqual(result["counts"]["collapse"], 1)
        self.assertEqual(result["counts"]["stop"], 1)
        self.assertEqual(result["n_cases"], 20)
        self.assertEqual(result["counts"]["missing"], 18)

    def test_the_audit_list_is_twenty_sourced_performance_delistings(self):
        from data.prices.delisting_audit_list import DELISTING_AUDIT_LIST

        self.assertEqual(len(DELISTING_AUDIT_LIST), 20)
        self.assertEqual(len({c.ticker for c in DELISTING_AUDIT_LIST}), 20)
        for case in DELISTING_AUDIT_LIST:
            with self.subTest(ticker=case.ticker):
                self.assertIn(case.venue, ("nyse_amex", "nasdaq"))
                self.assertIn(case.source_kind, ("sec_form_25", "exchange_notice"))
                self.assertTrue(case.source_url.startswith("https://www.sec.gov/"))
                self.assertTrue(case.event.strip())
                self.assertEqual(2015 <= case.delisting_date.year <= 2024, True)

    def test_the_threshold_moves_the_classification(self):
        lenient = run_audit(self.plane, collapse_threshold=-0.01)
        by_ticker = {case["ticker"]: case for case in lenient["cases"]}
        self.assertEqual(by_ticker["RAD"]["classification"], "collapse")

    def test_snapshot_records_audit(self):
        db = init_test_db("snapshot")
        self.addCleanup(db.cleanup)
        from database.db import get_session

        result = run_audit(self.plane)
        with get_session() as session:
            store.record_snapshot(
                session, "fixture-2026-09", self.plane.source,
                {"tickers": len(self.plane.tickers())}, result,
            )

        with get_session() as session:
            snapshot = store.get_snapshot(session, "fixture-2026-09")
            self.assertIsNotNone(snapshot)
            audit = snapshot.delisting_audit
            coverage = snapshot.coverage_summary

        self.assertEqual(audit["counts"]["collapse"], 1)
        self.assertEqual(audit["counts"]["stop"], 1)
        self.assertEqual(audit["spec"], "N-4.2-delisting-returns")
        self.assertEqual(coverage["tickers"], 7)
        self.assertFalse(audit["sources_verified_against_primary_filing"])

    def test_recording_coverage_again_does_not_wipe_the_audit(self):
        db = init_test_db("snapshot_keep")
        self.addCleanup(db.cleanup)
        from database.db import get_session

        with get_session() as session:
            store.record_snapshot(session, "s", "fixture", {"a": 1}, run_audit(self.plane))
            store.record_snapshot(session, "s", "fixture", {"a": 2})
        with get_session() as session:
            snapshot = store.get_snapshot(session, "s")
            self.assertEqual(snapshot.coverage_summary["a"], 2)
            self.assertEqual(snapshot.delisting_audit["counts"]["collapse"], 1)


class _StubResponse:
    """A direct-API response stand-in: `{"count": N, "data": [...]}` or an error body."""

    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class _StubStream:
    """A `client.stream(...)` context-manager stand-in, for `bulk_download`."""

    def __init__(self, content=b"", status_code=200, error_payload=None):
        self.status_code = status_code
        self._content = content
        self._error_payload = error_payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def json(self):
        if self._error_payload is None:
            raise ValueError("not JSON")
        return self._error_payload

    def iter_bytes(self):
        yield self._content


class _StubClient:
    """An `httpx.Client` stand-in. Records calls; opens no socket.

    `tables` maps a table name to either a flat list of row dicts (paged by
    `limit`/`offset` exactly like the real API) or a `_StubResponse` to force
    a particular status/error body. `streams` maps a table name to a
    `_StubStream`, for the bulk path.
    """

    def __init__(self, tables=None, streams=None):
        self.tables = tables or {}
        self.streams = streams or {}
        self.calls = []
        self.stream_calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        table = url.rsplit("/", 1)[-1]
        params = dict(params or {})
        self.calls.append((table, params, dict(headers or {})))
        entry = self.tables[table]
        if isinstance(entry, _StubResponse):
            return entry
        limit = params.get("limit")
        offset = params.get("offset", 0) or 0
        page = entry[offset:offset + limit] if limit is not None else entry
        return _StubResponse({"count": len(page), "data": page})

    def stream(self, method, url, params=None, headers=None, timeout=None, follow_redirects=None):
        table = url.rsplit("/", 1)[-1]
        self.stream_calls.append((table, dict(params or {}), dict(headers or {}), follow_redirects))
        return self.streams[table]


TICKERS_COLUMNS = (
    "permaticker", "ticker", "name", "exchange", "isdelisted",
    "firstpricedate", "lastpricedate",
)
STOCKS_COLUMNS = ("ticker", "date", "open", "high", "low", "close", "volume", "closeunadj")
ACTIONS_COLUMNS = ("date", "action", "ticker", "value")


def _sharadar_tables(**overrides):
    tables = {
        "tickers": [
            {
                "table": "stocks", "permaticker": "199059", "ticker": "BBBY",
                "name": "Bed Bath & Beyond Inc", "exchange": "NASDAQ", "isdelisted": "Y",
                "firstpricedate": "1992-07-28", "lastpricedate": "2023-05-02",
            },
        ],
        "actions": [
            {"date": "2023-04-24", "action": "bankruptcy", "ticker": "BBBY", "value": None},
            {"date": "2022-01-10", "action": "dividend", "ticker": "BBBY", "value": 0.10},
        ],
        "stocks": [
            {"ticker": "BBBY", "date": "2023-04-28", "open": 1.0, "high": 1.1, "low": 0.9,
             "close": 1.00, "volume": 1_000_000, "closeunadj": 1.00},
            {"ticker": "BBBY", "date": "2023-05-01", "open": 0.8, "high": 0.9, "low": 0.7,
             "close": 0.80, "volume": 2_000_000, "closeunadj": 0.80},
            {"ticker": "BBBY", "date": "2023-05-02", "open": 0.3, "high": 0.4, "low": 0.2,
             "close": 0.30, "volume": 3_000_000, "closeunadj": 0.30},
        ],
    }
    tables.update(overrides)
    return tables


class SharadarAdapterTests(unittest.TestCase):
    """The vendor adapter, exercised entirely against stub payloads recorded
    live in `tests/fixtures/sharadar_direct/` (see that directory's README)."""

    def test_parses_a_well_formed_payload(self):
        client = _StubClient(_sharadar_tables())
        plane = SharadarPricePlane(api_key="test-key", client=client)

        master = plane.security_master(["BBBY"])
        self.assertEqual(master[0].security_uid, "sharadar:199059")
        self.assertEqual(master[0].venue, "nasdaq")
        self.assertEqual(master[0].delisting_reason, "performance")
        self.assertEqual(master[0].delisting_date, date(2023, 5, 2))

        bars = plane.daily_bars("BBBY")
        self.assertEqual(len(bars), 3)
        check_reconstruction(bars)
        self.assertEqual(bars[-1].raw_close, 0.30)

    def test_the_key_travels_as_a_header_never_in_the_url(self):
        """Changed from the pre-port adapter: the direct API also accepts the
        key as an `api_key` query param, but a header keeps it out of logs,
        proxies and the bulk endpoint's redirect `Referer`."""
        client = _StubClient(_sharadar_tables())
        plane = SharadarPricePlane(api_key="test-key", client=client)
        plane.security_master(["BBBY"])
        table, params, headers = client.calls[0]
        self.assertEqual(table, "tickers")
        self.assertNotIn("api_key", params)
        self.assertNotIn("apiKey", params)
        self.assertEqual(headers["x-api-key"], "test-key")

    def test_security_master_filters_to_the_stocks_plan(self):
        """`tickers` returns one row per plan a ticker appears in unless
        filtered — confirmed live (README.md, `tickers_aapl_all_tables.json`).
        The adapter must send `table=stocks`."""
        client = _StubClient(_sharadar_tables())
        SharadarPricePlane(api_key="k", client=client).security_master(["BBBY"])
        _, params, _ = client.calls[0]
        self.assertEqual(params["table"], "stocks")

    def test_a_missing_key_refuses_rather_than_calling_anonymously(self):
        import os

        saved = {}
        for name in ("SHARADAR_API_KEY", "NASDAQ_DATA_LINK_API_KEY"):
            saved[name] = os.environ.pop(name, None)

        def restore():
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value

        self.addCleanup(restore)
        with self.assertRaises(PricePlaneConfigError):
            SharadarPricePlane()

    def test_adapter_schema_change_fails_loudly(self):
        """A renamed column is an error, not a silent null."""
        renamed = _sharadar_tables(
            stocks=[
                {"ticker": "BBBY", "date": "2023-05-02", "open": 0.3, "high": 0.4, "low": 0.2,
                 "close": 0.30, "volume": 3_000_000, "close_unadjusted": 0.30},
            ],
        )
        plane = SharadarPricePlane(api_key="k", client=_StubClient(renamed))
        with self.assertRaises(PricePlaneSchemaError) as caught:
            plane.daily_bars("BBBY")
        self.assertIn("closeunadj", str(caught.exception))

        # And in the security master.
        renamed_master = _sharadar_tables(
            tickers=[
                {"table": "stocks", "permaticker": "1", "ticker": "BBBY", "name": "x",
                 "exchange": "NASDAQ", "delisted": "Y",
                 "firstpricedate": "1992-07-28", "lastpricedate": "2023-05-02"},
            ],
        )
        with self.assertRaises(PricePlaneSchemaError):
            SharadarPricePlane(api_key="k", client=_StubClient(renamed_master)).security_master()

    def test_a_null_in_a_required_field_is_an_error(self):
        nulled = _sharadar_tables(
            stocks=[
                {"ticker": "BBBY", "date": "2023-05-02", "open": 0.3, "high": 0.4, "low": 0.2,
                 "close": 0.30, "volume": 3_000_000, "closeunadj": None},
            ],
        )
        plane = SharadarPricePlane(api_key="k", client=_StubClient(nulled))
        with self.assertRaises(PricePlaneSchemaError) as caught:
            plane.daily_bars("BBBY")
        self.assertIn("never stores a null", str(caught.exception))

    def test_a_401_is_a_rejected_key_error(self):
        """Observed live only on the bulk path, but any `/data` call could 401
        per the vendor docs; mapped the same way either way."""
        plane = SharadarPricePlane(
            api_key="k",
            client=_StubClient(_sharadar_tables(
                tickers=_StubResponse(
                    {"error": "Unauthorized", "description": "A valid API key is required."},
                    status_code=401,
                )
            )),
        )
        with self.assertRaises(PricePlaneAuthError) as caught:
            plane.security_master()
        self.assertIn("rejected the API key", str(caught.exception))

    def test_a_403_free_tier_error_names_the_plan_not_the_key(self):
        """Observed live: `GET /data/stocks?ticker=XOM` with `test-api-key`
        (`tests/fixtures/sharadar_direct/error_403_exceeds_free_tier.json`)."""
        plane = SharadarPricePlane(
            api_key="test-api-key",
            client=_StubClient(_sharadar_tables(
                stocks=_StubResponse(
                    {"error": "Exceeds free tier", "description": "Please sign up at /subscribe."},
                    status_code=403,
                )
            )),
        )
        with self.assertRaises(PricePlaneAuthError) as caught:
            plane.daily_bars("XOM")
        self.assertIn("free-tier", str(caught.exception))

    def test_an_unrelated_non_200_is_a_schema_error(self):
        """Observed live: `GET /data/notatable` -> 403 `Forbidden: Unknown
        table` (`error_403_unknown_table.json`) — not a free-tier message, so
        it means "this adapter is calling the API wrong", not "upgrade"."""
        plane = SharadarPricePlane(
            api_key="k",
            client=_StubClient(_sharadar_tables(
                tickers=_StubResponse({"error": "Forbidden", "description": "Unknown table."}, status_code=403)
            )),
        )
        with self.assertRaises(PricePlaneSchemaError) as caught:
            plane.security_master()
        self.assertIn("403", str(caught.exception))

    def test_a_payload_without_a_data_list_is_an_error(self):
        plane = SharadarPricePlane(
            api_key="k", client=_StubClient(_sharadar_tables(tickers=_StubResponse({"unexpected": "x"})))
        )
        with self.assertRaises(PricePlaneSchemaError):
            plane.security_master()

    def test_a_row_missing_an_expected_key_is_an_error(self):
        plane = SharadarPricePlane(
            api_key="k",
            client=_StubClient(_sharadar_tables(
                tickers=[{"permaticker": "1", "ticker": "BBBY"}],
            )),
        )
        with self.assertRaises(PricePlaneSchemaError):
            plane.security_master()

    def test_an_extra_vendor_column_is_tolerated(self):
        """A vendor adding a field must not stop ingest; a rename must."""
        widened = _sharadar_tables(
            tickers=[
                {
                    "table": "stocks", "permaticker": "1", "ticker": "BBBY", "name": "x",
                    "exchange": "NASDAQ", "isdelisted": "N", "firstpricedate": "1992-07-28",
                    "lastpricedate": "2023-05-02", "newfield": "whatever",
                },
            ],
        )
        plane = SharadarPricePlane(api_key="k", client=_StubClient(widened))
        self.assertEqual(plane.security_master()[0].ticker, "BBBY")

    def test_pagination_stops_on_a_short_page(self):
        """No cursor or total-count field exists in the envelope (confirmed
        live — see README.md); a page shorter than the requested `limit` is
        the only stop signal, and a full page keeps going."""
        rows = [
            {"ticker": "BBBY", "date": f"2023-05-{i:02d}", "open": 1.0, "high": 1.0, "low": 1.0,
             "close": 1.0, "volume": 1.0, "closeunadj": 1.0}
            for i in range(1, 6)
        ]
        client = _StubClient(_sharadar_tables(stocks=rows))
        plane = SharadarPricePlane(api_key="k", client=client, page_size=2)
        bars = plane.daily_bars("BBBY")
        self.assertEqual(len(bars), 5)
        self.assertEqual([b.session_date.day for b in bars], [1, 2, 3, 4, 5])
        stocks_calls = [c for c in client.calls if c[0] == "stocks"]
        self.assertEqual([c[1]["offset"] for c in stocks_calls], [0, 2, 4])
        self.assertEqual([c[1]["limit"] for c in stocks_calls], [2, 2, 2])

    def test_pagination_gives_up_after_max_pages(self):
        """A server that always returns a full page (or a stub that never
        shrinks) must not loop forever."""
        import data.prices.sharadar as sharadar_module

        class _InfiniteClient:
            def get(self, url, params=None, headers=None, timeout=None):
                return _StubResponse({"count": 2, "data": [
                    {"ticker": "BBBY", "date": "2023-05-01", "open": 1.0, "high": 1.0,
                     "low": 1.0, "close": 1.0, "volume": 1.0, "closeunadj": 1.0},
                    {"ticker": "BBBY", "date": "2023-05-02", "open": 1.0, "high": 1.0,
                     "low": 1.0, "close": 1.0, "volume": 1.0, "closeunadj": 1.0},
                ]})

        saved = sharadar_module.MAX_PAGES
        sharadar_module.MAX_PAGES = 3
        self.addCleanup(setattr, sharadar_module, "MAX_PAGES", saved)
        plane = SharadarPricePlane(api_key="k", client=_InfiniteClient(), page_size=2)
        with self.assertRaises(PricePlaneSchemaError) as caught:
            list(plane._rows("stocks", {}, STOCKS_COLUMNS))
        self.assertIn("did not terminate", str(caught.exception))


class SharadarBulkDownloadTests(unittest.TestCase):
    """`bulk_download` and the two `load_bulk_*` parsers.

    The 302-to-zip redirect itself could not be exercised live: the free
    sample key 401s on the bulk path outright
    (`tests/fixtures/sharadar_direct/error_401_bulk_no_key.json`), so these
    run entirely against a stubbed transport and a synthetic zip.
    """

    @staticmethod
    def _zip_bytes(name: str, csv_text: str) -> bytes:
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(name, csv_text)
        return buffer.getvalue()

    def test_bulk_download_streams_the_zip_to_disk(self):
        content = self._zip_bytes("stocks.csv", "ticker,date\nBBBY,2023-05-02\n")
        client = _StubClient(streams={"stocks": _StubStream(content=content)})
        plane = SharadarPricePlane(api_key="test-key", client=client)

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "bulk" / "stocks.zip"
            result = plane.bulk_download("stocks", 10, dest)
            self.assertEqual(result, dest)
            self.assertEqual(dest.read_bytes(), content)

        table, params, headers, follow_redirects = client.stream_calls[0]
        self.assertEqual(table, "stocks")
        self.assertEqual(params["years"], "10")
        self.assertEqual(headers["x-api-key"], "test-key")
        self.assertTrue(follow_redirects)

    def test_bulk_download_rejects_an_unknown_years_value(self):
        plane = SharadarPricePlane(api_key="k", client=_StubClient())
        with self.assertRaises(PricePlaneConfigError):
            plane.bulk_download("stocks", 7, "/tmp/whatever.zip")

    def test_bulk_download_maps_401_to_auth_error(self):
        """Observed live: `error_401_bulk_no_key.json`."""
        client = _StubClient(streams={"stocks": _StubStream(
            status_code=401,
            error_payload={"error": "Unauthorized", "description": "A valid API key is required for bulk downloads."},
        )})
        plane = SharadarPricePlane(api_key="test-api-key", client=client)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PricePlaneAuthError):
                plane.bulk_download("stocks", 5, Path(tmp) / "x.zip")

    def test_load_bulk_bars_parses_a_synthetic_zip(self):
        csv_text = (
            "ticker,date,open,high,low,close,volume,closeunadj\n"
            "BBBY,2023-04-28,1.0,1.1,0.9,1.00,1000000,1.00\n"
            "BBBY,2023-05-01,0.8,0.9,0.7,0.80,2000000,0.80\n"
            "BBBY,2023-05-02,0.3,0.4,0.2,0.30,3000000,0.30\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "stocks-10y.zip"
            zip_path.write_bytes(self._zip_bytes("SHARADAR_STOCKS.csv", csv_text))
            plane = SharadarPricePlane(api_key="k", client=_StubClient())
            bars_by_ticker = plane.load_bulk_bars(zip_path)

        self.assertEqual(set(bars_by_ticker), {"BBBY"})
        bars = bars_by_ticker["BBBY"]
        self.assertEqual(len(bars), 3)
        check_reconstruction(bars)
        self.assertEqual(bars[-1].raw_close, 0.30)

    def test_load_bulk_actions_parses_a_synthetic_zip(self):
        csv_text = (
            "date,action,ticker,value\n"
            "2023-04-24,bankruptcy,BBBY,\n"
            "2022-01-10,dividend,BBBY,0.10\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "actions-10y.zip"
            zip_path.write_bytes(self._zip_bytes("SHARADAR_ACTIONS.csv", csv_text))
            plane = SharadarPricePlane(api_key="k", client=_StubClient())
            actions_by_ticker = plane.load_bulk_actions(zip_path)

        self.assertEqual({a.action_type for a in actions_by_ticker["BBBY"]}, {"bankruptcy", "dividend"})

    def test_a_zip_with_the_wrong_number_of_members_is_an_error(self):
        import zipfile

        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "empty.zip"
            with zipfile.ZipFile(zip_path, "w"):
                pass
            plane = SharadarPricePlane(api_key="k", client=_StubClient())
            with self.assertRaises(PricePlaneSchemaError):
                plane.load_bulk_bars(zip_path)

    def test_backfill_bulk_end_to_end_against_a_disposable_database(self):
        """`scripts.price_backfill --bulk` — the whole path, stubbed transport."""
        from scripts.price_backfill import backfill_bulk

        db = init_test_db("sharadar_bulk_backfill")
        self.addCleanup(db.cleanup)

        stocks_csv = self._zip_bytes(
            "SHARADAR_STOCKS.csv",
            "ticker,date,open,high,low,close,volume,closeunadj\n"
            "BBBY,2023-04-28,1.0,1.1,0.9,1.00,1000000,1.00\n"
            "BBBY,2023-05-01,0.8,0.9,0.7,0.80,2000000,0.80\n"
            "BBBY,2023-05-02,0.3,0.4,0.2,0.30,3000000,0.30\n"
            "AAPL,2023-05-01,170.0,171.0,169.0,170.5,5000000,170.5\n"
            "AAPL,2023-05-02,170.5,172.0,170.0,171.8,4500000,171.8\n",
        )
        actions_csv = self._zip_bytes(
            "SHARADAR_ACTIONS.csv",
            "date,action,ticker,value\n"
            "2023-04-24,bankruptcy,BBBY,\n"
            "2022-01-10,dividend,BBBY,0.10\n",
        )
        client = _StubClient(
            tables=_sharadar_tables(tickers=[
                {"table": "stocks", "permaticker": "199059", "ticker": "BBBY",
                 "name": "Bed Bath & Beyond Inc", "exchange": "NASDAQ", "isdelisted": "Y",
                 "firstpricedate": "1992-07-28", "lastpricedate": "2023-05-02"},
                {"table": "stocks", "permaticker": "320193", "ticker": "AAPL",
                 "name": "Apple Inc", "exchange": "NASDAQ", "isdelisted": "N",
                 "firstpricedate": "1980-12-12", "lastpricedate": "2023-05-02"},
            ]),
            streams={
                "stocks": _StubStream(content=stocks_csv),
                "actions": _StubStream(content=actions_csv),
            },
        )
        plane = SharadarPricePlane(api_key="test-key", client=client)

        from database.db import get_session

        summary = backfill_bulk(plane, "10", None, since=None, until=None, check=True)
        self.assertEqual(summary["tickers_with_bars"], 2)
        self.assertEqual(summary["bars_written"], 5)
        self.assertEqual(summary["actions_written"], 2)
        self.assertEqual(summary["securities_written"], 2)

        with get_session() as session:
            self.assertEqual(len(store.load_all_bars(session)), 2)

        # Idempotent, like the per-ticker path.
        again = backfill_bulk(plane, "10", None, since=None, until=None, check=True)
        self.assertEqual(again["bars_written"], 5)
        with get_session() as session:
            self.assertEqual(
                sum(len(b) for b in store.load_all_bars(session).values()), 5
            )

    def test_backfill_bulk_since_until_filters_and_tickers_narrows(self):
        from scripts.price_backfill import backfill_bulk

        db = init_test_db("sharadar_bulk_backfill_filtered")
        self.addCleanup(db.cleanup)

        stocks_csv = self._zip_bytes(
            "SHARADAR_STOCKS.csv",
            "ticker,date,open,high,low,close,volume,closeunadj\n"
            "BBBY,2023-04-28,1.0,1.1,0.9,1.00,1000000,1.00\n"
            "BBBY,2023-05-01,0.8,0.9,0.7,0.80,2000000,0.80\n"
            "BBBY,2023-05-02,0.3,0.4,0.2,0.30,3000000,0.30\n"
            "AAPL,2023-05-01,170.0,171.0,169.0,170.5,5000000,170.5\n",
        )
        actions_csv = self._zip_bytes("SHARADAR_ACTIONS.csv", "date,action,ticker,value\n")
        client = _StubClient(
            tables=_sharadar_tables(tickers=[
                {"table": "stocks", "permaticker": "199059", "ticker": "BBBY",
                 "name": "Bed Bath & Beyond Inc", "exchange": "NASDAQ", "isdelisted": "Y",
                 "firstpricedate": "1992-07-28", "lastpricedate": "2023-05-02"},
            ]),
            streams={
                "stocks": _StubStream(content=stocks_csv),
                "actions": _StubStream(content=actions_csv),
            },
        )
        plane = SharadarPricePlane(api_key="test-key", client=client)

        summary = backfill_bulk(
            plane, "10", ["BBBY"], since=date(2023, 5, 1), until=None, check=True,
        )
        self.assertEqual(summary["tickers_with_bars"], 1)
        self.assertEqual(summary["bars_written"], 2)


class FixturePlaneContractTests(unittest.TestCase):
    def test_a_bad_fixture_header_is_rejected(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "securities.csv").write_text("wrong,header\n", encoding="utf-8")
            with self.assertRaises(PricePlaneSchemaError):
                FixturePricePlane(root).security_master()

    def test_a_missing_fixture_directory_is_rejected(self):
        with self.assertRaises(PricePlaneConfigError):
            FixturePricePlane("/nonexistent/prices")

    def test_an_unknown_ticker_returns_no_bars(self):
        self.assertEqual(plane().daily_bars("NOPE"), ())

    def test_bars_reject_a_non_finite_value(self):
        with self.assertRaises(PricePlaneSchemaError):
            DailyBar(
                security_uid="x", ticker="X", session_date=date(2023, 1, 3),
                raw_open=1.0, raw_high=1.0, raw_low=1.0, raw_close=float("nan"),
                volume=1.0, split_factor=1.0, dividend_cash=0.0,
                split_adjusted_close=1.0, total_return_close=1.0, source="test",
            )


class FlagTests(unittest.TestCase):
    def test_the_price_plane_is_off_by_default(self):
        from config.settings import Settings
        from data.prices import config as plane_config

        settings = Settings(price_plane_enabled=False)
        self.assertFalse(plane_config.is_enabled(settings))
        with self.assertRaises(PricePlaneConfigError):
            plane_config.require_enabled(settings)

    def test_the_factory_refuses_an_unknown_source(self):
        from config.settings import Settings
        from data.prices import config as plane_config

        with self.assertRaises(PricePlaneConfigError):
            plane_config.build_plane("norgate", Settings())


class NoNetworkTests(unittest.TestCase):
    def test_no_network_in_tests(self):
        """The offline surface must stay offline.

        Sockets are blocked around the pure price-plane paths only — the
        fixture plane, the derived series, the covariates, the universe rule and
        the audit. Database work is deliberately outside the guard: on the
        Postgres matrix entry the test database *is* a socket, and a guard that
        covered it would either fail there or have to be disabled there, which
        is the same as not having it.
        """
        blocked = []

        def refuse(*args, **kwargs):
            blocked.append(args)
            raise AssertionError("the price plane opened a socket in a test")

        saved = (socket.socket, socket.create_connection, socket.getaddrinfo)
        socket.socket, socket.create_connection, socket.getaddrinfo = refuse, refuse, refuse
        try:
            fixtures = FixturePricePlane()
            bars_by_uid = {}
            for ticker in fixtures.tickers():
                bars = fixtures.daily_bars(ticker)
                check_reconstruction(bars)
                fixtures.corporate_actions(ticker)
                bars_by_uid[bars[0].security_uid] = bars
            fixtures.security_master()
            fixtures.index_membership("sp500_fixture_v1")

            sessions = sorted({b.session_date for bars in bars_by_uid.values() for b in bars})
            universes.compute_membership(bars_by_uid, sessions, top_n=3, window=20)
            liquidity = liquidity_deciles(bars_by_uid, sessions[-1], 20)
            self.assertTrue(liquidity)

            audit = run_audit(fixtures)
            self.assertEqual(audit["counts"]["collapse"], 1)

            from data.prices import sp500_history

            self.assertGreater(len(sp500_history.membership_intervals()), 1000)

            # The vendor adapter parses stub payloads without a transport too.
            SharadarPricePlane(api_key="k", client=_StubClient(_sharadar_tables())).daily_bars("BBBY")
        finally:
            socket.socket, socket.create_connection, socket.getaddrinfo = saved

        self.assertEqual(blocked, [])


def _one_day():
    from datetime import timedelta

    return timedelta(days=1)


if __name__ == "__main__":
    unittest.main()
