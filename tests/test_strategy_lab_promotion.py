"""Spec Q §8, §13: a tier change is owner-only, bound, audited, and not an approval.

The PR 6 requirements this file holds, in the order the brief states them:

* promotions and demotions are **owner-only, confirmed and append-only**; nothing
  auto-promotes, and the strongest label the system produces is
  ``ready_for_owner_review``;
* an authorization **binds** the source-evidence arm and its snapshot, a
  *separate inactive* target arm carrying the same immutable strategy version, and
  the **requested mode and risk budget**;
* **evidence reuse for a different target is refused**;
* a live activation **atomically replaces the one global champion**, or leaves the
  prior champion exactly as it was;
* a tier change requires a **complete evidence snapshot** and **warning
  acknowledgement**;
* a live promotion requires PR 5's capability check, every feature flag,
  kill-switch clearance, the current risk gates and owner confirmation;
* **promotion is not entry approval**: a promoted live arm places nothing until a
  separate signed, expiring, single-use callback approves one execution.

The confirmation's own four controls — owner binding, expiry, single use,
signature — are tested against :class:`PendingPromotions` directly, because those
are the controls a replayed or forged tap has to get past.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from database.db import get_session
from execution.brokers.capabilities import BrokerCapabilities
from execution.brokers.fake import FakeExecutionBroker
from orchestrator import strategy_lab_promotion as wiring
from strategy_lab import promotion as pm
from strategy_lab import registry
from strategy_lab.domain import (
    ArmStatus,
    ExecutionMode,
    PromotionKind,
    PromotionRefused,
)
from strategy_lab.execution import LIVE_VENUE, PAPER_VENUE
from tests import proposalfixture as pf
from tests import strategyexecfixture as fx
from tests.dbfixture import init_test_db
from tests.test_strategy_lab_domain import (
    CUTOFF,
    a_version_spec,
    an_experiment_spec,
)

NOW = datetime(2026, 4, 2, 14, 0)


def complete_metrics(**overrides) -> dict:
    """Keyword arguments for an evidence snapshot that passes §10's completeness."""
    base = dict(
        n_decisions=400,
        n_matured=150,
        n_closed=45,
        warnings=("small_sample_in_one_regime",),
        metrics={"net_return_after_costs": 0.04, "mean_r": 0.31},
        cost_assumptions={"slippage_bps": 10.0, "half_spread_bps": 5.0},
        uncertainty={"level": 0.9, "lower": 0.004, "upper": 0.08},
        benchmark="spy_total_return",
    )
    base.update(overrides)
    return base


class PromotionFixture(unittest.TestCase):
    """A registered version, an experiment, and a shadow arm with evidence."""

    def setUp(self):
        self.db = init_test_db("strategy_lab_promotion")
        self.addCleanup(self._dispose)
        self.settings = self.settings_for()
        with get_session() as session:
            registry.register_strategy_version(session, a_version_spec())
            registry.register_experiment(session, an_experiment_spec())
            self.shadow = registry.create_arm(
                session, fx.EXPERIMENT, "momentum_v1", "1.0.0",
                ExecutionMode.SHADOW, risk_budget=0.01,
            )
            registry.activate_arm(session, self.shadow.id)
            self.shadow_id = self.shadow.id
            self.evidence_id = self._evidence(session, self.shadow_id, CUTOFF)
            session.commit()

    def _dispose(self):
        from database import db as db_module

        if db_module.engine is not None:
            db_module.engine.dispose()
        db_module.engine = None
        db_module.SessionLocal = None
        self.db.cleanup()

    def settings_for(self, **overrides):
        base = dict(
            strategy_lab_experiment=fx.EXPERIMENT,
            strategy_lab_experiment_owner="bryan",
            strategy_lab_paper_risk_budget=0.005,
            strategy_lab_live_risk_budget=0.0,
            strategy_lab_promotion_floor_shadow_matured=100,
            strategy_lab_promotion_floor_paper_closed=30,
            strategy_lab_promotion_ttl_seconds=300,
        )
        base.update(overrides)
        return fx.lab_settings(**base)

    def _evidence(self, session, arm_id: int, cutoff, *, acknowledge=True, **overrides):
        row = registry.record_metric_snapshot(
            session, arm_id, cutoff, **complete_metrics(**overrides)
        )
        if acknowledge:
            registry.acknowledge_metric_warnings(session, row.id, "bryan")
        return row.id

    # -- helpers ---------------------------------------------------------- #

    def request(self, *, to_mode=ExecutionMode.PAPER, source=None, evidence=None, **over):
        with get_session() as session:
            req = wiring.build_request(
                session,
                self.settings,
                source_arm_id=source or self.shadow_id,
                to_mode=to_mode,
                owner="bryan",
                reason="preregistered gate met",
                evidence_metric_snapshot_id=evidence,
            )
            session.commit()
        if over:
            from dataclasses import replace

            req = replace(req, **over)
        return req

    def plan(self, request, *, adapters=None):
        return wiring.plan(self.settings, request, adapters=adapters or {})

    def arm(self, arm_id: int):
        with get_session() as session:
            row = registry.require_arm(session, arm_id)
            return (row.mode, row.status, float(row.risk_budget))


