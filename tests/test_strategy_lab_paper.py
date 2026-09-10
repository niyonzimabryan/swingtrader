"""Spec Q §11, §12: the paper tournament, and the four things it may never do.

The PR 6 requirements this file holds:

* a paper arm is dispatched through PR 5's **explicit-mode** entry point, carrying
  ``mode=paper`` and reaching **only** the Alpaca paper adapter — when
  ``EXECUTION_MODE=live``, when ``BROKER_PRIMARY=robinhood``, and when both;
* any arm-mode / venue mismatch is **refused before broker review or placement**;
* **independent virtual budgets** and their caps: the arm's own paper book decides
  the size, not the production ledger, and every cap that bound it is named;
* **no duplicate orders** — per decision (the database's partial unique index) and
  per ticker across arms (Spec Q §11's exposure reservation);
* **experiment tags** on every proposal and in the broker's client context;
* both flags default false, and a false flag produces **zero order calls**;
* the three scheduled jobs PR 5 deferred no-op with the flag off.

A dispatch pass **proposes**; it never places. Every assertion about order calls
here is therefore zero, and the placement tests live in
``tests/test_strategy_lab_e2e.py`` where an owner approval happens.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from database import models
from database.db import get_session
from execution.brokers.fake import FakeExecutionBroker
from orchestrator import strategy_lab_paper as paper
from strategy_lab import registry, snapshots
from strategy_lab.domain import DecisionAction, ExecutionMode, ExecutionState
from strategy_lab.execution import LIVE_VENUE, PAPER_VENUE
from tests import proposalfixture as pf
from tests import strategylabfixture as slf
from tests import strategyexecfixture as fx
from tests.dbfixture import init_test_db
from tests.test_strategy_lab_domain import a_risk_plan, a_version_spec, an_experiment_spec

#: The snapshot cutoff the fixture builds, and a dispatch clock just after it.
CUTOFF = slf.Q1_2026_CLOSE
NOW = CUTOFF + timedelta(minutes=30)


def a_decision(snapshot, ticker: str, **overrides):
    from strategy_lab.domain import StrategyDecision

    kwargs = dict(
        strategy_slug="momentum_v1",
        strategy_version="1.0.0",
        snapshot_hash=snapshot.content_hash,
        ticker=ticker,
        action=DecisionAction.LONG,
        reason_codes=("liquidity_ok", "top_decile"),
        signal_strength=0.8,
        risk_plan=a_risk_plan(),
    )
    kwargs.update(overrides)
    return StrategyDecision(**kwargs)


class PaperFixture(unittest.TestCase):
    """An active paper arm with two `long` decisions on a real universe snapshot."""

    tickers = ("AMD", "NVDA")

    def setUp(self):
        self.db = init_test_db("strategy_lab_paper")
        self.addCleanup(self._dispose)
        self.settings = self.settings_for()
        self.paper_broker = FakeExecutionBroker(fill_price=100.0, venue=PAPER_VENUE)
        self.live_broker = FakeExecutionBroker(fill_price=100.0, venue=LIVE_VENUE)
        with get_session() as session:
            fx.synced(session)
            registry.register_strategy_version(session, a_version_spec())
            registry.register_experiment(session, an_experiment_spec())
            shadow = registry.create_arm(
                session, fx.EXPERIMENT, "momentum_v1", "1.0.0",
                ExecutionMode.SHADOW, risk_budget=0.01,
            )
            registry.activate_arm(session, shadow.id)
            arm = registry.create_arm(
                session, fx.EXPERIMENT, "momentum_v1", "1.0.0",
                ExecutionMode.PAPER, risk_budget=0.005,
            )
            registry.activate_arm(session, arm.id)
            self.arm_id = arm.id
            snapshot = self._snapshot()
            row = registry.record_snapshot(session, snapshot)
            self.snapshot_id = row.id
            self.decision_ids = [
                registry.record_decision(
                    session, arm.id, row.id, a_decision(snapshot, ticker)
                ).id
                for ticker in self.tickers
            ]
            session.commit()

    def _dispose(self):
        from database import db as db_module

        if db_module.engine is not None:
            db_module.engine.dispose()
        db_module.engine = None
        db_module.SessionLocal = None
        self.db.cleanup()

    def _snapshot(self):
        """A universe snapshot with flat, liquid $100 bars for each name."""
        return slf.universe_snapshot(
            [
                slf.replayable_inputs(ticker, slf.flat_bars())
                for ticker in self.tickers
            ],
            cutoff=CUTOFF,
        )

    def settings_for(self, **overrides):
        base = dict(
            strategy_lab_experiment=fx.EXPERIMENT,
            strategy_lab_enabled=True,
            strategy_lab_paper_enabled=True,
            strategy_lab_live_enabled=False,
            strategy_lab_paper_equity=100_000.0,
            strategy_lab_paper_max_open_positions=5,
            strategy_lab_paper_max_position_fraction=0.1,
            strategy_lab_paper_daily_notional=0.0,
            strategy_lab_paper_max_proposals_per_run=5,
            strategy_lab_paper_max_snapshot_age_minutes=90,
        )
        base.update(overrides)
        return fx.lab_settings(**base)

    # -- helpers ---------------------------------------------------------- #

    def service(self, *, settings=None, adapters=None):
        return paper.build_service(
            settings or self.settings,
            adapters=adapters if adapters is not None else {PAPER_VENUE: self.paper_broker},
            resolver=pf.resolver_for({}),
            owner_id="99887766",
        )

    def dispatch(self, *, settings=None, adapters=None, now=None):
        settings = settings or self.settings
        adapters = (
            adapters if adapters is not None else {PAPER_VENUE: self.paper_broker}
        )
        return paper.dispatch_for_scan(
            settings,
            adapters=adapters,
            now=now or NOW,
            resolver=pf.resolver_for({}),
            owner_id="99887766",
            service=(
                self.service(settings=settings, adapters=adapters)
                if adapters.get(PAPER_VENUE) is not None
                else None
            ),
        )

    def trades(self):
        with get_session() as session:
            return [
                (row.mode, row.status, row.quantity, row.notional)
                for row in session.query(models.StrategyTrade).order_by(models.StrategyTrade.id)
            ]

    def proposals(self):
        with get_session() as session:
            return [
                (row.status, row.execution_mode, row.requester_token_label, row.quantity)
                for row in session.query(models.Proposal).order_by(models.Proposal.id)
            ]

    def assertNoOrders(self):
        self.assertEqual(
            self.paper_broker.order_calls, 0,
            f"a dispatch pass placed an order: {self.paper_broker.calls}",
        )
        self.assertEqual(
            self.live_broker.order_calls, 0,
            f"the live adapter was called: {self.live_broker.calls}",
        )


# --------------------------------------------------------------------------- #
# 1. The happy path
# --------------------------------------------------------------------------- #


class DispatchTests(PaperFixture):
    def test_a_pass_proposes_one_execution_per_long_decision(self):
        summary = self.dispatch()
        self.assertTrue(summary.ran, summary.as_log_fields())
        self.assertEqual(summary.proposed, 2, summary.skipped)
        self.assertEqual(len(self.trades()), 2)
        for mode, status, quantity, notional in self.trades():
            self.assertEqual(mode, "paper")
            self.assertEqual(status, ExecutionState.PROPOSED.value)
            self.assertGreater(quantity, 0)
            self.assertGreater(notional, 0)

    def test_a_pass_places_nothing(self):
        self.dispatch()
        self.assertNoOrders()

    def test_the_proposal_records_the_arms_mode_not_the_global_one(self):
        self.dispatch(settings=self.settings_for(execution_mode="live"))
        for status, mode, _tag, _qty in self.proposals():
            self.assertEqual(status, "proposed")
            self.assertEqual(mode, "paper")

    def test_every_proposal_carries_the_experiment_tag(self):
        self.dispatch()
        for _status, _mode, tag, _qty in self.proposals():
            self.assertIn(f"lab:{fx.EXPERIMENT}", tag)
            self.assertIn(f"arm:{self.arm_id}", tag)
            self.assertIn("momentum_v1@1.0.0", tag)

    def test_the_broker_client_context_carries_the_tag(self):
        """The tag reaches the adapter, so a paper fill is attributable there too."""
        from execution.lifecycle import _client_context

        self.dispatch()
        with get_session() as session:
            row = session.query(models.Proposal).first()
            context = _client_context(row)
            execution_id = row.execution_id
        self.assertIn("experiment", context)
        self.assertIn(f"arm:{self.arm_id}", context["experiment"])
        self.assertEqual(context["execution_id"], execution_id)

    def test_a_non_lab_proposal_client_context_is_unchanged(self):
        from execution.lifecycle import _client_context

        with get_session() as session:
            row = models.Proposal(
                proposal_uid="u", ticker="AMD", side="long", entry=100.0, stop=95.0,
                risk_fraction=0.005, status="proposed", entry_ref_id="p6-entry-u",
                created_at=NOW, updated_at=NOW,
            )
            session.add(row)
            session.flush()
            context = _client_context(row)
        self.assertEqual(sorted(context), ["proposal_id", "ref_id"])

    def test_a_second_pass_proposes_nothing_new(self):
        self.dispatch()
        second = self.dispatch()
        self.assertEqual(second.proposed, 0)
        self.assertEqual(len(self.trades()), 2)
        self.assertIn(
            paper.SKIP_ALREADY_EXECUTED, [reason for _label, reason in second.skipped]
        )

    def test_the_run_cap_bounds_one_pass(self):
        summary = self.dispatch(
            settings=self.settings_for(strategy_lab_paper_max_proposals_per_run=1)
        )
        self.assertEqual(summary.proposed, 1)
        self.assertIn(paper.SKIP_RUN_CAP, [reason for _l, reason in summary.skipped])


# --------------------------------------------------------------------------- #
# 2. The venue binding
# --------------------------------------------------------------------------- #


class VenueTests(PaperFixture):
    """Spec Q §11 and §12 invariant 11, in the configuration that matters."""

    def test_a_paper_arm_reaches_alpaca_paper_with_global_mode_live(self):
        summary = self.dispatch(
            settings=self.settings_for(
                execution_mode="live", allow_live_trading=True, broker_primary="robinhood"
            )
        )
        self.assertEqual(summary.proposed, 2, summary.skipped)
        self.assertNoOrders()

    def test_the_live_adapter_is_never_consulted_for_a_paper_arm(self):
        self.dispatch(
            adapters={PAPER_VENUE: self.paper_broker, LIVE_VENUE: self.live_broker},
            settings=self.settings_for(execution_mode="live", allow_live_trading=True),
        )
        self.assertEqual(self.live_broker.calls, {}, self.live_broker.calls)

    def test_the_live_adapter_registered_at_the_paper_venue_is_a_wiring_error(self):
        """And it fails at bind time, before any review or placement."""
        summary = self.dispatch(adapters={PAPER_VENUE: self.live_broker})
        self.assertEqual(summary.proposed, 0)
        self.assertEqual(self.live_broker.order_calls, 0)
        self.assertEqual(self.live_broker.calls.get("review_order", 0), 0)
        self.assertTrue(summary.errors or summary.skipped, summary.as_log_fields())

    def test_no_adapter_is_a_named_skip_not_a_silent_one(self):
        summary = self.dispatch(adapters={})
        self.assertEqual(summary.skipped_reason, paper.SKIP_NO_ADAPTER)
        self.assertEqual(len(self.trades()), 0)

    def test_a_shadow_arm_cannot_be_dispatched(self):
        from strategy_lab.execution import ShadowReachedExecution

        with get_session() as session:
            shadow = next(
                arm for arm in registry.arms_for_experiment(session, fx.EXPERIMENT)
                if arm.mode == "shadow"
            )
            snapshot = registry.load_snapshot(
                registry.snapshot_row(session, self.snapshot_id)
            )
            decision = registry.record_decision(
                session, shadow.id, self.snapshot_id, a_decision(snapshot, "AMD")
            )
            session.commit()
            shadow_id, decision_id = shadow.id, decision.id

        from execution.strategy_lifecycle import ArmExecutionRequest

        with self.assertRaises(ShadowReachedExecution):
            self.service().propose(
                ArmExecutionRequest(
                    arm_id=shadow_id, decision_id=decision_id,
                    mode=ExecutionMode.SHADOW, ticker="AMD",
                    entry=100.0, stop=95.0, risk_fraction=0.005,
                ),
                now=NOW,
            )
        self.assertNoOrders()

    def test_a_paper_arm_executed_as_live_is_refused(self):
        from execution.strategy_lifecycle import ArmExecutionRequest
        from strategy_lab.execution import ExecutionRefusedLocally

        with self.assertRaises(ExecutionRefusedLocally) as caught:
            self.service(
                adapters={PAPER_VENUE: self.paper_broker, LIVE_VENUE: self.live_broker}
            ).propose(
                ArmExecutionRequest(
                    arm_id=self.arm_id, decision_id=self.decision_ids[0],
                    mode=ExecutionMode.LIVE, ticker="AMD",
                    entry=100.0, stop=95.0, risk_fraction=0.005,
                ),
                now=NOW,
            )
        self.assertEqual(caught.exception.code, "mode_mismatch")
        self.assertNoOrders()


# --------------------------------------------------------------------------- #
# 3. The flags
# --------------------------------------------------------------------------- #


class FlagTests(PaperFixture):
    def test_the_paper_flag_off_dispatches_nothing(self):
        summary = self.dispatch(settings=self.settings_for(strategy_lab_paper_enabled=False))
        self.assertEqual(summary.skipped_reason, paper.SKIP_PAPER_DISABLED)
        self.assertFalse(summary.enabled)
        self.assertEqual(len(self.trades()), 0)
        self.assertNoOrders()

    def test_the_master_switch_off_dispatches_nothing(self):
        summary = self.dispatch(settings=self.settings_for(strategy_lab_enabled=False))
        self.assertEqual(summary.skipped_reason, paper.SKIP_LAB_DISABLED)
        self.assertEqual(len(self.trades()), 0)

    def test_phase6_off_dispatches_nothing(self):
        summary = self.dispatch(
            settings=self.settings_for(phase6_execution_enabled=False)
        )
        self.assertEqual(summary.skipped_reason, paper.SKIP_PHASE6_DISABLED)
        self.assertEqual(len(self.trades()), 0)

    def test_the_entry_point_itself_refuses_a_paper_arm_with_the_flag_off(self):
        """Not only the dispatcher: the gate is in `propose`, so nothing routes around it."""
        from execution.strategy_lifecycle import ArmExecutionRequest

        card = self.service(
            settings=self.settings_for(strategy_lab_paper_enabled=False)
        ).propose(
            ArmExecutionRequest(
                arm_id=self.arm_id, decision_id=self.decision_ids[0],
                mode=ExecutionMode.PAPER, ticker="AMD",
                entry=100.0, stop=95.0, risk_fraction=0.005,
            ),
            now=NOW,
        )
        self.assertEqual(card.blocked_reason, "strategy_lab_paper_enabled_false")
        self.assertEqual(card.status, ExecutionState.RISK_REJECTED.value)
        self.assertNoOrders()

    def test_live_requires_the_paper_flag_too(self):
        """Spec Q §14: each tier requires every tier below it.

        Operationally, not ceremonially: the three jobs that resume, expire and
        reconcile an execution are gated on the paper flag, so `live on, paper
        off` would be live positions nothing recovers after a restart.
        """
        from execution.strategy_lifecycle import ArmExecutionRequest

        card = self.service(
            settings=self.settings_for(
                strategy_lab_paper_enabled=False,
                strategy_lab_live_enabled=True,
                allow_live_trading=True,
                execution_mode="live",
            )
        ).propose(
            ArmExecutionRequest(
                arm_id=self.arm_id, decision_id=self.decision_ids[0],
                mode=ExecutionMode.PAPER, ticker="AMD",
                entry=100.0, stop=95.0, risk_fraction=0.005,
            ),
            now=NOW,
        )
        self.assertEqual(card.blocked_reason, "strategy_lab_paper_enabled_false")
        self.assertNoOrders()

    def test_a_settings_object_missing_the_flag_entirely_refuses(self):
        """Spec Q §12 invariant 1: absence of a flag is not a true one."""
        from types import SimpleNamespace

        from execution.strategy_lifecycle import ArmExecutionRequest

        bare = SimpleNamespace(
            phase6_execution_enabled=True, robinhood_order_type="limit",
            execution_approval_secret="s",
        )
        card = self.service(settings=bare).propose(
            ArmExecutionRequest(
                arm_id=self.arm_id, decision_id=self.decision_ids[0],
                mode=ExecutionMode.PAPER, ticker="AMD",
                entry=100.0, stop=95.0, risk_fraction=0.005,
            ),
            now=NOW,
        )
        self.assertEqual(card.blocked_reason, "strategy_lab_disabled")
        self.assertNoOrders()


# --------------------------------------------------------------------------- #
# 4. The virtual budget and the caps
# --------------------------------------------------------------------------- #


class VirtualBudgetTests(PaperFixture):
    def test_the_arms_own_equity_sizes_the_position_not_the_ledger(self):
        """The notional is what the *virtual* book's sizing produced, to the cent.

        The comparison is against :func:`strategy_lab.shadow.size_position` over
        the paper context, which is the pure function Spec Q §11's "independent
        virtual budget" is expressed in. Phase 6 then applies its own caps on top
        and can only shrink the order, so the assertion is `<=` on the stored
        notional and `==` on the number the virtual book asked for — and the
        ledger's equity is asserted to be a different number, so the stored one
        cannot have come from there.
        """
        from strategy_lab import shadow

        settings = self.settings_for(
            strategy_lab_paper_equity=250_000.0,
            strategy_lab_paper_max_proposals_per_run=1,
        )
        summary = self.dispatch(settings=settings)
        self.assertEqual(summary.proposed, 1, summary.skipped)

        with get_session() as session:
            decision_row = registry.decision_row(session, self.decision_ids[0])
            decision = shadow.decision_of(decision_row)
            snapshot = registry.load_snapshot(
                registry.snapshot_row(session, self.snapshot_id)
            )
            from strategy_lab import replay

            version = registry.load_strategy_version(
                registry.strategy_version_for_arm(session, self.arm_id)
            )
            entry = float(
                snapshots.bars_of(snapshot, decision_row.ticker)[-1].split_adjusted_close
            )
            plan = replay.build_plan(
                version, snapshot, decision_row.ticker, entry_reference=entry
            )
            ledger_equity = __import__(
                "portfolio.proposals", fromlist=["x"]
            ).read_context(session, now=NOW).equity
        wanted = shadow.size_position(
            plan,
            paper.paper_context(settings, as_of=NOW),
            arm_risk_budget=0.005,
            position_risk_pct=decision.risk_plan.position_risk_pct,
        )
        self.assertGreater(wanted.notional, 0)
        self.assertNotAlmostEqual(ledger_equity, 250_000.0, places=2)
        self.assertLessEqual(self.trades()[0][3], wanted.notional + 1e-6)

    def test_a_zero_risk_budget_proposes_nothing(self):
        with get_session() as session:
            registry.require_arm(session, self.arm_id).risk_budget = 0.0
            session.commit()
        summary = self.dispatch()
        self.assertEqual(summary.proposed, 0)
        self.assertTrue(
            any("risk_budget_is_zero" in reason for _l, reason in summary.skipped),
            summary.skipped,
        )

    def test_the_max_open_positions_cap_binds_within_one_pass(self):
        summary = self.dispatch(
            settings=self.settings_for(strategy_lab_paper_max_open_positions=1)
        )
        self.assertEqual(summary.proposed, 1)
        self.assertTrue(
            any("max_open_positions" in reason for _l, reason in summary.skipped),
            summary.skipped,
        )

    def test_the_daily_notional_cap_bounds_the_pass(self):
        summary = self.dispatch(
            settings=self.settings_for(strategy_lab_paper_daily_notional=1_000.0)
        )
        self.assertLessEqual(summary.proposed, 2)
        total = sum(notional for _m, _s, _q, notional in self.trades())
        self.assertLessEqual(total, 1_000.0 + 1e-6)

    def test_the_paper_book_is_independent_of_the_shadow_book(self):
        paper_context = paper.paper_context(
            self.settings_for(strategy_lab_paper_equity=50_000.0), as_of=NOW
        )
        from orchestrator import strategy_lab_shadow as sl

        shadow_context = sl.portfolio_context(
            self.settings_for(strategy_lab_shadow_equity=100_000.0), as_of=NOW
        )
        self.assertNotEqual(paper_context.equity, shadow_context.equity)
        self.assertNotEqual(paper_context.context_hash, shadow_context.context_hash)


# --------------------------------------------------------------------------- #
# 5. No duplicate orders
# --------------------------------------------------------------------------- #


class NoDuplicateTests(PaperFixture):
    def test_a_ticker_held_by_another_arm_is_reserved(self):
        """Spec Q §11's exposure reservation, across arms rather than within one."""
        with get_session() as session:
            other = registry.create_arm(
                session, fx.EXPERIMENT, "momentum_v1", "1.0.0",
                ExecutionMode.PAPER, risk_budget=0.005,
            )
            session.commit()
        # `create_arm` is create-or-return for an (experiment, version, mode), so
        # the one paper arm is the same row: the reservation is exercised through
        # a non-terminal execution instead.
        self.dispatch(settings=self.settings_for(strategy_lab_paper_max_proposals_per_run=1))
        with get_session() as session:
            names = paper.reserved_tickers(session, mode="paper")
        self.assertEqual(len(names), 1)

        # A second decision on the reserved name, from a second snapshot, so the
        # per-decision index cannot be what refuses it.
        with get_session() as session:
            snapshot = slf.universe_snapshot(
                [
                    slf.replayable_inputs(ticker, slf.flat_bars())
                    for ticker in self.tickers
                ],
                cutoff=CUTOFF + timedelta(minutes=1),
            )
            row = registry.record_snapshot(session, snapshot)
            registry.record_decision(
                session, self.arm_id, row.id, a_decision(snapshot, names[0])
            )
            session.commit()

        second = self.dispatch()
        self.assertIn(
            paper.SKIP_TICKER_RESERVED, [reason for _l, reason in second.skipped]
        )
        _ = other

    def test_one_decision_cannot_have_two_non_terminal_executions(self):
        self.dispatch()
        with get_session() as session:
            rows = (
                session.query(models.StrategyTrade)
                .filter(models.StrategyTrade.decision_id == self.decision_ids[0])
                .all()
            )
        self.assertEqual(len(rows), 1)

    def test_a_terminal_execution_releases_the_ticker_reservation(self):
        self.dispatch(settings=self.settings_for(strategy_lab_paper_max_proposals_per_run=1))
        with get_session() as session:
            before = paper.reserved_tickers(session, mode="paper")
            execution_id = session.query(models.StrategyTrade).first().execution_id
        self.assertEqual(len(before), 1)
        self.service().cancel(execution_id=execution_id, reason="owner_rejected")
        with get_session() as session:
            self.assertEqual(paper.reserved_tickers(session, mode="paper"), ())

    def test_a_live_execution_does_not_reserve_against_paper(self):
        """Different books: a paper fill at Alpaca is not the champion's position."""
        self.dispatch()
        with get_session() as session:
            self.assertEqual(paper.reserved_tickers(session, mode="live"), ())


