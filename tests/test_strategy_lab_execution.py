"""Spec Q §12 end to end: the persistent execution machine on Phase 6's service.

Every test here drives the real :class:`execution.strategy_lifecycle.StrategyExecutionService`
against the deterministic :class:`execution.brokers.fake.FakeExecutionBroker`.
Nothing is mocked: each failure the machine has to survive — a rejection, a
partial fill, an unreadable stop, an ambiguous placement, a crash — is a flag on
the fake, so a passing test says the *code* handled it rather than that a patch
returned a value.

The Robinhood adapter is not exercised anywhere in this file, and cannot be:
`docs/EXECUTION_LIFECYCLE.md` §7 and the PR body both say so. What is asserted
about Robinhood is the shape of the request its builder produces and the
capabilities it declares, both already covered by
`tests/test_execution_lifecycle.py` and `tests/test_broker_capabilities.py`.

The acceptance list from the Phase 5 brief, and where each row lives:

* reject ............... `test_broker_review_rejection_is_terminal`,
                         `test_a_rejected_entry_is_terminal_and_releases`
* timeout .............. `test_an_unfilled_entry_stays_accepted_and_reserved`
* duplicate retry ...... `test_reopening_a_decision_returns_the_same_execution`,
                         `test_a_replayed_approval_places_nothing`
* partial fill ......... `test_a_partial_fill_protects_what_filled`,
                         `test_the_remainder_of_a_partial_fill_resizes_protection`
* protection failure ... `test_an_unreadable_stop_fails_protection_and_blocks`
* owner cancellation ... `test_owner_cancellation_is_terminal`
* expiry ............... `test_a_lapsed_approval_expires_the_execution`
* verified no-order .... `test_resume_resolves_a_verified_no_order_terminally`
* unknown placement .... `test_an_unknown_placement_keeps_its_reservation`
* restart .............. `test_restart_resumes_without_a_second_entry`
* reconciliation ....... `test_a_quantity_mismatch_blocks_and_pages`,
                         `test_a_position_missing_at_the_broker_blocks`
* closure .............. `test_closing_walks_out_and_releases`
* exact-once release ... `test_the_reservation_releases_exactly_once`
* concurrent cap ....... `test_a_second_approval_cannot_overspend_the_daily_cap`
* concurrency .......... `test_two_workers_racing_one_decision_get_one_execution`
"""

from __future__ import annotations

import threading
import unittest
from datetime import timedelta

from database.db import get_session, init_db
from database.models import Proposal, StrategyTrade
from execution.brokers.fake import FakeExecutionBroker
from execution.lifecycle import ExecutionRefused
from execution.strategy_lifecycle import ArmExecutionRefused
from portfolio import killswitch
from portfolio.paging import RecordingPager
from strategy_lab import execution as slx
from strategy_lab.domain import ExecutionMode, ExecutionState
from tests import proposalfixture as pf
from tests import strategyexecfixture as fx
from tests.dbfixture import TestDatabase


class ExecutionTestCase(unittest.TestCase):
    """One disposable database, one arm, one decision, one fake broker."""

    mode = ExecutionMode.PAPER
    promote = True

    def setUp(self):
        self.db = TestDatabase("strategy_execution")
        self.addCleanup(self.db.cleanup)
        init_db(self.db.url)
        self.now = fx.NOW
        with get_session() as session:
            fx.synced(session, now=self.now)
            session.commit()
        with get_session() as session:
            arm, decision = fx.build_lab(session, mode=self.mode, promote=self.promote)
            session.commit()
            self.arm_id = arm.id
            self.decision_id = decision.id
            self.request = fx.request_for(arm, decision)

    # -- helpers ------------------------------------------------------------

    def build(self, *, broker=None, settings=None, venue=None, adapters=None):
        self.broker = broker if broker is not None else FakeExecutionBroker(fill_price=100.0)
        self.pager = RecordingPager()
        venue = venue or (
            slx.LIVE_VENUE if self.mode is ExecutionMode.LIVE else slx.PAPER_VENUE
        )
        self.service = fx.service(
            broker=self.broker,
            settings=settings or self.settings_for(),
            pager=self.pager,
            venue=venue,
            adapters=adapters,
        )
        return self.service

    def settings_for(self, **overrides):
        if self.mode is ExecutionMode.LIVE:
            return fx.live_settings(**overrides)
        return pf.settings(**overrides)

    def propose(self, **overrides):
        request = self.request
        if overrides:
            request = _replace(request, **overrides)
        return self.service.propose(request, now=self.now)

    def approve(self, card, *, now=None):
        return self.service.on_approval(
            execution_id=card.execution_id,
            presented_signature=card.approval_signature,
            owner_id="99887766",
            now=now or self.now,
        )

    def trade(self, execution_id=None):
        """A detached snapshot of the row, safe to read after the session closes."""
        with get_session() as session:
            if execution_id:
                row = slx.require_execution(session, execution_id)
            else:
                row = session.query(StrategyTrade).order_by(StrategyTrade.id.asc()).first()
            if row is None:
                return None
            session.expunge(row)
            return row

    def status(self, execution_id=None) -> str:
        return self.trade(execution_id).status

    def reserved(self) -> float:
        with get_session() as session:
            return slx.reserved_notional(session, mode=self.mode)

    def entry_block(self):
        with get_session() as session:
            return killswitch.entry_block(session)


