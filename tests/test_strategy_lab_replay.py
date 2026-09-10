"""Spec Q §10, §15 PR 3: one simulator, one contract, and three refusals.

The parity tests here are the reason ``strategy_lab/replay.py`` may hand raw
fractions to ``backtest.simulator`` instead of importing
``comparables.outcomes.PolicySpec``: this module is on the other side of the
boundary, so it builds a real ``PolicySpec`` from the same
``policy_spec_fields()`` dict, expands it exactly as
``comparables.outcomes._replay`` does, and asserts the identical trade. If the
Strategy Lab's replay ever drifts from the cohort engine's, one of these fails.

The rest is the honest-evidence machinery: a version that freezes an LLM's
conclusion cannot be replayed at a historical T, a snapshot whose provenance is
reconstructed produces an exploratory label rather than clean evidence, a fact
that was not knowable at the cutoff is rejected instead of quietly dropped, and
a position whose bars run out before its time boundary is *open* rather than
flat at zero.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime

from backtest.simulator import simulate_trade
from comparables.outcomes import PolicySpec
from strategy_lab import replay, snapshots, strategies
from strategy_lab.execution_policy import (
    EVENT_SWING_14CAL_V1,
    MOMENTUM_QUARTERLY_89CAL_V1,
    REVERSAL_5CAL_V1,
)
from strategy_lab.validation import ReplayRefused
from tests import strategylabfixture as fx

COSTS = replay.CostAssumptions(slippage_bps=10.0, half_spread_bps=5.0)
VERSIONS = strategies.build_versions()


def a_long_decision(snapshot, slug="earnings_drift_v1"):
    """The roster's own decision for this snapshot, not a hand-built one."""
    decisions = strategies.get_strategy(slug).evaluate(snapshot)
    return next(d for d in decisions if d.action.value == "long")


def earnings_snapshot(*, replay_eligible=True, price_eligible=True, ticker="AAPL"):
    inputs = fx.replayable_inputs(
        ticker,
        fx.flat_bars(260, 100.0),
        earnings=fx.earnings_record(
            ticker, reported_eps=1.2, consensus_eps=1.0,
            replay_eligible=replay_eligible,
        ),
        price_replay_eligible=price_eligible,
        price_provenance_class=(
            snapshots.PROVENANCE_VENDOR_PIT if price_eligible
            else snapshots.PROVENANCE_ARCHIVAL
        ),
    )
    return fx.ticker_snapshot(inputs)


class SimulatorParityTests(unittest.TestCase):
    """The Strategy Lab and the cohort engine fill the same way, or neither."""

    def _assert_parity(self, policy, reference, atr, bars):
        plan = policy.resolve(reference, atr=atr)
        spec = PolicySpec(**plan.policy_spec_fields())
        through_comparables = simulate_trade(
            bars, 0, spec.direction,
            reference * spec.stop_frac,
            reference * spec.target1_frac,
            reference * spec.target2_frac,
            spec.max_holding_days,
            slippage_bps=COSTS.total_bps,
        )
        through_replay = replay.run_policy(
            plan, bars, 0, slippage_bps=COSTS.total_bps
        )
        self.assertEqual(through_replay.rule_fired, through_comparables.rule_fired)
        self.assertEqual(through_replay.exit_date, through_comparables.exit_date)
        self.assertEqual(through_replay.entry_price, through_comparables.entry_price)
        self.assertEqual(through_replay.exit_price, through_comparables.exit_price)
        self.assertAlmostEqual(
            through_replay.pnl_pct, through_comparables.pnl_pct, places=10
        )

    def test_event_swing_matches_the_cohort_engines_expansion(self):
        bars = replay.replay_bars(fx.rising_forward(16))
        self._assert_parity(EVENT_SWING_14CAL_V1, bars[1].open, 2.0, bars)

    def test_reversal_matches_the_cohort_engines_expansion(self):
        bars = replay.replay_bars(fx.falling_forward(8, start=50.0, step=0.5))
        self._assert_parity(REVERSAL_5CAL_V1, bars[1].open, 2.0, bars)

    def test_momentum_matches_the_cohort_engines_expansion(self):
        bars = replay.replay_bars(fx.rising_forward(95, start=200.0, step=0.5))
        self._assert_parity(MOMENTUM_QUARTERLY_89CAL_V1, bars[1].open, None, bars)

    def test_replay_bars_carry_the_split_adjusted_prices(self):
        """A raw series twice the adjusted one still replays on the adjusted one."""
        raw = fx.bars_from_closes([100.0, 101.0, 102.0], adjustment_ratio=0.5)
        bars = replay.replay_bars(raw)
        self.assertAlmostEqual(bars[0].close, 100.0)
        self.assertAlmostEqual(bars[0].open, 100.0)
        self.assertAlmostEqual(bars[-1].close, 102.0)


