"""Spec Q §6: what may enter a point-in-time snapshot, and what may not.

Pure tests: no database, no network. The rules under test are the ones a
strategy's honesty depends on — one cutoff, no future facts, reconstructed
inputs labelled as such, and a content hash that changes when any of it does.

Two constants are restated inside ``strategy_lab`` rather than imported, because
Spec Q §5 keeps the package out of ``data/`` and ``filings/``. The tests here
import both ends and assert they agree, so the restatement cannot drift.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta

from data.prices.fixture_plane import SESSION_CLOSE_UTC as PRICE_PLANE_SESSION_CLOSE
from data.prices.fixture_plane import session_close_utc as price_plane_session_close
from data.prices.derived import median_dollar_volume as price_plane_mdv
from data.prices.base import DailyBar
from data.prices.universes import UNIVERSE_SLUG as PRICE_PLANE_UNIVERSE_SLUG
from filings import observations as filings_observations
from strategy_lab import snapshots, universe
from strategy_lab.domain import SnapshotScope, StrategyLabError
from strategy_lab.indicators import median_dollar_volume
from tests.strategylabfixture import (
    Q1_2026_CLOSE,
    Q1_2026_SESSION,
    bars_from_closes,
    composite_result,
    earnings_record,
    flat_bars,
    replayable_inputs,
    ticker_snapshot,
    universe_snapshot,
)


class RestatedDefinitionsTests(unittest.TestCase):
    """The Strategy Lab restates a few shared constants. They must agree."""

    def test_the_session_close_matches_the_price_plane(self):
        self.assertEqual(
            snapshots.SESSION_CLOSE_UTC.hour, PRICE_PLANE_SESSION_CLOSE.hour
        )
        self.assertEqual(
            snapshots.SESSION_CLOSE_UTC.minute, PRICE_PLANE_SESSION_CLOSE.minute
        )
        theirs = price_plane_session_close(Q1_2026_SESSION)
        mine = snapshots.session_close_utc(Q1_2026_SESSION)
        self.assertEqual(mine, theirs.replace(tzinfo=None))

    def test_the_provenance_vocabulary_matches_the_observation_ledger(self):
        self.assertEqual(snapshots.PROVENANCE_CLASSES, filings_observations.PROVENANCE_CLASSES)
        self.assertEqual(snapshots.PROVENANCE_ARCHIVAL, filings_observations.PROVENANCE_ARCHIVAL)

    def test_the_universe_slug_matches_the_price_plane(self):
        self.assertEqual(universe.UNIVERSE_SLUG, PRICE_PLANE_UNIVERSE_SLUG)

    def test_median_dollar_volume_matches_the_price_planes_definition(self):
        bars = bars_from_closes([10.0, 12.0, 11.0, 30.0, 9.0], volume=1000.0)
        theirs = price_plane_mdv(
            [
                DailyBar(
                    security_uid="uid", ticker="T", session_date=b.session_date,
                    raw_open=b.raw_open, raw_high=b.raw_high, raw_low=b.raw_low,
                    raw_close=b.raw_close, volume=b.volume,
                    split_factor=1.0, dividend_cash=0.0,
                    split_adjusted_close=b.split_adjusted_close,
                    total_return_close=b.total_return_close, source="fixture",
                )
                for b in bars
            ],
            bars[-1].session_date,
            5,
        )
        mine = median_dollar_volume(
            [b.raw_close for b in bars], [b.volume for b in bars], 5
        )
        self.assertEqual(mine, theirs)


class PointInTimeTests(unittest.TestCase):
    def test_a_fact_known_after_the_cutoff_fails_closed(self):
        """Not filtered out quietly: the builder that produced it has a bug."""
        fact = snapshots.ObservationFact(
            observation_id=1,
            fact_type=snapshots.FACT_CONSENSUS_EPS,
            valid_at=datetime(2026, 3, 31, 12, 0),
            known_at_utc=datetime(2026, 3, 31, 23, 0),  # after the 21:00 cutoff
            precision="second",
            provenance_class=snapshots.PROVENANCE_VENDOR_PIT,
            replay_eligible=True,
            source="news_plane",
            source_trust="established_publisher",
        )
        inputs = replayable_inputs("AAPL", flat_bars(), facts=(fact,))
        with self.assertRaises(snapshots.NonPointInTimeInput) as caught:
            ticker_snapshot(inputs)
        self.assertIn("after the cutoff", str(caught.exception))

    def test_a_bar_that_had_not_closed_at_the_cutoff_fails_closed(self):
        inputs = replayable_inputs("AAPL", flat_bars())
        earlier = datetime(2026, 3, 31, 15, 0)  # intraday: today's bar is forming
        with self.assertRaises(snapshots.NonPointInTimeInput):
            ticker_snapshot(inputs, cutoff=earlier)

    def test_an_earnings_record_completed_after_the_cutoff_fails_closed(self):
        record = earnings_record(
            "AAPL", reported_eps=1.2, consensus_eps=1.0,
            known_at_utc=datetime(2026, 4, 1, 12, 0),
        )
        inputs = replayable_inputs("AAPL", flat_bars(), earnings=record)
        with self.assertRaises(snapshots.NonPointInTimeInput):
            ticker_snapshot(inputs)

    def test_membership_knowable_only_after_the_cutoff_fails_closed(self):
        inputs = replayable_inputs(
            "AAPL", flat_bars(),
            membership_known_at_utc=datetime(2026, 4, 1, 21, 0),
        )
        with self.assertRaises(snapshots.NonPointInTimeInput):
            ticker_snapshot(inputs)

    def test_a_composite_result_scored_after_the_cutoff_fails_closed(self):
        result = composite_result(scored_at=datetime(2026, 4, 1, 12, 0))
        inputs = replayable_inputs("AAPL", flat_bars(), composite=result)
        with self.assertRaises(snapshots.NonPointInTimeInput):
            ticker_snapshot(inputs)

    def test_the_cutoff_boundary_is_inclusive(self):
        """A fact known at exactly the cutoff was knowable at the cutoff."""
        fact = snapshots.ObservationFact(
            observation_id=1,
            fact_type=snapshots.FACT_CONSENSUS_EPS,
            valid_at=Q1_2026_CLOSE,
            known_at_utc=Q1_2026_CLOSE,
            precision="second",
            provenance_class=snapshots.PROVENANCE_VENDOR_PIT,
            replay_eligible=True,
            source="news_plane",
            source_trust="established_publisher",
        )
        snapshot = ticker_snapshot(replayable_inputs("AAPL", flat_bars(), facts=(fact,)))
        self.assertEqual(len(snapshots.facts_of(snapshot, "AAPL")), 1)


class QualityWarningTests(unittest.TestCase):
    def test_archival_prices_make_the_snapshot_exploratory(self):
        inputs = replayable_inputs(
            "AAPL", flat_bars(),
            price_provenance_class=snapshots.PROVENANCE_ARCHIVAL,
            price_replay_eligible=False,
        )
        snapshot = ticker_snapshot(inputs)
        self.assertIn("archival_reconstructed", snapshot.quality_warnings)
        self.assertFalse(snapshot.replay_eligible)

    def test_a_frozen_composite_result_marks_the_snapshot_not_point_in_time(self):
        snapshot = ticker_snapshot(
            replayable_inputs("AAPL", flat_bars(), composite=composite_result())
        )
        self.assertIn("not_point_in_time", snapshot.quality_warnings)
        self.assertFalse(snapshot.replay_eligible)

    def test_an_unknown_delisting_status_is_recorded(self):
        snapshot = ticker_snapshot(
            replayable_inputs("AAPL", flat_bars(), delisting_known=False)
        )
        self.assertIn("delisting_unknown", snapshot.quality_warnings)

    def test_a_replayable_series_with_no_warnings_stays_replay_eligible(self):
        snapshot = ticker_snapshot(replayable_inputs("AAPL", flat_bars()))
        self.assertEqual(snapshot.quality_warnings, ())
        self.assertTrue(snapshot.replay_eligible)


class ContentHashTests(unittest.TestCase):
    def test_the_same_inputs_hash_identically(self):
        one = ticker_snapshot(replayable_inputs("AAPL", flat_bars()))
        two = ticker_snapshot(replayable_inputs("AAPL", flat_bars()))
        self.assertEqual(one.content_hash, two.content_hash)

    def test_one_changed_close_changes_the_hash(self):
        closes = [100.0] * 260
        one = ticker_snapshot(replayable_inputs("AAPL", bars_from_closes(closes)))
        closes[-1] = 100.01
        two = ticker_snapshot(replayable_inputs("AAPL", bars_from_closes(closes)))
        self.assertNotEqual(one.content_hash, two.content_hash)

    def test_constituent_order_does_not_change_the_hash(self):
        a = replayable_inputs("AAA", flat_bars(10))
        b = replayable_inputs("BBB", flat_bars(10, close=50.0))
        self.assertEqual(
            universe_snapshot([a, b]).content_hash,
            universe_snapshot([b, a]).content_hash,
        )

    def test_a_universe_snapshot_holds_the_whole_constituent_set(self):
        snapshot = universe_snapshot([
            replayable_inputs("AAA", flat_bars(10)),
            replayable_inputs("BBB", flat_bars(10)),
            replayable_inputs("CCC", flat_bars(10)),
        ])
        self.assertEqual(snapshot.scope, SnapshotScope.UNIVERSE)
        self.assertEqual(snapshot.constituents, ("AAA", "BBB", "CCC"))
        self.assertEqual(snapshots.tickers_of(snapshot), ("AAA", "BBB", "CCC"))


class ReadBackTests(unittest.TestCase):
    def test_bars_round_trip_through_the_normalized_form(self):
        bars = bars_from_closes([10.0, 11.0, 12.5])
        snapshot = ticker_snapshot(replayable_inputs("AAPL", bars))
        self.assertEqual(snapshots.bars_of(snapshot, "AAPL"), bars)
        self.assertEqual(snapshots.bars_of(snapshot, "aapl"), bars)
        self.assertEqual(snapshots.bars_of(snapshot, "MSFT"), ())

    def test_the_earnings_record_round_trips(self):
        record = earnings_record("AAPL", reported_eps=1.2, consensus_eps=1.0)
        snapshot = ticker_snapshot(replayable_inputs("AAPL", flat_bars(), earnings=record))
        self.assertEqual(snapshots.earnings_of(snapshot, "AAPL"), record)

    def test_the_composite_result_round_trips_and_carries_an_output_hash(self):
        result = composite_result()
        snapshot = ticker_snapshot(replayable_inputs("AAPL", flat_bars(), composite=result))
        self.assertEqual(snapshots.composite_of(snapshot, "AAPL"), result)
        self.assertRegex(snapshots.composite_output_hash(snapshot, "AAPL"), r"^[0-9a-f]{64}$")

    def test_a_foreign_schema_is_refused_rather_than_guessed_at(self):
        from strategy_lab.domain import MarketSnapshot

        snapshot = MarketSnapshot(
            scope=SnapshotScope.TICKER,
            as_of_utc=Q1_2026_CLOSE,
            data_cutoff_utc=Q1_2026_CLOSE,
            provenance={"prices": "elsewhere"},
            ticker="AAPL",
            normalized_inputs={"schema": "somebody_elses.v9", "tickers": {}},
        )
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.bars_of(snapshot, "AAPL")

    def test_exclusions_travel_with_the_snapshot(self):
        snapshot = universe_snapshot(
            [replayable_inputs("AAA", flat_bars(10))],
            exclusions=[("ZZZ", "no_bars_at_or_before_cutoff")],
        )
        self.assertEqual(
            snapshots.exclusions_of(snapshot),
            (("ZZZ", "no_bars_at_or_before_cutoff"),),
        )

    def test_staleness_is_measured_from_the_last_session_close(self):
        snapshot = ticker_snapshot(replayable_inputs("AAPL", flat_bars(5)))
        self.assertEqual(snapshots.staleness_seconds(snapshot, "AAPL"), 0.0)
        later = ticker_snapshot(
            replayable_inputs("AAPL", flat_bars(5)),
            cutoff=Q1_2026_CLOSE + timedelta(days=2),
        )
        self.assertEqual(snapshots.staleness_seconds(later, "AAPL"), 2 * 86_400)

    def test_staleness_is_none_when_there_are_no_bars_at_all(self):
        """Missing is not stale. They are different abstentions."""
        snapshot = ticker_snapshot(
            replayable_inputs("AAPL", (), composite=composite_result())
        )
        self.assertIsNone(snapshots.staleness_seconds(snapshot, "AAPL"))


class CalendarTests(unittest.TestCase):
    def test_the_quarter_end_flag_is_frozen_into_the_snapshot(self):
        snapshot = ticker_snapshot(replayable_inputs("AAPL", flat_bars(5)))
        calendar = snapshots.calendar_of(snapshot)
        self.assertEqual(calendar["signal_session"], "2026-03-31")
        self.assertTrue(calendar["is_quarter_end_session"])
        self.assertEqual(calendar["prior_session"], "2026-03-30")

    def test_a_holiday_does_not_push_the_rebalance_past_the_quarter(self):
        """2027-03-26 is Good Friday; the quarter's last session is 2027-03-31."""
        self.assertFalse(snapshots.is_quarter_end_session(date(2027, 3, 25)))
        self.assertTrue(snapshots.is_quarter_end_session(date(2027, 3, 31)))

    def test_a_weekend_quarter_end_lands_on_the_friday(self):
        """2026-05-31 is a Sunday, so Q2 ends on Tuesday 2026-06-30 regardless."""
        self.assertTrue(snapshots.is_quarter_end_session(date(2026, 6, 30)))
        self.assertFalse(snapshots.is_quarter_end_session(date(2026, 6, 26)))

    def test_a_non_quarter_month_is_never_a_rebalance_session(self):
        for day in (date(2026, 1, 30), date(2026, 4, 30), date(2026, 7, 31)):
            with self.subTest(day):
                self.assertFalse(snapshots.is_quarter_end_session(day))