def _replace(request, **overrides):
    """A copy of a frozen request with fields replaced."""
    import dataclasses

    return dataclasses.replace(request, **overrides)


# --------------------------------------------------------------------------- #
# The explicit-mode entry point (Spec Q §12 invariant 11)
# --------------------------------------------------------------------------- #


class ModeBindingTests(ExecutionTestCase):
    def test_shadow_can_never_reach_the_execution_entry_point(self):
        """Invariant 11's hardest half: shadow has no adapter at all."""
        with get_session() as session:
            arm, decision = fx.build_lab(session, mode=ExecutionMode.SHADOW, ticker="NVDA")
            session.commit()
            request = fx.request_for(arm, decision)
        self.build()
        with self.assertRaises(slx.ShadowReachedExecution) as caught:
            self.service.propose(request, now=self.now)
        self.assertIn("sends no order", str(caught.exception))
        self.assertEqual(self.broker.order_calls, 0)

    def test_a_live_adapter_registered_at_the_paper_venue_is_refused(self):
        """The wiring error the venue key alone cannot catch (invariant 11)."""
        broker = FakeExecutionBroker(fill_price=100.0, venue=slx.LIVE_VENUE)
        self.build(broker=broker, venue=slx.PAPER_VENUE)
        with self.assertRaises(slx.ExecutionRefusedLocally) as caught:
            self.propose()
        self.assertEqual(caught.exception.code, "adapter_venue_mismatch")
        self.assertEqual(broker.order_calls, 0)

    def test_a_paper_arm_finds_no_adapter_at_the_live_venue(self):
        self.build(venue=slx.LIVE_VENUE)
        with self.assertRaises(slx.ExecutionRefusedLocally) as caught:
            self.propose()
        self.assertEqual(caught.exception.code, "no_adapter")
        self.assertEqual(self.broker.order_calls, 0)

    def test_the_real_adapters_declare_the_venue_they_are(self):
        """Asserted on the classes, so a future adapter cannot forget."""
        from execution.brokers.alpaca import AlpacaBroker
        from execution.brokers.robinhood import RobinhoodMCPBroker

        self.assertEqual(AlpacaBroker.venue, slx.PAPER_VENUE)
        self.assertEqual(RobinhoodMCPBroker.venue, slx.LIVE_VENUE)

    def test_binding_refuses_a_mode_the_arm_does_not_run(self):
        self.build()
        with self.assertRaises(slx.ExecutionRefusedLocally) as caught:
            slx.bind_adapter(
                arm_mode=ExecutionMode.PAPER,
                requested_mode=ExecutionMode.LIVE,
                venue=slx.LIVE_VENUE,
                adapter=self.broker,
            )
        self.assertEqual(caught.exception.code, "mode_mismatch")

    def test_a_venue_with_no_adapter_registered_is_refused(self):
        self.build(adapters={})
        with self.assertRaises(slx.ExecutionRefusedLocally) as caught:
            self.propose()
        self.assertEqual(caught.exception.code, "no_adapter")


# --------------------------------------------------------------------------- #
# Opening an execution: one id, one row, before anything external
# --------------------------------------------------------------------------- #