# --------------------------------------------------------------------------- #
# 1. The bindings
# --------------------------------------------------------------------------- #


class BindingTests(PromotionFixture):
    def test_rendering_prepares_an_inactive_target_and_activates_nothing(self):
        request = self.request()
        self.assertNotEqual(request.target_arm_id, self.shadow_id)
        self.assertEqual(
            self.arm(request.target_arm_id), ("paper", "inactive", 0.005)
        )

    def test_rendering_twice_prepares_one_arm(self):
        first = self.request()
        second = self.request()
        self.assertEqual(first.target_arm_id, second.target_arm_id)

    def test_the_plan_writes_no_promotion_event(self):
        self.plan(self.request())
        with get_session() as session:
            self.assertEqual(
                len(registry.promotions_for(session, self.request().target_arm_id)), 0
            )

    def test_a_confirmable_plan_is_ready_for_owner_review_and_not_promote(self):
        plan = self.plan(self.request())
        self.assertTrue(plan.confirmable, plan.refusals + plan.external_refusals)
        self.assertEqual(plan.recommendation, pm.READY_FOR_OWNER_REVIEW)
        self.assertNotIn("promote", plan.recommendation)

    def test_a_requested_mode_that_is_not_the_target_arms_mode_is_refused(self):
        """The card said paper; confirming must not activate something else."""
        request = self.request(requested_mode=ExecutionMode.LIVE)
        plan = self.plan(request)
        self.assertFalse(plan.confirmable)
        self.assertTrue(
            any("was requested" in item for item in plan.refusals), plan.refusals
        )

    def test_a_risk_budget_changed_after_the_render_invalidates_the_card(self):
        request = self.request()
        with get_session() as session:
            registry.require_arm(session, request.target_arm_id).risk_budget = 0.01
            session.commit()
        plan = self.plan(request)
        self.assertFalse(plan.confirmable)
        self.assertTrue(
            any("risk budget" in item for item in plan.refusals), plan.refusals
        )

    def test_the_source_arm_cannot_be_its_own_target(self):
        request = self.request(target_arm_id=self.shadow_id)
        plan = self.plan(request)
        self.assertFalse(plan.confirmable)

    def test_an_active_target_is_refused(self):
        request = self.request()
        with get_session() as session:
            registry.activate_arm(session, request.target_arm_id)
            session.commit()
        plan = self.plan(request)
        self.assertFalse(plan.confirmable)
        self.assertTrue(any("inactive" in item for item in plan.refusals), plan.refusals)

    def test_evidence_belonging_to_another_arm_cannot_authorize_this_one(self):
        request = self.request()
        with get_session() as session:
            other = registry.create_arm(
                session, fx.EXPERIMENT, "momentum_v1", "1.0.0",
                ExecutionMode.LIVE, risk_budget=0.0,
            )
            foreign = self._evidence(session, other.id, CUTOFF + timedelta(days=1))
            session.commit()
        from dataclasses import replace

        plan = self.plan(replace(request, evidence_metric_snapshot_id=foreign))
        self.assertFalse(plan.confirmable)
        self.assertTrue(
            any("cannot authorize another" in item for item in plan.refusals),
            plan.refusals,
        )


# --------------------------------------------------------------------------- #
# 2. Evidence
# --------------------------------------------------------------------------- #


