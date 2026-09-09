"""Spec Q §8: the registry service boundary, on whichever engine the run targets.

Two classes.

:class:`RegistryTests` exercises the service rules — immutability, lifecycle,
idempotency, promotion binding — against one disposable database of the engine
the suite is running on. CI runs the suite twice, so both engines see them.

:class:`SchemaConstraintTests` proves the *database* invariants, and does not
wait for the matrix to cover the second engine: it runs against the engine the
suite targets **and** against Postgres whenever one is reachable, in the same
run. A partial unique index is the whole mechanism behind "one globally active
live arm" and "one non-terminal execution per decision"; an invariant that
holds on only one of the two engines the deploy can use is not an invariant.
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import models
from database.schema import ensure_schema
from strategy_lab import registry
from strategy_lab.domain import (
    ArmStatus,
    DecisionAction,
    ExecutionMode,
    ExecutionState,
    ExperimentStatus,
    ImmutabilityError,
    InvalidTransition,
    PromotionRefused,
    SnapshotScope,
    StrategyLabError,
    StrategyVersionStatus,
)
from tests.dbfixture import TestDatabase, postgres_url, target_backend
from utils.timeutils import utcnow_naive
from tests.test_strategy_lab_domain import (
    CUTOFF,
    an_experiment_spec,
    a_risk_plan,
    a_universe_snapshot,
    a_version_spec,
)
from strategy_lab.domain import StrategyDecision


def a_decision(snapshot, **overrides) -> StrategyDecision:
    kwargs = dict(
        strategy_slug="momentum_v1",
        strategy_version="1.0.0",
        snapshot_hash=snapshot.content_hash,
        ticker="AAPL",
        action=DecisionAction.LONG,
        reason_codes=("liquidity_ok", "top_decile"),
        signal_strength=0.82,
        risk_plan=a_risk_plan(),
    )
    kwargs.update(overrides)
    return StrategyDecision(**kwargs)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("strategy_lab")
        self.addCleanup(self.db.cleanup)
        from database.db import get_session, init_db

        init_db(self.db.url)
        self._ctx = get_session()
        self.session = self._ctx.__enter__()
        self.addCleanup(lambda: self._ctx.__exit__(None, None, None))

    # -- fixtures ---------------------------------------------------------- #

    def registered(self, *, mode=ExecutionMode.SHADOW, risk_budget=10_000.0):
        registry.register_strategy_version(self.session, a_version_spec())
        registry.register_experiment(self.session, an_experiment_spec())
        return registry.create_arm(
            self.session, "q1_momentum_vs_composite", "momentum_v1", "1.0.0", mode,
            risk_budget=risk_budget,
        )

    # -- strategy versions ------------------------------------------------- #

    def test_registering_the_same_version_twice_is_idempotent(self):
        first = registry.register_strategy_version(self.session, a_version_spec())
        second = registry.register_strategy_version(self.session, a_version_spec())
        self.assertEqual(first.id, second.id)
        self.assertEqual(
            self.session.query(models.StrategyVersion).count(), 1
        )

    def test_a_rule_change_under_the_same_version_is_refused(self):
        registry.register_strategy_version(self.session, a_version_spec())
        with self.assertRaises(ImmutabilityError) as caught:
            registry.register_strategy_version(
                self.session, a_version_spec(config={"formation_sessions": 126})
            )
        message = str(caught.exception)
        self.assertIn("config", message)
        self.assertIn("new version", message)

    def test_a_manifest_change_under_the_same_version_is_refused(self):
        """Spec Q §6: changed code may never run under an existing identity."""
        registry.register_strategy_version(self.session, a_version_spec())
        with self.assertRaises(ImmutabilityError) as caught:
            registry.register_strategy_version(
                self.session,
                a_version_spec(implementation_manifest={"strategy_source_sha": "b" * 64}),
            )
        self.assertIn("implementation_manifest", str(caught.exception))

    def test_a_new_version_number_carries_the_change(self):
        registry.register_strategy_version(self.session, a_version_spec())
        changed = registry.register_strategy_version(
            self.session, a_version_spec(version="1.1.0", config={"formation_sessions": 126})
        )
        self.assertEqual(changed.version, "1.1.0")
        self.assertEqual(self.session.query(models.StrategyVersion).count(), 2)

    def test_a_stored_version_round_trips_to_the_same_hash(self):
        spec = a_version_spec()
        row = registry.register_strategy_version(self.session, spec)
        self.assertEqual(registry.load_strategy_version(row).content_hash, spec.content_hash)
        self.assertEqual(row.implementation_manifest_hash, spec.manifest_hash)

    def test_status_moves_but_content_does_not(self):
        row = registry.register_strategy_version(self.session, a_version_spec())
        before = row.content_hash
        registry.set_strategy_version_status(
            self.session, "momentum_v1", "1.0.0", StrategyVersionStatus.SHADOW
        )
        self.assertEqual(row.status, "shadow")
        self.assertEqual(row.content_hash, before)

        with self.assertRaises(InvalidTransition):
            registry.set_strategy_version_status(
                self.session, "momentum_v1", "1.0.0", StrategyVersionStatus.LIVE_ELIGIBLE
            )

    def test_an_unknown_version_is_a_named_refusal(self):
        with self.assertRaises(registry.NotFound):
            registry.require_strategy_version(self.session, "nope", "1.0.0")

    # -- experiments ------------------------------------------------------- #

    def test_registration_freezes_the_analysis_plan(self):
        registry.register_experiment(self.session, an_experiment_spec())
        with self.assertRaises(ImmutabilityError) as caught:
            registry.register_experiment(
                self.session, an_experiment_spec(primary_metric="win_rate")
            )
        self.assertIn("frozen", str(caught.exception))

    def test_a_draft_experiment_may_still_be_edited(self):
        row = registry.register_experiment(
            self.session, an_experiment_spec(), status=ExperimentStatus.DRAFT
        )
        self.assertIsNone(row.registered_at)
        revised = registry.register_experiment(
            self.session, an_experiment_spec(primary_metric="mean_r"),
            status=ExperimentStatus.DRAFT,
        )
        self.assertEqual(revised.id, row.id)
        self.assertEqual(revised.primary_metric, "mean_r")

    def test_registering_the_same_plan_twice_is_idempotent(self):
        first = registry.register_experiment(self.session, an_experiment_spec())
        second = registry.register_experiment(self.session, an_experiment_spec())
        self.assertEqual(first.id, second.id)
        self.assertIsNotNone(first.registered_at)

    def test_an_experiment_round_trips(self):
        spec = an_experiment_spec()
        row = registry.register_experiment(self.session, spec)
        loaded = registry.load_experiment(row)
        self.assertEqual(loaded.content_hash, spec.content_hash)
        self.assertEqual(loaded.benchmarks, spec.benchmarks)

    def test_the_experiment_lifecycle_is_enforced(self):
        registry.register_experiment(self.session, an_experiment_spec())
        name = "q1_momentum_vs_composite"
        running = registry.set_experiment_status(self.session, name, ExperimentStatus.RUNNING)
        self.assertIsNotNone(running.started_at)
        registry.set_experiment_status(self.session, name, ExperimentStatus.EVALUATING)
        completed = registry.set_experiment_status(self.session, name, ExperimentStatus.COMPLETED)
        self.assertIsNotNone(completed.ended_at)
        with self.assertRaises(InvalidTransition):
            registry.set_experiment_status(self.session, name, ExperimentStatus.RUNNING)

    # -- arms -------------------------------------------------------------- #

    def test_an_arm_is_created_inactive_and_creation_is_idempotent(self):
        arm = self.registered()
        self.assertEqual(arm.status, ArmStatus.INACTIVE.value)
        again = registry.create_arm(
            self.session, "q1_momentum_vs_composite", "momentum_v1", "1.0.0",
            ExecutionMode.SHADOW,
        )
        self.assertEqual(arm.id, again.id)

    def test_arms_of_different_modes_are_different_arms(self):
        shadow = self.registered()
        paper = registry.create_arm(
            self.session, "q1_momentum_vs_composite", "momentum_v1", "1.0.0",
            ExecutionMode.PAPER,
        )
        self.assertNotEqual(shadow.id, paper.id)
        self.assertEqual(paper.mode, "paper")

    def test_activation_is_idempotent(self):
        arm = self.registered()
        first = registry.activate_arm(self.session, arm.id)
        started = first.started_at
        second = registry.activate_arm(self.session, arm.id)
        self.assertEqual(second.status, ArmStatus.ACTIVE.value)
        self.assertEqual(second.started_at, started)

    def test_no_arm_runs_before_its_experiment_is_registered(self):
        registry.register_strategy_version(self.session, a_version_spec())
        registry.register_experiment(
            self.session, an_experiment_spec(), status=ExperimentStatus.DRAFT
        )
        arm = registry.create_arm(
            self.session, "q1_momentum_vs_composite", "momentum_v1", "1.0.0",
            ExecutionMode.SHADOW,
        )
        with self.assertRaises(StrategyLabError) as caught:
            registry.activate_arm(self.session, arm.id)
        self.assertIn("registered", str(caught.exception))

    def test_a_retired_arm_cannot_come_back(self):
        arm = self.registered()
        registry.set_arm_status(self.session, arm.id, ArmStatus.RETIRED)
        with self.assertRaises(InvalidTransition):
            registry.activate_arm(self.session, arm.id)

    def test_only_one_live_arm_is_active_and_the_refusal_names_the_champion(self):
        first = self.registered(mode=ExecutionMode.LIVE)
        registry.activate_arm(self.session, first.id)

        registry.register_strategy_version(self.session, a_version_spec(version="1.1.0"))
        second = registry.create_arm(
            self.session, "q1_momentum_vs_composite", "momentum_v1", "1.1.0",
            ExecutionMode.LIVE,
        )
        with self.assertRaises(StrategyLabError) as caught:
            registry.activate_arm(self.session, second.id)
        self.assertIn(f"arm {first.id}", str(caught.exception))
        self.assertEqual(registry.active_live_arm(self.session).id, first.id)

    def test_replacing_the_champion_swaps_exactly_one(self):
        first = self.registered(mode=ExecutionMode.LIVE)
        registry.activate_arm(self.session, first.id)
        registry.register_strategy_version(self.session, a_version_spec(version="1.1.0"))
        second = registry.create_arm(
            self.session, "q1_momentum_vs_composite", "momentum_v1", "1.1.0",
            ExecutionMode.LIVE,
        )

        registry.replace_live_champion(self.session, second.id)
        self.assertEqual(registry.active_live_arm(self.session).id, second.id)
        self.assertEqual(
            self.session.get(models.ExperimentArm, first.id).status, ArmStatus.INACTIVE.value
        )
        # Idempotent: replacing the champion with itself changes nothing.
        registry.replace_live_champion(self.session, second.id)
        self.assertEqual(registry.active_live_arm(self.session).id, second.id)

    def test_only_a_live_arm_can_be_the_live_champion(self):
        shadow = self.registered()
        with self.assertRaises(StrategyLabError) as caught:
            registry.replace_live_champion(self.session, shadow.id)
        self.assertIn("immutable", str(caught.exception))

    # -- snapshots and decisions ------------------------------------------- #

    def test_recording_a_snapshot_is_idempotent_by_content(self):
        first = registry.record_snapshot(self.session, a_universe_snapshot())
        second = registry.record_snapshot(self.session, a_universe_snapshot())
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.session.query(models.MarketSnapshot).count(), 1)

    def test_a_snapshot_round_trips(self):
        snapshot = a_universe_snapshot(quality_warnings=("stale",))
        row = registry.record_snapshot(self.session, snapshot)
        loaded = registry.load_snapshot(row)
        self.assertEqual(loaded.content_hash, snapshot.content_hash)
        self.assertEqual(loaded.quality_warnings, ("stale",))
        self.assertIs(loaded.scope, SnapshotScope.UNIVERSE)

    def test_a_universe_snapshot_carries_one_decision_per_constituent(self):
        arm = self.registered()
        snapshot = a_universe_snapshot()
        row = registry.record_snapshot(self.session, snapshot)
        registry.record_decision(self.session, arm.id, row.id, a_decision(snapshot))
        registry.record_decision(
            self.session, arm.id, row.id,
            a_decision(snapshot, ticker="MSFT", action=DecisionAction.FLAT,
                       risk_plan=None, signal_strength=None, reason_codes=("not_selected",)),
        )
        registry.record_decision(
            self.session, arm.id, row.id,
            a_decision(snapshot, ticker="NVDA", action=DecisionAction.ABSTAIN,
                       risk_plan=None, signal_strength=None, reason_codes=("no_price",),
                       blocked_reasons=("stale_data",)),
        )
        stored = registry.decisions_for(self.session, arm.id, row.id)
        self.assertEqual([d.ticker for d in stored], ["AAPL", "MSFT", "NVDA"])
        self.assertEqual([d.action for d in stored], ["long", "flat", "abstain"])

    def test_re_recording_the_same_decision_returns_the_stored_row(self):
        arm = self.registered()
        snapshot = a_universe_snapshot()
        row = registry.record_snapshot(self.session, snapshot)
        first = registry.record_decision(self.session, arm.id, row.id, a_decision(snapshot))
        second = registry.record_decision(self.session, arm.id, row.id, a_decision(snapshot))
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.session.query(models.StrategyDecision).count(), 1)

    def test_a_different_decision_for_the_same_triple_is_refused(self):
        arm = self.registered()
        snapshot = a_universe_snapshot()
        row = registry.record_snapshot(self.session, snapshot)
        registry.record_decision(self.session, arm.id, row.id, a_decision(snapshot))
        with self.assertRaises(ImmutabilityError) as caught:
            registry.record_decision(
                self.session, arm.id, row.id, a_decision(snapshot, signal_strength=0.1)
            )
        self.assertIn("new version", str(caught.exception))

    def test_a_decision_must_match_the_snapshot_it_is_recorded_against(self):
        arm = self.registered()
        snapshot = a_universe_snapshot()
        other = a_universe_snapshot(constituents=("AAPL", "MSFT"))
        row = registry.record_snapshot(self.session, snapshot)
        with self.assertRaises(StrategyLabError) as caught:
            registry.record_decision(self.session, arm.id, row.id, a_decision(other))
        self.assertIn("snapshot", str(caught.exception))

    def test_a_decision_must_come_from_the_version_the_arm_runs(self):
        arm = self.registered()
        snapshot = a_universe_snapshot()
        row = registry.record_snapshot(self.session, snapshot)
        with self.assertRaises(StrategyLabError) as caught:
            registry.record_decision(
                self.session, arm.id, row.id, a_decision(snapshot, strategy_version="1.1.0")
            )
        self.assertIn("momentum_v1@1.0.0", str(caught.exception))

    def test_two_arms_share_one_snapshot_and_keep_separate_decisions(self):
        """Spec Q §7: every rank in a rebalance references one snapshot id."""
        shadow = self.registered()
        paper = registry.create_arm(
            self.session, "q1_momentum_vs_composite", "momentum_v1", "1.0.0",
            ExecutionMode.PAPER,
        )
        snapshot = a_universe_snapshot()
        row = registry.record_snapshot(self.session, snapshot)
        first = registry.record_decision(self.session, shadow.id, row.id, a_decision(snapshot))
        second = registry.record_decision(self.session, paper.id, row.id, a_decision(snapshot))
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first.snapshot_id, second.snapshot_id)
        # The same version over the same snapshot decides the same thing,
        # whatever tier the arm runs in.
        self.assertEqual(first.decision_hash, second.decision_hash)

    def test_an_unknown_arm_or_snapshot_is_a_named_refusal(self):
        snapshot = a_universe_snapshot()
        row = registry.record_snapshot(self.session, snapshot)
        with self.assertRaises(registry.NotFound):
            registry.record_decision(self.session, 999, row.id, a_decision(snapshot))
        arm = self.registered()
        with self.assertRaises(registry.NotFound):
            registry.record_decision(self.session, arm.id, 999, a_decision(snapshot))

    # -- evidence and promotion -------------------------------------------- #

    def promotable(self):
        """A shadow arm with acknowledged evidence, and an inactive paper target."""
        shadow = self.registered()
        registry.activate_arm(self.session, shadow.id)
        paper = registry.create_arm(
            self.session, "q1_momentum_vs_composite", "momentum_v1", "1.0.0",
            ExecutionMode.PAPER, risk_budget=25_000.0,
        )
        evidence = registry.record_metric_snapshot(
            self.session, shadow.id, CUTOFF, n_decisions=400, n_matured=120, n_closed=110,
            warnings=("small_sample_in_one_regime",), metrics={"net_return_after_costs": 0.04},
        )
        registry.acknowledge_metric_warnings(self.session, evidence.id, "bryan")
        return shadow, paper, evidence

    def test_metric_snapshots_are_idempotent_per_arm_and_cutoff(self):
        shadow, _, evidence = self.promotable()
        again = registry.record_metric_snapshot(
            self.session, shadow.id, CUTOFF, n_matured=1
        )
        self.assertEqual(evidence.id, again.id)
        self.assertEqual(again.n_matured, 120)

    def test_a_promotion_activates_the_target_and_leaves_the_source_alone(self):
        shadow, paper, evidence = self.promotable()
        event = registry.record_promotion(
            self.session, shadow.id, paper.id, evidence.id,
            owner="bryan", reason="preregistered shadow gate met",
        )
        self.assertEqual(event.from_mode, "shadow")
        self.assertEqual(event.to_mode, "paper")
        self.assertEqual(event.kind, "promotion")
        self.assertEqual(
            self.session.get(models.ExperimentArm, paper.id).status, ArmStatus.ACTIVE.value
        )
        self.assertEqual(
            self.session.get(models.ExperimentArm, paper.id).promoted_from_arm_id, shadow.id
        )
        # The source arm's mode and status are untouched.
        source = self.session.get(models.ExperimentArm, shadow.id)
        self.assertEqual((source.mode, source.status), ("shadow", ArmStatus.ACTIVE.value))

    def test_unacknowledged_evidence_cannot_promote(self):
        shadow, paper, _ = self.promotable()
        fresh = registry.record_metric_snapshot(
            self.session, shadow.id, CUTOFF + timedelta(days=1), n_matured=130
        )
        with self.assertRaises(PromotionRefused) as caught:
            registry.record_promotion(
                self.session, shadow.id, paper.id, fresh.id, owner="bryan", reason="looks good"
            )
        self.assertIn("warnings", str(caught.exception))
        self.assertEqual(self.session.query(models.PromotionEvent).count(), 0)

    def test_evidence_from_one_arm_cannot_promote_another(self):
        """Spec Q §8: evidence binds to the arm that collected it."""
        shadow, paper, evidence = self.promotable()
        registry.register_strategy_version(self.session, a_version_spec(version="1.1.0"))
        other_shadow = registry.create_arm(
            self.session, "q1_momentum_vs_composite", "momentum_v1", "1.1.0",
            ExecutionMode.SHADOW,
        )
        self.assertNotEqual(other_shadow.id, shadow.id)
        with self.assertRaises(PromotionRefused) as caught:
            registry.record_promotion(
                self.session, other_shadow.id, paper.id, evidence.id,
                owner="bryan", reason="reuse",
            )
        self.assertIn("belongs to arm", str(caught.exception))
        self.assertEqual(self.session.query(models.PromotionEvent).count(), 0)

    def test_a_live_promotion_replaces_the_global_champion_atomically(self):
        shadow, paper, evidence = self.promotable()
        registry.record_promotion(
            self.session, shadow.id, paper.id, evidence.id,
            owner="bryan", reason="shadow gate met",
        )
        incumbent = registry.create_arm(
            self.session, "q1_momentum_vs_composite", "momentum_v1", "1.0.0",
            ExecutionMode.LIVE, risk_budget=1_000.0,
        )
        registry.activate_arm(self.session, incumbent.id)

        registry.register_strategy_version(self.session, a_version_spec(version="1.1.0"))
        challenger_paper = registry.create_arm(
            self.session, "q1_momentum_vs_composite", "momentum_v1", "1.1.0",
            ExecutionMode.PAPER, risk_budget=25_000.0,
        )
        registry.activate_arm(self.session, challenger_paper.id)
        challenger_live = registry.create_arm(
            self.session, "q1_momentum_vs_composite", "momentum_v1", "1.1.0",
            ExecutionMode.LIVE, risk_budget=2_000.0,
        )
        paper_evidence = registry.record_metric_snapshot(
            self.session, challenger_paper.id, CUTOFF, n_closed=40, n_matured=40
        )
        registry.acknowledge_metric_warnings(self.session, paper_evidence.id, "bryan")

        registry.record_promotion(
            self.session, challenger_paper.id, challenger_live.id, paper_evidence.id,
            owner="bryan", reason="paper gate met, capability check passed",
        )
        self.assertEqual(registry.active_live_arm(self.session).id, challenger_live.id)
        self.assertEqual(
            self.session.get(models.ExperimentArm, incumbent.id).status, ArmStatus.INACTIVE.value
        )
        self.assertEqual(
            [e.target_arm_id for e in registry.promotions_for(self.session, challenger_live.id)],
            [challenger_live.id],
        )

    def test_a_failed_promotion_leaves_the_prior_champion_in_place(self):
        shadow, paper, evidence = self.promotable()
        incumbent = registry.create_arm(
            self.session, "q1_momentum_vs_composite", "momentum_v1", "1.0.0",
            ExecutionMode.LIVE, risk_budget=1_000.0,
        )
        registry.activate_arm(self.session, incumbent.id)
        # The target is a live arm of a *different* strategy version, so the
        # binding fails before anything is written.
        registry.register_strategy_version(self.session, a_version_spec(version="1.1.0"))
        wrong_target = registry.create_arm(
            self.session, "q1_momentum_vs_composite", "momentum_v1", "1.1.0",
            ExecutionMode.PAPER,
        )
        with self.assertRaises(PromotionRefused):
            registry.record_promotion(
                self.session, shadow.id, wrong_target.id, evidence.id,
                owner="bryan", reason="mismatched version",
            )
        self.assertEqual(registry.active_live_arm(self.session).id, incumbent.id)
        self.assertEqual(self.session.query(models.PromotionEvent).count(), 0)

    def test_acknowledging_warnings_records_who_did_it(self):
        shadow, _, evidence = self.promotable()
        self.assertEqual(evidence.acknowledged_by, "bryan")
        self.assertIsNotNone(evidence.acknowledged_at)
        with self.assertRaises(StrategyLabError):
            registry.acknowledge_metric_warnings(self.session, evidence.id, "  ")

    def test_execution_ids_are_unique_per_call(self):
        self.assertNotEqual(registry.new_execution_id(), registry.new_execution_id())


class SchemaConstraintTests(unittest.TestCase):
    """The database invariants, proven on every engine reachable from this run.

    The application checks these too, with better messages. These tests bypass
    the application on purpose: Spec Q §12 invariant 6 requires the constraint
    to be held by the database, because an in-process check is invisible to a
    second worker and to a process that restarted mid-flight.
    """

    @classmethod
    def setUpClass(cls):
        cls.databases = []
        cls.sessions = {}
        targets = [("default", None)]
        pg = postgres_url()
        if pg is not None and target_backend() != "postgresql":
            targets.append(("postgresql", pg))
        for label, base_url in targets:
            db = TestDatabase("sl_constraints", base_url=base_url)
            engine = create_engine(db.url)
            ensure_schema(engine)
            cls.databases.append((db, engine))
            name = target_backend() if label == "default" else label
            cls.sessions[name] = sessionmaker(bind=engine)

    @classmethod
    def tearDownClass(cls):
        for db, engine in cls.databases:
            engine.dispose()
            db.cleanup()

    def engines(self):
        for name, factory in sorted(self.sessions.items()):
            session = factory()
            try:
                yield name, session
                session.commit()
            finally:
                self._wipe(session)
                session.close()

    @staticmethod
    def _wipe(session):
        session.rollback()
        for model in (
            models.PromotionEvent, models.StrategyTrade, models.ExperimentMetricSnapshot,
            models.StrategyDecision, models.MarketSnapshot, models.ExperimentArm,
            models.Experiment, models.StrategyVersion,
        ):
            session.query(model).delete()
        session.commit()

    def _seed(self, session, *, mode="live", status="inactive", version="1.0.0"):
        registry.register_strategy_version(session, a_version_spec(version=version))
        registry.register_experiment(session, an_experiment_spec())
        return registry.create_arm(
            session, "q1_momentum_vs_composite", "momentum_v1", version, mode
        )

    def test_at_most_one_globally_active_live_arm(self):
        from sqlalchemy.exc import IntegrityError

        for name, session in self.engines():
            with self.subTest(engine=name):
                first = self._seed(session)
                second = self._seed(session, version="1.1.0")
                first.status = "active"
                session.flush()
                second.status = "active"
                with self.assertRaises(IntegrityError):
                    session.flush()
                session.rollback()

    def test_an_inactive_live_arm_may_sit_beside_the_champion(self):
        for name, session in self.engines():
            with self.subTest(engine=name):
                champion = self._seed(session)
                prepared = self._seed(session, version="1.1.0")
                champion.status = "active"
                session.flush()
                self.assertEqual(prepared.status, "inactive")
                session.flush()

    def test_one_active_arm_per_experiment_version_and_mode(self):
        from sqlalchemy.exc import IntegrityError

        for name, session in self.engines():
            with self.subTest(engine=name):
                arm = self._seed(session, mode="shadow")
                arm.status = "active"
                session.flush()
                duplicate = models.ExperimentArm(
                    experiment_id=arm.experiment_id,
                    strategy_version_id=arm.strategy_version_id,
                    mode="shadow", risk_budget=0.0, status="active",
                    created_at=utcnow_naive(), updated_at=utcnow_naive(),
                )
                session.add(duplicate)
                with self.assertRaises(IntegrityError):
                    session.flush()
                session.rollback()

    def test_at_most_one_non_terminal_execution_per_decision(self):
        from sqlalchemy.exc import IntegrityError

        for name, session in self.engines():
            with self.subTest(engine=name):
                decision = self._a_decision_row(session)
                session.add(self._trade(decision, "e1", ExecutionState.PROPOSED))
                session.flush()
                session.add(self._trade(decision, "e2", ExecutionState.SUBMITTED))
                with self.assertRaises(IntegrityError):
                    session.flush()
                session.rollback()

    def test_terminal_executions_may_coexist_and_unblock_the_next_attempt(self):
        for name, session in self.engines():
            with self.subTest(engine=name):
                decision = self._a_decision_row(session)
                open_row = self._trade(decision, "e1", ExecutionState.PROPOSED)
                session.add(open_row)
                session.flush()
                for i, state in enumerate(sorted(
                    s.value for s in (
                        ExecutionState.CANCELLED, ExecutionState.ORDER_REJECTED,
                        ExecutionState.CLOSED,
                    )
                )):
                    session.add(self._trade(decision, f"terminal{i}", state))
                session.flush()

                open_row.status = ExecutionState.CLOSED.value
                session.flush()
                session.add(self._trade(decision, "e2", ExecutionState.PROPOSED))
                session.flush()

    def test_an_execution_id_is_unique(self):
        from sqlalchemy.exc import IntegrityError

        for name, session in self.engines():
            with self.subTest(engine=name):
                decision = self._a_decision_row(session)
                session.add(self._trade(decision, "same", ExecutionState.CLOSED))
                session.flush()
                session.add(self._trade(decision, "same", ExecutionState.CLOSED))
                with self.assertRaises(IntegrityError):
                    session.flush()
                session.rollback()

    def test_one_decision_per_arm_snapshot_and_ticker(self):
        from sqlalchemy.exc import IntegrityError

        for name, session in self.engines():
            with self.subTest(engine=name):
                row = self._a_decision_row(session)
                session.add(models.StrategyDecision(
                    arm_id=row.arm_id, snapshot_id=row.snapshot_id, ticker=row.ticker,
                    action="flat", reason_codes_json="[]", decision_json="{}",
                    decision_hash="f" * 64, blocked_reasons_json="[]",
                    created_at=utcnow_naive(),
                ))
                with self.assertRaises(IntegrityError):
                    session.flush()
                session.rollback()

    def test_the_status_and_mode_vocabularies_are_enforced_by_check_constraints(self):
        from sqlalchemy.exc import IntegrityError

        for name, session in self.engines():
            with self.subTest(engine=name):
                arm = self._seed(session, mode="shadow")
                arm.mode = "sideways"
                with self.assertRaises(IntegrityError):
                    session.flush()
                session.rollback()

    def test_a_universe_snapshot_cannot_carry_a_ticker(self):
        from sqlalchemy.exc import IntegrityError

        for name, session in self.engines():
            with self.subTest(engine=name):
                session.add(models.MarketSnapshot(
                    scope="universe", ticker="AAPL", as_of_utc=CUTOFF, data_cutoff_utc=CUTOFF,
                    normalized_inputs_json="{}", constituents_json="[]",
                    source_observation_ids_json="[]", provenance_json="{}",
                    data_quality_json="{}", content_hash="a" * 64, created_at=CUTOFF,
                ))
                with self.assertRaises(IntegrityError):
                    session.flush()
                session.rollback()

    def test_every_reachable_engine_was_covered(self):
        """The tests above are only worth their name if both engines ran.

        CI sets ``TEST_POSTGRES_URL`` on both matrix entries, so the SQLite
        entry proves the partial indexes on Postgres too and vice versa. Without
        a Postgres to hand this asserts what it can and says nothing it cannot.
        """
        self.assertIn(target_backend(), self.sessions)
        if postgres_url() is not None:
            self.assertIn(
                "postgresql", self.sessions,
                "a Postgres URL is configured but the constraints were not "
                "exercised against it",
            )

    def _a_decision_row(self, session):
        arm = self._seed(session, mode="shadow")
        snapshot = a_universe_snapshot()
        snapshot_row = registry.record_snapshot(session, snapshot)
        return registry.record_decision(session, arm.id, snapshot_row.id, a_decision(snapshot))

    @staticmethod
    def _trade(decision, execution_id: str, status):
        now = utcnow_naive()
        return models.StrategyTrade(
            execution_id=execution_id,
            arm_id=decision.arm_id,
            decision_id=decision.id,
            mode="shadow",
            status=status.value if isinstance(status, ExecutionState) else status,
            created_at=now,
            updated_at=now,
        )


if __name__ == "__main__":
    unittest.main()