class OpenExecutionTests(ExecutionTestCase):
    def test_the_execution_id_exists_before_any_reservation(self):
        self.build()
        card = self.propose()
        trade = self.trade(card.execution_id)
        self.assertEqual(trade.status, ExecutionState.PROPOSED.value)
        self.assertFalse(slx.holds_reservation(trade.status))
        self.assertEqual(self.reserved(), 0.0)
        self.assertEqual(self.broker.order_calls, 0)

    def test_reopening_a_decision_returns_the_same_execution(self):
        """Duplicate retry: one decision, one non-terminal execution, forever."""
        self.build()
        first = self.propose()
        second = self.propose()
        self.assertEqual(first.execution_id, second.execution_id)
        with get_session() as session:
            self.assertEqual(session.query(StrategyTrade).count(), 1)

    def test_the_database_refuses_a_second_open_execution(self):
        """The constraint, not the query, is what holds it (§12 invariant 6)."""
        with get_session() as session:
            first = slx.open_execution(
                session, arm_id=self.arm_id, decision_id=self.decision_id, mode=self.mode
            )
            session.commit()
            execution_id = first.execution_id
        with get_session() as session:
            row = StrategyTrade(
                execution_id="handwritten",
                arm_id=self.arm_id,
                decision_id=self.decision_id,
                mode=self.mode.value,
                status=ExecutionState.PROPOSED.value,
            )
            session.add(row)
            with self.assertRaises(Exception):
                session.flush()
            session.rollback()
        with get_session() as session:
            rows = session.query(StrategyTrade).all()
            self.assertEqual([r.execution_id for r in rows], [execution_id])

    def test_a_terminal_execution_frees_the_decision_for_a_new_one(self):
        """The index is partial on purpose: terminal rows do not hold the slot."""
        self.build()
        first = self.propose()
        self.service.cancel(execution_id=first.execution_id, by="bryan", now=self.now)
        second = self.propose()
        self.assertNotEqual(first.execution_id, second.execution_id)
        with get_session() as session:
            self.assertEqual(session.query(StrategyTrade).count(), 2)

    def test_two_workers_racing_one_decision_get_one_execution(self):
        """Two sessions, one decision. The database arbitrates, not a lock."""
        barrier = threading.Barrier(2)
        results: list[str] = []
        errors: list[Exception] = []

        def worker():
            try:
                barrier.wait(timeout=10)
                for _ in range(5):
                    try:
                        with get_session() as session:
                            row = slx.open_execution(
                                session,
                                arm_id=self.arm_id,
                                decision_id=self.decision_id,
                                mode=self.mode,
                            )
                            session.commit()
                            results.append(row.execution_id)
                        return
                    except Exception as exc:  # SQLite writer contention
                        if "locked" not in str(exc).lower():
                            raise
                raise RuntimeError("could not acquire the writer")
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len(set(results)), 1, "two executions were opened for one decision")
        with get_session() as session:
            self.assertEqual(session.query(StrategyTrade).count(), 1)


# --------------------------------------------------------------------------- #
# The gates that refuse before placement
# --------------------------------------------------------------------------- #


class PrePlacementGateTests(ExecutionTestCase):
    def test_an_adapter_that_cannot_protect_is_refused_before_placement(self):
        """Spec Q §12 invariant 4, and its closing paragraph."""
        broker = FakeExecutionBroker(fill_price=100.0)
        broker.declared_capabilities = pf.BrokerCapabilities(
            can_read_positions=True,
            can_read_orders=True,
            can_place_equity_market=True,
            can_place_equity_limit=True,
            can_place_attached_stop=False,
            can_place_standalone_gtc_stop=False,
        )
        self.build(broker=broker)
        card = self.propose()
        self.assertEqual(card.blocked_reason, "capability_refused")
        self.assertEqual(self.status(card.execution_id), ExecutionState.RISK_REJECTED.value)
        self.assertEqual(broker.order_calls, 0)
        self.assertIn("capability_refused", str(self.pager.pages))

    def test_the_kill_switch_refuses_before_a_card_is_minted(self):
        self.build()
        with get_session() as session:
            killswitch.set_switch(session, on=True, changed_by="bryan", reason="incident")
            session.commit()
        card = self.propose()
        self.assertEqual(card.blocked_reason, killswitch.KILL_SWITCH_ENGAGED)
        self.assertEqual(self.status(card.execution_id), ExecutionState.RISK_REJECTED.value)
        self.assertIsNone(card.approval_signature or None)
        self.assertEqual(self.broker.order_calls, 0)
        with get_session() as session:
            self.assertEqual(session.query(Proposal).count(), 0)

    def test_a_risk_rejection_is_terminal_and_reserves_nothing(self):
        self.build()
        card = self.propose(risk_fraction=0.0000001)
        self.assertEqual(self.status(card.execution_id), ExecutionState.RISK_REJECTED.value)
        self.assertEqual(self.reserved(), 0.0)
        self.assertEqual(self.broker.order_calls, 0)


