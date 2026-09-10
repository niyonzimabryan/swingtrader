"""Spec L §8: test_capabilities_gate_intent and test_fake_broker_contract.

Schwab is deferred (Spec L §5.2). What ships instead is a contract: the
capability declaration, the gate that reads it, and a fake broker that
satisfies every method a real adapter must. These tests are that contract —
when ``execution/brokers/schwab.py`` is written, it passes them or it is not
finished.
"""

from __future__ import annotations

import inspect
import unittest

from execution.brokers.base import BrokerClient, BrokerOrderRequest
from execution.brokers.capabilities import (
    PROTECTIVE_EXIT_CAPABILITIES,
    BrokerCapabilities,
    CapabilityRefused,
    OrderIntent,
    gate_intent,
)
from execution.brokers.fake import FakeBroker
from execution.brokers.robinhood import ROBINHOOD_CAPABILITIES
from portfolio.records import BrokerSnapshot
from tests import portfoliofixture as fx


class CapabilityGateTests(unittest.TestCase):
    def test_capabilities_gate_intent(self):
        """An intent needing a protective exit is refused on the declaration."""
        unprotectable = BrokerCapabilities(
            can_place_equity_market=True,
            can_place_attached_stop=False,
            can_place_standalone_gtc_stop=False,
        )
        with self.assertRaises(CapabilityRefused) as caught:
            gate_intent(unprotectable, OrderIntent("AMD", quantity=10))
        self.assertEqual(caught.exception.missing, PROTECTIVE_EXIT_CAPABILITIES)
        self.assertIn("outlives this process", caught.exception.reason)

        # The refusal is about *protection*, not about placing at all: the same
        # adapter serves an intent that does not need one.
        gate_intent(unprotectable, OrderIntent("AMD", quantity=10, requires_protective_exit=False))

    def test_a_standalone_gtc_stop_satisfies_the_protection_requirement(self):
        """Robinhood's real shape: no attached stop, but a separate gtc stop."""
        self.assertFalse(ROBINHOOD_CAPABILITIES.can_place_attached_stop)
        self.assertTrue(ROBINHOOD_CAPABILITIES.can_place_standalone_gtc_stop)
        self.assertTrue(ROBINHOOD_CAPABILITIES.can_protect_a_position)
        gate_intent(ROBINHOOD_CAPABILITIES, OrderIntent("AMD", quantity=10))

    def test_a_protected_entry_may_not_be_fractional_or_a_dollar_amount(self):
        """Stops are whole-share only, so the entry they protect must be too."""
        with self.assertRaises(CapabilityRefused) as fractional:
            gate_intent(ROBINHOOD_CAPABILITIES, OrderIntent("AMD", quantity=1.5))
        self.assertIn("whole-share", fractional.exception.reason)

        with self.assertRaises(CapabilityRefused) as dollars:
            gate_intent(ROBINHOOD_CAPABILITIES, OrderIntent("AMD", dollar_amount=250.0))
        self.assertIn("whole-share", dollars.exception.reason)

        # Unprotected, fractional is fine — that is the one case Robinhood
        # allows and the reason the two rules are separate.
        gate_intent(
            ROBINHOOD_CAPABILITIES,
            OrderIntent("AMD", quantity=1.5, requires_protective_exit=False),
        )

    def test_a_protected_entry_may_not_be_an_extended_hours_order(self):
        with self.assertRaises(CapabilityRefused) as caught:
            gate_intent(
                ROBINHOOD_CAPABILITIES,
                OrderIntent("AMD", quantity=10, extended_hours=True),
            )
        self.assertIn("regular hours", caught.exception.reason)

    def test_an_undeclared_order_type_is_refused(self):
        reads_only = BrokerCapabilities(
            can_read_positions=True, can_place_standalone_gtc_stop=True
        )
        with self.assertRaises(CapabilityRefused):
            gate_intent(reads_only, OrderIntent("AMD", order_type="market", quantity=10))
        with self.assertRaises(CapabilityRefused):
            gate_intent(reads_only, OrderIntent("AMD", order_type="limit", quantity=10))

    def test_capabilities_round_trip_through_the_stored_json_shape(self):
        stored = ROBINHOOD_CAPABILITIES.as_dict()
        self.assertEqual(BrokerCapabilities.from_dict(stored), ROBINHOOD_CAPABILITIES)
        # A row written by a revision that knew about a capability this build
        # does not must still load rather than raising.
        self.assertEqual(
            BrokerCapabilities.from_dict({**stored, "can_place_quantum_stop": True}),
            ROBINHOOD_CAPABILITIES,
        )


