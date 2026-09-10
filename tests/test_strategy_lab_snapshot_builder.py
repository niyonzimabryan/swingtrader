"""Spec Q §6: building a snapshot from the tables this repository already has.

These tests run against whichever engine the suite targets, so CI exercises
them on SQLite and on Postgres.

The point of the file is the *seams*. Every one of them is a place where the
Strategy Lab reads a table another phase owns, and each has a rule that has to
hold across the boundary:

* prices come from ``price_bars``, split-adjusted for signals and raw for
  fills, and carry no availability provenance — so every snapshot built from
  them is ``archival_reconstructed`` and exploratory;
* membership comes from ``universe_membership`` as of the cutoff and is
  filtered on ``known_at_utc`` as well as on the interval;
* facts come from ``source_observations``, written here through
  ``filings/observations.py`` so the test exercises the real writer rather than
  a hand-built row, and read back under ``known_at_utc <= cutoff``;
* the compatibility arm's payload comes from ``scored_candidates`` and
  ``memos``, frozen as-is.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta

from database import models
from filings.observations import Observation, write_observations
from strategy_lab import snapshot_builder, snapshots
from strategy_lab.domain import SnapshotScope
from strategy_lab.strategies import earnings_drift_v1 as earnings
from strategy_lab.strategies import momentum_v1 as momentum
from strategy_lab.strategies import swingtrader_composite_v1 as composite
from tests.dbfixture import TestDatabase

CUTOFF = datetime(2026, 3, 31, 21, 0)
UNIVERSE = "liquid_us_equity_v1"


def weekdays_ending(last: date, count: int) -> list[date]:
    out: list[date] = []
    day = last
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day -= timedelta(days=1)
    return list(reversed(out))


class BuilderTestCase(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("strategy_lab_builder")
        self.addCleanup(self.db.cleanup)
        from database.db import get_session, init_db

        init_db(self.db.url)
        self._ctx = get_session()
        self.session = self._ctx.__enter__()
        self.addCleanup(lambda: self._ctx.__exit__(None, None, None))

    # -- fixtures --------------------------------------------------------- #

    def add_security(self, ticker: str, uid: str | None = None) -> str:
        uid = uid or f"uid-{ticker.lower()}"
        self.session.add(models.Security(
            security_uid=uid, ticker=ticker, ticker_valid_from=date(2020, 1, 1),
            ticker_valid_to=None, venue="XNAS", source="fixture",
        ))
        self.session.flush()
        return uid

    def add_bars(
        self, ticker: str, closes, *, uid: str | None = None,
        last: date = date(2026, 3, 31), volume: float = 1_000_000.0,
        source: str = "fixture", ratio: float = 1.0,
    ) -> str:
        uid = uid or self.add_security(ticker)
        for day, adjusted in zip(weekdays_ending(last, len(closes)), closes):
            raw = adjusted / ratio
            self.session.add(models.PriceBar(
                security_uid=uid, ticker=ticker, session_date=day,
                raw_open=raw, raw_high=raw + 1.0, raw_low=raw - 1.0, raw_close=raw,
                volume=volume, split_factor=1.0, dividend_cash=0.0,
                split_adjusted_close=adjusted, total_return_close=adjusted,
                source=source,
            ))
        self.session.flush()
        return uid

    def add_membership(self, ticker: str, uid: str, *, known_at: datetime = CUTOFF):
        self.session.add(models.UniverseMembership(
            universe_slug=UNIVERSE, security_uid=uid, ticker=ticker,
            member_from=date(2026, 3, 31), member_to=None,
            source="rule:liquid_us_equity_v1", known_at_utc=known_at,
        ))
        self.session.flush()

    def add_earnings_observations(
        self, ticker: str, *, reported: float, consensus: float,
        event_at: datetime = datetime(2026, 3, 31, 20, 30),
        consensus_at: datetime | None = None,
    ):
        cik = "0000000320"
        rows = [
            Observation(
                source="sec_eight_k_index", entity_cik=cik, ticker_at_time=ticker,
                fact_type=snapshots.FACT_EARNINGS_RELEASE,
                valid_at=event_at, known_at_utc=event_at,
                known_at_source="acceptanceDateTime", precision="second",
                provenance_class="vendor_pit", replay_eligible=True,
                value_text="2.02", accession="0000320193-26-000001",
                source_url="https://www.sec.gov/fixture", source_trust="primary_regulator",
            ),
            Observation(
                source="sec_companyfacts", entity_cik=cik, ticker_at_time=ticker,
                fact_type=snapshots.FACT_EPS_DILUTED,
                valid_at=event_at, known_at_utc=event_at,
                known_at_source="acceptanceDateTime", precision="second",
                provenance_class="vendor_pit", replay_eligible=True,
                value_numeric=reported, unit="USD_per_share",
                accession="0000320193-26-000002",
                source_url="https://www.sec.gov/fixture", source_trust="primary_regulator",
            ),
            Observation(
                source="news_plane", entity_cik="0000000000", ticker_at_time=ticker,
                fact_type=snapshots.FACT_CONSENSUS_EPS,
                valid_at=consensus_at or event_at,
                known_at_utc=consensus_at or event_at,
                known_at_source="publisher_timestamp_of_source_article",
                precision="second", provenance_class="vendor_pit", replay_eligible=True,
                value_numeric=consensus, unit="USD_per_share",
                source_url="https://example.test/fixture",
                source_trust="established_publisher", mirror_allowed=False,
            ),
        ]
        write_observations(self.session, rows)
        self.session.flush()

    def add_scored_candidate(self, ticker: str, **overrides):
        kwargs = dict(
            run_id="run-1", ticker=ticker, scored_at=datetime(2026, 3, 31, 20, 0),
            source="tier2_gemini", final_score=0.82, direction="long",
            cohort="memo", memo_generated=True, catalyst_score=0.9,
        )
        kwargs.update(overrides)
        self.session.add(models.ScoredCandidate(**kwargs))
        self.session.flush()

    def add_memo(self, ticker: str, **overrides):
        import json

        row = models.Ticker(symbol=ticker)
        self.session.add(row)
        self.session.flush()
        kwargs = dict(
            ticker_id=row.id, composite_score=0.82, classification="high_conviction",
            direction="long", created_at=datetime(2026, 3, 31, 20, 10),
            trade_params=json.dumps({
                "entry_price": 100.1, "stop_loss": 94.0, "target_1": 112.2,
                "target_2": 118.3, "max_hold_days": 20, "position_pct": 5.0,
                "regime_multiplier": 1.0,
            }),
            signal_breakdown=json.dumps({"catalyst": 0.9}),
        )
        kwargs.update(overrides)
        self.session.add(models.Memo(**kwargs))
        self.session.flush()


class TickerSnapshotTests(BuilderTestCase):
    def test_stored_bars_produce_an_exploratory_snapshot(self):
        """No price source carries availability provenance, so nothing replays."""
        self.add_bars("AAPL", [100.0] * 260)
        snapshot = snapshot_builder.build_ticker_snapshot(
            self.session, "AAPL", cutoff=CUTOFF, with_earnings=False
        )
        self.assertEqual(snapshot.scope, SnapshotScope.TICKER)
        self.assertEqual(snapshot.ticker, "AAPL")
        self.assertIn("archival_reconstructed", snapshot.quality_warnings)
        self.assertFalse(snapshot.replay_eligible)
        self.assertEqual(
            snapshots.price_meta_of(snapshot, "AAPL")["provenance_class"],
            snapshots.PROVENANCE_ARCHIVAL,
        )

    def test_the_window_stops_at_the_cutoff_and_holds_the_last_253_sessions(self):
        self.add_bars("AAPL", [100.0] * 300)
        snapshot = snapshot_builder.build_ticker_snapshot(
            self.session, "AAPL", cutoff=CUTOFF, with_earnings=False
        )
        bars = snapshots.bars_of(snapshot, "AAPL")
        self.assertEqual(len(bars), snapshot_builder.DEFAULT_SESSION_WINDOW)
        self.assertEqual(bars[-1].session_date, date(2026, 3, 31))
        self.assertTrue(all(b.session_date <= date(2026, 3, 31) for b in bars))

    def test_a_bar_after_the_cutoff_is_never_loaded(self):
        self.add_bars("AAPL", [100.0] * 260, last=date(2026, 4, 10))
        snapshot = snapshot_builder.build_ticker_snapshot(
            self.session, "AAPL", cutoff=CUTOFF, with_earnings=False
        )
        bars = snapshots.bars_of(snapshot, "AAPL")
        self.assertLessEqual(bars[-1].session_date, date(2026, 3, 31))

    def test_the_three_price_series_survive_the_round_trip(self):
        # A 2-for-1 split: raw closes are twice the adjusted ones.
        self.add_bars("AAPL", [50.0] * 260, ratio=0.5)
        snapshot = snapshot_builder.build_ticker_snapshot(
            self.session, "AAPL", cutoff=CUTOFF, with_earnings=False
        )
        bar = snapshots.bars_of(snapshot, "AAPL")[-1]
        self.assertEqual(bar.raw_close, 100.0)
        self.assertEqual(bar.split_adjusted_close, 50.0)
        self.assertEqual(bar.adjustment_ratio, 0.5)

    def test_an_intraday_cutoff_drops_the_session_that_is_still_forming(self):
        """The builder withholds it rather than handing the rules a bad input."""
        self.add_bars("AAPL", [100.0] * 300)
        intraday = datetime(2026, 3, 31, 15, 0)
        snapshot = snapshot_builder.build_ticker_snapshot(
            self.session, "AAPL", cutoff=intraday, with_earnings=False
        )
        bars = snapshots.bars_of(snapshot, "AAPL")
        self.assertEqual(bars[-1].session_date, date(2026, 3, 30))
        self.assertEqual(
            len(bars), snapshot_builder.DEFAULT_SESSION_WINDOW,
            "an extra row is fetched so dropping the forming session still "
            "leaves a full window",
        )

    def test_a_ticker_with_nothing_stored_refuses_rather_than_returning_empty(self):
        with self.assertRaises(snapshot_builder.SnapshotBuildError):
            snapshot_builder.build_ticker_snapshot(self.session, "NOPE", cutoff=CUTOFF)


class EarningsRecordTests(BuilderTestCase):
    def test_a_complete_record_is_assembled_from_the_observation_ledger(self):
        self.add_bars("AAPL", [100.0] * 260)
        self.add_earnings_observations("AAPL", reported=1.25, consensus=1.00)
        snapshot = snapshot_builder.build_ticker_snapshot(
            self.session, "AAPL", cutoff=CUTOFF
        )
        record = snapshots.earnings_of(snapshot, "AAPL")
        self.assertIsNotNone(record)
        self.assertEqual(record.reported_eps, 1.25)
        self.assertEqual(record.consensus_eps, 1.00)
        self.assertEqual(record.event_date, date(2026, 3, 31))
        self.assertEqual(len(record.observation_ids), 3)
        self.assertEqual(
            snapshot.source_observation_ids, tuple(sorted(record.observation_ids))
        )

    def test_a_record_takes_the_latest_of_its_components_known_times(self):
        self.add_bars("AAPL", [100.0] * 260)
        self.add_earnings_observations(
            "AAPL", reported=1.25, consensus=1.00,
            event_at=datetime(2026, 3, 30, 20, 30),
            consensus_at=datetime(2026, 3, 31, 12, 0),
        )
        record = snapshots.earnings_of(
            snapshot_builder.build_ticker_snapshot(self.session, "AAPL", cutoff=CUTOFF),
            "AAPL",
        )
        self.assertEqual(record.known_at_utc, datetime(2026, 3, 31, 12, 0))
        self.assertEqual(record.event_known_at_utc, datetime(2026, 3, 30, 20, 30))

    def test_a_missing_consensus_means_no_record_rather_than_a_guess(self):
        self.add_bars("AAPL", [100.0] * 260)
        write_observations(self.session, [Observation(
            source="sec_eight_k_index", entity_cik="0000000320", ticker_at_time="AAPL",
            fact_type=snapshots.FACT_EARNINGS_RELEASE,
            valid_at=datetime(2026, 3, 31, 20, 30),
            known_at_utc=datetime(2026, 3, 31, 20, 30),
            known_at_source="acceptanceDateTime", precision="second",
            provenance_class="vendor_pit", replay_eligible=True, value_text="2.02",
            source_url="https://www.sec.gov/fixture", source_trust="primary_regulator",
        )])
        snapshot = snapshot_builder.build_ticker_snapshot(
            self.session, "AAPL", cutoff=CUTOFF
        )
        self.assertIsNone(snapshots.earnings_of(snapshot, "AAPL"))

    def test_a_consensus_published_after_the_cutoff_is_not_visible(self):
        self.add_bars("AAPL", [100.0] * 260)
        self.add_earnings_observations(
            "AAPL", reported=1.25, consensus=1.00,
            consensus_at=datetime(2026, 4, 1, 12, 0),
        )
        snapshot = snapshot_builder.build_ticker_snapshot(
            self.session, "AAPL", cutoff=CUTOFF
        )
        self.assertIsNone(
            snapshots.earnings_of(snapshot, "AAPL"),
            "the record is incomplete at the cutoff, so there is no record",
        )

    def test_the_assembled_record_drives_the_strategy_end_to_end(self):
        self.add_bars("AAPL", [100.0] * 260)
        self.add_earnings_observations("AAPL", reported=1.25, consensus=1.00)
        snapshot = snapshot_builder.build_ticker_snapshot(
            self.session, "AAPL", cutoff=CUTOFF
        )
        decisions = earnings.STRATEGY.evaluate(snapshot)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action.value, "long")
        self.assertIn(
            "data_quality_warning", decisions[0].reason_codes,
            "stored bars carry no availability provenance, and the decision says so",
        )


class UniverseSnapshotTests(BuilderTestCase):
    def test_membership_is_taken_as_of_the_cutoff_not_from_todays_list(self):
        for ticker in ("AAA", "BBB"):
            uid = self.add_bars(ticker, [100.0] * 260)
            self.add_membership(ticker, uid)
        # A name that only joins the universe next quarter.
        uid = self.add_bars("ZZZ", [100.0] * 260)
        self.session.add(models.UniverseMembership(
            universe_slug=UNIVERSE, security_uid=uid, ticker="ZZZ",
            member_from=date(2026, 6, 30), member_to=None,
            source="rule:liquid_us_equity_v1", known_at_utc=datetime(2026, 6, 30, 21, 0),
        ))
        self.session.flush()

        snapshot = snapshot_builder.build_universe_snapshot(
            self.session, cutoff=CUTOFF, universe_slug=UNIVERSE
        )
        self.assertEqual(snapshot.constituents, ("AAA", "BBB"))

    def test_a_membership_row_knowable_only_later_is_excluded(self):
        uid = self.add_bars("AAA", [100.0] * 260)
        self.add_membership("AAA", uid)
        late = self.add_bars("LATE", [100.0] * 260)
        self.add_membership("LATE", late, known_at=datetime(2026, 4, 5, 21, 0))
        snapshot = snapshot_builder.build_universe_snapshot(
            self.session, cutoff=CUTOFF, universe_slug=UNIVERSE
        )
        self.assertEqual(snapshot.constituents, ("AAA",))

    def test_a_member_with_no_bars_becomes_a_recorded_exclusion(self):
        uid = self.add_bars("AAA", [100.0] * 260)
        self.add_membership("AAA", uid)
        ghost = self.add_security("GHOST")
        self.add_membership("GHOST", ghost)
        snapshot = snapshot_builder.build_universe_snapshot(
            self.session, cutoff=CUTOFF, universe_slug=UNIVERSE
        )
        self.assertEqual(snapshot.constituents, ("AAA",))
        self.assertEqual(
            snapshots.exclusions_of(snapshot),
            (("GHOST", "no_bars_at_or_before_cutoff"),),
        )

    def test_two_securities_sharing_a_ticker_leave_a_recorded_exclusion(self):
        first = self.add_bars("AAA", [100.0] * 260, uid="uid-aaa-1")
        self.add_membership("AAA", first)
        second = self.add_bars("AAA", [90.0] * 260, uid="uid-aaa-2")
        self.add_membership("AAA", second)
        snapshot = snapshot_builder.build_universe_snapshot(
            self.session, cutoff=CUTOFF, universe_slug=UNIVERSE
        )
        self.assertEqual(snapshot.constituents, ("AAA",))
        self.assertEqual(
            snapshots.exclusions_of(snapshot),
            (("AAA", "duplicate_ticker_for_uid-aaa-2"),),
        )

    def test_an_empty_universe_refuses_rather_than_producing_a_hollow_snapshot(self):
        with self.assertRaises(snapshot_builder.SnapshotBuildError):
            snapshot_builder.build_universe_snapshot(
                self.session, cutoff=CUTOFF, universe_slug=UNIVERSE
            )

    def test_one_shared_cutoff_drives_every_constituent_decision(self):
        for index in range(50):
            ticker = f"T{index:03d}"
            closes = [100.0] + [100.0 * (1.0 + index / 100.0)] * 252
            uid = self.add_bars(ticker, closes)
            self.add_membership(ticker, uid)
        snapshot = snapshot_builder.build_universe_snapshot(
            self.session, cutoff=CUTOFF, universe_slug=UNIVERSE
        )
        self.assertEqual(len(snapshot.constituents), 50)
        decisions = momentum.STRATEGY.evaluate(snapshot)
        self.assertEqual(len(decisions), 50)
        self.assertEqual(
            {d.snapshot_hash for d in decisions}, {snapshot.content_hash},
            "every rank references the one universe snapshot",
        )
        longs = [d for d in decisions if d.action.value == "long"]
        self.assertEqual(len(longs), 5, "ceil(50 / 10) = 5")

    def test_the_universe_version_names_the_slug_and_the_cutoff_date(self):
        uid = self.add_bars("AAA", [100.0] * 260)
        self.add_membership("AAA", uid)
        snapshot = snapshot_builder.build_universe_snapshot(
            self.session, cutoff=CUTOFF, universe_slug=UNIVERSE
        )
        self.assertEqual(snapshot.universe_version, "liquid_us_equity_v1@2026-03-31")


class CompositeFreezeTests(BuilderTestCase):
    def test_the_pipelines_output_is_frozen_and_maps_to_a_long(self):
        self.add_bars("AAPL", [100.0] * 260)
        self.add_scored_candidate("AAPL")
        self.add_memo("AAPL")
        snapshot = snapshot_builder.build_ticker_snapshot(
            self.session, "AAPL", cutoff=CUTOFF, with_composite=True
        )
        frozen = snapshots.composite_of(snapshot, "AAPL")
        self.assertEqual(frozen.final_score, 0.82)
        self.assertEqual(frozen.classification, "high_conviction")
        self.assertEqual(frozen.cohort, "memo")
        self.assertEqual(frozen.trade_params["max_hold_days"], 20)
        self.assertIn("not_point_in_time", snapshot.quality_warnings)

        decision = composite.STRATEGY.evaluate(snapshot)[0]
        self.assertEqual(decision.action.value, "long")
        self.assertEqual(decision.risk_plan.stop_price, 94.0)
        self.assertEqual(decision.risk_plan.target_prices, (112.2, 118.3))

    def test_a_scored_row_with_no_memo_still_freezes_its_score(self):
        self.add_bars("AAPL", [100.0] * 260)
        self.add_scored_candidate("AAPL", cohort="below", final_score=0.31)
        snapshot = snapshot_builder.build_ticker_snapshot(
            self.session, "AAPL", cutoff=CUTOFF, with_composite=True
        )
        frozen = snapshots.composite_of(snapshot, "AAPL")
        self.assertEqual(frozen.final_score, 0.31)
        self.assertEqual(frozen.trade_params, {})
        decision = composite.STRATEGY.evaluate(snapshot)[0]
        self.assertEqual(decision.action.value, "flat")

    def test_a_score_produced_after_the_cutoff_is_not_visible(self):
        self.add_bars("AAPL", [100.0] * 260)
        self.add_scored_candidate("AAPL", scored_at=datetime(2026, 4, 1, 20, 0))
        snapshot = snapshot_builder.build_ticker_snapshot(
            self.session, "AAPL", cutoff=CUTOFF, with_composite=True
        )
        self.assertIsNone(snapshots.composite_of(snapshot, "AAPL"))

    def test_the_portfolio_context_hash_covers_the_recorded_sizing_context(self):
        self.add_bars("AAPL", [100.0] * 260)
        self.add_scored_candidate("AAPL")
        self.add_memo("AAPL")
        first = snapshots.composite_of(
            snapshot_builder.build_ticker_snapshot(
                self.session, "AAPL", cutoff=CUTOFF, with_composite=True
            ),
            "AAPL",
        )
        self.assertRegex(first.portfolio_context_hash, r"^[0-9a-f]{64}$")


class PersistenceTests(BuilderTestCase):
    def test_a_built_snapshot_records_through_the_registry(self):
        self.add_bars("AAPL", [100.0] * 260)
        snapshot = snapshot_builder.build_ticker_snapshot(
            self.session, "AAPL", cutoff=CUTOFF, with_earnings=False
        )
        row = snapshot_builder.record(self.session, snapshot)
        self.assertEqual(row.content_hash, snapshot.content_hash)
        again = snapshot_builder.record(self.session, snapshot)
        self.assertEqual(again.id, row.id, "recording the same snapshot is idempotent")


if __name__ == "__main__":
    unittest.main()
