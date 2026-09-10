"""Spec L §8 rows on the execution side (Phase 6, Spec L §6, Spec Q §12).

Every test drives the real :class:`execution.lifecycle.ExecutionService` against
the deterministic :class:`execution.brokers.fake.FakeExecutionBroker`, whose
failure modes (fill or not, stop readable or not, stop placement fails, stops
that vanish) are flags rather than mocked calls. Robinhood is unreachable from
the build environment, so the recorded-shape fixtures under
``tests/fixtures/robinhood/`` and this fake are the whole surface; what is
exercised against the fake only is called out in the module report.

Rows here:

* ``test_approval_is_single_use``
* ``test_risk_recomputed_at_approval``
* ``test_attached_stop_verified_at_broker``
* ``test_missing_stop_replaced_daily``
* ``test_unprotected_fill_pages``
* ``test_protected_entries_are_whole_shares``
* ``test_stop_orders_regular_hours_gtc``
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from database.db import get_session
from database.models import Proposal
from execution.brokers.fake import FakeExecutionBroker
from execution.lifecycle import ExecutionRefused, ExecutionService
from portfolio import approvals, killswitch, proposals
from portfolio.paging import RecordingPager
from tests import proposalfixture as pf
from tests.dbfixture import TestDatabase


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("lifecycle")
        self.addCleanup(self.db.cleanup)
        from database.db import init_db

        init_db(self.db.url)
        self.now = pf.NOW
        self.settings = pf.settings()
        with get_session() as session:
            pf.synced_session(session, now=self.now)
            session.commit()

    def _make_proposal(self, *, settings=None, **kwargs):
        params = dict(
            ticker="AMD",
            entry=100.0,
            stop=95.0,
            risk_fraction=0.005,
            settings=settings or self.settings,
            owner_id="99887766",
            now=self.now,
            resolver=pf.resolver_for({}),
        )
        params.update(kwargs)
        with get_session() as session:
            row = proposals.create_proposal(session, **params)
            proposal_id = row.id
            signature = row.approval_signature
            session.commit()
        return proposal_id, signature

    def _service(self, broker=None, pager=None, settings=None):
        return ExecutionService(
            session_factory=get_session,
            broker=broker or FakeExecutionBroker(fill_price=100.0),
            settings=settings or self.settings,
            pager=pager or RecordingPager(),
        )

    def _status(self, proposal_id):
        with get_session() as session:
            return session.get(Proposal, proposal_id).status

    # -- approval single-use ------------------------------------------------

    def test_approval_is_single_use(self):
        proposal_id, sig = self._make_proposal()
        service = self._service()

        result = service.on_approval(
            proposal_id=proposal_id, presented_signature=sig, owner_id="99887766", now=self.now
        )
        self.assertEqual(result.status, "protected")

        # A replay of the exact same callback is refused: single-use is enforced
        # by the consumed-at column, so it survives even a fresh service.
        with self.assertRaises(ExecutionRefused) as ctx:
            self._service().on_approval(
                proposal_id=proposal_id, presented_signature=sig, owner_id="99887766", now=self.now
            )
        # The proposal is no longer 'proposed', so the not-proposed guard fires.
        self.assertIn(ctx.exception.code, ("not_proposed", "approval_already_used"))

    def test_expired_approval_is_refused(self):
        proposal_id, sig = self._make_proposal()
        late = self.now + timedelta(seconds=self.settings.approval_ttl_seconds + 60)
        with self.assertRaises(approvals.ApprovalRefused) as ctx:
            self._service().on_approval(
                proposal_id=proposal_id, presented_signature=sig, owner_id="99887766", now=late
            )
        self.assertEqual(ctx.exception.code, "approval_expired")

    def test_wrong_owner_is_refused(self):
        proposal_id, sig = self._make_proposal()
        with self.assertRaises(approvals.ApprovalRefused) as ctx:
            self._service().on_approval(
                proposal_id=proposal_id, presented_signature=sig, owner_id="someone-else", now=self.now
            )
        self.assertEqual(ctx.exception.code, "owner_mismatch")

    def test_tampered_signature_is_refused(self):
        proposal_id, sig = self._make_proposal()
        bad = ("0" if sig[0] != "0" else "1") + sig[1:]
        with self.assertRaises(approvals.ApprovalRefused) as ctx:
            self._service().on_approval(
                proposal_id=proposal_id, presented_signature=bad, owner_id="99887766", now=self.now
            )
        self.assertEqual(ctx.exception.code, "signature_mismatch")

    # -- risk recomputed at approval ---------------------------------------

    def test_risk_recomputed_at_approval(self):
        """A proposal valid at creation is refused when fresh state breaches a limit.

        The book is re-read at approval and a concentration breach that did not
        exist at proposal time now does. The stored numbers are never reused
        (Spec L §6.2), so the approval is refused and the approval consumed.
        """
        proposal_id, sig = self._make_proposal(risk_fraction=0.005)
        # Between proposal and approval, a large AMD position appears in the
        # read-only primary account, pushing concentration to the cap.
        later = self.now + timedelta(minutes=5)
        with get_session() as session:
            pf.synced_session(
                session, now=later, primary_holdings=[pf.holding("AMD", 200, 100.0)]
            )
            session.commit()
        with self.assertRaises(ExecutionRefused) as ctx:
            self._service().on_approval(
                proposal_id=proposal_id, presented_signature=sig, owner_id="99887766", now=later
            )
        self.assertEqual(ctx.exception.code, "risk_recomputed_rejected")
        self.assertEqual(self._status(proposal_id), "risk_rejected")

    def test_kill_switch_blocks_placement(self):
        proposal_id, sig = self._make_proposal()
        with get_session() as session:
            killswitch.set_switch(session, on=True, changed_by="owner", reason="drill")
            session.commit()
        with self.assertRaises(ExecutionRefused) as ctx:
            self._service().on_approval(
                proposal_id=proposal_id, presented_signature=sig, owner_id="99887766", now=self.now
            )
        self.assertEqual(ctx.exception.code, killswitch.KILL_SWITCH_ENGAGED)
        # Nothing placed; still proposed.
        self.assertEqual(self._status(proposal_id), "proposed")

    def test_phase6_disabled_refuses(self):
        proposal_id, sig = self._make_proposal()
        service = self._service(settings=pf.settings(phase6_execution_enabled=False))
        with self.assertRaises(ExecutionRefused) as ctx:
            service.on_approval(
                proposal_id=proposal_id, presented_signature=sig, owner_id="99887766", now=self.now
            )
        self.assertEqual(ctx.exception.code, "phase6_disabled")

    def test_live_placement_needs_allow_live_and_mode(self):
        """A live proposal is refused unless ALLOW_LIVE_TRADING and EXECUTION_MODE=live."""
        live_settings = pf.settings(execution_mode="live", allow_live_trading=False)
        with get_session() as session:
            pf.synced_session(session, now=self.now)  # refresh ledger
            session.commit()
        proposal_id, sig = self._make_proposal(settings=live_settings)
        # allow_live_trading is false → refused even though the flags name live.
        with self.assertRaises(ExecutionRefused) as ctx:
            self._service(settings=live_settings).on_approval(
                proposal_id=proposal_id, presented_signature=sig, owner_id="99887766", now=self.now
            )
        self.assertEqual(ctx.exception.code, "live_trading_disabled")

    # -- protection ---------------------------------------------------------

    def test_attached_stop_verified_at_broker(self):
        """Protected only when the gtc stop_market is readable from the broker."""
        broker = FakeExecutionBroker(fill_price=100.0, stop_readable=True)
        result = self._run(broker)
        self.assertEqual(result.status, "protected")
        # The stop that was placed is gtc, regular-hours, whole-share (asserted
        # in test_stop_orders_regular_hours_gtc); here it is READ BACK.
        self.assertEqual(len(broker.placed_stops), 1)

    def test_unprotected_fill_pages(self):
        """A fill whose stop cannot be read back pages and blocks further entries."""
        broker = FakeExecutionBroker(fill_price=100.0, stop_readable=False)
        pager = RecordingPager()
        result = self._run(broker, pager=pager)
        self.assertEqual(result.status, "unprotected")
        self.assertTrue(result.paged)
        self.assertIn("execution_unprotected_fill", pager.events())
        # Every further entry is now blocked (Spec L §5.1, Spec Q §12 inv 3).
        with get_session() as session:
            block = killswitch.entry_block(session)
        self.assertIsNotNone(block)
        self.assertEqual(block[0], killswitch.UNPROTECTED_POSITION)

    def test_protected_entries_are_whole_shares(self):
        """A protected entry is placed whole-share, and the stop refuses fractions."""
        broker = FakeExecutionBroker(fill_price=100.0)
        self._run(broker)
        entry = broker.placed_entries[0]
        self.assertEqual(entry.quantity, int(entry.quantity))
        self.assertIsNone(entry.dollar_amount)
        stop = broker.placed_stops[0]
        self.assertEqual(stop["quantity"], int(stop["quantity"]))
        # And a fractional stop is refused by the adapter itself.
        refusal = broker.place_stop(symbol="AMD", quantity=1.5, stop_price=95.0, ref_id="x")
        self.assertFalse(refusal.success)

    def test_stop_orders_regular_hours_gtc(self):
        """Every protective stop is stop_market, regular_hours, gtc, with a ref_id.

        Asserted against the *Robinhood* adapter's own request builder, since it
        is the live venue whose schema forces these; the fake mirrors it.
        """
        from execution.brokers.robinhood import RobinhoodMCPBroker

        rh = RobinhoodMCPBroker(pf.settings(robinhood_account_number="RH123"))
        captured = {}

        def fake_review(order):
            captured["order"] = order
            from execution.brokers.base import BrokerOrderReview

            return BrokerOrderReview(broker="robinhood", request=order, approved=False, errors=["stop test: no placement"])

        rh.review_order = fake_review
        rh.place_stop(symbol="AMD", quantity=10, stop_price=95.0, ref_id="p6-stop-xyz")
        order = captured["order"]
        self.assertEqual(order.order_type, "stop_market")
        self.assertEqual(order.market_hours, "regular_hours")
        self.assertEqual(order.time_in_force, "gtc")
        self.assertEqual(order.client_context.get("ref_id"), "p6-stop-xyz")
        self.assertEqual(order.quantity, 10)

    def test_missing_stop_replaced_daily(self):
        """A stop that has vanished from get_equity_orders is re-placed and re-verified."""
        broker = FakeExecutionBroker(fill_price=100.0)
        self._run(broker)
        proposal_id = self._only_proposal_id()
        self.assertEqual(self._status(proposal_id), "protected")

        # Robinhood's unstated GTC horizon expires the stop between sessions.
        dropped = broker.drop_stop("AMD")
        self.assertEqual(dropped, 1)

        pager = RecordingPager()
        service = ExecutionService(
            session_factory=get_session, broker=broker, settings=self.settings, pager=pager
        )
        actions = service.replace_missing_stops(now=self.now + timedelta(days=1))
        self.assertEqual(actions, [{"proposal_id": proposal_id, "action": "replaced"}])
        self.assertEqual(self._status(proposal_id), "protected")
        self.assertIn("execution_stop_replaced", pager.events())
        # Two stops placed in total: the original and the replacement.
        self.assertEqual(len(broker.placed_stops), 2)

    def test_missing_stop_that_cannot_be_replaced_goes_unprotected(self):
        broker = FakeExecutionBroker(fill_price=100.0)
        self._run(broker)
        proposal_id = self._only_proposal_id()
        broker.drop_stop("AMD")
        broker.stop_place_succeeds = False
        pager = RecordingPager()
        service = ExecutionService(
            session_factory=get_session, broker=broker, settings=self.settings, pager=pager
        )
        service.replace_missing_stops(now=self.now + timedelta(days=1))
        self.assertEqual(self._status(proposal_id), "unprotected")
        self.assertIn("execution_stop_replace_failed", pager.events())

    def test_broker_review_rejection_fails_without_placing(self):
        broker = FakeExecutionBroker(review_approves=False)
        proposal_id, sig = self._make_proposal()
        with self.assertRaises(ExecutionRefused) as ctx:
            ExecutionService(
                session_factory=get_session, broker=broker, settings=self.settings
            ).on_approval(
                proposal_id=proposal_id, presented_signature=sig, owner_id="99887766", now=self.now
            )
        self.assertEqual(ctx.exception.code, "broker_review_rejected")
        self.assertEqual(self._status(proposal_id), "failed")
        self.assertEqual(broker.placed_entries, [])

    def test_journal_records_the_filled_decision(self):
        broker = FakeExecutionBroker(fill_price=100.0)
        self._run(broker)
        with get_session() as session:
            from research_workspace.store import journal_entries

            entries = journal_entries(session, ticker="AMD")
            self.assertTrue(entries)
            self.assertEqual(entries[-1].decision, "opened")
            self.assertEqual(entries[-1].budget, "discretionary")

    # -- helpers ------------------------------------------------------------

    def _run(self, broker, pager=None):
        proposal_id, sig = self._make_proposal()
        service = ExecutionService(
            session_factory=get_session,
            broker=broker,
            settings=self.settings,
            pager=pager or RecordingPager(),
        )
        return service.on_approval(
            proposal_id=proposal_id, presented_signature=sig, owner_id="99887766", now=self.now
        )

    def _only_proposal_id(self):
        with get_session() as session:
            return session.query(Proposal).order_by(Proposal.id.desc()).first().id


class RobinhoodAdapterContractTests(unittest.TestCase):
    """The Phase 6 adapter methods against recorded-shape Robinhood fixtures.

    Robinhood is unreachable from the build environment, so these run the real
    normalizers in ``execution/brokers/robinhood.py`` over the recorded-shape
    fixtures in ``tests/fixtures/robinhood/`` — the same substitution the
    ledger tests use. What they establish is that the adapter *parses* what
    Robinhood's schema says it sends; the live probe (owner action, in
    ``docs/EXECUTION_LIFECYCLE.md``) is what establishes that a real gtc stop is
    visible the next session.
    """

    def test_read_open_orders_normalizes_the_recorded_shape(self):
        from tests.robinhoodfixture import fixture_broker

        broker = fixture_broker()
        orders = broker.read_open_orders(symbol="AMD")
        self.assertEqual(len(orders), 1)
        order = orders[0]
        self.assertEqual(order.symbol, "AMD")
        self.assertEqual(order.order_type, "limit")
        self.assertEqual(order.time_in_force, "gtc")
        self.assertEqual(order.ref_id, "d2f1c0a4-7f31-4a19-9c62-9a6a1e0f4c11")
        self.assertTrue(order.is_open)  # 'queued' is not a finished state

    def test_read_open_orders_treats_filled_as_not_open(self):
        from tests.robinhoodfixture import fixture_broker

        broker = fixture_broker()
        tdw = [o for o in broker.read_open_orders(symbol="TDW")]
        self.assertEqual(len(tdw), 1)
        self.assertFalse(tdw[0].is_open)  # 'filled' is terminal

    def test_place_stop_refuses_a_fractional_quantity(self):
        from tests.robinhoodfixture import fixture_broker

        broker = fixture_broker()
        with self.assertRaises(Exception):
            broker.place_stop(symbol="AMD", quantity=1.5, stop_price=95.0, ref_id="x")

    def test_place_stop_requires_a_ref_id(self):
        from tests.robinhoodfixture import fixture_broker

        broker = fixture_broker()
        with self.assertRaises(Exception):
            broker.place_stop(symbol="AMD", quantity=2, stop_price=95.0, ref_id="")


if __name__ == "__main__":
    unittest.main()