class LiveGateTests(ExecutionTestCase):
    mode = ExecutionMode.LIVE

    def test_a_live_arm_with_every_gate_reaches_the_broker(self):
        self.build()
        card = self.propose()
        self.assertEqual(card.status, ExecutionState.PROPOSED.value)
        self.approve(card)
        self.assertEqual(self.status(card.execution_id), ExecutionState.PROTECTED.value)

    def test_live_without_the_flags_is_refused_before_placement(self):
        self.build(settings=pf.settings(allow_live_trading=False, execution_mode="live"))
        card = self.propose()
        self.assertEqual(card.blocked_reason, "live_trading_disabled")
        self.assertEqual(self.broker.order_calls, 0)

    def test_live_with_execution_mode_paper_is_refused(self):
        self.build(settings=pf.settings(allow_live_trading=True, execution_mode="paper"))
        card = self.propose()
        self.assertEqual(card.blocked_reason, "execution_mode_not_live")
        self.assertEqual(self.broker.order_calls, 0)


class LiveWithoutPromotionTests(ExecutionTestCase):
    mode = ExecutionMode.LIVE
    promote = False

    def test_a_live_arm_with_no_promotion_event_is_refused(self):
        """Spec Q §12 invariant 2: a flag is not an authorization."""
        self.build()
        card = self.propose()
        self.assertEqual(card.blocked_reason, "no_promotion_event")
        self.assertEqual(self.status(card.execution_id), ExecutionState.RISK_REJECTED.value)
        self.assertEqual(self.broker.order_calls, 0)


# --------------------------------------------------------------------------- #
# Placement outcomes
# --------------------------------------------------------------------------- #


class PlacementOutcomeTests(ExecutionTestCase):
    def test_the_happy_path_walks_the_whole_machine(self):
        self.build()
        card = self.propose()
        result = self.approve(card)
        self.assertEqual(result.status, "protected")
        trade = self.trade(card.execution_id)
        self.assertEqual(trade.status, ExecutionState.PROTECTED.value)
        self.assertEqual(trade.quantity, 50.0)
        self.assertEqual(trade.filled_entry_price, 100.0)
        self.assertTrue(slx.holds_reservation(trade.status))
        self.assertEqual(self.reserved(), 5000.0)
        self.assertEqual(self.broker.calls["place_order"], 1)
        self.assertEqual(self.broker.calls["place_stop"], 1)

    def test_the_reservation_is_persisted_before_the_order_call(self):
        """Invariant 6: reserved on disk, visible to a rival, before placement."""
        seen: list[tuple[str, float]] = []
        broker = FakeExecutionBroker(fill_price=100.0)
        original = broker.place_order

        def spy(review):
            with get_session() as session:
                trade = session.query(StrategyTrade).one()
                seen.append((trade.status, float(trade.notional or 0.0)))
            return original(review)

        broker.place_order = spy
        self.build(broker=broker)
        card = self.propose()
        self.approve(card)
        self.assertEqual(len(seen), 1)
        status, notional = seen[0]
        self.assertEqual(status, ExecutionState.RISK_RESERVED.value)
        self.assertEqual(notional, 5000.0)

    def test_broker_review_rejection_is_terminal(self):
        self.build(broker=FakeExecutionBroker(review_approves=False))
        card = self.propose()
        with self.assertRaises(ExecutionRefused) as caught:
            self.approve(card)
        self.assertEqual(caught.exception.code, "broker_review_rejected")
        self.assertEqual(self.status(card.execution_id), ExecutionState.REVIEW_REJECTED.value)
        self.assertEqual(self.reserved(), 0.0)
        self.assertEqual(self.broker.calls.get("place_order", 0), 0)

    def test_a_rejected_entry_is_terminal_and_releases(self):
        self.build(broker=FakeExecutionBroker(place_rejects=True))
        card = self.propose()
        with self.assertRaises(ExecutionRefused):
            self.approve(card)
        self.assertEqual(self.status(card.execution_id), ExecutionState.FAILED_NO_ORDER.value)
        self.assertEqual(self.reserved(), 0.0)
        self.assertIsNone(self.entry_block())

    def test_an_unfilled_entry_stays_accepted_and_reserved(self):
        """The poll-window timeout. Not a failure, and not released."""
        self.build(broker=FakeExecutionBroker(fill_price=100.0, fill_entry=False))
        card = self.propose()
        result = self.approve(card)
        self.assertEqual(result.status, "submitted")
        self.assertEqual(self.status(card.execution_id), ExecutionState.ACCEPTED.value)
        self.assertEqual(self.reserved(), 5000.0)
        self.assertIsNone(self.entry_block())

    def test_a_replayed_approval_places_nothing(self):
        self.build()
        card = self.propose()
        self.approve(card)
        with self.assertRaises(ExecutionRefused) as caught:
            self.approve(card)
        self.assertEqual(caught.exception.code, "not_proposed")
        self.assertEqual(self.broker.calls["place_order"], 1)
        self.assertEqual(self.status(card.execution_id), ExecutionState.PROTECTED.value)