class InputValidationTests(unittest.TestCase):
    def test_bars_must_ascend(self):
        bars = bars_from_closes([1.0, 2.0, 3.0])
        with self.assertRaises(snapshots.SnapshotError):
            replayable_inputs("AAPL", tuple(reversed(bars)))

    def test_an_unknown_provenance_class_is_refused(self):
        with self.assertRaises(snapshots.SnapshotError):
            replayable_inputs("AAPL", flat_bars(3), price_provenance_class="vibes")

    def test_a_duplicate_constituent_is_refused(self):
        with self.assertRaises(snapshots.SnapshotError):
            universe_snapshot([
                replayable_inputs("AAA", flat_bars(3)),
                replayable_inputs("AAA", flat_bars(3)),
            ])

    def test_an_empty_universe_is_refused(self):
        with self.assertRaises(snapshots.SnapshotError):
            universe_snapshot([])

    def test_source_observation_ids_are_collected_sorted_and_deduped(self):
        record = earnings_record("AAPL", reported_eps=1.0, consensus_eps=0.5)
        fact = snapshots.ObservationFact(
            observation_id=102,  # already one of the record's components
            fact_type=snapshots.FACT_EPS_DILUTED,
            valid_at=Q1_2026_CLOSE,
            known_at_utc=Q1_2026_CLOSE,
            precision="second",
            provenance_class=snapshots.PROVENANCE_VENDOR_PIT,
            replay_eligible=True,
            source="sec_companyfacts",
            source_trust="primary_regulator",
            value_numeric=1.0,
        )
        snapshot = ticker_snapshot(
            replayable_inputs("AAPL", flat_bars(3), earnings=record, facts=(fact,))
        )
        self.assertEqual(snapshot.source_observation_ids, (101, 102, 103))


class AdjustedSeriesTests(unittest.TestCase):
    def test_the_adjustment_ratio_back_adjusts_the_whole_bar(self):
        """A 2-for-1 split: adjusted closes are half the raw ones."""
        bars = bars_from_closes([50.0, 50.0], adjustment_ratio=0.5)
        bar = bars[-1]
        self.assertEqual(bar.raw_close, 100.0)
        self.assertEqual(bar.split_adjusted_close, 50.0)
        self.assertEqual(bar.split_adjusted_high, 51.0)
        self.assertEqual(bar.split_adjusted_low, 49.0)
        # Dollar volume is split-invariant only in raw units.
        self.assertEqual(bar.dollar_volume, 100.0 * 1_000_000.0)

    def test_a_non_positive_raw_close_has_no_adjustment_ratio(self):
        bar = snapshots.SnapshotBar(
            session_date=Q1_2026_SESSION,
            raw_open=0.0, raw_high=0.0, raw_low=0.0, raw_close=0.0,
            volume=0.0, split_adjusted_close=0.0, total_return_close=0.0,
        )
        with self.assertRaises(StrategyLabError):
            _ = bar.adjustment_ratio


if __name__ == "__main__":
    unittest.main()
