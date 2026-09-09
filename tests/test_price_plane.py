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

import socket
import unittest
from datetime import date, datetime, timezone

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
from data.prices.sharadar import SharadarPricePlane
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
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class _StubClient:
    """An `httpx.Client` stand-in. Records calls; opens no socket."""

    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def get(self, url, params=None, timeout=None):
        table = url.rsplit("/", 1)[-1].split(".")[0]
        self.calls.append((table, dict(params or {})))
        payload = self.payloads[table]
        if isinstance(payload, _StubResponse):
            return payload
        return _StubResponse(payload)


def _datatable(columns, rows):
    return {
        "datatable": {"columns": [{"name": c} for c in columns], "data": rows},
        "meta": {"next_cursor_id": None},
    }


TICKERS_COLUMNS = (
    "permaticker", "ticker", "name", "exchange", "isdelisted",
    "firstpricedate", "lastpricedate",
)
SEP_COLUMNS = ("ticker", "date", "open", "high", "low", "close", "volume", "closeunadj")
ACTIONS_COLUMNS = ("date", "action", "ticker", "value")


def _sharadar_payloads(**overrides):
    payloads = {
        "TICKERS": _datatable(
            TICKERS_COLUMNS,
            [[199059, "BBBY", "Bed Bath & Beyond Inc", "NASDAQ", "Y", "1992-07-28", "2023-05-02"]],
        ),
        "ACTIONS": _datatable(
            ACTIONS_COLUMNS,
            [["2023-04-24", "bankruptcy", "BBBY", None], ["2022-01-10", "dividend", "BBBY", 0.10]],
        ),
        "SEP": _datatable(
            SEP_COLUMNS,
            [
                ["BBBY", "2023-04-28", 1.0, 1.1, 0.9, 1.00, 1_000_000, 1.00],
                ["BBBY", "2023-05-01", 0.8, 0.9, 0.7, 0.80, 2_000_000, 0.80],
                ["BBBY", "2023-05-02", 0.3, 0.4, 0.2, 0.30, 3_000_000, 0.30],
            ],
        ),
    }
    payloads.update(overrides)
    return payloads


class SharadarAdapterTests(unittest.TestCase):
    """The vendor adapter, exercised entirely against stub payloads."""

    def test_parses_a_well_formed_payload(self):
        client = _StubClient(_sharadar_payloads())
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

    def test_the_key_is_never_in_a_url_path_and_comes_from_config(self):
        client = _StubClient(_sharadar_payloads())
        plane = SharadarPricePlane(api_key="test-key", client=client)
        plane.security_master(["BBBY"])
        table, params = client.calls[0]
        self.assertEqual(table, "TICKERS")
        self.assertEqual(params["api_key"], "test-key")

    def test_a_missing_key_refuses_rather_than_calling_anonymously(self):
        import os

        saved = os.environ.pop("NASDAQ_DATA_LINK_API_KEY", None)
        self.addCleanup(
            lambda: os.environ.__setitem__("NASDAQ_DATA_LINK_API_KEY", saved)
            if saved is not None else None
        )
        with self.assertRaises(PricePlaneConfigError):
            SharadarPricePlane()

    def test_adapter_schema_change_fails_loudly(self):
        """A renamed column is an error, not a silent null."""
        renamed = _sharadar_payloads(
            SEP=_datatable(
                ("ticker", "date", "open", "high", "low", "close", "volume", "close_unadjusted"),
                [["BBBY", "2023-05-02", 0.3, 0.4, 0.2, 0.30, 3_000_000, 0.30]],
            )
        )
        plane = SharadarPricePlane(api_key="k", client=_StubClient(renamed))
        with self.assertRaises(PricePlaneSchemaError) as caught:
            plane.daily_bars("BBBY")
        self.assertIn("closeunadj", str(caught.exception))

        # And in the security master.
        renamed_master = _sharadar_payloads(
            TICKERS=_datatable(
                ("permaticker", "ticker", "name", "exchange", "delisted",
                 "firstpricedate", "lastpricedate"),
                [[1, "BBBY", "x", "NASDAQ", "Y", "1992-07-28", "2023-05-02"]],
            )
        )
        with self.assertRaises(PricePlaneSchemaError):
            SharadarPricePlane(api_key="k", client=_StubClient(renamed_master)).security_master()

    def test_a_null_in_a_required_field_is_an_error(self):
        nulled = _sharadar_payloads(
            SEP=_datatable(
                SEP_COLUMNS,
                [["BBBY", "2023-05-02", 0.3, 0.4, 0.2, 0.30, 3_000_000, None]],
            )
        )
        plane = SharadarPricePlane(api_key="k", client=_StubClient(nulled))
        with self.assertRaises(PricePlaneSchemaError) as caught:
            plane.daily_bars("BBBY")
        self.assertIn("never stores a null", str(caught.exception))

    def test_a_non_200_is_an_error(self):
        plane = SharadarPricePlane(
            api_key="k",
            client=_StubClient(_sharadar_payloads(
                TICKERS=_StubResponse(None, status_code=403, text="forbidden")
            )),
        )
        with self.assertRaises(PricePlaneSchemaError) as caught:
            plane.security_master()
        self.assertIn("403", str(caught.exception))

    def test_a_payload_without_a_datatable_is_an_error(self):
        plane = SharadarPricePlane(
            api_key="k", client=_StubClient(_sharadar_payloads(TICKERS={"quandl_error": "x"}))
        )
        with self.assertRaises(PricePlaneSchemaError):
            plane.security_master()

    def test_a_row_of_the_wrong_width_is_an_error(self):
        plane = SharadarPricePlane(
            api_key="k",
            client=_StubClient(_sharadar_payloads(
                TICKERS=_datatable(TICKERS_COLUMNS, [[1, "BBBY", "x"]])
            )),
        )
        with self.assertRaises(PricePlaneSchemaError):
            plane.security_master()

    def test_an_extra_vendor_column_is_tolerated(self):
        """A vendor adding a field must not stop ingest; a rename must."""
        widened = _sharadar_payloads(
            TICKERS=_datatable(
                TICKERS_COLUMNS + ("newfield",),
                [[1, "BBBY", "x", "NASDAQ", "N", "1992-07-28", "2023-05-02", "whatever"]],
            )
        )
        plane = SharadarPricePlane(api_key="k", client=_StubClient(widened))
        self.assertEqual(plane.security_master()[0].ticker, "BBBY")


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
            SharadarPricePlane(api_key="k", client=_StubClient(_sharadar_payloads())).daily_bars("BBBY")
        finally:
            socket.socket, socket.create_connection, socket.getaddrinfo = saved

        self.assertEqual(blocked, [])


def _one_day():
    from datetime import timedelta

    return timedelta(days=1)


if __name__ == "__main__":
    unittest.main()