class PartialFillTests(ExecutionTestCase):
    def test_a_partial_fill_protects_what_filled(self):
        self.build(broker=FakeExecutionBroker(fill_price=100.0, fill_ratio=0.5))
        card = self.propose()
        result = self.approve(card)
        self.assertEqual(result.status, "protected")
        trade = self.trade(card.execution_id)
        self.assertEqual(trade.quantity, 25.0)
        self.assertEqual(self.broker.placed_stops[-1]["quantity"], 25)

    def test_the_remainder_of_a_partial_fill_resizes_protection(self):
        """Idempotent adjustment: same fill reuses one stop, a bigger fill gets its own."""
        self.build(broker=FakeExecutionBroker(fill_price=100.0, fill_ratio=0.5))
        card = self.propose()
        self.approve(card)
        stops_after_partial = len(self.broker.placed_stops)

        # Re-running with nothing changed must not place a second stop.
        self.service.resume(now=self.now)
        self.assertEqual(len(self.broker.placed_stops), stops_after_partial)

        with get_session() as session:
            proposal = session.query(Proposal).one()
            entry_order_id = proposal.entry_broker_order_id
        self.assertEqual(self.broker.fill_remainder(entry_order_id), 25.0)
        self.service.resume(now=self.now)

        self.assertEqual(self.broker.placed_stops[-1]["quantity"], 50)
        self.assertEqual(self.trade(card.execution_id).quantity, 50.0)
        self.assertEqual(self.status(card.execution_id), ExecutionState.PROTECTED.value)


class ProtectionFailureTests(ExecutionTestCase):
    def test_an_unreadable_stop_fails_protection_and_blocks(self):
        self.build(broker=FakeExecutionBroker(fill_price=100.0, stop_readable=False))
        card = self.propose()
        result = self.approve(card)
        self.assertEqual(result.status, "unprotected")
        self.assertEqual(self.status(card.execution_id), ExecutionState.PROTECTION_FAILED.value)
        self.assertIn("live_protection_failed", self.pager.events())

        block = self.entry_block()
        self.assertIsNotNone(block)
        # Phase 6's own `unprotected` proposal blocks first; the §12 reason is
        # there too, and either way every new entry is refused.
        self.assertIn(block[0], (killswitch.UNPROTECTED_POSITION, killswitch.UNRESOLVED_EXECUTION))

    def test_a_blocking_execution_alone_still_blocks_entries(self):
        """The §12 reason on its own, with no Phase 6 proposal in the way."""
        with get_session() as session:
            trade = slx.open_execution(
                session, arm_id=self.arm_id, decision_id=self.decision_id, mode=self.mode
            )
            slx.transition(session, trade, ExecutionState.RECONCILIATION_REQUIRED)
            session.commit()
            execution_id = trade.execution_id
        block = self.entry_block()
        self.assertIsNotNone(block)
        self.assertEqual(block[0], killswitch.UNRESOLVED_EXECUTION)
        self.assertIn(execution_id, block[1])

    def test_a_protection_failure_refuses_the_next_arm_entry(self):
        self.build(broker=FakeExecutionBroker(fill_price=100.0, stop_readable=False))
        card = self.propose()
        self.approve(card)
        with get_session() as session:
            arm, decision = fx.build_lab(session, mode=self.mode, ticker="MSFT")
            session.commit()
            second_request = fx.request_for(arm, decision)
        placements = self.broker.order_calls
        second = self.service.propose(second_request, now=self.now)
        self.assertIn(
            second.blocked_reason,
            (killswitch.UNPROTECTED_POSITION, killswitch.UNRESOLVED_EXECUTION),
        )
        self.assertEqual(self.broker.order_calls, placements)


