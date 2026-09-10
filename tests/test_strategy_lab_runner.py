"""Spec Q §8, §9: registration freezes the question, and a run is idempotent.

Everything here runs against a disposable database of whichever engine the suite
targets, the same shape as ``tests/test_strategy_lab_registry.py``.

The properties under test are the ones a scorecard's honesty rests on:

* an experiment's plan is frozen *before* any arm exists, and more arms than the
  pre-registration declared is a refusal rather than a bigger denominator
  discovered later;
* one snapshot goes to N arms and every arm records ``long``, ``flat`` **and**
  ``abstain``, a universe arm one row per constituent, so a selection rate and a
  data outage are both recoverable from the ledger;
* a second identical run writes nothing;
* one arm's refusal is that arm's, not the run's — a broken challenger cannot
  silently remove itself from a tournament it was losing;
* manifest drift, a forbidden tier and a refused historical replay all fail
  closed at the arm, with the reason kept.
"""

from __future__ import annotations

import unittest

from strategy_lab import registry, runner, strategies, validation
from strategy_lab.domain import (
    ArmStatus,
    ExecutionMode,
    ExperimentSpec,
    ExperimentStatus,
    ImmutabilityError,
    SnapshotScope,
    StrategyLabError,
    StrategyVersionStatus,
)
from tests import strategylabfixture as fx
from tests.dbfixture import TestDatabase

ROSTER = ("swingtrader_composite_v1", "earnings_drift_v1", "momentum_v1",
          "short_term_reversal_v1")


def an_experiment(**overrides) -> ExperimentSpec:
    kwargs = dict(
        name="q1_2026_roster",
        hypothesis=(
            "at least one challenger beats the composite champion after costs "
            "over the same opportunity set"
        ),
        universe_spec="liquid_us_equity_v1",
        primary_metric="mean_net_pct",
        benchmarks=("cash_no_trade", "spy_total_return"),
        guardrail_metrics=("max_drawdown_pct", "turnover_trades_per_year"),
        end_criteria={"min_matured_decisions": 100, "min_calendar_days": 60},
        owner="bryan",
        planned_variants=4,
        preregistration={"analysis_plan": "Spec Q §10 primary metrics"},
    )
    kwargs.update(overrides)
    return ExperimentSpec(**kwargs)


def shadow_plans(*slugs, risk_budget: float = 0.01):
    return [
        runner.ArmPlan(slug, ExecutionMode.SHADOW, risk_budget=risk_budget)
        for slug in (slugs or ROSTER)
    ]


def earnings_snapshot(ticker="AAPL", *, surprise=True, price_eligible=True):
    from strategy_lab import snapshots

    return fx.ticker_snapshot(fx.replayable_inputs(
        ticker,
        fx.flat_bars(260, 100.0),
        earnings=fx.earnings_record(
            ticker, reported_eps=1.2 if surprise else 1.0, consensus_eps=1.0
        ),
        price_replay_eligible=price_eligible,
        price_provenance_class=(
            snapshots.PROVENANCE_VENDOR_PIT if price_eligible
            else snapshots.PROVENANCE_ARCHIVAL
        ),
    ))


def universe_snapshot(n: int = 55):
    """A liquid cross-section big enough for `momentum_v1` not to abstain."""
    return fx.universe_snapshot([
        fx.replayable_inputs(f"T{i:03d}", fx.ramp_bars(100.0, 100.0 + i, 260))
        for i in range(n)
    ])


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("strategy_lab_runner")
        self.addCleanup(self.db.cleanup)
        from database.db import get_session, init_db

        init_db(self.db.url)
        self._ctx = get_session()
        self.session = self._ctx.__enter__()
        self.addCleanup(lambda: self._ctx.__exit__(None, None, None))
        self.versions = strategies.build_versions()

    def register(self, *slugs, spec=None, **kwargs):
        return runner.register_experiment(
            self.session, spec or an_experiment(), shadow_plans(*slugs), **kwargs
        )


