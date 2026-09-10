"""Spec Q §7: the three normative execution policies, against the one simulator.

Two things are proved here.

**The arithmetic is the specification's.** Every stop, target and horizon is a
hand-computed golden vector, written out in the test with the sum that produced
it, not recorded from a run.

**There is one execution-policy contract, not two.** Spec N's cohort engine
drives ``backtest/simulator.py`` through
``comparables/outcomes.py::PolicySpec``, whose fields are fractions of the entry
reference. :meth:`ResolvedExecutionPlan.policy_spec_fields` produces exactly
those constructor keywords, and the parity tests build a real ``PolicySpec``
from them, expand it back to prices the way ``comparables.outcomes._replay``
does, and assert the simulator returns the identical trade. The imports live
here rather than in ``strategy_lab`` because Spec Q §5 keeps the Strategy Lab
out of ``comparables`` and ``backtest``; a fraction tuple crossing the boundary
is the dependency, and this file is where the two ends are checked against each
other.
"""

from __future__ import annotations

import unittest
from datetime import date

from backtest.simulator import simulate_trade
from comparables.outcomes import Bar, PolicySpec
from strategy_lab.execution_policy import (
    EVENT_SWING_14CAL_V1,
    MOMENTUM_QUARTERLY_89CAL_V1,
    POLICIES,
    REVERSAL_5CAL_V1,
    SWINGTRADER_MEMO_TRADE_PARAMS_V1,
    ExecutionPolicyError,
    policy_manifest,
    require_policy,
)


def bars(rows) -> list[Bar]:
    return [Bar(date(2026, 3, d), o, h, l, c) for d, o, h, l, c in rows]


class NormativeValuesTests(unittest.TestCase):
    """The values, transcribed from Spec Q §7 and checked one at a time."""

    def test_event_swing_is_2_atr_and_2r_3r_over_14_calendar_days(self):
        # entry 100.00, ATR 2.00 -> stop = 100 - 2*2 = 96.00; R = 4.00;
        # target 1 = 100 + 2*4 = 108.00; target 2 = 100 + 3*4 = 112.00.
        plan = EVENT_SWING_14CAL_V1.resolve(100.0, atr=2.0)
        self.assertEqual(plan.stop_price, 96.0)
        self.assertEqual(plan.target_prices, (108.0, 112.0))
        self.assertEqual(plan.risk_per_share, 4.0)
        self.assertEqual(plan.max_holding_days, 14)
        self.assertEqual(plan.slippage_bps, 10.0)
        self.assertEqual(EVENT_SWING_14CAL_V1.atr_period, 14)

    def test_reversal_is_1_5_atr_one_target_at_1r_over_5_calendar_days(self):
        # entry 50.00, ATR 2.00 -> stop = 50 - 1.5*2 = 47.00; R = 3.00;
        # target 1 = 50 + 1*3 = 53.00; no target 2.
        plan = REVERSAL_5CAL_V1.resolve(50.0, atr=2.0)
        self.assertEqual(plan.stop_price, 47.0)
        self.assertEqual(plan.target_prices, (53.0,))
        self.assertEqual(plan.max_holding_days, 5)
        self.assertEqual(plan.target_frac(1), 0.0, "no target 2 under this policy")

    def test_reversal_declares_the_mandatory_cost_stress_levels(self):
        self.assertEqual(REVERSAL_5CAL_V1.stress_slippage_bps, (25.0, 50.0))

    def test_momentum_is_a_10_percent_overlay_with_no_target_over_89_days(self):
        # entry 200.00 -> stop = 200 * (1 - 0.10) = 180.00; no profit target.
        plan = MOMENTUM_QUARTERLY_89CAL_V1.resolve(200.0)
        self.assertEqual(plan.stop_price, 180.0)
        self.assertEqual(plan.target_prices, ())
        self.assertEqual(plan.max_holding_days, 89)
        self.assertEqual(plan.policy_spec_fields()["target1_frac"], 0.0)

    def test_momentum_rebalances_only_at_a_quarter_end_session(self):
        self.assertEqual(
            MOMENTUM_QUARTERLY_89CAL_V1.signal_cutoff_rule,
            "quarter_end_regular_session",
        )

    def test_every_policy_costs_10_bps_at_baseline_and_is_keyed_by_its_version(self):
        for version, policy in POLICIES.items():
            with self.subTest(version):
                self.assertEqual(policy.baseline_slippage_bps, 10.0)
                self.assertEqual(policy.version, version)

    def test_every_derived_plan_is_long_only(self):
        """Spec Q §3 scopes V1 to long-only US equities."""
        for policy in (EVENT_SWING_14CAL_V1, REVERSAL_5CAL_V1, MOMENTUM_QUARTERLY_89CAL_V1):
            with self.subTest(policy.version):
                plan = policy.resolve(100.0, atr=2.0 if policy.requires_atr else None)
                self.assertEqual(plan.direction, "long")
                self.assertLess(plan.stop_price, plan.entry_reference)
                for target in plan.target_prices:
                    self.assertGreater(target, plan.entry_reference)