class EvidenceTests(PromotionFixture):
    def test_unacknowledged_warnings_block_a_tier_change(self):
        with get_session() as session:
            unacknowledged = self._evidence(
                session, self.shadow_id, CUTOFF + timedelta(days=2), acknowledge=False
            )
            session.commit()
        plan = self.plan(self.request(evidence=unacknowledged))
        self.assertFalse(plan.confirmable)
        self.assertTrue(
            any("acknowledg" in item for item in plan.refusals), plan.refusals
        )

    def test_an_incomplete_snapshot_is_not_weaker_evidence_but_no_evidence(self):
        with get_session() as session:
            incomplete = self._evidence(
                session, self.shadow_id, CUTOFF + timedelta(days=3),
                cost_assumptions={}, benchmark="",
            )
            session.commit()
        plan = self.plan(self.request(evidence=incomplete))
        self.assertFalse(plan.confirmable)
        self.assertIn("cost_assumptions", plan.evidence.missing)
        self.assertIn("benchmark", plan.evidence.missing)
        self.assertEqual(plan.recommendation, pm.BLOCKED)

    def test_completeness_alone_is_not_the_floor(self):
        with get_session() as session:
            thin = self._evidence(
                session, self.shadow_id, CUTOFF + timedelta(days=4), n_matured=7
            )
            session.commit()
        plan = self.plan(self.request(evidence=thin))
        self.assertTrue(plan.evidence.complete)
        self.assertFalse(plan.evidence.floor_met)
        self.assertFalse(plan.confirmable)

    def test_the_floor_that_applies_depends_on_the_tier(self):
        """Shadow evidence is counted in matured decisions, paper in closed ones."""
        with get_session() as session:
            to_paper = pm.evidence_completeness(
                session, self.evidence_id,
                to_mode=ExecutionMode.PAPER, floors=wiring.floors(self.settings),
            )
            to_live = pm.evidence_completeness(
                session, self.evidence_id,
                to_mode=ExecutionMode.LIVE, floors=wiring.floors(self.settings),
            )
        self.assertEqual(to_paper.floor_name, "shadow_matured_decisions")
        self.assertEqual(to_live.floor_name, "paper_closed_executions")

    def test_an_insufficient_sample_is_labelled_rather_than_ranked(self):
        evidence = pm.EvidenceCompleteness(
            metric_snapshot_id=1, arm_id=1, complete=True, missing=(), warnings=(),
            warnings_acknowledged=True, n_decisions=10, n_matured=7, n_closed=0,
            floor_name="shadow_matured_decisions", floor_value=100, floor_met=False,
        )
        self.assertEqual(pm.recommendation(evidence, ()), pm.INSUFFICIENT_EVIDENCE)


# --------------------------------------------------------------------------- #
# 3. Evidence reuse
# --------------------------------------------------------------------------- #


class EvidenceReuseTests(PromotionFixture):
    def setUp(self):
        super().setUp()
        self.paper_request = self.request()
        wiring.confirm(self.settings, self.paper_request, adapters={})

    def test_the_confirmed_promotion_activated_the_target(self):
        self.assertEqual(
            self.arm(self.paper_request.target_arm_id), ("paper", "active", 0.005)
        )

    def test_the_same_evidence_cannot_authorize_a_different_target(self):
        with get_session() as session:
            second = registry.create_arm(
                session, fx.EXPERIMENT, "momentum_v1", "1.0.0",
                ExecutionMode.LIVE, risk_budget=0.0,
            )
            session.commit()
            second_id = second.id
        from dataclasses import replace

        request = replace(
            self.paper_request,
            target_arm_id=second_id,
            requested_mode=ExecutionMode.LIVE,
            requested_risk_budget=0.0,
        )
        plan = self.plan(request)
        self.assertFalse(plan.confirmable)
        self.assertTrue(
            any("already authorized" in item for item in plan.refusals), plan.refusals
        )

    def test_the_same_evidence_cannot_re_promote_the_same_arm(self):
        plan = self.plan(self.paper_request)
        self.assertFalse(plan.confirmable)
        self.assertTrue(
            any("already been used" in item for item in plan.refusals), plan.refusals
        )

    def test_the_promotion_event_log_is_append_only(self):
        with get_session() as session:
            rows = registry.promotions_for(session, self.paper_request.target_arm_id)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].kind, PromotionKind.PROMOTION.value)
            self.assertEqual(rows[0].owner, "bryan")
            self.assertEqual(
                rows[0].evidence_metric_snapshot_id, self.evidence_id
            )


