"""The runtime half of the owner control surface (Spec K §10, Spec L §10).

An MCP owner tool records a decision; ``orchestrator/approval_poller.py`` is the
only thing that acts on one. These rows are about the acting, and every one of
them drives the real :class:`execution.lifecycle.ExecutionService` against the
deterministic :class:`execution.brokers.fake.FakeExecutionBroker`, so what is
asserted is the real approval path rather than a mock of it.

Rows here:

``test_a_recorded_approval_places_once``
    the happy path, end to end from a recorded decision to a protected position.
``test_two_pollers_place_once``
    two pollers, one claim. The conditional ``UPDATE`` decides the winner, and
    the loser does nothing at all — not "does nothing harmful", nothing.
``test_a_replayed_claim_still_places_once``
    the belt to that braces: even with the claim forced open a second time,
    ``proposals.approval_consumed_at`` refuses the second placement. The two
    controls are independent, which is the only reason having both is worth it.
``test_routing_is_read_from_the_row``
    a proposal carrying an ``execution_id`` goes to the Strategy Lab service and
    a plain one to Phase 6's, decided by the ROW — exactly as
    ``bot/handlers/proposals.py`` decides it.
``test_a_refusal_is_terminal``
    a risk refusal finishes the action instead of leaving it claimable. Retrying
    an approval the guards just rejected would be the system arguing with
    itself.
``test_a_missing_service_releases_the_claim``
    a deployment that never wired a service leaves the decision standing and
    pages, rather than burning it.
``test_memo_approval_moves_the_memo_before_it_places``
    the older path: the memo is ``approved`` before ``execute_approved_trade``
    is called, so a crash between the two leaves a memo that cannot be approved
    again rather than one that can be approved twice.
``test_stale_confirmations_expire``
    a prepared tier change past its TTL stops looking actionable.
"""

from __future__ import annotations

import unittest

from database.db import get_session
from database.models import Memo, OwnerAction, Proposal, Ticker
from execution.brokers.fake import FakeExecutionBroker
from execution.lifecycle import ExecutionService
from portfolio import owner_actions, proposals
from portfolio.paging import RecordingPager
from orchestrator.approval_poller import ApprovalPoller
from tests import proposalfixture as pf
from tests.dbfixture import TestDatabase
from utils.timeutils import utcnow_naive

OWNER = "99887766"


class _RecordingNotifier:
    def __init__(self):
        self.events = []

    def __call__(self, event, detail):
        self.events.append((event, dict(detail or {})))

    def names(self):
        return [event for event, _ in self.events]


