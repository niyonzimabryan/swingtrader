"""The PR 6 end-to-end fixture, and the one safety proof the brief names.

Requirement 5 asks for the complete chain in one test:

    scan -> snapshot -> four arms -> shadow results -> paper orders (a fake
    Alpaca paper broker) -> closures -> metrics -> owner report -> promotion
    dry run

:class:`EndToEndTests` walks exactly that, in order, through the production code
paths: a real ``TradingPipeline._run_full_scan_inner`` with the agents stubbed,
the real shadow hook, the real maturation job against real ``price_bars``, the
real promotion workflow, the real paper dispatcher, and the real Phase 6 approval
turning into a placement at a fake adapter. The only doubles are the model agents,
the brokers, and the clock.

The acceptance criterion, separately, is :class:`PaperUnderGlobalLiveModeTests`:

    a safety E2E sets the global execution mode to **live** while dispatching a
    **paper** arm and proves only the Alpaca paper fake is called and the live
    broker sees **zero** calls.

Not "zero order calls" — zero calls of any kind. ``FakeExecutionBroker.calls``
counts every method by name, so the assertion is that the live adapter's call
dictionary is empty: a paper arm has no business reading a position, a quote or an
order at the live venue either.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta

from database import models
from database.db import get_session
from execution.brokers.fake import FakeExecutionBroker
from orchestrator import strategy_lab_paper as paper
from orchestrator import strategy_lab_promotion as wiring
from orchestrator import strategy_lab_shadow as lab
from strategy_lab import promotion as pm
from strategy_lab import registry
from strategy_lab.domain import ExecutionMode, ExecutionState, TERMINAL_EXECUTION_STATES
from strategy_lab.execution import LIVE_VENUE, PAPER_VENUE
from tests import proposalfixture as pf
from tests import strategyexecfixture as fx
from tests.test_strategy_lab_integration import ScanFixture, _settings
from utils.timeutils import utcnow_naive


def e2e_settings(**overrides):
    """The integration fixture's settings, plus every Phase 6 and PR 6 knob."""
    base = dict(
        strategy_lab_enabled=True,
        strategy_lab_shadow_enabled=True,
        strategy_lab_paper_enabled=True,
        strategy_lab_live_enabled=False,
        strategy_lab_paper_equity=100_000.0,
        strategy_lab_paper_risk_budget=0.005,
        strategy_lab_paper_max_open_positions=5,
        strategy_lab_paper_max_position_fraction=0.1,
        strategy_lab_paper_daily_notional=0.0,
        strategy_lab_paper_max_proposals_per_run=5,
        strategy_lab_paper_max_snapshot_age_minutes=24 * 60,
        strategy_lab_promotion_floor_shadow_matured=1,
        strategy_lab_promotion_floor_paper_closed=1,
        strategy_lab_live_risk_budget=0.0,
        strategy_lab_promotion_ttl_seconds=300,
        # Phase 6, from the proposal fixture so the numbers match its ledger.
        phase6_execution_enabled=True,
        allow_live_trading=False,
        execution_mode="paper",
        risk_fraction_percentage_floor=0.05,
        risk_fraction_hard_cap=0.01,
        evidenced_risk_cap=0.01,
        evidenced_daily_notional=0.0,
        discretionary_risk_cap=0.01,
        discretionary_daily_notional=0.0,
        evidence_gate_mode="advisory",
        citation_max_age_sessions=5,
        protection_window_seconds=1,
        protection_poll_interval_seconds=0.01,
        approval_ttl_seconds=1800,
        execution_approval_secret="test-approval-secret",
        proposal_max_position_pct=0.10,
        proposal_max_sector_pct=0.30,
        portfolio_freshness_budget_minutes=10_000_000,
        robinhood_order_type="limit",
        telegram_chat_id="99887766",
    )
    base.update(overrides)
    return _settings(**base)