# --------------------------------------------------------------------------- #
# 4. The live tier
# --------------------------------------------------------------------------- #


def a_live_adapter(**overrides):
    """A fake declaring Robinhood's capabilities, never reaching a network."""
    broker = FakeExecutionBroker(venue=LIVE_VENUE, **overrides)
    return broker


def a_blind_adapter():
    """An adapter that cannot say whether it can protect a position."""

    class Blind:
        venue = LIVE_VENUE

        def capabilities(self):
            return None

    return Blind()


def an_unprotectable_adapter():
    """Robinhood, minus the one primitive Spec Q §12's closing paragraph names."""
    return FakeExecutionBroker(
        venue=LIVE_VENUE,
        declared_capabilities=BrokerCapabilities(
            can_read_positions=True,
            can_read_orders=True,
            can_read_cash=True,
            can_place_equity_market=True,
            can_place_equity_limit=True,
            can_place_attached_stop=False,
            can_place_standalone_gtc_stop=False,
            supports_fractional=True,
            supports_specified_lot_sale=True,
        ),
    )


class LiveTierFixture(PromotionFixture):
    """The shadow arm promoted to paper, with paper evidence of its own."""

    def setUp(self):
        super().setUp()
        wiring.confirm(self.settings, self.request(), adapters={})
        with get_session() as session:
            self.paper_id = registry.active_live_arm(session) and None
            arms = registry.arms_for_experiment(session, fx.EXPERIMENT)
            self.paper_id = next(
                a.id for a in arms if a.mode == "paper" and a.status == "active"
            )
            self.paper_evidence = self._evidence(
                session, self.paper_id, CUTOFF + timedelta(days=30), n_closed=45
            )
            session.commit()

    def live_request(self, **over):
        return self.request(
            to_mode=ExecutionMode.LIVE,
            source=self.paper_id,
            evidence=self.paper_evidence,
            **over,
        )


class LiveGateTests(LiveTierFixture):
    def test_every_live_gate_closed_is_named_at_once(self):
        self.settings = self.settings_for(
            allow_live_trading=False,
            execution_mode="paper",
            strategy_lab_live_enabled=False,
        )
        plan = self.plan(self.live_request(), adapters={})
        self.assertFalse(plan.confirmable)
        joined = " | ".join(plan.external_refusals)
        self.assertIn("STRATEGY_LAB_LIVE_ENABLED is false", joined)
        self.assertIn("live_trading_disabled", joined)
        self.assertIn("exit capability cannot be verified", joined)

    def test_a_live_promotion_with_every_gate_open_is_confirmable(self):
        self.settings = self.settings_for(
            allow_live_trading=True,
            execution_mode="live",
            strategy_lab_live_enabled=True,
        )
        plan = self.plan(self.live_request(), adapters={LIVE_VENUE: a_live_adapter()})
        self.assertTrue(plan.confirmable, plan.refusals + plan.external_refusals)

    def test_the_kill_switch_blocks_a_live_promotion(self):
        from portfolio import killswitch

        self.settings = self.settings_for(
            allow_live_trading=True, execution_mode="live",
            strategy_lab_live_enabled=True,
        )
        with get_session() as session:
            killswitch.set_switch(session, on=True, changed_by="bryan", reason="incident")
            session.commit()
        plan = self.plan(self.live_request(), adapters={LIVE_VENUE: a_live_adapter()})
        self.assertFalse(plan.confirmable)
        self.assertTrue(
            any("kill_switch" in item for item in plan.external_refusals),
            plan.external_refusals,
        )

    def test_an_adapter_that_cannot_protect_a_position_blocks_live(self):
        self.settings = self.settings_for(
            allow_live_trading=True, execution_mode="live",
            strategy_lab_live_enabled=True,
        )
        plan = self.plan(
            self.live_request(), adapters={LIVE_VENUE: an_unprotectable_adapter()}
        )
        self.assertFalse(plan.confirmable)
        self.assertTrue(
            any("capability_refused" in item for item in plan.external_refusals),
            plan.external_refusals,
        )

    def test_an_adapter_that_declares_nothing_is_treated_as_one_that_cannot(self):
        self.settings = self.settings_for(
            allow_live_trading=True, execution_mode="live",
            strategy_lab_live_enabled=True,
        )
        plan = self.plan(self.live_request(), adapters={LIVE_VENUE: a_blind_adapter()})
        self.assertFalse(plan.confirmable)
        self.assertTrue(
            any("declares no capabilities" in item for item in plan.external_refusals),
            plan.external_refusals,
        )

    def test_a_live_arm_is_prepared_with_a_zero_budget_by_default(self):
        """And that is the safety property, not an unfinished default."""
        request = self.live_request()
        self.assertEqual(self.arm(request.target_arm_id), ("live", "inactive", 0.0))

    def test_confirming_refuses_when_a_gate_closes_after_the_render(self):
        from portfolio import killswitch

        self.settings = self.settings_for(
            allow_live_trading=True, execution_mode="live",
            strategy_lab_live_enabled=True,
        )
        request = self.live_request()
        self.assertTrue(
            self.plan(request, adapters={LIVE_VENUE: a_live_adapter()}).confirmable
        )
        with get_session() as session:
            killswitch.set_switch(session, on=True, changed_by="bryan", reason="incident")
            session.commit()
        with self.assertRaises(PromotionRefused):
            wiring.confirm(
                self.settings, request, adapters={LIVE_VENUE: a_live_adapter()}
            )
        self.assertEqual(self.arm(request.target_arm_id)[1], "inactive")


