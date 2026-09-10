"""Spec Q §6: the guards that stand between a strategy and a persisted decision.

Four of them, and each has its own class here: decision-set validity, the
implementation manifest and its drift check, the historical-replay refusal, and
the structural shadow-only refusal.

The manifest is the one worth reading carefully. Spec Q §6: "an in-place code,
helper, formula, policy, or runtime dependency change may never run under an
existing version identity." The tests below prove each of those five limbs
independently — a strategy edit, a helper edit, an indicator-formula edit, a
policy-parameter change and a runtime change each move the hash — and then
prove the registry refuses the run rather than quietly using the stored one.
"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from strategy_lab import registry, validation
from strategy_lab.domain import (
    DecisionAction,
    ExecutionMode,
    ImmutabilityError,
    RiskPlan,
    StrategyDecision,
    StrategyVersion,
)
from strategy_lab.execution_policy import EVENT_SWING_14CAL_V1
from strategy_lab.indicators import wilder_atr
from strategy_lab.strategies import build_versions
from strategy_lab.strategies import earnings_drift_v1 as earnings
from strategy_lab.strategies import momentum_v1 as momentum
from tests.dbfixture import TestDatabase
from tests.strategylabfixture import (
    earnings_record,
    flat_bars,
    replayable_inputs,
    ticker_snapshot,
    universe_snapshot,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def a_snapshot():
    return ticker_snapshot(replayable_inputs(
        "AAPL", flat_bars(),
        earnings=earnings_record("AAPL", reported_eps=1.25, consensus_eps=1.0),
    ))


def a_decision(snapshot, **overrides) -> StrategyDecision:
    kwargs = dict(
        strategy_slug=earnings.SLUG,
        strategy_version=earnings.VERSION,
        snapshot_hash=snapshot.content_hash,
        ticker="AAPL",
        action=DecisionAction.FLAT,
        reason_codes=("liquidity_screen_passed",),
    )
    kwargs.update(overrides)
    return StrategyDecision(**kwargs)


def with_manifest(version: StrategyVersion, manifest) -> StrategyVersion:
    """A copy of ``version`` whose manifest is ``manifest``. Stands in for an edit."""
    return StrategyVersion(**{**version.__dict__, "implementation_manifest": manifest})


class DecisionSetTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = a_snapshot()
        self.version = earnings.STRATEGY.metadata

    def test_a_valid_single_decision_passes_through_unchanged(self):
        decision = a_decision(self.snapshot)
        self.assertEqual(
            validation.validate_decision_set(self.snapshot, self.version, (decision,)),
            (decision,),
        )

    def test_a_decision_for_a_ticker_not_in_the_snapshot_is_refused(self):
        with self.assertRaises(validation.DecisionSetInvalid) as caught:
            validation.validate_decision_set(
                self.snapshot, self.version, (a_decision(self.snapshot, ticker="MSFT"),)
            )
        self.assertIn("one decision per constituent", str(caught.exception))

    def test_a_missing_constituent_is_refused(self):
        universe = universe_snapshot([
            replayable_inputs("AAA", flat_bars(5)),
            replayable_inputs("BBB", flat_bars(5)),
        ])
        one = a_decision(universe, ticker="AAA", snapshot_hash=universe.content_hash)
        with self.assertRaises(validation.DecisionSetInvalid) as caught:
            validation.validate_decision_set(universe, self.version, (one,))
        self.assertIn("BBB", str(caught.exception))

    def test_a_duplicate_ticker_is_refused(self):
        universe = universe_snapshot([
            replayable_inputs("AAA", flat_bars(5)),
            replayable_inputs("BBB", flat_bars(5)),
        ])
        duplicate = a_decision(universe, ticker="AAA", snapshot_hash=universe.content_hash)
        with self.assertRaises(validation.DecisionSetInvalid) as caught:
            validation.validate_decision_set(
                universe, self.version, (duplicate, duplicate)
            )
        self.assertIn("repeat", str(caught.exception))

    def test_decisions_out_of_ticker_order_are_refused(self):
        universe = universe_snapshot([
            replayable_inputs("AAA", flat_bars(5)),
            replayable_inputs("BBB", flat_bars(5)),
        ])
        pair = (
            a_decision(universe, ticker="BBB", snapshot_hash=universe.content_hash),
            a_decision(universe, ticker="AAA", snapshot_hash=universe.content_hash),
        )
        with self.assertRaises(validation.DecisionSetInvalid) as caught:
            validation.validate_decision_set(universe, self.version, pair)
        self.assertIn("ascending", str(caught.exception))

    def test_a_decision_pinned_to_another_snapshot_is_refused(self):
        """Two snapshots means two cutoffs, which is the Spec Q §6 failure."""
        other = ticker_snapshot(replayable_inputs("AAPL", flat_bars(50)))
        with self.assertRaises(validation.DecisionSetInvalid) as caught:
            validation.validate_decision_set(
                self.snapshot, self.version,
                (a_decision(self.snapshot, snapshot_hash=other.content_hash),),
            )
        self.assertIn("assembled from two snapshots", str(caught.exception))

    def test_a_decision_claiming_another_strategy_is_refused(self):
        with self.assertRaises(validation.DecisionSetInvalid):
            validation.validate_decision_set(
                self.snapshot, self.version,
                (a_decision(self.snapshot, strategy_slug="momentum_v1"),),
            )

    def test_a_risk_plan_under_the_wrong_policy_is_refused(self):
        plan = RiskPlan(
            execution_policy_version="reversal_5cal_v1",
            entry_style="next_regular_session_open",
            max_hold_calendar_days=5,
            position_risk_pct=1.0,
        )
        decision = a_decision(
            self.snapshot,
            action=DecisionAction.LONG,
            signal_strength=0.5,
            risk_plan=plan,
        )
        with self.assertRaises(validation.DecisionSetInvalid) as caught:
            validation.validate_decision_set(self.snapshot, self.version, (decision,))
        self.assertIn("event_swing_14cal_v1", str(caught.exception))

    def test_the_set_hash_covers_the_order_and_every_member(self):
        universe = universe_snapshot([
            replayable_inputs("AAA", flat_bars(5)),
            replayable_inputs("BBB", flat_bars(5)),
        ])
        version = momentum.STRATEGY.metadata
        base = tuple(
            StrategyDecision(
                strategy_slug=version.slug, strategy_version=version.version,
                snapshot_hash=universe.content_hash, ticker=ticker,
                action=DecisionAction.FLAT, reason_codes=("outside_top_decile",),
            )
            for ticker in ("AAA", "BBB")
        )
        first = validation.decision_set_hash(universe, version, base)
        self.assertEqual(first, validation.decision_set_hash(universe, version, base))
        changed = (base[0], StrategyDecision(
            strategy_slug=version.slug, strategy_version=version.version,
            snapshot_hash=universe.content_hash, ticker="BBB",
            action=DecisionAction.ABSTAIN, reason_codes=("outside_top_decile",),
            blocked_reasons=("missing_dependency",),
        ))
        self.assertNotEqual(first, validation.decision_set_hash(universe, version, changed))


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.version = earnings.STRATEGY.metadata
        self.manifest = dict(self.version.implementation_manifest)

    def test_the_manifest_covers_every_limb_spec_q_names(self):
        self.assertEqual(self.manifest["schema"], validation.MANIFEST_SCHEMA)
        self.assertEqual(
            self.manifest["strategy"]["module"], "strategy_lab.strategies.earnings_drift_v1"
        )
        self.assertEqual(
            sorted(self.manifest["helpers"]), sorted(validation.HELPER_MODULES)
        )
        self.assertEqual(
            self.manifest["execution_policy"]["version"], "event_swing_14cal_v1"
        )
        self.assertIn("wilder_atr", self.manifest["indicator_formulas"])
        self.assertIn("python", self.manifest["runtime"])
        self.assertIn("dependencies", self.manifest["runtime"])

    def test_the_source_hashes_are_the_files_on_disk(self):
        for name in validation.HELPER_MODULES:
            with self.subTest(name):
                expected = hashlib.sha256((REPO_ROOT / name).read_bytes()).hexdigest()
                self.assertEqual(self.manifest["helpers"][name], expected)
        strategy_path = REPO_ROOT / "strategy_lab/strategies/earnings_drift_v1.py"
        self.assertEqual(
            self.manifest["strategy"]["source_sha256"],
            hashlib.sha256(strategy_path.read_bytes()).hexdigest(),
        )

    def test_an_indicator_formula_is_hashed_by_its_own_source(self):
        import inspect

        self.assertEqual(
            self.manifest["indicator_formulas"]["wilder_atr"],
            hashlib.sha256(inspect.getsource(wilder_atr).encode("utf-8")).hexdigest(),
        )

    def test_editing_a_file_moves_its_hash(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "helper.py"
            path.write_text("def atr():\n    return 1\n", encoding="utf-8")
            before = validation.source_sha256(path)
            path.write_text("def atr():\n    return 2\n", encoding="utf-8")
            self.assertNotEqual(before, validation.source_sha256(path))

    def test_a_policy_parameter_change_moves_the_manifest_hash(self):
        """A 2.0-ATR stop and a 2.5-ATR stop are not the same version."""
        from dataclasses import replace

        widened = replace(EVENT_SWING_14CAL_V1, stop_atr_multiple=2.5)
        drifted = dict(self.manifest)
        drifted["execution_policy"] = {
            **drifted["execution_policy"], "config": widened.canonical(),
        }
        self.assertNotEqual(
            validation.manifest_hash(self.manifest), validation.manifest_hash(drifted)
        )

    def test_a_runtime_change_moves_the_manifest_hash(self):
        drifted = dict(self.manifest)
        drifted["runtime"] = {**drifted["runtime"], "python": "3.99"}
        self.assertNotEqual(
            validation.manifest_hash(self.manifest), validation.manifest_hash(drifted)
        )

    def test_the_manifest_is_stable_across_rebuilds(self):
        self.assertEqual(
            earnings.build_version().manifest_hash, earnings.build_version().manifest_hash
        )

    def test_verify_manifest_accepts_the_matching_hash_and_refuses_a_stale_one(self):
        validation.verify_manifest(self.version, self.version.manifest_hash)
        with self.assertRaises(validation.ManifestDrift) as caught:
            validation.verify_manifest(self.version, "0" * 64)
        self.assertIn("register a new version", str(caught.exception))

    def test_every_roster_version_declares_no_third_party_dependency(self):
        """The V1 roster is stdlib only, and says so rather than omitting it."""
        for slug, version in build_versions().items():
            with self.subTest(slug):
                self.assertEqual(
                    version.implementation_manifest["runtime"]["dependencies"], {}
                )


class ReplayAndModeGuardTests(unittest.TestCase):
    def test_replay_is_allowed_for_a_replayable_arm_over_a_clean_snapshot(self):
        validation.guard_historical_replay(earnings.STRATEGY.metadata, a_snapshot())

    def test_a_shadow_only_version_cannot_be_asked_for_paper_or_live(self):
        from strategy_lab.strategies import short_term_reversal_v1 as reversal

        for mode in (ExecutionMode.PAPER, ExecutionMode.LIVE):
            with self.subTest(mode), self.assertRaises(validation.ModeRefused):
                validation.require_mode_allowed(reversal.STRATEGY.metadata, mode)

    def test_a_normal_version_may_run_in_any_tier(self):
        for mode in ExecutionMode:
            with self.subTest(mode):
                self.assertEqual(
                    validation.require_mode_allowed(earnings.STRATEGY.metadata, mode), mode
                )


class RegistryDriftTests(unittest.TestCase):
    """A source edit without a new registered version, rejected before evaluation."""

    def setUp(self):
        self.db = TestDatabase("strategy_lab_manifest")
        self.addCleanup(self.db.cleanup)
        from database.db import get_session, init_db

        init_db(self.db.url)
        self._ctx = get_session()
        self.session = self._ctx.__enter__()
        self.addCleanup(lambda: self._ctx.__exit__(None, None, None))
        self.version = earnings.build_version()
        registry.register_strategy_version(self.session, self.version)

    def test_the_registered_manifest_verifies_against_the_source_on_disk(self):
        registry.verify_registered_manifest(self.session, earnings.build_version())

    def test_an_edit_without_a_new_version_is_refused_before_evaluation(self):
        drifted = with_manifest(
            self.version,
            {
                **dict(self.version.implementation_manifest),
                "helpers": {
                    **dict(self.version.implementation_manifest["helpers"]),
                    "strategy_lab/indicators.py": "f" * 64,
                },
            },
        )
        with self.assertRaises(validation.ManifestDrift) as caught:
            registry.verify_registered_manifest(self.session, drifted)
        self.assertIn(earnings.SLUG, str(caught.exception))

    def test_activation_recomputes_the_manifest_before_moving_the_status(self):
        from strategy_lab.domain import StrategyVersionStatus

        drifted = with_manifest(
            self.version,
            {**dict(self.version.implementation_manifest), "runtime": {"python": "3.99"}},
        )
        with self.assertRaises(validation.ManifestDrift):
            registry.activate_strategy_version(
                self.session, drifted, StrategyVersionStatus.SHADOW
            )
        row = registry.require_strategy_version(
            self.session, self.version.slug, self.version.version
        )
        self.assertEqual(
            row.status, "draft", "a refused activation leaves the status alone"
        )
        registry.activate_strategy_version(
            self.session, earnings.build_version(), StrategyVersionStatus.SHADOW
        )
        row = registry.require_strategy_version(
            self.session, self.version.slug, self.version.version
        )
        self.assertEqual(row.status, "shadow")

    def test_re_registering_changed_rules_under_the_same_identity_is_refused(self):
        changed = StrategyVersion(**{
            **self.version.__dict__,
            "config": {**dict(self.version.config), "min_surprise_pct": 3.0},
        })
        with self.assertRaises(ImmutabilityError) as caught:
            registry.register_strategy_version(self.session, changed)
        self.assertIn("bump the version", str(caught.exception))

    def test_the_whole_roster_registers_and_verifies(self):
        for slug, version in build_versions().items():
            with self.subTest(slug):
                registry.register_strategy_version(self.session, version)
                registry.verify_registered_manifest(self.session, version)


if __name__ == "__main__":
    unittest.main()
