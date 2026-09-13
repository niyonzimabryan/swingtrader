"""The owner control surface over the real MCP round-trip (Spec K §10).

The transport is real — a uvicorn server on an ephemeral port, the official
``mcp`` client, a bearer token — for the same reason ``test_propose_order_mcp``
does it that way: a scope check that only ran under an ASGI shortcut is not the
one the deployed server runs.

Rows here:

* the ten tools appear only when ``WORKSPACE_OWNER_TOOLS_ENABLED`` is on, and
  the flag defaults off;
* a ``read`` token is refused on every ``admin`` tool and allowed on
  ``proposals_pending``;
* ``approve_order`` records a decision and **places nothing** — the proposal is
  still ``proposed``, its approval still unconsumed, and no broker was touched;
* an expired card, an already-consumed one, and a second decision on the same
  proposal are each refused, with the code that says which;
* ``reject_order`` is applied immediately and consumes the approval, so the
  proposal can never be approved afterwards;
* the kill switch engages on a ``read`` token and refuses to **release** on one;
* ``pause_experiment`` and ``resume_experiment`` move a real experiment;
* ``promote_arm`` prepares rather than promotes, and confirming an unprepared
  reference is refused.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import timedelta

from tests import proposalfixture as pf
from tests import workspacefixture as ws
from tests.dbfixture import TestDatabase
from utils.timeutils import utcnow_naive

OWNER = "99887766"

#: Every tool that must refuse a token without `admin`.
ADMIN_TOOLS = (
    "approve_order",
    "reject_order",
    "approve_memo",
    "reject_memo",
    "pause_experiment",
    "resume_experiment",
    "promote_arm",
    "demote_arm",
)


def _text(result) -> str:
    return "".join(
        block.text for block in result.content if getattr(block, "type", "") == "text"
    )


class OwnerToolsMcpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = TestDatabase("owner_tools_mcp")
        from database.db import init_db

        init_db(cls.db.url)
        cls.app, cls.settings = ws.build_app(
            cls.db.url,
            phase6_execution_enabled=True,
            workspace_owner_tools_enabled=True,
            execution_approval_secret="test-approval-secret",
            telegram_chat_id=OWNER,
            owner_id=OWNER,
            strategy_lab_enabled=True,
            strategy_lab_experiment="shadow_roster_v1",
        )
        cls._live_ctx = ws.running(cls.app)
        cls.live = cls._live_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._live_ctx.__exit__(None, None, None)
        cls.db.cleanup()

    def setUp(self):
        from database.db import get_session, init_db
        from database.models import (
            ExecutionKillSwitch,
            OwnerAction,
            Proposal,
            WorkspaceToken,
        )

        init_db(self.db.url)
        with get_session() as session:
            session.query(OwnerAction).delete()
            session.query(Proposal).delete()
            session.query(WorkspaceToken).delete()
            # The switch is a single persistent row and this class engages it on
            # purpose in one test. Leaving it engaged would risk_reject every
            # proposal a later test creates, which is the switch working — and
            # an alphabetically-ordered cross-test dependency all the same.
            session.query(ExecutionKillSwitch).delete()
        # A reissued token can carry a rowid an earlier test's token did, and
        # the rate limiter's window is keyed by that id.
        self.app.state.workspace_auth.limiter.reset()

        self.read_token = ws.issue_token("claude-read", ["read"])
        self.admin_token = ws.issue_token("bryan-admin", ["read", "admin"])

        self.now = utcnow_naive()
        with get_session() as session:
            pf.synced_session(session, now=self.now)
            session.commit()

    # -- helpers ------------------------------------------------------------

    def _call(self, token, name, arguments=None):
        return asyncio.run(ws.call_tool(self.live.base_url, token, name, arguments or {}))

    def _payload(self, result):
        self.assertFalse(result.isError, _text(result))
        return json.loads(_text(result))

    def _proposal(self, **kwargs):
        from database.db import get_session
        from portfolio import proposals

        params = dict(
            ticker="AMD",
            entry=100.0,
            stop=95.0,
            risk_fraction=0.005,
            settings=pf.settings(owner_id=OWNER, telegram_chat_id=OWNER),
            owner_id=OWNER,
            now=self.now,
            resolver=pf.resolver_for({}),
        )
        params.update(kwargs)
        with get_session() as session:
            row = proposals.create_proposal(session, **params)
            uid, proposal_id = row.proposal_uid, row.id
            session.commit()
        return proposal_id, uid

    def _row(self, proposal_id) -> dict:
        """The fields under test, read inside the session that loaded them."""
        from database.db import get_session
        from database.models import Proposal

        with get_session() as session:
            row = session.get(Proposal, proposal_id)
            return {
                "status": row.status,
                "approval_consumed_at": row.approval_consumed_at,
                "approval_signature": row.approval_signature,
                "approval_owner_id": row.approval_owner_id,
            }

    # -- registration and scopes -------------------------------------------

    def test_the_ten_tools_are_listed(self):
        from workspace.owner_tools import OWNER_TOOLS

        listing = asyncio.run(ws.list_tools(self.live.base_url, self.admin_token))
        names = {t.name for t in listing.tools}
        self.assertEqual(set(OWNER_TOOLS) - names, set())

    def test_every_owner_tool_declares_a_scope(self):
        from workspace import scopes
        from workspace.owner_tools import OWNER_TOOLS

        for name in OWNER_TOOLS:
            self.assertIn(name, scopes.TOOL_SCOPES, f"{name} has no declared scope")

    def test_a_read_token_is_refused_on_every_admin_tool(self):
        for name in ADMIN_TOOLS:
            with self.subTest(tool=name):
                result = self._call(self.read_token, name, {})
                self.assertTrue(result.isError, f"{name} accepted a read token")
                self.assertIn("insufficient_scope", _text(result))

    def test_a_read_token_may_list_what_is_pending(self):
        self._proposal()
        payload = self._payload(self._call(self.read_token, "proposals_pending"))
        self.assertEqual(len(payload["proposals"]), 1)
        self.assertTrue(payload["proposals"][0]["card_md"])
        self.assertIn("provenance", payload)
        self.assertFalse(payload["proposals"][0]["placed"])

    # -- approve ------------------------------------------------------------

    def test_approve_records_and_places_nothing(self):
        proposal_id, uid = self._proposal()
        payload = self._payload(
            self._call(self.admin_token, "approve_order", {"proposal_uid": uid})
        )
        self.assertEqual(payload["decision"], "approve")
        self.assertFalse(payload["applied"])
        self.assertFalse(payload["placed"])
        self.assertEqual(payload["action"]["status"], "confirmed")
        self.assertEqual(payload["action"]["requested_token_label"], "bryan-admin")

        row = self._row(proposal_id)
        self.assertEqual(
            row["status"],
            "proposed",
            "recording an approval must not move the proposal's execution state",
        )
        self.assertIsNone(
            row["approval_consumed_at"],
            "recording an approval must not consume the single-use reference; "
            "only on_approval may, inside the transaction that places",
        )

    def test_a_second_decision_on_one_proposal_is_refused(self):
        _proposal_id, uid = self._proposal()
        self._payload(self._call(self.admin_token, "approve_order", {"proposal_uid": uid}))
        again = self._call(self.admin_token, "approve_order", {"proposal_uid": uid})
        self.assertTrue(again.isError, _text(again))
        self.assertIn("already_decided", _text(again))

        # And the other direction is refused too: one decision, not one of each.
        reject = self._call(self.admin_token, "reject_order", {"proposal_uid": uid})
        self.assertTrue(reject.isError, _text(reject))
        self.assertIn("already_decided", _text(reject))

    def test_an_expired_card_is_refused(self):
        from database.db import get_session
        from database.models import Proposal

        from portfolio import approvals

        proposal_id, uid = self._proposal()
        # Re-sign as well as re-date. The expiry is one of the four fields the
        # signature binds, so moving it alone would fail `signature_mismatch`
        # first and this row would assert nothing about expiry.
        with get_session() as session:
            row = session.get(Proposal, proposal_id)
            row.approval_expires_at = (utcnow_naive() - timedelta(seconds=5)).replace(
                microsecond=0
            )
            row.approval_signature = approvals.sign(
                proposal_uid=row.proposal_uid,
                nonce=row.approval_nonce,
                expires_at=row.approval_expires_at,
                owner_id=row.approval_owner_id,
                secret="test-approval-secret",
            )
            session.commit()
        result = self._call(self.admin_token, "approve_order", {"proposal_uid": uid})
        self.assertTrue(result.isError, _text(result))
        self.assertIn("approval_expired", _text(result))

    def test_an_already_used_approval_is_refused(self):
        from database.db import get_session
        from database.models import Proposal

        proposal_id, uid = self._proposal()
        with get_session() as session:
            session.get(Proposal, proposal_id).approval_consumed_at = utcnow_naive()
            session.commit()
        result = self._call(self.admin_token, "approve_order", {"proposal_uid": uid})
        self.assertTrue(result.isError, _text(result))
        self.assertIn("approval_already_used", _text(result))

    def test_a_wrong_owner_card_is_refused(self):
        """A card minted for somebody else does not become approvable here."""
        from database.db import get_session
        from database.models import Proposal

        proposal_id, uid = self._proposal()
        with get_session() as session:
            session.get(Proposal, proposal_id).approval_owner_id = "someone-else"
            session.commit()
        result = self._call(self.admin_token, "approve_order", {"proposal_uid": uid})
        self.assertTrue(result.isError, _text(result))
        self.assertIn("owner_mismatch", _text(result))

    def test_an_unknown_proposal_is_refused(self):
        result = self._call(
            self.admin_token, "approve_order", {"proposal_uid": "not-a-uid"}
        )
        self.assertTrue(result.isError, _text(result))
        self.assertIn("unknown_proposal", _text(result))

    # -- reject -------------------------------------------------------------

    def test_reject_is_applied_immediately_and_burns_the_approval(self):
        proposal_id, uid = self._proposal()
        payload = self._payload(
            self._call(
                self.admin_token,
                "reject_order",
                {"proposal_uid": uid, "reason": "prefer the cohort's other name"},
            )
        )
        self.assertTrue(payload["applied"])
        row = self._row(proposal_id)
        self.assertEqual(row["status"], "rejected")
        self.assertIsNotNone(row["approval_consumed_at"])
        self.assertEqual(payload["action"]["reason"], "prefer the cohort's other name")

    def test_a_risk_rejected_proposal_cannot_be_decided(self):
        _proposal_id, uid = self._proposal(risk_fraction=0.5)
        result = self._call(self.admin_token, "approve_order", {"proposal_uid": uid})
        self.assertTrue(result.isError, _text(result))
        self.assertIn("not_approvable", _text(result))

    # -- kill switch --------------------------------------------------------

    def test_engaging_the_kill_switch_needs_only_read(self):
        payload = self._payload(
            self._call(self.read_token, "kill_switch", {"state": "on", "reason": "drill"})
        )
        self.assertTrue(payload["switch"]["engaged"])
        self.assertTrue(payload["changed"])

        state = self._payload(self._call(self.read_token, "kill_switch", {"state": "status"}))
        self.assertTrue(state["switch"]["engaged"])
        self.assertEqual(state["entry_block"]["code"], "kill_switch_engaged")

    def test_releasing_the_kill_switch_needs_admin(self):
        self._payload(self._call(self.read_token, "kill_switch", {"state": "on"}))
        refused = self._call(self.read_token, "kill_switch", {"state": "off"})
        self.assertTrue(refused.isError, _text(refused))
        self.assertIn("insufficient_scope", _text(refused))

        allowed = self._payload(self._call(self.admin_token, "kill_switch", {"state": "off"}))
        self.assertFalse(allowed["switch"]["engaged"])

    def test_an_unknown_state_is_refused(self):
        result = self._call(self.admin_token, "kill_switch", {"state": "maybe"})
        self.assertTrue(result.isError, _text(result))
        self.assertIn("invalid_argument", _text(result))

    # -- Strategy Lab -------------------------------------------------------

    def test_pause_and_resume_move_a_real_experiment(self):
        from database.db import get_session
        from strategy_lab import registry
        from tests.test_strategy_lab_domain import an_experiment_spec

        spec = an_experiment_spec()
        with get_session() as session:
            registry.register_experiment(session, spec)
            session.commit()

        paused = self._payload(
            self._call(self.admin_token, "pause_experiment", {"name": spec.name})
        )
        self.assertTrue(paused["ok"])
        self.assertEqual(paused["status"], "paused")
        with get_session() as session:
            row = registry.experiment_by_name_or_id(session, spec.name)
            self.assertEqual(row.status, "paused")

        resumed = self._payload(
            self._call(self.admin_token, "resume_experiment", {"name": spec.name})
        )
        self.assertTrue(resumed["ok"])
        self.assertEqual(resumed["status"], "running")

    def test_an_unknown_experiment_is_refused(self):
        result = self._call(self.admin_token, "pause_experiment", {"name": "no-such"})
        self.assertTrue(result.isError, _text(result))
        self.assertIn("refused", _text(result))

    def test_promote_prepares_rather_than_promotes(self):
        from database.db import get_session
        from database.models import OwnerAction, PromotionEvent

        payload = self._payload(
            self._call(
                self.admin_token,
                "promote_arm",
                {"source_arm_id": 42, "to_tier": "paper", "reason": "evidence is in"},
            )
        )
        self.assertEqual(payload["action"]["status"], "requested")
        self.assertFalse(payload["confirmed"])
        self.assertFalse(payload["placed"])
        with get_session() as session:
            self.assertEqual(session.query(OwnerAction).count(), 1)
            self.assertEqual(
                session.query(PromotionEvent).count(),
                0,
                "asking for a tier change must promote nothing by itself",
            )

    def test_confirming_an_unprepared_reference_is_refused(self):
        payload = self._payload(
            self._call(
                self.admin_token,
                "promote_arm",
                {"source_arm_id": 42, "to_tier": "paper"},
            )
        )
        uid = payload["action"]["action_uid"]
        result = self._call(
            self.admin_token,
            "promote_arm",
            {"confirmation_reference": f"{uid}:" + "f" * 18},
        )
        self.assertTrue(result.isError, _text(result))
        self.assertIn("not_prepared", _text(result))

    def test_a_tier_change_for_another_arm_while_one_is_open_is_refused(self):
        self._payload(
            self._call(
                self.admin_token,
                "promote_arm",
                {"source_arm_id": 42, "to_tier": "paper"},
            )
        )
        result = self._call(
            self.admin_token, "promote_arm", {"source_arm_id": 42, "to_tier": "live"}
        )
        self.assertTrue(result.isError, _text(result))
        self.assertIn("already_pending", _text(result))

    def test_re_asking_after_a_refusal_replays_it_rather_than_queueing_again(self):
        """Otherwise an agent that was refused asks forever and never sees why.

        A refusal is terminal, so the open-action check steps over it. Without
        this, every re-ask would queue a fresh request and the agent would keep
        being told "recorded; the runtime is planning it".
        """
        from database.db import get_session
        from database.models import OwnerAction
        from portfolio import owner_actions

        first = self._payload(
            self._call(
                self.admin_token,
                "promote_arm",
                {"source_arm_id": 42, "to_tier": "paper"},
            )
        )
        # Stand in for the runtime's preparing pass deciding the plan is not
        # confirmable — that decision is the poller's and is tested there.
        with get_session() as session:
            row = owner_actions.get_by_uid(session, first["action"]["action_uid"])
            row.card_md = "TIER CHANGE — NOT CONFIRMABLE\nrefusals:\n  - evidence incomplete"
            owner_actions.finish(
                session,
                row,
                status="refused",
                outcome_code="not_confirmable",
                outcome_detail="evidence incomplete",
            )
            session.commit()

        again = self._payload(
            self._call(
                self.admin_token,
                "promote_arm",
                {"source_arm_id": 42, "to_tier": "paper"},
            )
        )
        self.assertEqual(again["action"]["status"], "refused")
        self.assertEqual(
            again["action"]["action_uid"], first["action"]["action_uid"]
        )
        self.assertIn("evidence incomplete", again["action"]["card_md"])
        with get_session() as session:
            self.assertEqual(
                session.query(OwnerAction).count(),
                1,
                "re-asking after a refusal must not queue a second request",
            )

    def test_an_unknown_tier_is_refused(self):
        result = self._call(
            self.admin_token, "promote_arm", {"source_arm_id": 42, "to_tier": "moon"}
        )
        self.assertTrue(result.isError, _text(result))
        self.assertIn("invalid_argument", _text(result))


class OwnerToolsFlagOffTests(unittest.TestCase):
    """With the flag off the tools are not registered at all."""

    def setUp(self):
        self.db = TestDatabase("owner_tools_off")
        self.addCleanup(self.db.cleanup)
        from database.db import init_db

        init_db(self.db.url)
        self.admin_token = ws.issue_token("bryan-admin", ["read", "admin"])
        app, _ = ws.build_app(self.db.url, phase6_execution_enabled=True)
        self._live = ws.running(app)
        self.live = self._live.__enter__()
        self.addCleanup(lambda: self._live.__exit__(None, None, None))

    def test_the_flag_defaults_off(self):
        from config.settings import Settings

        self.assertFalse(
            Settings.model_fields["workspace_owner_tools_enabled"].default
        )
        self.assertFalse(Settings.model_fields["owner_action_poller_enabled"].default)

    def test_no_owner_tool_is_advertised(self):
        from workspace.owner_tools import OWNER_TOOLS

        listing = asyncio.run(ws.list_tools(self.live.base_url, self.admin_token))
        names = {t.name for t in listing.tools}
        self.assertEqual(names & set(OWNER_TOOLS), set())

    def test_calling_one_errors(self):
        result = asyncio.run(
            ws.call_tool(
                self.live.base_url, self.admin_token, "approve_order", {"proposal_uid": "x"}
            )
        )
        self.assertTrue(result.isError)


class OwnerIdentityTests(unittest.TestCase):
    """`OWNER_ID` names the owner once, and defaults to the Telegram chat id."""

    def test_owner_id_defaults_to_the_telegram_chat_id(self):
        from types import SimpleNamespace

        from portfolio.approvals import resolve_owner_id

        self.assertEqual(
            resolve_owner_id(SimpleNamespace(owner_id="", telegram_chat_id="123")), "123"
        )

    def test_owner_id_wins_when_it_is_set(self):
        from types import SimpleNamespace

        from portfolio.approvals import resolve_owner_id

        self.assertEqual(
            resolve_owner_id(SimpleNamespace(owner_id="bryan", telegram_chat_id="123")),
            "bryan",
        )

    def test_it_tolerates_a_settings_object_with_neither(self):
        from types import SimpleNamespace

        from portfolio.approvals import resolve_owner_id

        self.assertEqual(resolve_owner_id(SimpleNamespace()), "")


if __name__ == "__main__":
    unittest.main()