class ChampionTests(LiveTierFixture):
    def setUp(self):
        super().setUp()
        self.settings = self.settings_for(
            allow_live_trading=True, execution_mode="live",
            strategy_lab_live_enabled=True,
        )
        self.adapters = {LIVE_VENUE: a_live_adapter()}

    def test_a_live_confirmation_makes_the_target_the_single_champion(self):
        request = self.live_request()
        wiring.confirm(self.settings, request, adapters=self.adapters)
        with get_session() as session:
            champion = registry.active_live_arm(session)
            self.assertIsNotNone(champion)
            self.assertEqual(champion.id, request.target_arm_id)

    def test_a_failed_live_activation_leaves_the_prior_champion_unchanged(self):
        first = self.live_request()
        wiring.confirm(self.settings, first, adapters=self.adapters)

        # A second live arm, under a second strategy version, whose promotion is
        # refused: its evidence snapshot is incomplete.
        with get_session() as session:
            from dataclasses import replace as dc_replace

            spec = dc_replace(a_version_spec(), version="1.1.0")
            registry.register_strategy_version(session, spec)
            other_shadow = registry.create_arm(
                session, fx.EXPERIMENT, "momentum_v1", "1.1.0",
                ExecutionMode.LIVE, risk_budget=0.0,
            )
            bad_evidence = self._evidence(
                session, self.paper_id, CUTOFF + timedelta(days=60),
                benchmark="", acknowledge=True,
            )
            session.commit()
            other_id = other_shadow.id

        request = pm.PromotionRequest(
            source_arm_id=self.paper_id,
            target_arm_id=other_id,
            evidence_metric_snapshot_id=bad_evidence,
            requested_mode=ExecutionMode.LIVE,
            requested_risk_budget=0.0,
            owner="bryan",
            reason="a second champion",
        )
        with self.assertRaises(PromotionRefused):
            wiring.confirm(self.settings, request, adapters=self.adapters)
        with get_session() as session:
            champion = registry.active_live_arm(session)
            self.assertEqual(champion.id, first.target_arm_id)


# --------------------------------------------------------------------------- #
# 5. Demotion
# --------------------------------------------------------------------------- #


