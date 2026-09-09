"""Spec Q §6, §8, §9, §12: the domain contracts, without a database.

Everything here is pure: dataclass validation, content hashing, the four
lifecycle state machines, and the promotion bindings. The registry tests cover
the same rules where they meet SQL; these cover them where they are decided.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from strategy_lab.domain import (
    ARM_TRANSITIONS,
    BLOCKED_REASONS,
    EXECUTION_TRANSITIONS,
    EXPERIMENT_TRANSITIONS,
    PROMOTION_LADDER,
    STRATEGY_VERSION_TRANSITIONS,
    TERMINAL_EXECUTION_STATES,
    ArmFacts,
    ArmStatus,
    DecisionAction,
    Direction,
    EvidenceFacts,
    ExecutionMode,
    ExecutionState,
    ExperimentSpec,
    ExperimentStatus,
    InvalidTransition,
    MarketSnapshot,
    PromotionKind,
    PromotionRefused,
    RiskPlan,
    SnapshotScope,
    StrategyDecision,
    StrategyLabError,
    StrategyVersion,
    StrategyVersionStatus,
    authorize_promotion,
    is_terminal,
    require_transition,
    tier_move,
)

CUTOFF = datetime(2026, 3, 31, 20, 0)


def a_version_spec(**overrides) -> StrategyVersion:
    kwargs = dict(
        slug="momentum_v1",
        version="1.0.0",
        hypothesis="Cross-sectional momentum persists over a quarter.",
        universe="liquid_us_equity_v1",
        direction=Direction.LONG,
        required_snapshot_fields=("adj_close", "universe_membership"),
        execution_policy_version="momentum_quarterly_89cal_v1",
        expected_holding_days=89,
        max_data_staleness_seconds=86_400,
        historically_replayable=True,
        replayability_reason="structured adjusted prices only; no LLM output",
        implementation_manifest={"strategy_source_sha": "a" * 64, "python": "3.12.7"},
        config={"formation_sessions": 252, "skip_sessions": 20, "top_decile": True},
        dependencies=("numpy==1.26.4", "pandas==2.2.2"),
    )
    kwargs.update(overrides)
    return StrategyVersion(**kwargs)


def an_experiment_spec(**overrides) -> ExperimentSpec:
    kwargs = dict(
        name="q1_momentum_vs_composite",
        hypothesis="momentum_v1 beats the composite net of costs.",
        universe_spec="liquid_us_equity_v1",
        primary_metric="net_return_after_costs",
        benchmarks=("cash", "spy"),
        end_criteria={"min_matured_decisions": 100, "min_calendar_days": 60},
        owner="bryan",
        planned_variants=4,
    )
    kwargs.update(overrides)
    return ExperimentSpec(**kwargs)


def a_universe_snapshot(**overrides) -> MarketSnapshot:
    kwargs = dict(
        scope=SnapshotScope.UNIVERSE,
        as_of_utc=CUTOFF,
        data_cutoff_utc=CUTOFF,
        provenance={"prices": "sharadar", "universe": "liquid_us_equity_v1"},
        universe_version="liquid_us_equity_v1@2026-03-31",
        constituents=("AAPL", "MSFT", "NVDA"),
        source_observation_ids=(11, 12),
    )
    kwargs.update(overrides)
    return MarketSnapshot(**kwargs)


def a_risk_plan(**overrides) -> RiskPlan:
    kwargs = dict(
        execution_policy_version="momentum_quarterly_89cal_v1",
        entry_style="next_regular_session_open",
        max_hold_calendar_days=89,
        position_risk_pct=0.05,
    )
    kwargs.update(overrides)
    return RiskPlan(**kwargs)


class StrategyVersionTests(unittest.TestCase):
    def test_content_hash_is_stable_and_covers_the_rules(self):
        self.assertEqual(a_version_spec().content_hash, a_version_spec().content_hash)
        changed = a_version_spec(config={"formation_sessions": 126})
        self.assertNotEqual(a_version_spec().content_hash, changed.content_hash)

    def test_status_is_not_content(self):
        """The rules are the version; the status is where the version got to."""
        shadowed = a_version_spec(status=StrategyVersionStatus.SHADOW)
        self.assertEqual(shadowed.content_hash, a_version_spec().content_hash)

    def test_a_manifest_change_is_a_different_version(self):
        other = a_version_spec(implementation_manifest={"strategy_source_sha": "b" * 64})
        self.assertNotEqual(other.content_hash, a_version_spec().content_hash)
        self.assertNotEqual(other.manifest_hash, a_version_spec().manifest_hash)

    def test_an_empty_manifest_is_refused(self):
        with self.assertRaises(StrategyLabError):
            a_version_spec(implementation_manifest={})

    def test_the_frozen_fields_cannot_be_assigned(self):
        version = a_version_spec()
        with self.assertRaises(Exception):
            version.expected_holding_days = 5
        with self.assertRaises(TypeError):
            version.config["formation_sessions"] = 1

    def test_v1_is_long_only(self):
        with self.assertRaises(StrategyLabError) as caught:
            a_version_spec(direction=Direction.SHORT)
        self.assertIn("long-only", str(caught.exception))

    def test_malformed_metadata_is_refused(self):
        cases = {
            "slug": {"slug": "Momentum V1"},
            "version": {"version": "v1"},
            "hypothesis": {"hypothesis": "   "},
            "holding": {"expected_holding_days": 0},
            "staleness": {"max_data_staleness_seconds": -1},
            "unsorted fields": {"required_snapshot_fields": ("b", "a")},
            "repeated fields": {"required_snapshot_fields": ("a", "a")},
            "no fields": {"required_snapshot_fields": ()},
            "no replay reason": {"replayability_reason": ""},
            "unserialisable config": {"config": {"when": object()}},
        }
        for label, overrides in cases.items():
            with self.subTest(label), self.assertRaises(StrategyLabError):
                a_version_spec(**overrides)

    def test_status_moves_along_the_lifecycle_only(self):
        version = a_version_spec()
        shadow = version.with_status(StrategyVersionStatus.SHADOW)
        paper = shadow.with_status(StrategyVersionStatus.PAPER)
        self.assertIs(paper.status, StrategyVersionStatus.PAPER)
        self.assertEqual(paper.content_hash, version.content_hash)

        with self.assertRaises(InvalidTransition):
            version.with_status(StrategyVersionStatus.LIVE_ELIGIBLE)
        retired = paper.with_status(StrategyVersionStatus.RETIRED)
        with self.assertRaises(InvalidTransition):
            retired.with_status(StrategyVersionStatus.SHADOW)

    def test_demotion_from_live_eligible_is_allowed(self):
        live = a_version_spec(status=StrategyVersionStatus.LIVE_ELIGIBLE)
        self.assertIs(
            live.with_status(StrategyVersionStatus.PAPER).status, StrategyVersionStatus.PAPER
        )


class ExperimentSpecTests(unittest.TestCase):
    def test_content_hash_covers_the_analysis_plan(self):
        self.assertEqual(an_experiment_spec().content_hash, an_experiment_spec().content_hash)
        self.assertNotEqual(
            an_experiment_spec().content_hash,
            an_experiment_spec(primary_metric="win_rate").content_hash,
        )
        self.assertNotEqual(
            an_experiment_spec().content_hash, an_experiment_spec(planned_variants=9).content_hash
        )

    def test_a_plan_without_benchmarks_or_end_criteria_is_refused(self):
        with self.assertRaises(StrategyLabError):
            an_experiment_spec(benchmarks=())
        with self.assertRaises(StrategyLabError):
            an_experiment_spec(end_criteria={})

    def test_planned_variants_is_at_least_one(self):
        with self.assertRaises(StrategyLabError):
            an_experiment_spec(planned_variants=0)

    def test_aware_cutoffs_are_normalised_to_naive_utc(self):
        aware = datetime(2026, 3, 31, 16, 0, tzinfo=timezone(timedelta(hours=-4)))
        spec = an_experiment_spec(data_cutoff_utc=aware)
        self.assertIsNone(spec.data_cutoff_utc.tzinfo)
        self.assertEqual(spec.data_cutoff_utc, datetime(2026, 3, 31, 20, 0))
        self.assertEqual(
            spec.content_hash, an_experiment_spec(data_cutoff_utc=datetime(2026, 3, 31, 20, 0)).content_hash
        )


class MarketSnapshotTests(unittest.TestCase):
    def test_universe_and_ticker_scopes_have_different_shapes(self):
        with self.assertRaises(StrategyLabError):
            a_universe_snapshot(ticker="AAPL")
        with self.assertRaises(StrategyLabError):
            MarketSnapshot(
                scope=SnapshotScope.TICKER, as_of_utc=CUTOFF, data_cutoff_utc=CUTOFF,
                provenance={"prices": "sharadar"}, constituents=("AAPL",),
            )
        with self.assertRaises(StrategyLabError):
            a_universe_snapshot(universe_version=None)
        with self.assertRaises(StrategyLabError):
            a_universe_snapshot(constituents=())

    def test_constituents_must_be_sorted_and_unique_for_a_stable_hash(self):
        with self.assertRaises(StrategyLabError):
            a_universe_snapshot(constituents=("MSFT", "AAPL"))
        with self.assertRaises(StrategyLabError):
            a_universe_snapshot(constituents=("AAPL", "AAPL"))

    def test_data_cannot_come_from_after_the_snapshot(self):
        with self.assertRaises(StrategyLabError):
            a_universe_snapshot(data_cutoff_utc=CUTOFF + timedelta(hours=1))

    def test_content_hash_changes_with_every_input(self):
        base = a_universe_snapshot().content_hash
        self.assertNotEqual(base, a_universe_snapshot(constituents=("AAPL", "MSFT")).content_hash)
        self.assertNotEqual(base, a_universe_snapshot(source_observation_ids=(11,)).content_hash)
        self.assertNotEqual(
            base, a_universe_snapshot(normalized_inputs={"AAPL": {"r": 0.1}}).content_hash
        )

    def test_reconstructed_and_non_pit_snapshots_are_not_replay_eligible(self):
        self.assertTrue(a_universe_snapshot().replay_eligible)
        self.assertTrue(a_universe_snapshot(quality_warnings=("stale",)).replay_eligible)
        for warning in ("archival_reconstructed", "not_point_in_time"):
            with self.subTest(warning):
                self.assertFalse(
                    a_universe_snapshot(quality_warnings=(warning,)).replay_eligible
                )

    def test_unknown_quality_warnings_are_refused(self):
        with self.assertRaises(StrategyLabError):
            a_universe_snapshot(quality_warnings=("looks_fine",))

    def test_provenance_is_required(self):
        with self.assertRaises(StrategyLabError):
            a_universe_snapshot(provenance={})

    def test_source_observation_ids_are_plain_sorted_positive_integers(self):
        with self.assertRaises(StrategyLabError):
            a_universe_snapshot(source_observation_ids=(12, 11))
        with self.assertRaises(StrategyLabError):
            a_universe_snapshot(source_observation_ids=(0,))

    def test_covers_answers_membership_for_both_scopes(self):
        universe = a_universe_snapshot()
        self.assertTrue(universe.covers("nvda"))
        self.assertFalse(universe.covers("TSLA"))
        ticker = MarketSnapshot(
            scope=SnapshotScope.TICKER, as_of_utc=CUTOFF, data_cutoff_utc=CUTOFF,
            provenance={"prices": "sharadar"}, ticker="tsla",
        )
        self.assertEqual(ticker.ticker, "TSLA")
        self.assertTrue(ticker.covers("TSLA"))


class StrategyDecisionTests(unittest.TestCase):
    def a_decision(self, **overrides) -> StrategyDecision:
        kwargs = dict(
            strategy_slug="momentum_v1",
            strategy_version="1.0.0",
            snapshot_hash=a_universe_snapshot().content_hash,
            ticker="AAPL",
            action=DecisionAction.LONG,
            reason_codes=("liquidity_ok", "top_decile"),
            signal_strength=0.82,
            risk_plan=a_risk_plan(),
        )
        kwargs.update(overrides)
        return StrategyDecision(**kwargs)

    def test_the_same_version_and_snapshot_hash_identically(self):
        self.assertEqual(self.a_decision().decision_hash, self.a_decision().decision_hash)

    def test_the_hash_excludes_the_arm_and_all_portfolio_state(self):
        """Spec Q §6: a decision is reproducible; execution eligibility is not.

        There is no arm, cash, or exposure field to change — which is the
        property under test. The same decision recorded under a shadow arm and
        a paper arm carries one hash.
        """
        self.assertNotIn("arm", self.a_decision().canonical())
        self.assertEqual(
            self.a_decision().decision_hash,
            self.a_decision(confidence=None).decision_hash,
        )

    def test_every_input_moves_the_hash(self):
        base = self.a_decision().decision_hash
        for label, overrides in {
            "ticker": {"ticker": "MSFT"},
            "action": {"action": DecisionAction.FLAT, "risk_plan": None, "signal_strength": None},
            "reasons": {"reason_codes": ("top_decile",)},
            "strength": {"signal_strength": 0.5},
            "confidence": {"confidence": 0.4},
            "plan": {"risk_plan": a_risk_plan(position_risk_pct=0.01)},
            "snapshot": {"snapshot_hash": a_universe_snapshot(constituents=("AAPL",)).content_hash},
            "version": {"strategy_version": "1.0.1"},
        }.items():
            with self.subTest(label):
                self.assertNotEqual(base, self.a_decision(**overrides).decision_hash)

    def test_a_long_decision_needs_a_plan_and_a_strength(self):
        with self.assertRaises(StrategyLabError):
            self.a_decision(risk_plan=None)
        with self.assertRaises(StrategyLabError):
            self.a_decision(signal_strength=None)

    def test_flat_and_abstain_carry_no_plan(self):
        with self.assertRaises(StrategyLabError):
            self.a_decision(action=DecisionAction.FLAT, signal_strength=None)
        flat = self.a_decision(
            action=DecisionAction.FLAT, risk_plan=None, signal_strength=None,
            reason_codes=("not_selected",),
        )
        self.assertIs(flat.action, DecisionAction.FLAT)

    def test_abstain_requires_a_signal_generation_block(self):
        with self.assertRaises(StrategyLabError) as caught:
            self.a_decision(
                action=DecisionAction.ABSTAIN, risk_plan=None, signal_strength=None,
                reason_codes=("no_data",),
            )
        self.assertIn("blocked_reason", str(caught.exception))
        abstain = self.a_decision(
            action=DecisionAction.ABSTAIN, risk_plan=None, signal_strength=None,
            reason_codes=("no_data",), blocked_reasons=("stale_data",),
        )
        self.assertEqual(abstain.blocked_reasons, ("stale_data",))

    def test_flat_cannot_carry_a_blocked_reason(self):
        with self.assertRaises(StrategyLabError):
            self.a_decision(
                action=DecisionAction.FLAT, risk_plan=None, signal_strength=None,
                reason_codes=("not_selected",), blocked_reasons=("stale_data",),
            )

    def test_a_portfolio_block_is_not_a_decision_block(self):
        """Spec Q §6: blocked_reasons here cover signal generation only."""
        with self.assertRaises(StrategyLabError) as caught:
            self.a_decision(
                action=DecisionAction.ABSTAIN, risk_plan=None, signal_strength=None,
                reason_codes=("no_cash",), blocked_reasons=("insufficient_buying_power",),
            )
        self.assertIn("strategy_trades", str(caught.exception))
        self.assertEqual(
            BLOCKED_REASONS,
            frozenset({"stale_data", "missing_dependency", "invalid_point_in_time_input"}),
        )

    def test_reason_codes_are_required_sorted_and_unique(self):
        for overrides in ({"reason_codes": ()}, {"reason_codes": ("b", "a")},
                          {"reason_codes": ("a", "a")}):
            with self.subTest(overrides), self.assertRaises(StrategyLabError):
                self.a_decision(**overrides)

    def test_a_decision_always_names_a_ticker(self):
        for bad in ("", "   ", "not a ticker"):
            with self.subTest(bad), self.assertRaises(StrategyLabError):
                self.a_decision(ticker=bad)

    def test_normalised_scores_stay_in_range(self):
        for value in (-0.1, 1.1):
            with self.subTest(value), self.assertRaises(StrategyLabError):
                self.a_decision(signal_strength=value)
            with self.subTest(value), self.assertRaises(StrategyLabError):
                self.a_decision(confidence=value)


class RiskPlanTests(unittest.TestCase):
    def test_bounds(self):
        for overrides in (
            {"max_hold_calendar_days": 0},
            {"position_risk_pct": 0.0},
            {"position_risk_pct": 1.5},
            {"stop_price": 0.0},
            {"target_prices": (10.0, 5.0)},
            {"entry_style": ""},
        ):
            with self.subTest(overrides), self.assertRaises(StrategyLabError):
                a_risk_plan(**overrides)

    def test_a_next_open_entry_may_have_no_prices_yet(self):
        plan = a_risk_plan()
        self.assertIsNone(plan.stop_price)
        self.assertEqual(plan.target_prices, ())


class TransitionTests(unittest.TestCase):
    def test_every_experiment_transition(self):
        for state, allowed in EXPERIMENT_TRANSITIONS.items():
            for target in ExperimentStatus:
                with self.subTest(f"{state.value}->{target.value}"):
                    if target is state or target in allowed:
                        require_transition(
                            EXPERIMENT_TRANSITIONS, state, target, label="experiment"
                        )
                    else:
                        with self.assertRaises(InvalidTransition):
                            require_transition(
                                EXPERIMENT_TRANSITIONS, state, target, label="experiment"
                            )

    def test_every_arm_transition(self):
        for state, allowed in ARM_TRANSITIONS.items():
            for target in ArmStatus:
                with self.subTest(f"{state.value}->{target.value}"):
                    if target is state or target in allowed:
                        require_transition(ARM_TRANSITIONS, state, target, label="arm")
                    else:
                        with self.assertRaises(InvalidTransition):
                            require_transition(ARM_TRANSITIONS, state, target, label="arm")

    def test_every_strategy_version_transition(self):
        for state, allowed in STRATEGY_VERSION_TRANSITIONS.items():
            for target in StrategyVersionStatus:
                with self.subTest(f"{state.value}->{target.value}"):
                    if target is state or target in allowed:
                        require_transition(
                            STRATEGY_VERSION_TRANSITIONS, state, target, label="version"
                        )
                    else:
                        with self.assertRaises(InvalidTransition):
                            require_transition(
                                STRATEGY_VERSION_TRANSITIONS, state, target, label="version"
                            )

    def test_completed_and_cancelled_experiments_are_terminal(self):
        for state in (ExperimentStatus.COMPLETED, ExperimentStatus.CANCELLED):
            self.assertEqual(EXPERIMENT_TRANSITIONS[state], frozenset())

    def test_a_no_op_transition_is_allowed_because_retries_re_assert_state(self):
        self.assertIs(
            require_transition(ARM_TRANSITIONS, ArmStatus.ACTIVE, ArmStatus.ACTIVE, label="arm"),
            ArmStatus.ACTIVE,
        )

    def test_the_refusal_names_the_available_moves(self):
        with self.assertRaises(InvalidTransition) as caught:
            require_transition(ARM_TRANSITIONS, ArmStatus.RETIRED, ArmStatus.ACTIVE, label="arm")
        self.assertIn("<terminal>", str(caught.exception))


class ExecutionStateTests(unittest.TestCase):
    """Spec Q §12. Phase 5 implements the machine; these are its rails."""

    def test_terminal_states_are_exactly_the_ones_the_diagram_marks(self):
        self.assertEqual(
            {state.value for state in TERMINAL_EXECUTION_STATES},
            {
                "owner_rejected", "risk_rejected", "review_rejected", "order_rejected",
                "failed_no_order", "cancelled", "expired", "closed",
            },
        )

    def test_an_unknown_placement_is_not_terminal(self):
        """It cannot release its reservation until reconciliation resolves it."""
        self.assertFalse(is_terminal(ExecutionState.PLACEMENT_UNKNOWN))
        self.assertEqual(
            EXECUTION_TRANSITIONS[ExecutionState.PLACEMENT_UNKNOWN],
            frozenset({ExecutionState.RECONCILIATION_REQUIRED}),
        )

    def test_every_non_terminal_state_can_reach_reconciliation(self):
        for state in ExecutionState:
            if state in TERMINAL_EXECUTION_STATES or state is ExecutionState.RECONCILIATION_REQUIRED:
                continue
            with self.subTest(state.value):
                self.assertIn(
                    ExecutionState.RECONCILIATION_REQUIRED, EXECUTION_TRANSITIONS[state]
                )

    def test_terminal_states_go_nowhere(self):
        for state in TERMINAL_EXECUTION_STATES:
            with self.subTest(state.value):
                self.assertEqual(EXECUTION_TRANSITIONS[state], frozenset())

    def test_a_fill_must_pass_through_protection_before_it_can_close(self):
        self.assertEqual(
            EXECUTION_TRANSITIONS[ExecutionState.FILLED],
            frozenset({ExecutionState.PROTECTION_PENDING, ExecutionState.RECONCILIATION_REQUIRED}),
        )
        self.assertNotIn(ExecutionState.CLOSED, EXECUTION_TRANSITIONS[ExecutionState.FILLED])
        self.assertEqual(
            EXECUTION_TRANSITIONS[ExecutionState.CLOSING],
            frozenset({ExecutionState.CLOSED, ExecutionState.RECONCILIATION_REQUIRED}),
        )

    def test_a_proposed_execution_cannot_jump_the_owner(self):
        self.assertNotIn(ExecutionState.SUBMITTED, EXECUTION_TRANSITIONS[ExecutionState.PROPOSED])
        self.assertIn(ExecutionState.OWNER_APPROVED, EXECUTION_TRANSITIONS[ExecutionState.PROPOSED])


class PromotionTests(unittest.TestCase):
    def source(self, **overrides) -> ArmFacts:
        kwargs = dict(
            arm_id=1, experiment_id=1, strategy_version_id=7,
            mode=ExecutionMode.SHADOW, status=ArmStatus.ACTIVE, risk_budget=10_000.0,
        )
        kwargs.update(overrides)
        return ArmFacts(**kwargs)

    def target(self, **overrides) -> ArmFacts:
        kwargs = dict(
            arm_id=2, experiment_id=1, strategy_version_id=7,
            mode=ExecutionMode.PAPER, status=ArmStatus.INACTIVE, risk_budget=25_000.0,
        )
        kwargs.update(overrides)
        return ArmFacts(**kwargs)

    def evidence(self, **overrides) -> EvidenceFacts:
        kwargs = dict(metric_snapshot_id=5, arm_id=1, warnings_acknowledged=True)
        kwargs.update(overrides)
        return EvidenceFacts(**kwargs)

    def authorize(self, **overrides):
        kwargs = dict(owner="bryan", reason="preregistered gate met")
        kwargs.update(overrides.pop("kwargs", {}))
        return authorize_promotion(
            overrides.get("source", self.source()),
            overrides.get("target", self.target()),
            overrides.get("evidence", self.evidence()),
            **kwargs,
        )

    def test_a_well_formed_promotion_binds_arm_evidence_and_version(self):
        auth = self.authorize()
        self.assertIs(auth.kind, PromotionKind.PROMOTION)
        self.assertEqual((auth.source_arm_id, auth.target_arm_id), (1, 2))
        self.assertEqual(auth.strategy_version_id, 7)
        self.assertEqual(auth.evidence_metric_snapshot_id, 5)
        self.assertEqual(auth.new_risk_budget, 25_000.0)

    def test_evidence_from_another_arm_cannot_authorize(self):
        with self.assertRaises(PromotionRefused) as caught:
            self.authorize(evidence=self.evidence(arm_id=99))
        self.assertIn("belongs to arm", str(caught.exception))

    def test_a_version_only_authorization_is_never_valid(self):
        """Spec Q §8: the target must be a distinct arm, not the source re-tiered."""
        with self.assertRaises(PromotionRefused) as caught:
            self.authorize(target=self.target(arm_id=1, mode=ExecutionMode.PAPER))
        self.assertIn("distinct arm", str(caught.exception))

    def test_the_target_must_share_the_immutable_strategy_version(self):
        with self.assertRaises(PromotionRefused) as caught:
            self.authorize(target=self.target(strategy_version_id=8))
        self.assertIn("different strategy version", str(caught.exception))

    def test_the_target_must_be_inactive(self):
        for status in (ArmStatus.ACTIVE, ArmStatus.PAUSED, ArmStatus.RETIRED):
            with self.subTest(status.value), self.assertRaises(PromotionRefused):
                self.authorize(target=self.target(status=status))

    def test_a_tier_cannot_be_skipped(self):
        with self.assertRaises(PromotionRefused) as caught:
            self.authorize(target=self.target(mode=ExecutionMode.LIVE))
        self.assertIn("skips a tier", str(caught.exception))

    def test_a_demotion_is_recorded_as_one(self):
        auth = self.authorize(
            source=self.source(mode=ExecutionMode.LIVE),
            target=self.target(mode=ExecutionMode.SHADOW),
        )
        self.assertIs(auth.kind, PromotionKind.DEMOTION)

    def test_a_same_tier_move_is_not_a_promotion(self):
        with self.assertRaises(PromotionRefused):
            self.authorize(target=self.target(mode=ExecutionMode.SHADOW))

    def test_unacknowledged_warnings_block_a_tier_change(self):
        with self.assertRaises(PromotionRefused) as caught:
            self.authorize(evidence=self.evidence(warnings_acknowledged=False))
        self.assertIn("warnings", str(caught.exception))

    def test_owner_and_reason_are_required(self):
        with self.assertRaises(PromotionRefused):
            self.authorize(kwargs={"owner": "  ", "reason": "r"})
        with self.assertRaises(PromotionRefused):
            self.authorize(kwargs={"owner": "bryan", "reason": ""})

    def test_the_ladder_is_shadow_paper_live(self):
        self.assertEqual(
            tuple(mode.value for mode in PROMOTION_LADDER), ("shadow", "paper", "live")
        )
        self.assertIs(tier_move(ExecutionMode.PAPER, ExecutionMode.LIVE), PromotionKind.PROMOTION)
        self.assertIs(tier_move(ExecutionMode.LIVE, ExecutionMode.PAPER), PromotionKind.DEMOTION)


if __name__ == "__main__":
    unittest.main()