class LabE2EFixture(ScanFixture):
    """The scan fixture, plus a synced Phase 6 ledger and two fake adapters."""

    def setUp(self):
        super().setUp()
        self.settings = e2e_settings()
        self.pipeline.settings = self.settings
        self.paper_broker = FakeExecutionBroker(fill_price=100.0, venue=PAPER_VENUE)
        self.live_broker = FakeExecutionBroker(fill_price=100.0, venue=LIVE_VENUE)
        # The pipeline's own paper adapter, for the production hook path.
        self.pipeline.paper_broker = self.paper_broker
        self.pipeline.primary_broker = self.live_broker
        with get_session() as session:
            fx.synced(session)
            session.commit()

    # -- the chain, as reusable steps -------------------------------------- #

    def adapters(self, *, with_live: bool = False) -> dict:
        out = {PAPER_VENUE: self.paper_broker}
        if with_live:
            out[LIVE_VENUE] = self.live_broker
        return out

    def service(self, *, settings=None, adapters=None):
        return paper.build_service(
            settings or self.settings,
            adapters=adapters if adapters is not None else self.adapters(),
            resolver=pf.resolver_for({}),
            owner_id="99887766",
        )

    def insert_forward_bars(self, ticker: str = "NVDA", *, sessions: int = 30):
        """A rising series after the scan's cutoff, so a shadow decision matures."""
        uid = f"uid-{ticker.lower()}"
        with get_session() as session:
            if session.query(models.Security).filter_by(security_uid=uid).first() is None:
                session.add(models.Security(
                    security_uid=uid, ticker=ticker,
                    ticker_valid_from=date(2020, 1, 1), ticker_valid_to=None,
                    venue="XNAS", source="fixture",
                ))
            price = 100.10
            day = utcnow_naive().date()
            added = 0
            while added < sessions:
                day += timedelta(days=1)
                if day.weekday() >= 5:
                    continue
                price += 1.0
                session.add(models.PriceBar(
                    security_uid=uid, ticker=ticker, session_date=day,
                    raw_open=price, raw_high=price + 1.0, raw_low=price - 1.0,
                    raw_close=price, volume=1_000_000.0, split_factor=1.0,
                    dividend_cash=0.0, split_adjusted_close=price,
                    total_return_close=price, source="fixture",
                ))
                added += 1
            session.commit()

    def arms(self) -> list[tuple]:
        with get_session() as session:
            return [
                (row.id, row.mode, row.status)
                for row in registry.arms_for_experiment(
                    session, self.settings.strategy_lab_experiment
                )
            ]

    def shadow_arm_with_a_closed_trade(self) -> int:
        with get_session() as session:
            row = (
                session.query(models.StrategyTrade)
                .filter(models.StrategyTrade.mode == "shadow")
                .filter(models.StrategyTrade.status == ExecutionState.CLOSED.value)
                .first()
            )
            self.assertIsNotNone(row, "no shadow trade settled")
            return row.arm_id

    def record_evidence(self, arm_id: int, *, cutoff=None, **overrides) -> int:
        """The evaluator's output for one arm, recorded the way the job does."""
        cutoff = cutoff or (utcnow_naive() + timedelta(days=90))
        base = dict(
            n_decisions=40,
            n_matured=12,
            n_closed=4,
            warnings=("small_sample",),
            metrics={"net_return_after_costs": 0.03},
            cost_assumptions=lab.cost_assumptions(self.settings).as_dict()
            if hasattr(lab.cost_assumptions(self.settings), "as_dict")
            else {"slippage_bps": 10.0},
            uncertainty={"level": 0.9, "lower": 0.001, "upper": 0.06},
            benchmark="spy_total_return",
        )
        base.update(overrides)
        with get_session() as session:
            row = registry.record_metric_snapshot(session, arm_id, cutoff, **base)
            registry.acknowledge_metric_warnings(session, row.id, "bryan")
            session.commit()
            return row.id

    def promote_to_paper(self, source_arm_id: int, evidence_id: int) -> int:
        with get_session() as session:
            request = wiring.build_request(
                session,
                self.settings,
                source_arm_id=source_arm_id,
                to_mode=ExecutionMode.PAPER,
                owner="bryan",
                reason="shadow gate met; paper tournament",
                evidence_metric_snapshot_id=evidence_id,
            )
            session.commit()
        plan = wiring.plan(self.settings, request, adapters=self.adapters())
        self.assertTrue(plan.confirmable, plan.refusals + plan.external_refusals)
        wiring.confirm(self.settings, request, adapters=self.adapters())
        return request.target_arm_id

    def approve(self, execution_id: str, *, service=None, now=None):
        """The owner's single-use approval, through Phase 6's verification."""
        with get_session() as session:
            proposal = (
                session.query(models.Proposal)
                .filter(models.Proposal.execution_id == execution_id)
                .one()
            )
            signature = proposal.approval_signature
        return (service or self.service()).on_approval(
            execution_id=execution_id,
            presented_signature=signature,
            owner_id="99887766",
            now=now,
        )

    def executions(self, *, mode: str) -> list[tuple]:
        with get_session() as session:
            return [
                (row.execution_id, row.status, row.quantity, row.notional)
                for row in session.query(models.StrategyTrade)
                .filter(models.StrategyTrade.mode == mode)
                .order_by(models.StrategyTrade.id)
            ]


