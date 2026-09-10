"""Spec Q §6, §11, §12: a decision is stable while an execution is judged fresh.

The identity test the agent brief asks for is
:meth:`ExecutionEligibilityTests.test_the_same_decision_is_blocked_after_the_portfolio_changes`:
one decision, two attempts against two portfolio contexts, one allowed and one
refused, and the ``strategy_decisions`` row untouched and un-duplicated in
between. That is what makes "a decision does not go stale merely because
portfolio state changed" a property of the data rather than a claim in a
docstring.

The rest is the shadow executor's own contract: it refuses any arm that is not
``shadow``, it walks the same §12 state machine a live order will, it sizes from
the arm's virtual budget and the plan's stop distance and records every cap that
bound the result, and it leaves an immature position **open** instead of closing
it at whatever the last available bar happened to be.
"""

from __future__ import annotations

import unittest
from datetime import datetime

from strategy_lab import registry, replay, runner, shadow, strategies
from strategy_lab.domain import ExecutionMode, ExecutionState, ExperimentSpec
from tests import strategylabfixture as fx
from tests.dbfixture import TestDatabase

COSTS = replay.CostAssumptions(slippage_bps=10.0, half_spread_bps=5.0)


def an_experiment(**overrides) -> ExperimentSpec:
    kwargs = dict(
        name="shadow_identity",
        hypothesis="a decision survives a portfolio change",
        universe_spec="liquid_us_equity_v1",
        primary_metric="mean_net_pct",
        benchmarks=("cash_no_trade",),
        end_criteria={"min_matured_decisions": 100},
        owner="bryan",
        planned_variants=4,
    )
    kwargs.update(overrides)
    return ExperimentSpec(**kwargs)


def a_context(**overrides) -> shadow.PortfolioContext:
    kwargs = dict(
        as_of_utc=datetime(2026, 3, 31, 21, 0),
        equity=100_000.0,
        max_open_positions=5,
        max_daily_notional=250_000.0,
        max_position_fraction=0.2,
    )
    kwargs.update(overrides)
    return shadow.PortfolioContext(**kwargs)