class RefusalTests(unittest.TestCase):
    def test_an_atr_policy_refuses_to_resolve_without_an_atr(self):
        with self.assertRaises(ExecutionPolicyError):
            EVENT_SWING_14CAL_V1.resolve(100.0)

    def test_a_non_positive_atr_is_refused_rather_than_floored(self):
        with self.assertRaises(ExecutionPolicyError):
            EVENT_SWING_14CAL_V1.resolve(100.0, atr=0.0)

    def test_an_atr_wide_enough_to_push_the_stop_negative_is_refused(self):
        # 100 - 2*60 = -20. A stop below zero is not a stop.
        with self.assertRaises(ExecutionPolicyError):
            EVENT_SWING_14CAL_V1.resolve(100.0, atr=60.0)

    def test_an_unknown_policy_names_the_registered_ones(self):
        with self.assertRaises(ExecutionPolicyError) as caught:
            require_policy("momentum_monthly_30cal_v1")
        self.assertIn("event_swing_14cal_v1", str(caught.exception))

    def test_the_frozen_plan_policy_derives_nothing(self):
        with self.assertRaises(ExecutionPolicyError):
            SWINGTRADER_MEMO_TRADE_PARAMS_V1.resolve(100.0, atr=2.0)
        with self.assertRaises(ExecutionPolicyError):
            EVENT_SWING_14CAL_V1.resolve_frozen(
                entry_reference=100.0, stop_price=96.0,
                target_prices=(108.0,), max_holding_days=14,
            )

    def test_a_frozen_plan_with_an_inverted_stop_is_refused(self):
        with self.assertRaises(ExecutionPolicyError):
            SWINGTRADER_MEMO_TRADE_PARAMS_V1.resolve_frozen(
                entry_reference=100.0, stop_price=101.0,
                target_prices=(112.0,), max_holding_days=20,
            )

    def test_a_frozen_plan_with_a_target_below_the_entry_is_refused(self):
        with self.assertRaises(ExecutionPolicyError):
            SWINGTRADER_MEMO_TRADE_PARAMS_V1.resolve_frozen(
                entry_reference=100.0, stop_price=94.0,
                target_prices=(99.0,), max_holding_days=20,
            )

    def test_a_frozen_policy_cannot_also_declare_a_horizon(self):
        with self.assertRaises(ExecutionPolicyError):
            type(SWINGTRADER_MEMO_TRADE_PARAMS_V1)(
                version="bad_v1",
                entry_style="x",
                signal_cutoff_rule="completed_regular_session",
                stop_rule="frozen_trade_params",
                max_hold_calendar_days=20,
                baseline_slippage_bps=10.0,
            )


class RiskPlanTests(unittest.TestCase):
    def test_a_derived_policy_leaves_stop_and_targets_to_the_fill(self):
        plan = EVENT_SWING_14CAL_V1.risk_plan(position_risk_pct=1.0)
        self.assertEqual(plan.execution_policy_version, "event_swing_14cal_v1")
        self.assertEqual(plan.max_hold_calendar_days, 14)
        self.assertIsNone(
            plan.stop_price,
            "a 2-ATR stop is anchored to the fill, which does not exist yet",
        )
        self.assertEqual(plan.target_prices, ())

    def test_the_frozen_policy_carries_the_prices_the_pipeline_computed(self):
        plan = SWINGTRADER_MEMO_TRADE_PARAMS_V1.risk_plan(
            position_risk_pct=1.0, max_hold_calendar_days=20,
            stop_price=94.0, target_prices=(112.2, 118.3),
        )
        self.assertEqual(plan.stop_price, 94.0)
        self.assertEqual(plan.target_prices, (112.2, 118.3))
        self.assertEqual(plan.max_hold_calendar_days, 20)

    def test_the_frozen_policy_refuses_a_risk_plan_with_no_horizon(self):
        with self.assertRaises(ExecutionPolicyError):
            SWINGTRADER_MEMO_TRADE_PARAMS_V1.risk_plan(position_risk_pct=1.0)