class DemotionTests(LiveTierFixture):
    def setUp(self):
        super().setUp()
        self.settings = self.settings_for(
            allow_live_trading=True, execution_mode="live",
            strategy_lab_live_enabled=True,
        )
        self.live_request_row = self.live_request()
        wiring.confirm(
            self.settings, self.live_request_row, adapters={LIVE_VENUE: a_live_adapter()}
        )
        self.live_id = self.live_request_row.target_arm_id

    def test_a_demotion_stands_the_source_arm_down(self):
        """The one asymmetry: promoting says nothing about the arm you left."""
        with get_session() as session:
            # A fresh paper arm to demote into, under a second version, plus
            # evidence collected by the live arm itself.
            from dataclasses import replace as dc_replace

            registry.register_strategy_version(
                session, dc_replace(a_version_spec(), version="1.2.0")
            )
            target = registry.create_arm(
                session, fx.EXPERIMENT, "momentum_v1", "1.0.0",
                ExecutionMode.PAPER, risk_budget=0.005,
            )
            session.commit()
        # The one active paper arm is already the promotion target from setUp, so
        # demote the live champion back into it after standing it down.
        with get_session() as session:
            registry.set_arm_status(session, self.paper_id, ArmStatus.INACTIVE)
            evidence = self._evidence(
                session, self.live_id, CUTOFF + timedelta(days=90), n_closed=45
            )
            session.commit()
        request = pm.PromotionRequest(
            source_arm_id=self.live_id,
            target_arm_id=self.paper_id,
            evidence_metric_snapshot_id=evidence,
            requested_mode=ExecutionMode.PAPER,
            requested_risk_budget=0.005,
            owner="bryan",
            reason="drawdown breached the guardrail",
        )
        plan = wiring.plan(self.settings, request, adapters={})
        self.assertEqual(plan.kind, PromotionKind.DEMOTION)
        self.assertTrue(plan.confirmable, plan.refusals + plan.external_refusals)
        wiring.confirm(self.settings, request, adapters={})
        self.assertEqual(self.arm(self.paper_id), ("paper", "active", 0.005))
        self.assertEqual(self.arm(self.live_id)[1], "paused")
        with get_session() as session:
            self.assertIsNone(registry.active_live_arm(session))
        _ = target


# --------------------------------------------------------------------------- #
# 6. Promotion is not entry approval
# --------------------------------------------------------------------------- #


class PromotionIsNotApprovalTests(LiveTierFixture):
    """Spec Q §13: "A strategy promotion never counts as approval of an entry.\""""

    def setUp(self):
        super().setUp()
        self.settings = self.settings_for(
            allow_live_trading=True,
            execution_mode="live",
            strategy_lab_live_enabled=True,
            strategy_lab_live_risk_budget=0.005,
        )
        self.broker = a_live_adapter(fill_price=100.0)
        wiring.confirm(
            self.settings, self.live_request(), adapters={LIVE_VENUE: self.broker}
        )

    def test_a_promoted_live_arm_has_placed_nothing(self):
        self.assertEqual(self.broker.order_calls, 0, self.broker.calls)

    def test_the_promoted_arm_still_needs_a_separate_signed_approval(self):
        from strategy_lab.domain import ExecutionState

        with get_session() as session:
            fx.synced(session)
            champion = registry.active_live_arm(session)
            snapshot = registry.record_snapshot(
                session, __import__("tests.test_strategy_lab_domain",
                                    fromlist=["x"]).a_universe_snapshot()
            )
            decision = registry.record_decision(
                session, champion.id, snapshot.id,
                fx.a_decision(snapshot, "AMD"),
            )
            session.commit()
            arm_id, decision_id = champion.id, decision.id

        service = fx.service(
            broker=self.broker,
            settings=self.settings,
            venue=LIVE_VENUE,
            resolver=pf.resolver_for({}),
        )
        from execution.strategy_lifecycle import ArmExecutionRequest

        card = service.propose(
            ArmExecutionRequest(
                arm_id=arm_id,
                decision_id=decision_id,
                mode=ExecutionMode.LIVE,
                ticker="AMD",
                entry=100.0,
                stop=95.0,
                risk_fraction=0.005,
                expected_hold_sessions=5,
            ),
            now=pf.NOW,
        )
        self.assertEqual(card.status, ExecutionState.PROPOSED.value, card.message)
        self.assertEqual(
            self.broker.order_calls, 0,
            "a promotion placed an order; it may only produce a proposal",
        )
        self.assertTrue(card.approval_signature)


# --------------------------------------------------------------------------- #
# 7. The confirmation's four controls
# --------------------------------------------------------------------------- #