# --------------------------------------------------------------------------- #
# Requirement 5: the whole chain
# --------------------------------------------------------------------------- #


class EndToEndTests(LabE2EFixture):
    def test_the_complete_chain(self):
        # --- 1. scan -> snapshot -> four arms -> decisions ----------------- #
        self.run_scan()
        arms = self.arms()
        self.assertEqual(len(arms), 4, arms)
        self.assertEqual({mode for _id, mode, _status in arms}, {"shadow"})
        self.assertGreaterEqual(
            self.lab_row_counts()["strategy_decisions"], 1, self.lab_row_counts()
        )
        self.assertEqual(self.lab_row_counts()["market_snapshots"], 1)

        # --- 2. shadow results -------------------------------------------- #
        self.insert_forward_bars()
        summary = lab.mature_shadow_decisions(
            self.settings, now=utcnow_naive() + timedelta(days=90)
        )
        self.assertEqual(summary.snapshots_settled, 1, summary.as_log_fields())
        self.assertEqual(summary.executions_opened, 1)
        self.assertEqual(
            [status for _id, status, _q, _n in self.executions(mode="shadow")],
            [ExecutionState.CLOSED.value],
        )
        self.assertEqual(self.paper_broker.calls, {}, "shadow touched a broker")

        # --- 3. evidence, then an owner promotion to paper ----------------- #
        shadow_arm = self.shadow_arm_with_a_closed_trade()
        evidence = self.record_evidence(shadow_arm)
        paper_arm = self.promote_to_paper(shadow_arm, evidence)
        self.assertIn((paper_arm, "paper", "active"), self.arms())
        self.assertEqual(
            self.paper_broker.order_calls, 0, "a promotion placed an order"
        )

        # --- 4. a second scan, now producing the paper arm's decisions ----- #
        self.pipeline._run_full_scan_inner(utcnow_naive(), "scan-test-2")
        with get_session() as session:
            paper_decisions = registry.decisions_for_arm(session, paper_arm)
        self.assertGreaterEqual(len(paper_decisions), 1)

        # --- 5. paper orders, proposed by the production hook -------------- #
        # The scan above already dispatched: `_run_full_scan_inner` calls the
        # paper hook after the shadow pass, so the proposal below is the one the
        # production path made, not one this test arranged.
        paper_rows = self.executions(mode="paper")
        self.assertEqual(len(paper_rows), 1, paper_rows)
        execution_id = paper_rows[0][0]
        self.assertEqual(paper_rows[0][1], ExecutionState.PROPOSED.value)
        self.assertEqual(
            self.paper_broker.order_calls, 0,
            "a dispatch pass placed an order before the owner approved",
        )

        # And a second pass proposes nothing: the decision already has its one
        # execution, held by the database rather than by this process.
        service = self.service()
        again = paper.dispatch_for_scan(
            self.settings,
            adapters=self.adapters(),
            now=utcnow_naive(),
            service=service,
        )
        self.assertEqual(again.proposed, 0)
        self.assertEqual(
            [reason for _label, reason in again.skipped],
            [paper.SKIP_ALREADY_EXECUTED],
        )
        self.assertEqual(len(self.executions(mode="paper")), 1)

        # --- 6. the owner approves exactly one execution ------------------- #
        result = self.approve(execution_id, service=service)
        self.assertEqual(result.status, "protected", result.message)
        self.assertEqual(self.paper_broker.calls.get("place_order", 0), 1)
        self.assertEqual(self.paper_broker.calls.get("place_stop", 0), 1)
        self.assertEqual(
            self.executions(mode="paper")[0][1], ExecutionState.PROTECTED.value
        )
        self.assertEqual(self.live_broker.calls, {}, self.live_broker.calls)

        # The approval is single-use: a replay places nothing more.
        with self.assertRaises(Exception):
            self.approve(execution_id, service=service)
        self.assertEqual(self.paper_broker.calls.get("place_order", 0), 1)

        # --- 7. closure ---------------------------------------------------- #
        service.close(execution_id=execution_id, exit_price=104.0, exit_reason="target")
        self.assertEqual(
            self.executions(mode="paper")[0][1], ExecutionState.CLOSED.value
        )

        # --- 8. metrics and the owner report ------------------------------- #
        payload = lab.scoreboard(self.settings)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["schema"], "strategy_lab.scoreboard.v1")
        from bot.handlers.strategy_lab import scoreboard_lines

        card = "\n".join(scoreboard_lines(payload))
        self.assertIn("SCOREBOARD", card)

        overview = lab.experiment_overview(self.settings)
        modes = {
            arm["mode"]
            for experiment in overview["experiments"]
            for arm in experiment["arms"]
        }
        self.assertEqual(modes, {"shadow", "paper"})

        # --- 9. the promotion dry run to live ------------------------------ #
        paper_evidence = self.record_evidence(
            paper_arm, cutoff=utcnow_naive() + timedelta(days=120), n_closed=1
        )
        with get_session() as session:
            live_request = wiring.build_request(
                session,
                self.settings,
                source_arm_id=paper_arm,
                to_mode=ExecutionMode.LIVE,
                owner="bryan",
                reason="dry run only",
                evidence_metric_snapshot_id=paper_evidence,
            )
            session.commit()
        dry_run = wiring.plan(
            self.settings, live_request, adapters=self.adapters(with_live=True)
        )
        self.assertFalse(dry_run.confirmable)
        self.assertTrue(
            any(
                "STRATEGY_LAB_LIVE_ENABLED is false" in item
                for item in dry_run.external_refusals
            ),
            dry_run.external_refusals,
        )
        # The dry run prepared an inactive arm and activated nothing.
        self.assertIn((live_request.target_arm_id, "live", "inactive"), self.arms())
        with get_session() as session:
            self.assertIsNone(registry.active_live_arm(session))

        # --- 10. and no live order happened at any point ------------------- #
        self.assertEqual(self.live_broker.calls, {}, self.live_broker.calls)
        self.assertEqual(self.live_broker.order_calls, 0)

    def test_the_audit_trail_names_the_owner_and_the_evidence(self):
        self.run_scan()
        self.insert_forward_bars()
        lab.mature_shadow_decisions(self.settings, now=utcnow_naive() + timedelta(days=90))
        shadow_arm = self.shadow_arm_with_a_closed_trade()
        evidence = self.record_evidence(shadow_arm)
        paper_arm = self.promote_to_paper(shadow_arm, evidence)
        with get_session() as session:
            history = pm.promotion_history(session)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["owner"], "bryan")
        self.assertEqual(history[0]["target_arm_id"], paper_arm)
        self.assertEqual(history[0]["evidence_metric_snapshot_id"], evidence)
        self.assertEqual(history[0]["to_mode"], "paper")