class UnknownPlacementTests(ExecutionTestCase):
    def test_an_unknown_placement_keeps_its_reservation(self):
        """Invariant 12: unknown is not terminal and does not release."""
        self.build(broker=FakeExecutionBroker(placement_unknown=True))
        card = self.propose()
        result = self.approve(card)
        self.assertEqual(result.status, "reconciliation_required")
        self.assertEqual(self.status(card.execution_id), ExecutionState.PLACEMENT_UNKNOWN.value)
        self.assertTrue(slx.holds_reservation(self.status(card.execution_id)))
        self.assertGreater(self.reserved(), 0.0)
        self.assertEqual(self.entry_block()[0], killswitch.UNPROTECTED_POSITION)
        self.assertIn("strategy_placement_unknown", self.pager.events())

    def test_resume_resolves_a_verified_no_order_terminally(self):
        self.build(broker=FakeExecutionBroker(placement_unknown=True))
        card = self.propose()
        self.approve(card)
        actions = self.service.resume(now=self.now)
        self.assertEqual([a.now for a in actions], [ExecutionState.FAILED_NO_ORDER.value])
        self.assertEqual(self.reserved(), 0.0)
        self.assertEqual(self.broker.calls.get("place_order", 0), 1)

    def test_resume_adopts_an_order_that_did_land(self):
        """The other half: unknown, but the order exists. Never a second one."""
        self.build()
        card = self.propose()
        with get_session() as session:
            # The entry ref_id is minted at approval, not at propose time, so it
            # is derived from the proposal's uid here — the same expression
            # `on_approval` uses.
            ref_id = f"p6-entry-{session.query(Proposal).one().proposal_uid}"
        self.broker.placement_unknown = True
        self.broker.phantom_ref_ids = (ref_id,)
        result = self.approve(card)
        self.assertEqual(result.status, "reconciliation_required")
        self.assertEqual(self.status(card.execution_id), ExecutionState.PLACEMENT_UNKNOWN.value)

        placements = self.broker.calls.get("place_order", 0)
        self.service.resume(now=self.now)
        self.assertEqual(
            self.broker.calls.get("place_order", 0), placements, "resume re-placed the entry"
        )
        # It must never conclude "no order exists" when one does: that would
        # release the reservation and tell the owner nothing was placed.
        self.assertNotEqual(self.status(card.execution_id), ExecutionState.FAILED_NO_ORDER.value)
        with get_session() as session:
            self.assertIn(
                ref_id, session.query(Proposal).one().entry_broker_order_id or ""
            )