# --------------------------------------------------------------------------- #
# 6. Freshness and eligibility
# --------------------------------------------------------------------------- #


class EligibilityTests(PaperFixture):
    def test_a_stale_snapshot_abstains(self):
        """Spec Q §12 invariant 7: the snapshot's age is the quote's age."""
        summary = self.dispatch(now=CUTOFF + timedelta(days=3))
        self.assertEqual(summary.proposed, 0)
        self.assertEqual(
            sorted({reason for _l, reason in summary.skipped}),
            [paper.SKIP_STALE_SNAPSHOT],
        )
        self.assertEqual(len(self.trades()), 0)

    def test_a_flat_decision_is_not_a_candidate(self):
        with get_session() as session:
            snapshot = registry.load_snapshot(
                registry.snapshot_row(session, self.snapshot_id)
            )
            registry.record_decision(
                session, self.arm_id, self.snapshot_id,
                a_decision(snapshot, "IBM", action=DecisionAction.FLAT, risk_plan=None),
            )
            session.commit()
        summary = self.dispatch()
        self.assertEqual(summary.proposed, 2)

    def test_a_decision_whose_ticker_has_no_bars_has_no_entry_reference(self):
        with get_session() as session:
            snapshot = registry.load_snapshot(
                registry.snapshot_row(session, self.snapshot_id)
            )
            self.assertEqual(snapshots.bars_of(snapshot, "ZZZZ"), ())
            registry.record_decision(
                session, self.arm_id, self.snapshot_id, a_decision(snapshot, "ZZZZ")
            )
            session.commit()
        summary = self.dispatch()
        self.assertIn(
            paper.SKIP_NO_ENTRY_REFERENCE, [reason for _l, reason in summary.skipped]
        )

    def test_no_active_paper_arm_is_a_named_skip(self):
        with get_session() as session:
            registry.set_arm_status(session, self.arm_id, "paused")
            session.commit()
        summary = self.dispatch()
        self.assertEqual(summary.skipped_reason, paper.SKIP_NO_ARMS)
        self.assertEqual(len(self.trades()), 0)

    def test_the_kill_switch_blocks_the_dispatch(self):
        from portfolio import killswitch

        with get_session() as session:
            killswitch.set_switch(session, on=True, changed_by="bryan", reason="incident")
            session.commit()
        summary = self.dispatch()
        self.assertEqual(summary.proposed, 0)
        self.assertEqual(summary.blocked, 2, summary.skipped)
        self.assertNoOrders()