class PessimisticFillTests(unittest.TestCase):
    """The simulator's conservatism survives the trip through this module."""

    def _bars(self, rows):
        return tuple(
            replay.ReplayBar(date(2026, 4, day), o, h, l, c)
            for day, o, h, l, c in rows
        )

    def test_a_gap_through_the_stop_fills_at_the_worse_open(self):
        # entry 100 at the T+1 open; stop = 100 - 2 x 2 = 96. The next bar opens
        # at 90, below the stop, so the fill is 90 and not 96.
        bars = self._bars([
            (1, 100.0, 101.0, 99.0, 100.0),
            (2, 100.0, 101.0, 99.5, 100.0),
            (3, 90.0, 91.0, 88.0, 89.0),
        ])
        plan = EVENT_SWING_14CAL_V1.resolve(100.0, atr=2.0)
        result = replay.run_policy(plan, bars, 0, slippage_bps=0.0)
        self.assertEqual(result.rule_fired, "stop")
        self.assertEqual(result.exit_price, 90.0)

    def test_a_bar_spanning_both_stop_and_target_resolves_to_the_stop(self):
        bars = self._bars([
            (1, 100.0, 101.0, 99.0, 100.0),
            (2, 100.0, 101.0, 99.5, 100.0),
            (3, 100.0, 120.0, 95.0, 110.0),   # spans stop 96 and target 108
        ])
        plan = EVENT_SWING_14CAL_V1.resolve(100.0, atr=2.0)
        result = replay.run_policy(plan, bars, 0, slippage_bps=0.0)
        self.assertEqual(result.rule_fired, "stop")

    def test_costs_are_adverse_on_every_fill(self):
        snapshot = earnings_snapshot()
        decision = a_long_decision(snapshot)
        outcome = replay.replay_decision(
            decision, VERSIONS["earnings_drift_v1"], snapshot,
            fx.rising_forward(16), costs=COSTS, historical=True,
        )
        self.assertGreater(outcome.gross_pct, outcome.net_pct)
        self.assertEqual(outcome.costs.total_bps, 15.0)

    def test_a_replay_without_a_cost_model_reports_no_net_cost(self):
        """Gross and net coincide, and ``costs`` stays ``None`` to say why."""
        snapshot = earnings_snapshot()
        outcome = replay.replay_decision(
            a_long_decision(snapshot), VERSIONS["earnings_drift_v1"], snapshot,
            fx.rising_forward(16), historical=True,
        )
        self.assertIsNone(outcome.costs)
        self.assertEqual(outcome.gross_pct, outcome.net_pct)

    def test_the_reversal_policys_mandatory_stress_levels_are_reported(self):
        """Spec Q §7: 25 and 50 bps stresses are mandatory for the reversal arm."""
        levels = replay.stress_levels_for(VERSIONS["short_term_reversal_v1"])
        self.assertEqual(levels, (25.0, 50.0))


class RefusalTests(unittest.TestCase):
    def test_a_non_replayable_version_is_refused_before_any_bar(self):
        snapshot = fx.ticker_snapshot(fx.replayable_inputs(
            "AAPL", fx.flat_bars(260, 100.0), composite=fx.composite_result("AAPL"),
        ))
        decision = a_long_decision(snapshot, "swingtrader_composite_v1")
        with self.assertRaises(ReplayRefused) as caught:
            replay.replay_decision(
                decision, VERSIONS["swingtrader_composite_v1"], snapshot,
                fx.rising_forward(16), costs=COSTS, historical=True,
            )
        self.assertIn("historically_replayable=False", str(caught.exception))

    def test_a_reconstructed_snapshot_is_refused_for_a_historical_replay(self):
        snapshot = earnings_snapshot(price_eligible=False)
        self.assertEqual(
            replay.classify_evidence(snapshot), replay.EVIDENCE_EXPLORATORY
        )
        with self.assertRaises(ReplayRefused):
            replay.replay_decision(
                a_long_decision(snapshot), VERSIONS["earnings_drift_v1"], snapshot,
                fx.rising_forward(16), costs=COSTS, historical=True,
            )

    def test_a_reconstructed_snapshot_still_produces_a_labelled_exploratory_result(self):
        """Spec Q §10: shown separately, never combined, never a promotion gate."""
        snapshot = earnings_snapshot(price_eligible=False)
        outcome = replay.replay_decision(
            a_long_decision(snapshot), VERSIONS["earnings_drift_v1"], snapshot,
            fx.rising_forward(16), costs=COSTS, historical=False,
        )
        self.assertEqual(outcome.evidence_class, replay.EVIDENCE_EXPLORATORY)

    def test_one_bar_cannot_be_entered_at_a_t_plus_one_open(self):
        snapshot = earnings_snapshot()
        with self.assertRaises(replay.InsufficientBars):
            replay.replay_decision(
                a_long_decision(snapshot), VERSIONS["earnings_drift_v1"], snapshot,
                fx.rising_forward(1), costs=COSTS, historical=True,
            )

    def test_a_flat_decision_has_nothing_to_replay(self):
        snapshot = earnings_snapshot()
        flat = next(
            d for d in strategies.get_strategy("earnings_drift_v1").evaluate(
                fx.ticker_snapshot(fx.replayable_inputs(
                    "AAPL", fx.flat_bars(260, 100.0),
                    earnings=fx.earnings_record(
                        "AAPL", reported_eps=1.0, consensus_eps=1.0
                    ),
                ))
            )
            if d.action.value == "flat"
        )
        with self.assertRaises(ReplayRefused):
            replay.replay_decision(
                flat, VERSIONS["earnings_drift_v1"], snapshot,
                fx.rising_forward(16), costs=COSTS, historical=True,
            )