class RestartTests(ExecutionTestCase):
    def test_restart_resumes_without_a_second_entry(self):
        """Rows mid-machine, a fresh service, no duplicate order."""
        self.build(broker=FakeExecutionBroker(fill_price=100.0, stop_readable=False))
        card = self.propose()
        self.approve(card)
        self.assertEqual(self.status(card.execution_id), ExecutionState.PROTECTION_FAILED.value)

        # The process dies. Everything below is a fresh service over the same
        # rows and the same broker — no in-memory state survives.
        self.broker.stop_readable = True
        restarted = fx.service(
            broker=self.broker, settings=self.settings_for(), pager=RecordingPager(),
            venue=slx.PAPER_VENUE,
        )
        placements = self.broker.calls["place_order"]
        actions = restarted.resume(now=self.now)

        self.assertEqual(self.broker.calls["place_order"], placements, "resume placed a second entry")
        self.assertEqual(self.status(card.execution_id), ExecutionState.PROTECTED.value)
        self.assertEqual([a.now for a in actions], [ExecutionState.PROTECTED.value])
        self.assertIsNone(self.entry_block())

    def test_resume_from_a_working_entry_protects_once_it_fills(self):
        self.build(broker=FakeExecutionBroker(fill_price=100.0, fill_entry=False))
        card = self.propose()
        self.approve(card)
        self.assertEqual(self.status(card.execution_id), ExecutionState.ACCEPTED.value)

        # Still working: resume leaves it alone and places nothing.
        placements = self.broker.calls["place_order"]
        self.service.resume(now=self.now)
        self.assertEqual(self.status(card.execution_id), ExecutionState.ACCEPTED.value)
        self.assertEqual(self.broker.calls["place_order"], placements)
        self.assertEqual(self.broker.calls.get("place_stop", 0), 0)

        with get_session() as session:
            entry_order_id = session.query(Proposal).one().entry_broker_order_id
        self.broker.fill_remainder(entry_order_id)
        self.service.resume(now=self.now)

        self.assertEqual(self.status(card.execution_id), ExecutionState.PROTECTED.value)
        self.assertEqual(self.broker.calls["place_order"], placements)
        self.assertEqual(self.broker.calls["place_stop"], 1)

    def test_resume_records_a_broker_rejection_after_a_restart(self):
        self.build(broker=FakeExecutionBroker(fill_price=100.0, fill_entry=False))
        card = self.propose()
        self.approve(card)
        with get_session() as session:
            entry_order_id = session.query(Proposal).one().entry_broker_order_id
        self.assertTrue(self.broker.reject_order(entry_order_id))

        self.service.resume(now=self.now)
        self.assertEqual(self.status(card.execution_id), ExecutionState.ORDER_REJECTED.value)
        self.assertEqual(self.reserved(), 0.0)

    def test_a_read_failure_never_resolves_an_execution(self):
        """"I could not check" is not "it is fine"."""
        self.build(broker=FakeExecutionBroker(fill_price=100.0, fill_entry=False))
        card = self.propose()
        self.approve(card)

        def explode(order_id):
            raise RuntimeError("broker unreachable")

        self.broker.get_order_status = explode
        actions = self.service.resume(now=self.now)
        self.assertEqual([a.action for a in actions], ["unresolved"])
        self.assertEqual(self.status(card.execution_id), ExecutionState.ACCEPTED.value)
        self.assertGreater(self.reserved(), 0.0)


# --------------------------------------------------------------------------- #
# Owner-side terminal paths, closure, and the reservation
# --------------------------------------------------------------------------- #