# --------------------------------------------------------------------------- #
# The acceptance criterion: a paper arm under a live global mode
# --------------------------------------------------------------------------- #


class PaperUnderGlobalLiveModeTests(LabE2EFixture):
    """EXECUTION_MODE=live, BROKER_PRIMARY=robinhood, ALLOW_LIVE_TRADING=true.

    The most adversarial configuration available without real credentials: every
    global setting says "live", the live adapter is registered and working, and the
    arm being dispatched is ``paper``. The arm's mode must win, and the live
    adapter must not be touched at all.
    """

    def setUp(self):
        super().setUp()
        self.settings = e2e_settings(
            execution_mode="live",
            allow_live_trading=True,
            broker_primary="robinhood",
            strategy_lab_live_enabled=True,
        )
        self.pipeline.settings = self.settings

    def _to_paper(self) -> int:
        self.run_scan()
        self.insert_forward_bars()
        lab.mature_shadow_decisions(self.settings, now=utcnow_naive() + timedelta(days=90))
        shadow_arm = self.shadow_arm_with_a_closed_trade()
        evidence = self.record_evidence(shadow_arm)
        paper_arm = self.promote_to_paper(shadow_arm, evidence)
        self.pipeline._run_full_scan_inner(utcnow_naive(), "scan-live-mode")
        return paper_arm

    def test_only_the_paper_fake_is_called_and_the_live_broker_sees_nothing(self):
        self._to_paper()
        service = self.service(adapters=self.adapters(with_live=True))
        rows = self.executions(mode="paper")
        self.assertEqual(len(rows), 1, rows)
        execution_id = rows[0][0]
        result = self.approve(execution_id, service=service)

        self.assertEqual(result.status, "protected", result.message)
        self.assertEqual(self.paper_broker.calls.get("place_order", 0), 1)
        self.assertEqual(self.paper_broker.calls.get("place_stop", 0), 1)
        self.assertEqual(
            self.live_broker.calls, {},
            f"the live broker was called while dispatching a paper arm: "
            f"{self.live_broker.calls}",
        )
        self.assertEqual(self.live_broker.order_calls, 0)

    def test_the_row_records_paper_while_the_global_mode_says_live(self):
        self._to_paper()
        with get_session() as session:
            trade = session.query(models.StrategyTrade).filter(
                models.StrategyTrade.mode == "paper"
            ).one()
            proposal = (
                session.query(models.Proposal)
                .filter(models.Proposal.execution_id == trade.execution_id)
                .one()
            )
            self.assertEqual(trade.mode, "paper")
            self.assertEqual(
                proposal.execution_mode, "paper",
                "the proposal recorded the global EXECUTION_MODE instead of the "
                "arm's own (Spec Q §12 invariant 11)",
            )

    def test_the_paper_venue_holding_the_live_adapter_places_nothing(self):
        """The wiring error the venue label alone cannot catch."""
        self._to_paper()
        dispatch = paper.dispatch_for_scan(
            self.settings,
            adapters={PAPER_VENUE: self.live_broker},
            now=utcnow_naive(),
            service=paper.build_service(
                self.settings,
                adapters={PAPER_VENUE: self.live_broker},
                resolver=pf.resolver_for({}),
            ),
        )
        self.assertEqual(dispatch.proposed, 0)
        self.assertEqual(self.live_broker.calls, {}, self.live_broker.calls)

    def test_a_resume_pass_after_a_restart_places_no_entry(self):
        """Invariant 5, through the job wrapper: protection may be re-placed."""
        self._to_paper()
        service = self.service(adapters=self.adapters(with_live=True))
        execution_id = self.executions(mode="paper")[0][0]
        self.approve(execution_id, service=service)
        placed = self.paper_broker.calls.get("place_order", 0)

        # A "restart": a brand-new service over the same rows and the same broker.
        actions = paper.resume_executions(
            self.settings,
            adapters=self.adapters(with_live=True),
            service=self.service(adapters=self.adapters(with_live=True)),
        )
        self.assertIsInstance(actions, list)
        self.assertEqual(self.paper_broker.calls.get("place_order", 0), placed)
        self.assertEqual(self.live_broker.calls, {}, self.live_broker.calls)

    def test_a_card_minted_before_the_flag_was_turned_off_places_nothing(self):
        """The gate is a condition on the placement, not on the proposal.

        An approval card can sit in a chat while the world moves. Phase 6
        re-checks what it owns at approval time; this re-checks what it cannot
        see — the Strategy Lab's own tier flag.
        """
        from execution.strategy_lifecycle import ArmExecutionRefused

        self._to_paper()
        execution_id = self.executions(mode="paper")[0][0]
        off = e2e_settings(
            execution_mode="live",
            allow_live_trading=True,
            strategy_lab_paper_enabled=False,
        )
        with self.assertRaises(ArmExecutionRefused) as caught:
            self.approve(
                execution_id,
                service=self.service(settings=off, adapters=self.adapters(with_live=True)),
            )
        self.assertEqual(caught.exception.code, "strategy_lab_paper_enabled_false")
        self.assertEqual(self.paper_broker.order_calls, 0, self.paper_broker.calls)
        self.assertEqual(self.live_broker.calls, {}, self.live_broker.calls)

        # The card was not consumed: turning the flag back on lets the same tap
        # through, which is what makes the refusal a gate rather than a loss.
        result = self.approve(execution_id, service=self.service())
        self.assertEqual(result.status, "protected", result.message)

    def test_a_live_arm_cannot_be_dispatched_by_the_paper_pass(self):
        """Even with every live flag on, the paper dispatcher only sees paper arms."""
        paper_arm = self._to_paper()
        with get_session() as session:
            live = registry.create_arm(
                session, self.settings.strategy_lab_experiment,
                *self._slug_version(session, paper_arm),
                ExecutionMode.LIVE, risk_budget=0.005,
            )
            registry.activate_arm(session, live.id)
            session.commit()
            live_id = live.id
        with get_session() as session:
            dispatchable = paper.active_paper_arms(session, self.settings)
        self.assertNotIn(live_id, [arm_id for arm_id, _slug, _v in dispatchable])
        paper.dispatch_for_scan(
            self.settings,
            adapters=self.adapters(with_live=True),
            now=utcnow_naive(),
            service=self.service(adapters=self.adapters(with_live=True)),
        )
        self.assertEqual(self.executions(mode="live"), [])
        self.assertEqual(self.live_broker.calls, {}, self.live_broker.calls)

    @staticmethod
    def _slug_version(session, arm_id: int) -> tuple[str, str]:
        row = registry.strategy_version_for_arm(session, arm_id)
        return row.slug, row.version