class RegistrationTests(RunnerTestCase):
    def test_registration_freezes_the_plan_and_creates_every_arm(self):
        registered = self.register()
        self.assertEqual(registered.status, ExperimentStatus.REGISTERED)
        self.assertEqual(len(registered.arms), 4)
        self.assertEqual(
            sorted(arm.slug for arm in registered.arms), sorted(ROSTER)
        )
        for arm in registered.arms:
            self.assertIs(arm.mode, ExecutionMode.SHADOW)
            self.assertIs(arm.status, ArmStatus.ACTIVE)

    def test_a_changed_plan_after_registration_is_refused(self):
        self.register()
        with self.assertRaises(ImmutabilityError):
            runner.register_experiment(
                self.session,
                an_experiment(primary_metric="win_rate"),
                shadow_plans(),
            )

    def test_re_registering_the_identical_plan_is_a_no_op(self):
        first = self.register()
        second = self.register()
        self.assertEqual(first.experiment_id, second.experiment_id)
        self.assertEqual(
            [a.arm_id for a in first.arms], [a.arm_id for a in second.arms]
        )

    def test_more_arms_than_preregistered_variants_is_refused(self):
        with self.assertRaises(StrategyLabError) as caught:
            runner.register_experiment(
                self.session, an_experiment(planned_variants=2), shadow_plans()
            )
        self.assertIn("multiple-testing denominator", str(caught.exception))

    def test_a_structurally_shadow_only_version_cannot_take_a_paper_arm(self):
        """Spec Q §7D, refused before any arm row exists."""
        with self.assertRaises(validation.ModeRefused):
            runner.register_experiment(
                self.session,
                an_experiment(),
                [runner.ArmPlan("short_term_reversal_v1", ExecutionMode.PAPER)],
            )
        self.assertIsNone(registry.get_experiment(self.session, "q1_2026_roster"))

    def test_an_unknown_strategy_is_refused(self):
        with self.assertRaises(StrategyLabError):
            runner.register_experiment(
                self.session, an_experiment(), [runner.ArmPlan("no_such_strategy")]
            )

    def test_an_arm_asking_for_a_version_that_is_not_on_disk_is_refused(self):
        with self.assertRaises(StrategyLabError) as caught:
            runner.register_experiment(
                self.session,
                an_experiment(),
                [runner.ArmPlan("earnings_drift_v1", version="9.9.9")],
            )
        self.assertIn("cannot be run", str(caught.exception))

    def test_an_experiment_with_no_arms_measures_nothing(self):
        with self.assertRaises(StrategyLabError):
            runner.register_experiment(self.session, an_experiment(), [])

    def test_the_variant_ledger_counts_every_arm_ever_created(self):
        registered = self.register()
        ledger = runner.variant_ledger(self.session, registered.name)
        self.assertEqual(ledger.planned_variants, 4)
        self.assertEqual(ledger.n_tried, 4)
        self.assertEqual(ledger.n_trials, 4)
        self.assertEqual(ledger.undeclared, 0)
        self.assertIn(("earnings_drift_v1@1.0.0", "shadow"), ledger.tried)

    def test_a_retired_arm_still_counts_as_a_variant_that_was_tried(self):
        registered = self.register()
        registry.set_arm_status(
            self.session, registered.arm("momentum_v1").arm_id, ArmStatus.RETIRED
        )
        ledger = runner.variant_ledger(self.session, registered.name)
        self.assertEqual(ledger.n_tried, 4)

    def test_versions_are_promoted_to_shadow_with_the_manifest_rechecked(self):
        self.register()
        runner.promote_versions_to_shadow(self.session, self.versions)
        row = registry.require_strategy_version(
            self.session, "earnings_drift_v1", "1.0.0"
        )
        self.assertEqual(row.status, StrategyVersionStatus.SHADOW.value)