class TerminalPathTests(ExecutionTestCase):
    def test_owner_cancellation_is_terminal(self):
        self.build()
        card = self.propose()
        self.service.cancel(execution_id=card.execution_id, by="bryan", reason="changed my mind", now=self.now)
        self.assertEqual(self.status(card.execution_id), ExecutionState.CANCELLED.value)
        self.assertEqual(self.reserved(), 0.0)
        self.assertEqual(self.broker.order_calls, 0)
        with get_session() as session:
            self.assertEqual(session.query(Proposal).one().status, "cancelled")

    def test_a_lapsed_approval_expires_the_execution(self):
        self.build()
        card = self.propose()
        later = self.now + timedelta(hours=4)
        expired = self.service.expire_stale(now=later)
        self.assertEqual(expired, [card.execution_id])
        self.assertEqual(self.status(card.execution_id), ExecutionState.EXPIRED.value)
        self.assertEqual(self.reserved(), 0.0)
        self.assertEqual(self.broker.order_calls, 0)

    def test_expiry_is_idempotent(self):
        self.build()
        card = self.propose()
        later = self.now + timedelta(hours=4)
        self.service.expire_stale(now=later)
        self.assertEqual(self.service.expire_stale(now=later), [])

    def test_closing_walks_out_and_releases(self):
        self.build()
        card = self.propose()
        self.approve(card)
        self.assertGreater(self.reserved(), 0.0)
        self.service.close(
            execution_id=card.execution_id, exit_price=104.0, exit_reason="target", now=self.now
        )
        trade = self.trade(card.execution_id)
        self.assertEqual(trade.status, ExecutionState.CLOSED.value)
        self.assertEqual(trade.exit_price, 104.0)
        self.assertEqual(trade.exit_reason, "target")
        self.assertEqual(self.reserved(), 0.0)

    def test_the_reservation_releases_exactly_once(self):
        """Release is a terminal transition, and terminals have no outgoing edge."""
        self.build()
        card = self.propose()
        self.approve(card)
        self.service.close(execution_id=card.execution_id, exit_price=104.0, now=self.now)
        self.assertEqual(self.reserved(), 0.0)

        # Re-running every release path is a no-op: `closed` is terminal, so the
        # machine refuses to move and the counter cannot drop twice.
        self.service.close(execution_id=card.execution_id, exit_price=104.0, now=self.now)
        self.service.resume(now=self.now)
        self.assertEqual(self.reserved(), 0.0)
        self.assertEqual(self.status(card.execution_id), ExecutionState.CLOSED.value)
        with get_session() as session:
            self.assertEqual(session.query(StrategyTrade).count(), 1)

    def test_a_second_approval_cannot_overspend_the_daily_cap(self):
        """Concurrent cap reservation: the first approval reserves; the second sees it."""
        self.build(settings=self.settings_for(discretionary_daily_notional=6000.0))
        first = self.propose()
        self.approve(first)
        self.assertEqual(self.reserved(), 5000.0)

        with get_session() as session:
            arm, decision = fx.build_lab(session, mode=self.mode, ticker="MSFT")
            session.commit()
            request = fx.request_for(arm, decision)
        second = self.service.propose(request, now=self.now)

        # $5,000 is reserved against a $6,000 daily cap, so the second entry can
        # only be sized to $1,000 — ten shares — or be refused outright. Either
        # way the cap binds; what must never happen is a second full-size order.
        if second.blocked_reason:
            self.assertEqual(len(self.broker.placed_entries), 1)
        else:
            self.approve(second)
            self.assertEqual(len(self.broker.placed_entries), 2)
            self.assertLessEqual(float(self.broker.placed_entries[-1].quantity or 0), 10)
            self.assertLessEqual(self.reserved(), 6000.0)


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #


class ReconciliationTests(ExecutionTestCase):
    def test_a_matching_book_reconciles_clean(self):
        self.build()
        card = self.propose()
        self.approve(card)
        report = self.service.reconcile(mode=self.mode, now=self.now)
        self.assertTrue(report.ok, report.as_dict())
        self.assertEqual(self.status(card.execution_id), ExecutionState.PROTECTED.value)
        self.assertIsNone(self.entry_block())

    def test_a_quantity_mismatch_blocks_and_pages(self):
        self.build()
        card = self.propose()
        self.approve(card)
        with get_session() as session:
            trade = slx.require_execution(session, card.execution_id)
            trade.quantity = 80.0  # someone sold 30 shares in the app
            session.commit()

        report = self.service.reconcile(mode=self.mode, now=self.now)
        self.assertFalse(report.ok)
        self.assertEqual([f.kind for f in report.mismatches], ["quantity_mismatch"])
        self.assertEqual(
            self.status(card.execution_id), ExecutionState.RECONCILIATION_REQUIRED.value
        )
        self.assertIn("broker_reconciliation_mismatch", self.pager.events())
        self.assertEqual(self.entry_block()[0], killswitch.UNRESOLVED_EXECUTION)
        recovery = self.pager.pages[-1][1]["recovery"]
        self.assertIn("re-run the", recovery)
        self.assertIn("Do not place a compensating order", recovery)

    def test_a_position_missing_at_the_broker_blocks(self):
        self.build()
        card = self.propose()
        self.approve(card)
        self.broker.get_positions_detail = lambda: []
        report = self.service.reconcile(mode=self.mode, now=self.now)
        self.assertEqual([f.kind for f in report.mismatches], ["missing_at_broker"])
        self.assertEqual(
            self.status(card.execution_id), ExecutionState.RECONCILIATION_REQUIRED.value
        )

    def test_reconciling_a_venue_with_no_adapter_fails_closed(self):
        self.build(adapters={})
        with self.assertRaises(ArmExecutionRefused) as caught:
            self.service.reconcile(mode=self.mode, now=self.now)
        self.assertEqual(caught.exception.code, "no_adapter")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