# --------------------------------------------------------------------------- #
# Existing behaviour with every flag false
# --------------------------------------------------------------------------- #


class EveryFlagFalseTests(LabE2EFixture):
    """The acceptance criterion PR 4 established, restated for PR 6's flags.

    PR 4 proved a scan with ``STRATEGY_LAB_ENABLED=false`` writes no Strategy Lab
    row. PR 6 adds two more flags and three more code paths, so the same claim is
    re-made against the surface that now exists: the paper hook, the dispatcher
    and the scheduled jobs are all no-ops, and no broker is touched.
    """

    def setUp(self):
        super().setUp()
        self.settings = e2e_settings(
            strategy_lab_enabled=False,
            strategy_lab_shadow_enabled=False,
            strategy_lab_paper_enabled=False,
            strategy_lab_live_enabled=False,
        )
        self.pipeline.settings = self.settings

    def test_a_scan_writes_no_strategy_lab_row_and_touches_no_adapter(self):
        self.run_scan()
        self.assertEqual(
            sum(self.lab_row_counts().values()), 0, self.lab_row_counts()
        )
        self.assertEqual(self.paper_broker.calls, {})
        self.assertEqual(self.live_broker.calls, {})

    def test_the_paper_hook_returns_before_it_reaches_a_session(self):
        self.pipeline._run_strategy_lab_paper(run_id="x")
        self.assertEqual(self.paper_broker.calls, {})

    def test_the_dispatcher_is_a_no_op(self):
        summary = paper.dispatch_for_scan(
            self.settings, adapters=self.adapters(), now=utcnow_naive()
        )
        self.assertFalse(summary.enabled)
        self.assertEqual(summary.skipped_reason, paper.SKIP_LAB_DISABLED)

    def test_all_three_scheduled_jobs_are_no_ops(self):
        self.assertEqual(paper.resume_executions(self.settings, adapters=self.adapters()), [])
        self.assertEqual(
            paper.expire_stale_approvals(self.settings, adapters=self.adapters()), []
        )
        self.assertIsNone(paper.reconcile(self.settings, adapters=self.adapters()))
        self.assertEqual(self.paper_broker.calls, {})

    def test_no_terminal_state_is_left_holding_a_reservation(self):
        """A property of the vocabulary, asserted where the E2E can see it."""
        from strategy_lab.execution import holds_reservation

        for state in TERMINAL_EXECUTION_STATES:
            self.assertFalse(
                holds_reservation(state),
                f"{state.value} is terminal and still holds its reservation",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