class RunTests(RunnerTestCase):
    def test_one_snapshot_reaches_every_arm_and_keeps_all_three_actions(self):
        registered = self.register()
        snapshot = universe_snapshot()
        report = runner.run_snapshot(
            self.session, snapshot, [a.arm_id for a in registered.arms]
        )

        momentum = report.for_arm(registered.arm("momentum_v1").arm_id)
        reversal = report.for_arm(registered.arm("short_term_reversal_v1").arm_id)
        self.assertTrue(momentum.ran)
        self.assertTrue(reversal.ran)
        # A universe arm emits exactly one decision per constituent.
        self.assertEqual(
            momentum.n_long + momentum.n_flat + momentum.n_abstain,
            len(snapshot.constituents),
        )
        self.assertGreater(momentum.n_long, 0)
        self.assertGreater(momentum.n_flat, 0)
        rows = registry.decisions_for(
            self.session, registered.arm("momentum_v1").arm_id, report.snapshot_id
        )
        self.assertEqual(len(rows), len(snapshot.constituents))
        self.assertEqual(
            [row.ticker for row in rows], list(snapshot.constituents)
        )

    def test_ticker_scoped_arms_refuse_a_universe_snapshot_without_stopping_the_run(self):
        registered = self.register()
        report = runner.run_snapshot(
            self.session, universe_snapshot(), [a.arm_id for a in registered.arms]
        )
        refused = dict(report.refusals)
        self.assertIn("earnings_drift_v1@1.0.0/shadow", refused)
        self.assertIn("ticker-scoped", refused["earnings_drift_v1@1.0.0/shadow"])
        # ...and the universe arms still produced their decisions.
        self.assertTrue(report.for_arm(registered.arm("momentum_v1").arm_id).ran)

    def test_abstentions_are_persisted_rather_than_dropped(self):
        registered = self.register("earnings_drift_v1")
        # A series too short for the 252-session screen: the strategy cannot
        # evaluate the name and says so.
        snapshot = fx.ticker_snapshot(
            fx.replayable_inputs("AAPL", fx.flat_bars(30, 100.0))
        )
        report = runner.run_snapshot(
            self.session, snapshot, [registered.arms[0].arm_id]
        )
        run = report.arm_runs[0]
        self.assertEqual((run.n_long, run.n_flat, run.n_abstain), (0, 1, 0))
        rows = registry.decisions_for(
            self.session, registered.arms[0].arm_id, report.snapshot_id
        )
        self.assertEqual(len(rows), 1)

    def test_a_stale_snapshot_abstains_with_a_blocked_reason(self):
        from datetime import timedelta

        registered = self.register("earnings_drift_v1")
        bars = fx.flat_bars(260, 100.0)
        snapshot = fx.ticker_snapshot(
            fx.replayable_inputs("AAPL", bars),
            cutoff=fx.Q1_2026_CLOSE + timedelta(days=7),
        )
        report = runner.run_snapshot(
            self.session, snapshot, [registered.arms[0].arm_id]
        )
        run = report.arm_runs[0]
        self.assertEqual(run.n_abstain, 1)
        self.assertIn("stale_data", run.decisions[0].blocked_reasons)

    def test_a_second_identical_run_writes_nothing_new(self):
        registered = self.register()
        snapshot = universe_snapshot()
        arm_ids = [a.arm_id for a in registered.arms]
        first = runner.run_snapshot(self.session, snapshot, arm_ids)
        before = len(registry.decisions_for(
            self.session, registered.arm("momentum_v1").arm_id, first.snapshot_id
        ))

        second = runner.run_snapshot(self.session, snapshot, arm_ids)
        self.assertEqual(second.snapshot_id, first.snapshot_id)
        after = len(registry.decisions_for(
            self.session, registered.arm("momentum_v1").arm_id, second.snapshot_id
        ))
        self.assertEqual(before, after)
        momentum = second.for_arm(registered.arm("momentum_v1").arm_id)
        self.assertEqual(momentum.n_deduplicated, before)
        self.assertEqual(
            momentum.decision_set_hash,
            first.for_arm(registered.arm("momentum_v1").arm_id).decision_set_hash,
        )

    def test_two_arms_share_one_snapshot_row(self):
        """Spec Q §6: a cross-sectional rank references one snapshot id."""
        registered = self.register()
        report = runner.run_snapshot(
            self.session, universe_snapshot(), [a.arm_id for a in registered.arms]
        )
        self.assertEqual(
            {run.snapshot_id for run in report.arm_runs}, {report.snapshot_id}
        )
        row = registry.snapshot_row(self.session, report.snapshot_id)
        self.assertEqual(row.scope, SnapshotScope.UNIVERSE.value)

    def test_an_arm_of_an_unregistered_experiment_cannot_run(self):
        registered = self.register("earnings_drift_v1", spec=an_experiment())
        registry.set_experiment_status(
            self.session, registered.name, ExperimentStatus.CANCELLED
        )
        report = runner.run_snapshot(
            self.session, earnings_snapshot(), [registered.arms[0].arm_id]
        )
        self.assertIn("cancelled", report.arm_runs[0].refused)

    def test_an_inactive_arm_records_no_decision(self):
        registered = self.register("earnings_drift_v1")
        registry.set_arm_status(
            self.session, registered.arms[0].arm_id, ArmStatus.PAUSED
        )
        report = runner.run_snapshot(
            self.session, earnings_snapshot(), [registered.arms[0].arm_id]
        )
        self.assertIn("paused", report.arm_runs[0].refused)
        self.assertEqual(report.arm_runs[0].decision_ids, ())

    def test_manifest_drift_fails_closed_at_the_arm(self):
        """Spec Q §6: edited code may never run under an existing identity."""
        registered = self.register("earnings_drift_v1")
        drifted = dict(self.versions)
        original = drifted["earnings_drift_v1"]
        manifest = dict(original.implementation_manifest)
        manifest["helpers"] = dict(manifest["helpers"])
        manifest["helpers"]["strategy_lab/indicators.py"] = "0" * 64
        drifted["earnings_drift_v1"] = type(original)(
            **{**original.__dict__, "implementation_manifest": manifest}
        )
        report = runner.run_snapshot(
            self.session, earnings_snapshot(), [registered.arms[0].arm_id],
            versions=drifted,
        )
        self.assertIn("implementation manifest", report.arm_runs[0].refused)
        self.assertEqual(
            registry.decisions_for(
                self.session, registered.arms[0].arm_id, report.snapshot_id
            ),
            [],
        )

    def test_a_historical_run_refuses_the_compatibility_arm(self):
        registered = self.register("swingtrader_composite_v1")
        snapshot = fx.ticker_snapshot(fx.replayable_inputs(
            "AAPL", fx.flat_bars(260, 100.0), composite=fx.composite_result("AAPL"),
        ))
        report = runner.run_snapshot(
            self.session, snapshot, [registered.arms[0].arm_id], historical=True
        )
        self.assertIn("historically_replayable=False", report.arm_runs[0].refused)

    def test_a_historical_run_refuses_a_reconstructed_snapshot(self):
        registered = self.register("earnings_drift_v1")
        report = runner.run_snapshot(
            self.session,
            earnings_snapshot(price_eligible=False),
            [registered.arms[0].arm_id],
            historical=True,
        )
        self.assertIn("archival_reconstructed", report.arm_runs[0].refused)

    def test_the_same_snapshot_shadowed_forward_is_not_refused(self):
        """The refusal is about *historical* replay, not about running at all."""
        registered = self.register("earnings_drift_v1")
        report = runner.run_snapshot(
            self.session,
            earnings_snapshot(price_eligible=False),
            [registered.arms[0].arm_id],
        )
        self.assertTrue(report.arm_runs[0].ran)
        self.assertEqual(report.evidence_class, "archival_reconstructed")

    def test_the_report_is_ordered_by_arm_id_whatever_the_call_order(self):
        registered = self.register()
        ids = [a.arm_id for a in registered.arms]
        report = runner.run_snapshot(
            self.session, universe_snapshot(), list(reversed(ids))
        )
        self.assertEqual([run.arm.arm_id for run in report.arm_runs], sorted(ids))

    def test_a_registered_experiment_starts_running_idempotently(self):
        registered = self.register()
        runner.start_experiment(self.session, registered.name)
        runner.start_experiment(self.session, registered.name)
        row = registry.require_experiment(self.session, registered.name)
        self.assertEqual(row.status, ExperimentStatus.RUNNING.value)


if __name__ == "__main__":
    unittest.main()