class MaturityTests(unittest.TestCase):
    """Spec Q §9: an incomplete position is open, not a zero."""

    def test_bars_that_run_out_before_the_boundary_leave_the_position_open(self):
        snapshot = earnings_snapshot()
        # Four sessions of a 14-calendar-day policy: the simulator time-exits at
        # the last bar it has, which is not the same event as the horizon ending.
        outcome = replay.replay_decision(
            a_long_decision(snapshot), VERSIONS["earnings_drift_v1"], snapshot,
            fx.rising_forward(4), costs=COSTS, historical=True,
        )
        self.assertFalse(outcome.matured)
        self.assertLess(outcome.last_bar_date, outcome.maturity_boundary)

    def test_a_stop_that_fired_is_matured_whatever_the_data_edge(self):
        snapshot = earnings_snapshot()
        outcome = replay.replay_decision(
            a_long_decision(snapshot), VERSIONS["earnings_drift_v1"], snapshot,
            fx.falling_forward(4, step=3.0), costs=COSTS, historical=True,
        )
        self.assertTrue(outcome.stopped_out)
        self.assertTrue(outcome.matured)

    def test_running_to_the_time_boundary_is_matured(self):
        snapshot = earnings_snapshot()
        outcome = replay.replay_decision(
            a_long_decision(snapshot), VERSIONS["earnings_drift_v1"], snapshot,
            fx.rising_forward(16), costs=COSTS, historical=True,
        )
        self.assertTrue(outcome.matured)
        self.assertGreaterEqual(outcome.last_bar_date, outcome.maturity_boundary)


def a_fact(observation_id, *, known_at, valid_at=None, value=1.0, ticker="AAPL"):
    return snapshots.ObservationFact(
        observation_id=observation_id,
        fact_type="eps_diluted",
        valid_at=valid_at or datetime(2026, 3, 31, 0, 0),
        known_at_utc=known_at,
        precision="exact",
        provenance_class=snapshots.PROVENANCE_VENDOR_PIT,
        replay_eligible=True,
        source="sec_companyfacts",
        source_trust="primary",
        ticker=ticker,
        value_numeric=value,
    )


class BitemporalTests(unittest.TestCase):
    """Agent-prompt requirement 11, and Spec Q §8's bitemporal source ledger."""

    CUTOFF = datetime(2026, 4, 1, 21, 0)

    def test_an_observation_known_after_the_cutoff_is_rejected_not_dropped(self):
        facts = [
            a_fact(1, known_at=datetime(2026, 3, 31, 20, 0)),
            a_fact(2, known_at=datetime(2026, 4, 2, 20, 0)),
        ]
        with self.assertRaises(replay.ObservationAfterCutoff) as caught:
            replay.reject_after_cutoff(facts, self.CUTOFF)
        self.assertIn("after the decision cutoff", str(caught.exception))

    def test_facts_knowable_at_the_cutoff_pass_through_unchanged(self):
        facts = [a_fact(1, known_at=datetime(2026, 3, 31, 20, 0))]
        self.assertEqual(replay.reject_after_cutoff(facts, self.CUTOFF), tuple(facts))

    def test_a_revision_resolves_to_the_vintage_current_at_the_cutoff(self):
        original = a_fact(1, known_at=datetime(2026, 3, 31, 20, 0), value=1.00)
        revision = a_fact(2, known_at=datetime(2026, 5, 1, 20, 0), value=0.80)
        resolved = replay.resolve_as_of([original, revision], self.CUTOFF)
        self.assertEqual([f.observation_id for f in resolved], [1])
        self.assertEqual(resolved[0].value_numeric, 1.00)

        later = replay.resolve_as_of(
            [original, revision], datetime(2026, 6, 1, 21, 0)
        )
        self.assertEqual(later[0].value_numeric, 0.80)

    def test_resolution_is_per_subject_and_deterministically_ordered(self):
        facts = [
            a_fact(3, known_at=datetime(2026, 3, 20, 20, 0), ticker="MSFT"),
            a_fact(1, known_at=datetime(2026, 3, 20, 20, 0), ticker="AAPL"),
            a_fact(2, known_at=datetime(2026, 3, 25, 20, 0), ticker="AAPL"),
        ]
        resolved = replay.resolve_as_of(facts, self.CUTOFF)
        self.assertEqual(
            [(f.ticker, f.observation_id) for f in resolved],
            [("AAPL", 2), ("MSFT", 3)],
        )

    def test_the_snapshots_own_cutoff_is_what_a_replay_checks_against(self):
        snapshot = earnings_snapshot()
        self.assertEqual(snapshot.data_cutoff_utc, fx.Q1_2026_CLOSE)
        record = snapshots.earnings_of(snapshot, "AAPL")
        self.assertLessEqual(record.known_at_utc, snapshot.data_cutoff_utc)