class SimulatorParityTests(unittest.TestCase):
    """One simulator, one contract: `PolicySpec` and the plan must agree."""

    #  d   open  high   low  close
    SWING = bars([
        (2, 100.0, 101.0, 99.0, 100.0),   # signal bar
        (3, 100.0, 102.0, 99.0, 101.0),   # entry at the open: 100 x 1.001 = 100.10
        (4, 100.0, 109.0, 99.0, 108.0),   # target 1 at 108 -> 108 x 0.999 = 107.892
        (5, 108.0, 113.0, 107.0, 112.0),  # target 2 at 112 -> 112 x 0.999 = 111.888
    ])

    def test_the_event_swing_golden_trade(self):
        plan = EVENT_SWING_14CAL_V1.resolve(100.0, atr=2.0)
        result = simulate_trade(self.SWING, 0, **plan.simulator_arguments())
        self.assertEqual(result.rule_fired, "t1_then_t2")
        self.assertEqual(result.entry_price, 100.1)
        # blended exit = 0.5 x 107.892 + 0.5 x 111.888 = 109.89
        self.assertEqual(result.exit_price, 109.89)
        # (109.89 - 100.10) / 100.10 x 100 = 9.78%
        self.assertEqual(result.pnl_pct, 9.78)

    def _assert_parity(self, policy, reference, atr, series):
        plan = policy.resolve(reference, atr=atr)
        spec = PolicySpec(**plan.policy_spec_fields())
        # Exactly what comparables.outcomes._replay does with a PolicySpec.
        through_fractions = simulate_trade(
            series, 0, spec.direction,
            reference * spec.stop_frac,
            reference * spec.target1_frac,
            reference * spec.target2_frac,
            spec.max_holding_days,
            slippage_bps=plan.slippage_bps,
        )
        direct = simulate_trade(series, 0, **plan.simulator_arguments())
        self.assertEqual(through_fractions.rule_fired, direct.rule_fired)
        self.assertEqual(through_fractions.exit_date, direct.exit_date)
        self.assertAlmostEqual(through_fractions.pnl_pct, direct.pnl_pct, places=6)
        self.assertEqual(spec.slug, policy.version)
        self.assertEqual(spec.max_holding_days, policy.max_hold_calendar_days)

    def test_event_swing_matches_through_the_policy_spec_fields(self):
        self._assert_parity(EVENT_SWING_14CAL_V1, 100.0, 2.0, self.SWING)

    def test_reversal_matches_through_the_policy_spec_fields(self):
        series = bars([
            (2, 50.0, 50.5, 49.5, 50.0),
            (3, 50.0, 51.0, 49.0, 50.5),
            (4, 50.5, 54.0, 50.0, 53.5),   # target 1 at 53
            (5, 53.0, 53.5, 46.0, 47.5),   # remainder stops at 47
        ])
        self._assert_parity(REVERSAL_5CAL_V1, 50.0, 2.0, series)

    def test_momentum_matches_through_the_policy_spec_fields(self):
        series = bars([
            (2, 200.0, 202.0, 198.0, 200.0),
            (3, 200.0, 205.0, 198.0, 204.0),
            (4, 204.0, 206.0, 175.0, 178.0),  # crosses the 180 overlay stop
        ])
        self._assert_parity(MOMENTUM_QUARTERLY_89CAL_V1, 200.0, None, series)

    def test_the_reversal_remainder_keeps_the_original_stop_and_times_out(self):
        """Spec Q §7: no target 2; the rest leaves at the time boundary."""
        series = bars([
            (2, 50.0, 50.5, 49.5, 50.0),
            (3, 50.0, 51.0, 49.0, 50.5),
            (4, 50.5, 54.0, 50.0, 53.5),   # target 1 at 53
            (5, 53.0, 53.5, 52.0, 53.0),
            (6, 53.0, 53.5, 52.0, 53.0),
            (9, 53.0, 53.5, 52.0, 53.0),   # first bar on/after entry + 5 days
        ])
        plan = REVERSAL_5CAL_V1.resolve(50.0, atr=2.0)
        result = simulate_trade(series, 0, **plan.simulator_arguments())
        self.assertEqual(result.rule_fired, "t1_then_time")
        self.assertEqual(result.exit_date, date(2026, 3, 9))

    def test_a_frozen_plan_reaches_the_simulator_through_the_same_contract(self):
        plan = SWINGTRADER_MEMO_TRADE_PARAMS_V1.resolve_frozen(
            entry_reference=100.0, stop_price=96.0,
            target_prices=(108.0, 112.0), max_holding_days=14,
        )
        spec = PolicySpec(**plan.policy_spec_fields())
        self.assertEqual(spec.stop_frac, 0.96)
        self.assertEqual(spec.target1_frac, 1.08)
        result = simulate_trade(self.SWING, 0, **plan.simulator_arguments())
        self.assertEqual(result.rule_fired, "t1_then_t2")


class ManifestSurfaceTests(unittest.TestCase):
    def test_the_policy_manifest_covers_every_registered_policy(self):
        manifest = policy_manifest()
        self.assertEqual(sorted(manifest), sorted(POLICIES))
        self.assertEqual(
            manifest["event_swing_14cal_v1"]["stop_atr_multiple"], 2.0,
            "a change to a multiple must show up in the manifest, so that it "
            "cannot run under an existing version identity",
        )


if __name__ == "__main__":
    unittest.main()