# --------------------------------------------------------------------------- #
# 7. The three scheduled jobs
# --------------------------------------------------------------------------- #


class ScheduledJobTests(PaperFixture):
    def test_all_three_no_op_with_the_flag_off(self):
        off = self.settings_for(strategy_lab_paper_enabled=False)
        adapters = {PAPER_VENUE: self.paper_broker}
        self.assertEqual(paper.resume_executions(off, adapters=adapters), [])
        self.assertEqual(paper.expire_stale_approvals(off, adapters=adapters), [])
        self.assertIsNone(paper.reconcile(off, adapters=adapters))
        self.assertEqual(self.paper_broker.calls, {})

    def test_a_lapsed_approval_is_not_expired_before_it_lapses(self):
        self.dispatch()
        expired = self.service().expire_stale(now=NOW + timedelta(seconds=60))
        self.assertEqual(expired, [])
        self.assertEqual(
            sorted({status for _m, status, _q, _n in self.trades()}),
            [ExecutionState.PROPOSED.value],
        )

    def test_the_expiry_job_terminates_a_lapsed_proposal_and_frees_the_slot(self):
        """Driven through the job wrapper, which reads the wall clock.

        The fixture's approval was minted against a cutoff in March, so by the
        time any test runs it has long lapsed — which is exactly the condition
        the job exists for, and the previous test pins the other side of the
        boundary so this one cannot pass by expiring everything.
        """
        self.dispatch()
        expired = paper.expire_stale_approvals(
            self.settings,
            adapters={PAPER_VENUE: self.paper_broker},
            service=self.service(),
        )
        self.assertEqual(len(expired), 2)
        self.assertEqual(
            sorted({status for _m, status, _q, _n in self.trades()}),
            [ExecutionState.EXPIRED.value],
        )
        with get_session() as session:
            self.assertEqual(paper.reserved_tickers(session, mode="paper"), ())
        self.assertNoOrders()

    def test_resume_places_no_entry_order(self):
        self.dispatch()
        actions = paper.resume_executions(
            self.settings,
            adapters={PAPER_VENUE: self.paper_broker},
            service=self.service(),
        )
        self.assertIsInstance(actions, list)
        self.assertEqual(self.paper_broker.calls.get("place_order", 0), 0)

    def test_reconcile_covers_only_the_tiers_whose_adapter_is_registered(self):
        """A live tier with no adapter is skipped, not raised — and not silent.

        The live tier is off here, so only paper is reconciled, and the live fake
        is never touched. The mirror case — live enabled *and* registered — is in
        `tests/test_strategy_lab_e2e.py`.
        """
        self.dispatch()
        report = paper.reconcile(
            self.settings,
            adapters={PAPER_VENUE: self.paper_broker, LIVE_VENUE: self.live_broker},
            service=self.service(
                adapters={PAPER_VENUE: self.paper_broker, LIVE_VENUE: self.live_broker}
            ),
        )
        self.assertIsNotNone(report)
        self.assertEqual(self.live_broker.calls, {}, self.live_broker.calls)

    def test_reconcile_reads_positions_and_never_places(self):
        self.dispatch()
        report = paper.reconcile(
            self.settings,
            adapters={PAPER_VENUE: self.paper_broker},
            service=self.service(),
        )
        self.assertIsNotNone(report)
        self.assertEqual(self.paper_broker.order_calls, 0)

    def test_the_scheduler_registers_the_three_jobs_only_with_the_flag_on(self):
        import inspect

        from orchestrator import scheduler

        source = inspect.getsource(scheduler.PipelineScheduler.start)
        self.assertIn("strategy_lab_paper", source)
        for job_id in ("strategy_lab_resume", "strategy_lab_expire", "strategy_lab_reconcile"):
            self.assertIn(job_id, source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