class PlanTests(unittest.TestCase):
    def test_the_plan_read_back_from_an_outcome_is_the_plan_that_ran(self):
        snapshot = earnings_snapshot()
        outcome = replay.replay_decision(
            a_long_decision(snapshot), VERSIONS["earnings_drift_v1"], snapshot,
            fx.rising_forward(16), costs=COSTS, historical=True,
        )
        plan = replay.plan_of(outcome)
        rebuilt = replay.build_plan(
            VERSIONS["earnings_drift_v1"], snapshot, "AAPL",
            entry_reference=outcome.entry_reference,
        )
        self.assertEqual(plan.stop_price, rebuilt.stop_price)
        self.assertEqual(plan.target_prices, rebuilt.target_prices)
        self.assertEqual(plan.max_holding_days, rebuilt.max_holding_days)

    def test_the_compatibility_arms_plan_comes_out_of_the_frozen_trade_params(self):
        snapshot = fx.ticker_snapshot(fx.replayable_inputs(
            "AAPL", fx.flat_bars(260, 100.0), composite=fx.composite_result("AAPL"),
        ))
        plan = replay.build_plan(
            VERSIONS["swingtrader_composite_v1"], snapshot, "AAPL",
            entry_reference=100.10,
        )
        self.assertEqual(plan.stop_price, 94.0)
        self.assertEqual(plan.target_prices, (112.2, 118.3))
        self.assertEqual(plan.max_holding_days, 20)

    def test_the_atr_comes_from_the_snapshots_own_bars(self):
        """A flat fixture series has true range 2.0 on every bar, so ATR is 2.0."""
        snapshot = earnings_snapshot()
        atr = replay.atr_for(snapshot, "AAPL", EVENT_SWING_14CAL_V1)
        self.assertAlmostEqual(atr, 2.0)

    def test_a_percentage_stop_policy_needs_no_atr(self):
        snapshot = earnings_snapshot()
        self.assertIsNone(
            replay.atr_for(snapshot, "AAPL", MOMENTUM_QUARTERLY_89CAL_V1)
        )


class DeterminismTests(unittest.TestCase):
    def test_the_same_inputs_produce_the_same_outcome_payload(self):
        snapshot = earnings_snapshot()
        decision = a_long_decision(snapshot)
        bars = fx.rising_forward(16)
        first = replay.replay_decision(
            decision, VERSIONS["earnings_drift_v1"], snapshot, bars,
            costs=COSTS, historical=True,
        ).canonical()
        second = replay.replay_decision(
            decision, VERSIONS["earnings_drift_v1"], snapshot, bars,
            costs=COSTS, historical=True,
        ).canonical()
        self.assertEqual(first, second)

    def test_outcomes_payload_is_sorted_regardless_of_input_order(self):
        snapshot_a = earnings_snapshot(ticker="AAPL")
        snapshot_b = earnings_snapshot(ticker="MSFT")
        outcomes = [
            replay.replay_decision(
                a_long_decision(snapshot_b), VERSIONS["earnings_drift_v1"],
                snapshot_b, fx.rising_forward(16), costs=COSTS, historical=True,
            ),
            replay.replay_decision(
                a_long_decision(snapshot_a), VERSIONS["earnings_drift_v1"],
                snapshot_a, fx.rising_forward(16), costs=COSTS, historical=True,
            ),
        ]
        payload = replay.outcomes_payload(outcomes)
        self.assertEqual([row["ticker"] for row in payload], ["AAPL", "MSFT"])
        self.assertEqual(
            payload, replay.outcomes_payload(list(reversed(outcomes)))
        )


if __name__ == "__main__":
    unittest.main()