class PendingConfirmationTests(PromotionFixture):
    def setUp(self):
        super().setUp()
        self.store = wiring.PendingPromotions(self.settings)
        self.req = self.request()
        self.token, self.confirm_cb, self.cancel_cb, self.expires = self.store.offer(
            self.req, owner_id="99887766", now=NOW
        )

    def _presented(self) -> str:
        return self.confirm_cb.split(":")[-1]

    def test_the_callback_fits_telegrams_sixty_four_byte_budget(self):
        self.assertLessEqual(len(self.confirm_cb.encode("utf-8")), 64)
        self.assertLessEqual(len(self.cancel_cb.encode("utf-8")), 64)

    def test_a_valid_confirmation_returns_the_request(self):
        taken = self.store.take(
            self.token, presented=self._presented(), owner_id="99887766", now=NOW
        )
        self.assertEqual(taken.target_arm_id, self.req.target_arm_id)

    def test_it_is_single_use(self):
        self.store.take(
            self.token, presented=self._presented(), owner_id="99887766", now=NOW
        )
        with self.assertRaises(wiring.PromotionGateRefused):
            self.store.take(
                self.token, presented=self._presented(), owner_id="99887766", now=NOW
            )

    def test_it_is_owner_bound(self):
        with self.assertRaises(wiring.PromotionGateRefused):
            self.store.take(
                self.token, presented=self._presented(), owner_id="12345", now=NOW
            )

    def test_it_expires(self):
        later = NOW + timedelta(seconds=3600)
        with self.assertRaises(wiring.PromotionGateRefused):
            self.store.take(
                self.token, presented=self._presented(), owner_id="99887766", now=later
            )

    def test_a_forged_signature_is_refused(self):
        with self.assertRaises(wiring.PromotionGateRefused):
            self.store.take(
                self.token, presented="0" * 18, owner_id="99887766", now=NOW
            )

    def test_a_truncated_signature_is_refused(self):
        with self.assertRaises(wiring.PromotionGateRefused):
            self.store.take(
                self.token, presented=self._presented()[:6], owner_id="99887766", now=NOW
            )

    def test_cancelling_consumes_it(self):
        self.assertTrue(self.store.cancel(self.token))
        with self.assertRaises(wiring.PromotionGateRefused):
            self.store.take(
                self.token, presented=self._presented(), owner_id="99887766", now=NOW
            )

    def test_a_malformed_callback_is_refused_rather_than_parsed(self):
        for data in ("", "nonsense", "slpr:abc:def", "p6ok:1:x"):
            with self.assertRaises(wiring.PromotionGateRefused):
                self.store.parse(data)

    def test_an_unset_signing_secret_mints_nothing(self):
        from portfolio.approvals import ApprovalRefused

        store = wiring.PendingPromotions(
            self.settings_for(execution_approval_secret="")
        )
        with self.assertRaises(ApprovalRefused):
            store.offer(self.req, owner_id="99887766", now=NOW)


# --------------------------------------------------------------------------- #
# 8. Nothing auto-promotes
# --------------------------------------------------------------------------- #


class NoAutomaticPromotionTests(unittest.TestCase):
    def test_no_module_calls_confirm_outside_the_owner_path(self):
        """Spec Q §3: promotion authority is owner-only.

        The only callers of a confirmation are the Telegram handler (behind
        ``@authorized`` and a signed callback) and the tests. A scheduled job, the
        pipeline hook or the dispatcher calling it would be an automatic
        promotion, which is the one thing §3 forbids outright.
        """
        import subprocess
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        out = subprocess.run(
            ["grep", "-rn", "-E", r"(promotion\.confirm|wiring\.confirm|record_promotion)\(",
             "--include=*.py", "."],
            cwd=root, capture_output=True, text=True,
        ).stdout.splitlines()
        allowed_prefixes = (
            # The workflow itself and its one writer.
            "./strategy_lab/promotion.py",
            "./strategy_lab/registry.py",
            "./orchestrator/strategy_lab_promotion.py",
            # The owner path: `@authorized`, behind a signed single-use callback.
            "./bot/handlers/strategy_lab.py",
            "./tests/",
            "./docs/",
        )
        offenders = [
            line for line in out
            if not line.startswith(allowed_prefixes)
        ]
        self.assertEqual(offenders, [], f"a non-owner path promotes: {offenders}")

    def test_the_scheduler_registers_no_promotion_job(self):
        import inspect

        from orchestrator import scheduler

        source = inspect.getsource(scheduler)
        for forbidden in ("promote", "promotion"):
            self.assertNotIn(
                forbidden, source,
                f"the scheduler mentions {forbidden!r}; promotion is owner-only",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