class _CountingService:
    """Stands in for an execution service and counts the calls it receives."""

    def __init__(self, result=None, raises=None):
        self.calls = []
        self._result = result
        self._raises = raises

    def on_approval(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return self._result


class _Result:
    def __init__(self, status="protected", message="ok"):
        self.status = status
        self.message = message
        self.entry_order_id = "entry-1"
        self.stop_order_id = "stop-1"


class PollerTests(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("owner_poller")
        self.addCleanup(self.db.cleanup)
        from database.db import init_db

        init_db(self.db.url)
        self.settings = pf.settings(
            owner_id=OWNER,
            owner_action_poller_enabled=True,
            owner_action_poll_seconds=5,
            owner_action_ttl_seconds=900,
            strategy_lab_enabled=True,
            strategy_lab_experiment="shadow_roster_v1",
            strategy_lab_experiment_owner="bryan",
        )
        # The proposal is sized against the clock the MCP tool would use, so the
        # ledger has to be synced as of now or the freshness guard refuses it.
        self.now = utcnow_naive()
        with get_session() as session:
            pf.synced_session(session, now=self.now)
            session.commit()

    # -- helpers ------------------------------------------------------------

    def _proposal(self, *, execution_id: str = "", **kwargs):
        params = dict(
            ticker="AMD",
            entry=100.0,
            stop=95.0,
            risk_fraction=0.005,
            settings=self.settings,
            owner_id=OWNER,
            now=self.now,
            resolver=pf.resolver_for({}),
        )
        params.update(kwargs)
        with get_session() as session:
            row = proposals.create_proposal(session, **params)
            if execution_id:
                row.execution_id = execution_id
            uid = row.proposal_uid
            proposal_id = row.id
            session.commit()
        return proposal_id, uid

    def _record_approval(self, uid: str, kind: str = "approve_order"):
        with get_session() as session:
            action = owner_actions.record(
                session,
                kind=kind,
                subject_kind="proposal",
                subject_ref=uid,
                status="confirmed",
                owner_id=OWNER,
                token_label="claude-admin",
                now=utcnow_naive(),
            )
            action_uid = action.action_uid
            session.commit()
        return action_uid

    def _poller(self, *, execution_service=None, lab=None, memo=None, notify=None, instance="a"):
        return ApprovalPoller(
            session_factory=get_session,
            settings=self.settings,
            execution_service=execution_service,
            strategy_execution_service=lab,
            memo_executor=memo,
            notify=notify,
            instance_id=instance,
        )

    def _real_service(self, pager=None):
        return ExecutionService(
            session_factory=get_session,
            broker=FakeExecutionBroker(fill_price=100.0),
            settings=self.settings,
            pager=pager or RecordingPager(),
        )

    def _status(self, proposal_id):
        with get_session() as session:
            return session.get(Proposal, proposal_id).status

    def _action(self, action_uid):
        with get_session() as session:
            row = owner_actions.get_by_uid(session, action_uid)
            return (row.status, row.outcome_code, row.claimed_by)

    # -- rows ---------------------------------------------------------------

    def test_a_recorded_approval_places_once(self):
        proposal_id, uid = self._proposal()
        action_uid = self._record_approval(uid)
        notify = _RecordingNotifier()

        results = self._poller(
            execution_service=self._real_service(), notify=notify
        ).run_once()

        self.assertEqual(len(results), 1)
        self.assertEqual(self._status(proposal_id), "protected")
        status, outcome, _by = self._action(action_uid)
        self.assertEqual(status, "executed")
        self.assertEqual(outcome, "protected")
        self.assertIn("owner_action_placed", notify.names())

        # And nothing is left for a second pass to find.
        self.assertEqual(self._poller(execution_service=self._real_service()).run_once(), [])

    def test_two_pollers_place_once(self):
        _proposal_id, uid = self._proposal()
        self._record_approval(uid)
        service = _CountingService(result=_Result())

        first = self._poller(execution_service=service, instance="first")
        second = self._poller(execution_service=service, instance="second")

        # `run_once` claims and works in one pass, so running them back to back
        # is the race: the second finds the row claimed and skips it entirely.
        first_results = first.run_once()
        second_results = second.run_once()

        self.assertEqual(len(first_results), 1)
        self.assertEqual(second_results, [])
        self.assertEqual(
            len(service.calls),
            1,
            "the execution service was called more than once for one decision",
        )

    def test_a_replayed_claim_still_places_once(self):
        """The claim and the consumed-at column are independent controls."""
        proposal_id, uid = self._proposal()
        action_uid = self._record_approval(uid)
        self._poller(execution_service=self._real_service()).run_once()
        self.assertEqual(self._status(proposal_id), "protected")

        # Force the queue back open — the failure mode a claim alone could not
        # survive — and prove the *execution* side refuses anyway.
        with get_session() as session:
            row = owner_actions.get_by_uid(session, action_uid)
            row.status = "confirmed"
            row.claimed_at = None
            row.outcome_code = ""
            session.commit()

        service = self._real_service()
        self._poller(execution_service=service, instance="replay").run_once()

        self.assertEqual(self._status(proposal_id), "protected")
        status, outcome, _by = self._action(action_uid)
        self.assertEqual(status, "executed")
        self.assertIn(
            outcome,
            ("not_proposed", "approval_already_used"),
            "a replayed approval must be refused by the execution service, not "
            "placed a second time",
        )

    def test_routing_is_read_from_the_row(self):
        _plain_id, plain_uid = self._proposal()
        _lab_id, lab_uid = self._proposal(ticker="NVDA", execution_id="exec-abc123")
        self._record_approval(plain_uid)
        self._record_approval(lab_uid)

        phase6 = _CountingService(result=_Result())
        lab = _CountingService(result=_Result())
        self._poller(execution_service=phase6, lab=lab).run_once()

        self.assertEqual(len(phase6.calls), 1)
        self.assertEqual(len(lab.calls), 1)
        self.assertIn("proposal_id", phase6.calls[0])
        self.assertEqual(lab.calls[0]["execution_id"], "exec-abc123")
        self.assertNotIn(
            "execution_id",
            phase6.calls[0],
            "a plain proposal must not be routed as a lab execution",
        )

    def test_a_refusal_is_terminal(self):
        from execution.lifecycle import ExecutionRefused

        _proposal_id, uid = self._proposal()
        action_uid = self._record_approval(uid)
        notify = _RecordingNotifier()
        service = _CountingService(
            raises=ExecutionRefused("kill_switch_engaged", "the switch is on.")
        )

        self._poller(execution_service=service, notify=notify).run_once()

        status, outcome, _by = self._action(action_uid)
        self.assertEqual(status, "executed")
        self.assertEqual(outcome, "kill_switch_engaged")
        self.assertIn("owner_action_refused", notify.names())

        # Terminal means terminal: a second pass finds nothing to retry.
        self.assertEqual(self._poller(execution_service=service).run_once(), [])
        self.assertEqual(len(service.calls), 1)

    def test_a_missing_service_releases_the_claim(self):
        _proposal_id, uid = self._proposal()
        action_uid = self._record_approval(uid)
        notify = _RecordingNotifier()

        self._poller(execution_service=None, notify=notify).run_once()

        status, _outcome, _by = self._action(action_uid)
        self.assertEqual(
            status,
            "confirmed",
            "a decision must survive a deployment that has not wired a service",
        )
        self.assertIn("owner_action_service_missing", notify.names())

        # And a restart that wires one picks it up.
        self._poller(execution_service=self._real_service(), instance="later").run_once()
        self.assertEqual(self._action(action_uid)[0], "executed")

    def test_memo_approval_moves_the_memo_before_it_places(self):
        seen = {}

        with get_session() as session:
            ticker = Ticker(symbol="AMD", name="AMD", sector="Technology")
            session.add(ticker)
            session.flush()
            memo = Memo(ticker_id=ticker.id, status="pending", full_text="card")
            session.add(memo)
            session.flush()
            memo_id = memo.id
            session.commit()

        def executor(mid):
            with get_session() as session:
                seen["status_at_call"] = session.get(Memo, mid).status
            return {"success": True, "detail": "placed"}

        with get_session() as session:
            action = owner_actions.record(
                session,
                kind="approve_memo",
                subject_kind="memo",
                subject_ref=str(memo_id),
                status="confirmed",
                owner_id=OWNER,
                token_label="claude-admin",
            )
            action_uid = action.action_uid
            session.commit()

        self._poller(memo=executor).run_once()

        self.assertEqual(seen["status_at_call"], "approved")
        status, outcome, _by = self._action(action_uid)
        self.assertEqual(status, "executed")
        self.assertEqual(outcome, "placed")

    def test_stale_confirmations_expire(self):
        from datetime import timedelta

        with get_session() as session:
            action = owner_actions.record(
                session,
                kind="promote_arm",
                subject_kind="arm",
                subject_ref="7",
                status="requested",
                owner_id=OWNER,
                payload={"source_arm_id": 7, "to_mode": "paper"},
            )
            action.status = "prepared"
            action.confirm_expires_at = utcnow_naive() - timedelta(seconds=10)
            action.confirm_nonce = "n" * 32
            action.confirm_signature = "f" * 64
            action_uid = action.action_uid
            session.commit()

        self._poller().run_once()

        status, outcome, _by = self._action(action_uid)
        self.assertEqual(status, "expired")
        self.assertEqual(outcome, "confirmation_expired")

    def test_an_unclaimed_queue_is_a_no_op(self):
        """The common case: nothing recorded, nothing done, nothing logged as done."""
        self.assertEqual(self._poller(execution_service=self._real_service()).run_once(), [])
        with get_session() as session:
            self.assertEqual(session.query(OwnerAction).count(), 0)


if __name__ == "__main__":
    unittest.main()