class FakeBrokerContractTests(unittest.TestCase):
    """test_fake_broker_contract — the surface a future adapter must satisfy."""

    def setUp(self):
        self.broker = fx.two_account_broker()

    def test_fake_broker_contract(self):
        """Every BrokerClient method and every capability path is implemented."""
        required = [
            name
            for name, value in vars(BrokerClient).items()
            if not name.startswith("_") and callable(value)
        ]
        self.assertTrue(required, "the BrokerClient protocol has no methods to check")
        for name in required:
            with self.subTest(method=name):
                self.assertTrue(
                    callable(getattr(self.broker, name, None)),
                    f"FakeBroker is missing BrokerClient.{name}",
                )
                self.assertEqual(
                    _positional_arity(getattr(BrokerClient, name)),
                    _positional_arity(getattr(type(self.broker), name)),
                    f"FakeBroker.{name} does not take the protocol's arguments",
                )

        for attribute in ("name", "supports_fractional", "supports_order_review", "live_trading"):
            self.assertTrue(hasattr(self.broker, attribute), attribute)

        # The capability declaration path, and every field in it.
        capabilities = self.broker.capabilities()
        self.assertIsInstance(capabilities, BrokerCapabilities)
        for field in BrokerCapabilities.__dataclass_fields__:
            self.assertIsInstance(getattr(capabilities, field), bool, field)

        # The ledger source path.
        snapshot = self.broker.fetch_ledger_snapshot()
        self.assertIsInstance(snapshot, BrokerSnapshot)
        self.assertEqual(len(snapshot.accounts), 2)
        for account in snapshot.accounts:
            self.assertTrue(account.ok)
            self.assertIsNotNone(account.as_of_utc)

    def test_the_fake_declares_the_most_constrained_real_shape(self):
        """A permissive fake would let the capability gate's bug through."""
        self.assertFalse(self.broker.capabilities().can_place_attached_stop)
        self.assertFalse(self.broker.capabilities().can_place_bracket)

    def test_the_fake_refuses_every_write_path(self):
        review = self.broker.review_order(
            BrokerOrderRequest(symbol="AMD", side="buy", order_type="market", quantity=1)
        )
        self.assertFalse(review.approved)
        self.assertFalse(self.broker.place_order(review).success)
        self.assertFalse(self.broker.cancel_order("ord-1")["success"])
        self.assertFalse(self.broker.close_position("AMD")["success"])

    def test_a_broker_wide_failure_raises_rather_than_returning_an_empty_book(self):
        broker = FakeBroker(fail_with="connection reset")
        with self.assertRaises(RuntimeError):
            broker.fetch_ledger_snapshot()

    def test_the_read_methods_answer_from_the_same_state_the_snapshot_does(self):
        positions = self.broker.get_positions_detail()
        snapshot_symbols = {
            holding.symbol
            for account in self.broker.fetch_ledger_snapshot().accounts
            for holding in account.holdings
        }
        self.assertEqual({row["ticker"] for row in positions}, snapshot_symbols)
        self.assertEqual(len(self.broker.get_orders()), 1)
        self.assertEqual(self.broker.get_orders(status="filled"), [])


def _positional_arity(function) -> int:
    signature = inspect.signature(function)
    return len(
        [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        ]
    )


if __name__ == "__main__":
    unittest.main()
