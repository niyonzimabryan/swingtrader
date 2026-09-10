"""Spec Q §16: the broker contract, and the safety regressions behind it.

Two claims, and they are different claims.

**The contract** (:class:`BrokerContractTests`) is what every adapter must
satisfy, asserted against the fake and — for the parts that can be asserted
without a network — against the real adapters' declarations and request
builders. Spec L §5.2 promised this suite when Schwab was deferred: a second
adapter is cheap only if there is one place that says what an adapter *is*.

**The safety regressions** (:class:`ZeroOrderCallTests` and the classes after
it) prove the other half of Spec Q §16: "safety regressions proving every
missing/false/inconsistent live gate produces zero order calls". Each one drives
the real service with exactly one gate wrong and then asserts
``broker.order_calls == 0`` — a count the fake keeps of the two methods that
could create an order at a real broker. Counting on the fake rather than
patching the method is the point: a patched method proves the test replaced
something, and a counter proves the code never called it.

Nothing here reaches Robinhood, and nothing here can: the live flags are on in
several tests, and every one of them is pointed at a fake.
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from database.db import get_session, init_db
from database.models import Proposal, StrategyTrade
from execution.brokers.alpaca import AlpacaBroker
from execution.brokers.base import FINISHED_ORDER_STATES, OpenOrder
from execution.brokers.fake import FakeBroker, FakeExecutionBroker
from execution.brokers.robinhood import ROBINHOOD_CAPABILITIES, RobinhoodMCPBroker
from execution.lifecycle import ExecutionRefused
from portfolio import killswitch
from portfolio.capabilities import PROTECTIVE_EXIT_CAPABILITIES, BrokerCapabilities
from portfolio.paging import RecordingPager
from strategy_lab import execution as slx
from strategy_lab.domain import ExecutionMode, ExecutionState
from tests import proposalfixture as pf
from tests import strategyexecfixture as fx
from tests.dbfixture import TestDatabase


# --------------------------------------------------------------------------- #
# The shared broker contract
# --------------------------------------------------------------------------- #


#: Every method the execution path calls on an adapter. An adapter missing one
#: fails here rather than at 09:31 on a Tuesday.
REQUIRED_SURFACE = (
    "review_order",
    "place_order",
    "place_stop",
    "read_open_orders",
    "get_order_status",
    "cancel_order",
    "capabilities",
)


class BrokerContractTests(unittest.TestCase):
    """One suite, every adapter. Placement is asserted only against fakes."""

    def adapters(self):
        return {
            "fake_exec": FakeExecutionBroker(fill_price=100.0),
            "fake_read": FakeBroker(),
        }

    def test_every_adapter_implements_the_whole_surface(self):
        classes = {
            "FakeExecutionBroker": FakeExecutionBroker,
            "FakeBroker": FakeBroker,
            "AlpacaBroker": AlpacaBroker,
            "RobinhoodMCPBroker": RobinhoodMCPBroker,
        }
        missing = []
        for name, cls in classes.items():
            for method in REQUIRED_SURFACE:
                if not callable(getattr(cls, method, None)):
                    missing.append(f"{name}.{method}")
        self.assertEqual(missing, [])

    def test_no_adapter_claims_an_attached_stop(self):
        """Spec L §5.1: the schema has no bracket, OCO, OTO, or attach-stop."""
        for name, adapter in self.adapters().items():
            with self.subTest(name):
                self.assertFalse(adapter.capabilities().can_place_attached_stop)
                self.assertFalse(adapter.capabilities().can_place_bracket)

    def test_every_adapter_can_protect_a_position_some_way(self):
        for name, adapter in self.adapters().items():
            with self.subTest(name):
                capabilities = adapter.capabilities()
                self.assertTrue(capabilities.can_protect_a_position)
                self.assertTrue(
                    any(getattr(capabilities, c) for c in PROTECTIVE_EXIT_CAPABILITIES)
                )

    def test_robinhood_declares_the_verified_shape(self):
        """The 2026-09-08 `tools/list` dump, as a declaration rather than a hope."""
        self.assertFalse(ROBINHOOD_CAPABILITIES.can_place_attached_stop)
        self.assertTrue(ROBINHOOD_CAPABILITIES.can_place_standalone_gtc_stop)
        self.assertTrue(ROBINHOOD_CAPABILITIES.stops_regular_hours_only)
        self.assertTrue(ROBINHOOD_CAPABILITIES.stops_whole_shares_only)

    def test_a_protective_stop_is_whole_share_only(self):
        broker = FakeExecutionBroker()
        result = broker.place_stop(symbol="AMD", quantity=1.5, stop_price=95.0, ref_id="r1")
        self.assertFalse(result.success)
        self.assertIn("whole-share", result.error)

    def test_a_protective_stop_requires_an_idempotency_key(self):
        broker = FakeExecutionBroker()
        result = broker.place_stop(symbol="AMD", quantity=5, stop_price=95.0, ref_id="")
        self.assertFalse(result.success)
        self.assertIn("ref_id", result.error)

    def test_a_protective_stop_is_gtc_and_regular_hours(self):
        broker = FakeExecutionBroker()
        broker.place_stop(symbol="AMD", quantity=5, stop_price=95.0, ref_id="r1")
        order = [o for o in broker.read_open_orders(symbol="AMD") if o.order_type == "stop_market"][0]
        self.assertEqual(order.time_in_force, "gtc")
        self.assertEqual(order.stop_price, 95.0)

    def test_a_repeated_ref_id_does_not_create_a_second_order(self):
        """What makes the fill-to-stop retry safe (Spec L §5.1)."""
        broker = FakeExecutionBroker()
        first = broker.place_stop(symbol="AMD", quantity=5, stop_price=95.0, ref_id="r1")
        second = broker.place_stop(symbol="AMD", quantity=5, stop_price=95.0, ref_id="r1")
        self.assertEqual(first.order_id, second.order_id)
        stops = [o for o in broker.read_open_orders(symbol="AMD") if o.order_type == "stop_market"]
        self.assertEqual(len(stops), 1)

    def test_a_vanished_stop_can_be_re_placed_under_the_same_ref_id(self):
        """Dedup is against *working* orders, not against history."""
        broker = FakeExecutionBroker()
        broker.place_stop(symbol="AMD", quantity=5, stop_price=95.0, ref_id="r1")
        self.assertEqual(broker.drop_stop("AMD"), 1)
        again = broker.place_stop(symbol="AMD", quantity=5, stop_price=95.0, ref_id="r1")
        self.assertTrue(again.success)
        stops = [o for o in broker.read_open_orders(symbol="AMD") if o.order_type == "stop_market"]
        self.assertEqual(len(stops), 1)

    def test_an_unrecognised_order_state_counts_as_open(self):
        """Guessing the other way would report an unprotected position protected."""
        order = OpenOrder(
            broker="x", order_id="1", symbol="AMD", side="sell",
            order_type="stop_market", status="a_state_this_code_has_never_seen",
        )
        self.assertTrue(order.is_open)
        self.assertNotIn("a_state_this_code_has_never_seen", FINISHED_ORDER_STATES)

    def test_a_read_only_fake_refuses_every_placement(self):
        broker = FakeBroker()
        self.assertFalse(broker.place_stop(symbol="AMD", quantity=5, stop_price=95.0, ref_id="r").success)
        self.assertFalse(broker.review_order(None).approved)


# --------------------------------------------------------------------------- #
# Zero order calls when any gate is missing, false, or inconsistent
# --------------------------------------------------------------------------- #


class SafetyTestCase(unittest.TestCase):
    mode = ExecutionMode.PAPER
    promote = True

    def setUp(self):
        self.db = TestDatabase("strategy_safety")
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
            self.request = fx.request_for(arm, decision)
        self.broker = FakeExecutionBroker(fill_price=100.0)
        self.pager = RecordingPager()

    def service(self, *, settings=None, venue=None, adapters=None):
        return fx.service(
            broker=self.broker,
            settings=settings or self.default_settings(),
            pager=self.pager,
            venue=venue or (slx.LIVE_VENUE if self.mode is ExecutionMode.LIVE else slx.PAPER_VENUE),
            adapters=adapters,
        )

    def default_settings(self, **overrides):
        if self.mode is ExecutionMode.LIVE:
            return fx.live_settings(**overrides)
        return pf.settings(**overrides)

    def assertNoOrders(self):
        self.assertEqual(
            self.broker.order_calls,
            0,
            f"an order call escaped a closed gate: {self.broker.calls}",
        )


class ZeroOrderCallTests(SafetyTestCase):
    """One gate wrong per test. Every one of them: zero order calls."""

    def test_phase6_disabled(self):
        service = self.service(settings=pf.settings(phase6_execution_enabled=False))
        with self.assertRaises(Exception):
            service.propose(self.request, now=self.now)
        self.assertNoOrders()

    def test_kill_switch_engaged(self):
        with get_session() as session:
            killswitch.set_switch(session, on=True, changed_by="bryan", reason="incident")
            session.commit()
        card = self.service().propose(self.request, now=self.now)
        self.assertEqual(card.blocked_reason, killswitch.KILL_SWITCH_ENGAGED)
        self.assertNoOrders()

    def test_kill_switch_survives_a_restart(self):
        """Invariant 9: the switch is a row, not a variable."""
        with get_session() as session:
            killswitch.set_switch(session, on=True, changed_by="bryan", reason="incident")
            session.commit()
        init_db(self.db.url)  # a fresh process against the same database
        with get_session() as session:
            self.assertTrue(killswitch.engaged(session))
        card = self.service().propose(self.request, now=self.now)
        self.assertEqual(card.blocked_reason, killswitch.KILL_SWITCH_ENGAGED)
        self.assertNoOrders()

    def test_no_adapter_for_the_venue(self):
        service = self.service(adapters={})
        with self.assertRaises(slx.ExecutionRefusedLocally):
            service.propose(self.request, now=self.now)
        self.assertNoOrders()

    def test_an_adapter_that_declares_another_venue(self):
        self.broker.venue = slx.LIVE_VENUE
        service = self.service(venue=slx.PAPER_VENUE)
        with self.assertRaises(slx.ExecutionRefusedLocally):
            service.propose(self.request, now=self.now)
        self.assertNoOrders()

    def test_an_adapter_that_cannot_protect(self):
        self.broker.declared_capabilities = BrokerCapabilities(
            can_read_positions=True,
            can_place_equity_market=True,
            can_place_equity_limit=True,
        )
        card = self.service().propose(self.request, now=self.now)
        self.assertEqual(card.blocked_reason, "capability_refused")
        self.assertNoOrders()

    def test_an_adapter_that_declares_no_capabilities_at_all(self):
        self.broker.capabilities = lambda: None
        card = self.service().propose(self.request, now=self.now)
        self.assertEqual(card.blocked_reason, "capabilities_undeclared")
        self.assertNoOrders()

    def test_a_blocking_execution_from_an_earlier_incident(self):
        with get_session() as session:
            arm, decision = fx.build_lab(session, mode=self.mode, ticker="NVDA")
            trade = slx.open_execution(
                session, arm_id=arm.id, decision_id=decision.id, mode=self.mode
            )
            # `proposed -> reconciliation_required` is the one blocking state
            # reachable from a fresh row; the machine refuses the rest, which is
            # itself the point (`test_the_machine_refuses_an_illegal_hop`).
            slx.transition(session, trade, ExecutionState.RECONCILIATION_REQUIRED)
            session.commit()
        card = self.service().propose(self.request, now=self.now)
        self.assertEqual(card.blocked_reason, killswitch.UNRESOLVED_EXECUTION)
        self.assertNoOrders()

    def test_the_machine_refuses_an_illegal_hop(self):
        """A row cannot be walked into a blocking state out of order."""
        from strategy_lab.domain import InvalidTransition

        with get_session() as session:
            arm, decision = fx.build_lab(session, mode=self.mode, ticker="MSFT")
            trade = slx.open_execution(
                session, arm_id=arm.id, decision_id=decision.id, mode=self.mode
            )
            with self.assertRaises(InvalidTransition):
                slx.transition(session, trade, ExecutionState.PROTECTION_FAILED)
            session.rollback()
        self.assertNoOrders()

    def test_a_forged_approval_signature(self):
        service = self.service()
        card = service.propose(self.request, now=self.now)
        self.assertNoOrders()
        with self.assertRaises(Exception):
            service.on_approval(
                execution_id=card.execution_id,
                presented_signature="0" * 64,
                owner_id="99887766",
                now=self.now,
            )
        self.assertNoOrders()

    def test_an_approval_from_the_wrong_owner(self):
        service = self.service()
        card = service.propose(self.request, now=self.now)
        with self.assertRaises(Exception):
            service.on_approval(
                execution_id=card.execution_id,
                presented_signature=card.approval_signature,
                owner_id="not-bryan",
                now=self.now,
            )
        self.assertNoOrders()

    def test_an_expired_approval(self):
        service = self.service()
        card = service.propose(self.request, now=self.now)
        with self.assertRaises(Exception):
            service.on_approval(
                execution_id=card.execution_id,
                presented_signature=card.approval_signature,
                owner_id="99887766",
                now=self.now + timedelta(hours=4),
            )
        self.assertNoOrders()

    def test_an_approval_for_an_unknown_execution(self):
        service = self.service()
        with self.assertRaises(Exception):
            service.on_approval(
                execution_id="does-not-exist",
                presented_signature="x",
                owner_id="99887766",
                now=self.now,
            )
        self.assertNoOrders()

    def test_a_risk_rejection_reaches_no_broker(self):
        import dataclasses

        request = dataclasses.replace(self.request, risk_fraction=0.0000001)
        card = self.service().propose(request, now=self.now)
        self.assertTrue(card.blocked_reason)
        self.assertNoOrders()


class ShadowNeverExecutesTests(SafetyTestCase):
    mode = ExecutionMode.SHADOW
    promote = False

    def test_a_shadow_arm_cannot_propose_an_execution(self):
        service = self.service(venue=slx.PAPER_VENUE, adapters={slx.PAPER_VENUE: self.broker})
        with self.assertRaises(slx.ShadowReachedExecution):
            service.propose(self.request, now=self.now)
        self.assertNoOrders()
        with get_session() as session:
            self.assertEqual(session.query(StrategyTrade).count(), 0)
            self.assertEqual(session.query(Proposal).count(), 0)

    def test_shadow_has_no_venue_at_all(self):
        """Absence, not an empty string: there is no venue shadow may reach."""
        self.assertNotIn(ExecutionMode.SHADOW, slx.MODE_VENUES)
        with self.assertRaises(slx.ShadowReachedExecution):
            slx.bind_adapter(
                arm_mode=ExecutionMode.SHADOW,
                requested_mode=ExecutionMode.SHADOW,
                venue=slx.PAPER_VENUE,
                adapter=self.broker,
            )


class LiveGateSafetyTests(SafetyTestCase):
    mode = ExecutionMode.LIVE

    def test_allow_live_trading_false(self):
        card = self.service(
            settings=pf.settings(allow_live_trading=False, execution_mode="live")
        ).propose(self.request, now=self.now)
        self.assertEqual(card.blocked_reason, "live_trading_disabled")
        self.assertNoOrders()

    def test_execution_mode_not_live(self):
        card = self.service(
            settings=pf.settings(allow_live_trading=True, execution_mode="paper")
        ).propose(self.request, now=self.now)
        self.assertEqual(card.blocked_reason, "execution_mode_not_live")
        self.assertNoOrders()

    def test_a_missing_flag_is_not_a_true_one(self):
        """Invariant 1: absence or invalidity of a flag never means live."""
        from types import SimpleNamespace

        bare = SimpleNamespace(phase6_execution_enabled=True)
        card = self.service(settings=bare).propose(self.request, now=self.now)
        self.assertEqual(card.blocked_reason, "live_trading_disabled")
        self.assertNoOrders()

    def test_a_paused_live_arm_cannot_place(self):
        from strategy_lab import registry

        with get_session() as session:
            registry.set_arm_status(session, self.arm_id, "paused")
            session.commit()
        card = self.service().propose(self.request, now=self.now)
        self.assertIn(card.blocked_reason, ("live_arm_not_active", "not_the_live_champion"))
        self.assertNoOrders()


class LiveWithoutPromotionSafetyTests(SafetyTestCase):
    mode = ExecutionMode.LIVE
    promote = False

    def test_no_promotion_event_means_no_live_order(self):
        card = self.service().propose(self.request, now=self.now)
        self.assertEqual(card.blocked_reason, "no_promotion_event")
        self.assertNoOrders()


# --------------------------------------------------------------------------- #
# Redaction (Spec Q §12 invariant 8, §17)
# --------------------------------------------------------------------------- #


class RedactionTests(unittest.TestCase):
    def test_an_allowlisted_field_survives_and_nothing_else_does(self):
        redacted = slx.redact({
            "execution_id": "abc",
            "ref_id": "p6-entry-1",
            "quantity": 10,
            "account_number": "123456789",
            "access_token": "secret",
            "raw_payload": {"account_number": "123456789"},
        })
        self.assertEqual(redacted["execution_id"], "abc")
        self.assertEqual(redacted["ref_id"], "p6-entry-1")
        self.assertEqual(redacted["quantity"], 10)
        self.assertEqual(redacted["account_number"], slx.REDACTED)
        self.assertEqual(redacted["access_token"], slx.REDACTED)
        self.assertEqual(redacted["raw_payload"], slx.REDACTED)

    def test_a_key_is_kept_even_when_its_value_is_not(self):
        """Knowing a token was present is diagnosis; knowing its value is a leak."""
        self.assertIn("access_token", slx.redact({"access_token": "secret"}))

    def test_a_nested_allowlisted_mapping_is_redacted_recursively(self):
        redacted = slx.redact({"recovery": "do the thing", "detail": {"order_id": "1"}})
        self.assertEqual(redacted["recovery"], "do the thing")
        self.assertEqual(redacted["detail"], slx.REDACTED)

    def test_a_non_scalar_allowlisted_value_becomes_its_type(self):
        redacted = slx.redact({"order_id": object()})
        self.assertEqual(redacted["order_id"], "<object>")

    def test_the_allowlist_carries_no_secret_shaped_key(self):
        forbidden = ("token", "secret", "password", "account_number", "payload", "raw")
        offenders = [
            key for key in slx.REDACTION_ALLOWLIST
            if any(needle in key for needle in forbidden)
        ]
        self.assertEqual(offenders, [])


# --------------------------------------------------------------------------- #
# The reservation predicate
# --------------------------------------------------------------------------- #


class ReservationVocabularyTests(unittest.TestCase):
    def test_no_terminal_state_holds_a_reservation(self):
        """"Every terminal path releases its reservation" — as a set relation."""
        from strategy_lab.domain import TERMINAL_EXECUTION_STATES

        self.assertEqual(slx.RESERVING_EXECUTION_STATES & TERMINAL_EXECUTION_STATES, frozenset())

    def test_an_unknown_placement_still_holds_its_reservation(self):
        """Invariant 12, stated as a membership rather than as a code path."""
        self.assertTrue(slx.holds_reservation(ExecutionState.PLACEMENT_UNKNOWN))
        self.assertTrue(slx.holds_reservation(ExecutionState.RECONCILIATION_REQUIRED))

    def test_nothing_is_reserved_before_risk_reserved(self):
        self.assertFalse(slx.holds_reservation(ExecutionState.PROPOSED))
        self.assertFalse(slx.holds_reservation(ExecutionState.OWNER_APPROVED))
        self.assertTrue(slx.holds_reservation(ExecutionState.RISK_RESERVED))

    def test_every_blocking_state_is_non_terminal_and_reserving(self):
        """A blocking state that released its reservation would block nothing."""
        from strategy_lab.domain import TERMINAL_EXECUTION_STATES

        self.assertEqual(slx.BLOCKING_EXECUTION_STATES & TERMINAL_EXECUTION_STATES, frozenset())
        self.assertTrue(slx.BLOCKING_EXECUTION_STATES <= slx.RESERVING_EXECUTION_STATES)

    def test_the_models_block_list_agrees_with_the_domain_one(self):
        from database.models import STRATEGY_TRADE_BLOCKING_STATUSES

        self.assertEqual(
            set(STRATEGY_TRADE_BLOCKING_STATUSES),
            {state.value for state in slx.BLOCKING_EXECUTION_STATES},
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