class ShadowTestCase(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("strategy_lab_shadow")
        self.addCleanup(self.db.cleanup)
        from database.db import get_session, init_db

        init_db(self.db.url)
        self._ctx = get_session()
        self.session = self._ctx.__enter__()
        self.addCleanup(lambda: self._ctx.__exit__(None, None, None))
        self.versions = strategies.build_versions()

    def an_earnings_arm(self, mode=ExecutionMode.SHADOW, risk_budget=0.01):
        registered = runner.register_experiment(
            self.session,
            an_experiment(),
            [runner.ArmPlan("earnings_drift_v1", mode, risk_budget=risk_budget)],
        )
        return registered.arms[0]

    def a_long_decision(self, arm, ticker="AAPL"):
        snapshot = fx.ticker_snapshot(fx.replayable_inputs(
            ticker,
            fx.flat_bars(260, 100.0),
            earnings=fx.earnings_record(
                ticker, reported_eps=1.2, consensus_eps=1.0
            ),
        ))
        report = runner.run_snapshot(self.session, snapshot, [arm.arm_id])
        run = report.arm_runs[0]
        self.assertTrue(run.ran, run.refused)
        return snapshot, run.decision_ids[0]

    def a_plan(self, snapshot, ticker="AAPL"):
        return replay.build_plan(
            self.versions["earnings_drift_v1"], snapshot, ticker,
            entry_reference=100.0,
        )


class ExecutionEligibilityTests(ShadowTestCase):
    def test_the_same_decision_is_blocked_after_the_portfolio_changes(self):
        arm = self.an_earnings_arm()
        snapshot, decision_id = self.a_long_decision(arm)
        plan = self.a_plan(snapshot)
        before = registry.decision_row(self.session, decision_id)
        stored_hash, stored_id = before.decision_hash, before.id

        allowed = shadow.open_execution(
            self.session, arm.arm_id, decision_id, plan, a_context()
        )
        self.assertFalse(allowed.blocked)
        self.assertIs(allowed.status, ExecutionState.PROPOSED)

        # The portfolio now already holds the name. Same decision, same
        # snapshot, same strategy version — a different answer about execution.
        blocked = shadow.open_execution(
            self.session, arm.arm_id, decision_id, plan,
            a_context(open_tickers=("AAPL",)),
        )
        self.assertTrue(blocked.blocked)
        self.assertEqual(blocked.blocked_reason, shadow.BLOCK_TICKER_ALREADY_HELD)
        self.assertNotEqual(blocked.context_hash, allowed.context_hash)

        after = registry.decision_row(self.session, decision_id)
        self.assertEqual(after.id, stored_id)
        self.assertEqual(after.decision_hash, stored_hash)
        self.assertEqual(
            len(registry.decisions_for(
                self.session, arm.arm_id, after.snapshot_id
            )),
            1,
            "a blocked execution must not duplicate the decision it was for",
        )
        self.assertEqual(len(registry.executions_for_arm(self.session, arm.arm_id)), 2)

    def test_re_running_a_block_against_the_same_context_is_idempotent(self):
        arm = self.an_earnings_arm()
        snapshot, decision_id = self.a_long_decision(arm)
        plan = self.a_plan(snapshot)
        context = a_context(open_tickers=("AAPL",))
        first = shadow.open_execution(
            self.session, arm.arm_id, decision_id, plan, context
        )
        second = shadow.open_execution(
            self.session, arm.arm_id, decision_id, plan, context
        )
        self.assertEqual(first.execution_id, second.execution_id)
        self.assertEqual(len(registry.executions_for_arm(self.session, arm.arm_id)), 1)

    def test_a_retry_reuses_the_one_non_terminal_execution(self):
        """Spec Q §12 invariant 6: one decision never gets two placements."""
        arm = self.an_earnings_arm()
        snapshot, decision_id = self.a_long_decision(arm)
        plan = self.a_plan(snapshot)
        first = shadow.open_execution(
            self.session, arm.arm_id, decision_id, plan, a_context()
        )
        retry = shadow.open_execution(
            self.session, arm.arm_id, decision_id, plan,
            a_context(equity=250_000.0),
        )
        self.assertEqual(first.execution_id, retry.execution_id)
        self.assertEqual(len(registry.executions_for_arm(self.session, arm.arm_id)), 1)

    def test_every_execution_side_block_is_reachable_and_named(self):
        arm = self.an_earnings_arm()
        snapshot, decision_id = self.a_long_decision(arm)
        plan = self.a_plan(snapshot)
        decision = shadow._decision_of(
            registry.decision_row(self.session, decision_id)
        )
        cases = {
            shadow.BLOCK_TICKER_ALREADY_HELD: (a_context(open_tickers=("AAPL",)), 0.01),
            shadow.BLOCK_MAX_OPEN_POSITIONS: (
                a_context(open_tickers=("MSFT", "NVDA"), max_open_positions=2), 0.01
            ),
            shadow.BLOCK_NO_RISK_BUDGET: (a_context(), 0.0),
            shadow.BLOCK_DAILY_NOTIONAL_CAP: (
                a_context(max_daily_notional=1_000.0, daily_notional_used=1_000.0),
                0.01,
            ),
        }
        for expected, (context, budget) in cases.items():
            with self.subTest(expected):
                verdict = shadow.assess(
                    decision, plan, context, arm_risk_budget=budget
                )
                self.assertFalse(verdict.allowed)
                self.assertEqual(verdict.blocked_reason, expected)
                self.assertIn(verdict.blocked_reason, shadow.BLOCK_REASONS)

    def test_a_flat_decision_has_no_execution_to_assess(self):
        arm = self.an_earnings_arm()
        snapshot = fx.ticker_snapshot(fx.replayable_inputs(
            "AAPL", fx.flat_bars(260, 100.0),
            earnings=fx.earnings_record("AAPL", reported_eps=1.0, consensus_eps=1.0),
        ))
        report = runner.run_snapshot(self.session, snapshot, [arm.arm_id])
        decision_id = report.arm_runs[0].decision_ids[0]
        with self.assertRaises(shadow.ShadowRefused):
            shadow.open_execution(
                self.session, arm.arm_id, decision_id, self.a_plan(snapshot),
                a_context(),
            )


class ModeTests(ShadowTestCase):
    def test_the_shadow_executor_refuses_a_paper_arm(self):
        """Spec Q §11: an arm's mode selects its adapter; shadow is not paper."""
        registered = runner.register_experiment(
            self.session,
            an_experiment(),
            [runner.ArmPlan("earnings_drift_v1", ExecutionMode.PAPER, risk_budget=0.01)],
        )
        arm = registered.arms[0]
        snapshot = fx.ticker_snapshot(fx.replayable_inputs(
            "AAPL", fx.flat_bars(260, 100.0),
            earnings=fx.earnings_record("AAPL", reported_eps=1.2, consensus_eps=1.0),
        ))
        report = runner.run_snapshot(self.session, snapshot, [arm.arm_id])
        decision_id = report.arm_runs[0].decision_ids[0]
        with self.assertRaises(shadow.ShadowRefused) as caught:
            shadow.open_execution(
                self.session, arm.arm_id, decision_id, self.a_plan(snapshot),
                a_context(),
            )
        self.assertIn("shadow arms only", str(caught.exception))

    def test_the_row_carries_the_arms_immutable_mode(self):
        arm = self.an_earnings_arm()
        snapshot, decision_id = self.a_long_decision(arm)
        execution = shadow.open_execution(
            self.session, arm.arm_id, decision_id, self.a_plan(snapshot), a_context()
        )
        row = registry.execution_row(self.session, execution.execution_id)
        self.assertEqual(row.mode, ExecutionMode.SHADOW.value)


class SizingTests(ShadowTestCase):
    def test_risk_sizing_is_arithmetic_and_names_the_cap_that_bound_it(self):
        # equity 100,000 x budget 0.01 x risk fraction 1.0 = $1,000 of risk.
        # A 2-ATR stop on a $100 name with ATR 2.0 is $96, so the risk per share
        # is 4% and the unconstrained notional is 1,000 / 0.04 = $25,000 —
        # above the 20% position cap of $20,000, which therefore binds.
        from strategy_lab.execution_policy import EVENT_SWING_14CAL_V1

        resolved = EVENT_SWING_14CAL_V1.resolve(100.0, atr=2.0)
        sized = shadow.size_position(
            resolved, a_context(), arm_risk_budget=0.01, position_risk_pct=1.0
        )
        self.assertAlmostEqual(sized.risk_pct, 4.0)
        self.assertAlmostEqual(sized.notional, 20_000.0)
        self.assertEqual(sized.caps_applied, ("max_position_fraction",))
        self.assertAlmostEqual(sized.quantity, 200.0)

    def test_a_daily_notional_cap_binds_after_the_position_cap(self):
        from strategy_lab.execution_policy import EVENT_SWING_14CAL_V1

        resolved = EVENT_SWING_14CAL_V1.resolve(100.0, atr=2.0)
        sized = shadow.size_position(
            resolved,
            a_context(max_daily_notional=30_000.0, daily_notional_used=25_000.0),
            arm_risk_budget=0.01,
            position_risk_pct=1.0,
        )
        self.assertAlmostEqual(sized.notional, 5_000.0)
        self.assertEqual(
            sized.caps_applied, ("max_position_fraction", "max_daily_notional")
        )


class LifecycleTests(ShadowTestCase):
    def _shadow_one(self, forward):
        arm = self.an_earnings_arm()
        snapshot, decision_id = self.a_long_decision(arm)
        executions, outcomes = shadow.execute_arm(
            self.session, arm.arm_id, self.versions["earnings_drift_v1"], snapshot,
            [decision_id], {"AAPL": forward}, context=a_context(), costs=COSTS,
        )
        return arm, executions[0], outcomes[0]

    def test_a_matured_trade_walks_to_closed_with_its_costs_recorded(self):
        arm, execution, outcome = self._shadow_one(fx.rising_forward(16))
        self.assertTrue(outcome.matured)
        row = registry.execution_row(self.session, execution.execution_id)
        self.assertEqual(row.status, ExecutionState.CLOSED.value)
        self.assertIsNotNone(row.closed_at)
        self.assertGreater(row.costs, 0)
        # net = notional x net_pct / 100, exactly as the scoreboard reads it back
        self.assertAlmostEqual(
            row.realized_pnl, row.notional * outcome.net_pct / 100.0, places=6
        )
        self.assertAlmostEqual(
            row.costs,
            row.notional * (outcome.gross_pct - outcome.net_pct) / 100.0,
            places=6,
        )

    def test_an_immature_trade_is_filled_and_left_open(self):
        """Spec Q §9: an incomplete position is not a closed one at zero."""
        arm, execution, outcome = self._shadow_one(fx.rising_forward(4))
        self.assertFalse(outcome.matured)
        row = registry.execution_row(self.session, execution.execution_id)
        self.assertEqual(row.status, ExecutionState.PROTECTED.value)
        self.assertIsNone(row.closed_at)
        self.assertIsNone(row.realized_pnl)

    def test_the_fill_walks_the_specification_state_machine(self):
        arm, execution, _ = self._shadow_one(fx.rising_forward(4))
        row = registry.execution_row(self.session, execution.execution_id)
        self.assertEqual(row.status, ExecutionState.PROTECTED.value)
        self.assertIsNotNone(row.filled_at)
        self.assertIsNotNone(row.filled_entry_price)

    def test_settling_twice_changes_nothing(self):
        arm = self.an_earnings_arm()
        snapshot, decision_id = self.a_long_decision(arm)
        forward = {"AAPL": fx.rising_forward(16)}
        first, _ = shadow.execute_arm(
            self.session, arm.arm_id, self.versions["earnings_drift_v1"], snapshot,
            [decision_id], forward, context=a_context(), costs=COSTS,
        )
        second, _ = shadow.execute_arm(
            self.session, arm.arm_id, self.versions["earnings_drift_v1"], snapshot,
            [decision_id], forward, context=a_context(), costs=COSTS,
        )
        self.assertEqual(first[0].execution_id, second[0].execution_id)
        self.assertEqual(len(registry.executions_for_arm(self.session, arm.arm_id)), 1)

    def test_a_blocked_decision_is_never_filled(self):
        arm = self.an_earnings_arm()
        snapshot, decision_id = self.a_long_decision(arm)
        executions, _ = shadow.execute_arm(
            self.session, arm.arm_id, self.versions["earnings_drift_v1"], snapshot,
            [decision_id], {"AAPL": fx.rising_forward(16)},
            context=a_context(open_tickers=("AAPL",)), costs=COSTS,
        )
        row = registry.execution_row(self.session, executions[0].execution_id)
        self.assertEqual(row.status, ExecutionState.RISK_REJECTED.value)
        self.assertIsNone(row.filled_at)
        self.assertEqual(row.blocked_reason, shadow.BLOCK_TICKER_ALREADY_HELD)

    def test_the_context_advances_as_positions_open_within_a_run(self):
        """The second name of a one-position portfolio is blocked, and says so."""
        registered = runner.register_experiment(
            self.session,
            an_experiment(),
            [runner.ArmPlan("momentum_v1", ExecutionMode.SHADOW, risk_budget=0.01)],
        )
        arm = registered.arms[0]
        snapshot = fx.universe_snapshot([
            fx.replayable_inputs(f"T{i:03d}", fx.ramp_bars(100.0, 100.0 + i, 260))
            for i in range(55)
        ])
        report = runner.run_snapshot(self.session, snapshot, [arm.arm_id])
        run = report.arm_runs[0]
        self.assertTrue(run.ran, run.refused)
        longs = [
            decision_id
            for decision_id, decision in zip(run.decision_ids, run.decisions)
            if decision.action.value == "long"
        ]
        self.assertGreaterEqual(len(longs), 2)
        forward = {
            decision.ticker: fx.rising_forward(95, start=200.0, step=0.5)
            for decision in run.decisions
        }
        executions, _ = shadow.execute_arm(
            self.session, arm.arm_id, self.versions["momentum_v1"], snapshot,
            longs, forward,
            context=a_context(max_open_positions=1), costs=COSTS,
        )
        self.assertEqual(len(executions), len(longs))
        self.assertFalse(executions[0].blocked)
        self.assertTrue(all(e.blocked for e in executions[1:]))
        self.assertEqual(
            executions[1].blocked_reason, shadow.BLOCK_MAX_OPEN_POSITIONS
        )

    def test_a_decision_from_another_snapshot_is_refused(self):
        """Spec Q §6: no cross-cutoff assembly, even in a shadow run."""
        arm = self.an_earnings_arm()
        snapshot, decision_id = self.a_long_decision(arm, "AAPL")
        other = fx.ticker_snapshot(fx.replayable_inputs(
            "AAPL", fx.flat_bars(260, 101.0),
            earnings=fx.earnings_record("AAPL", reported_eps=1.2, consensus_eps=1.0),
        ))
        with self.assertRaises(shadow.ShadowRefused) as caught:
            shadow.execute_arm(
                self.session, arm.arm_id, self.versions["earnings_drift_v1"],
                other, [decision_id], {"AAPL": fx.rising_forward(16)},
                context=a_context(), costs=COSTS,
            )
        self.assertIn("was made from snapshot", str(caught.exception))


class ContextTests(unittest.TestCase):
    def test_the_context_hash_changes_with_the_portfolio(self):
        base = a_context()
        self.assertEqual(base.context_hash, a_context().context_hash)
        self.assertNotEqual(
            base.context_hash, a_context(open_tickers=("AAPL",)).context_hash
        )

    def test_open_tickers_are_normalised_so_two_spellings_hash_alike(self):
        self.assertEqual(
            a_context(open_tickers=("aapl", "MSFT")).context_hash,
            a_context(open_tickers=("MSFT", "AAPL")).context_hash,
        )

    def test_with_position_advances_both_the_names_and_the_notional(self):
        after = a_context().with_position("AAPL", notional=20_000.0)
        self.assertEqual(after.open_tickers, ("AAPL",))
        self.assertEqual(after.daily_notional_used, 20_000.0)


if __name__ == "__main__":
    unittest.main()
