"""``propose_order`` over the MCP round-trip, with scope enforcement (Spec K §4.2).

The transport is real: a uvicorn server on an ephemeral port, the official
``mcp`` client, a bearer token. Nothing is faked at the transport layer, which
is the point — a scope check that only ran under an ASGI shortcut would not be
the one the deployed server runs.

Asserted here:

* the tool appears in ``tools/list`` only when ``PHASE6_EXECUTION_ENABLED`` is on;
* a ``read`` token is refused, a ``propose`` token is allowed (Spec K §4.1);
* the call writes a proposal row and returns it with ``placed: false``;
* a ``quantity`` argument is refused rather than honoured.
"""

from __future__ import annotations

import asyncio
import json
import unittest

from tests import proposalfixture as pf
from tests import workspacefixture as ws
from tests.dbfixture import TestDatabase


def _text(result) -> str:
    return "".join(
        block.text for block in result.content if getattr(block, "type", "") == "text"
    )


class ProposeOrderMcpTests(unittest.TestCase):
    """One database and one live server for the whole class.

    Every test resyncs a fresh cash Agentic ledger and reissues its own
    tokens; `run_sync` reconciles a broker snapshot idempotently, so replaying
    it against a shared, already-synced ledger is exactly what production does
    on every real sync, and `_reset_proposals` clears the one table only this
    class's own tool call writes.
    """

    @classmethod
    def setUpClass(cls):
        cls.db = TestDatabase("propose_mcp")
        from database.db import init_db

        init_db(cls.db.url)
        cls.app, cls.settings = ws.build_app(
            cls.db.url,
            phase6_execution_enabled=True,
            execution_approval_secret="test-approval-secret",
            telegram_chat_id="99887766",
        )
        cls._live_ctx = ws.running(cls.app)
        cls.live = cls._live_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._live_ctx.__exit__(None, None, None)
        cls.db.cleanup()

    def setUp(self):
        from database.db import get_session, init_db
        from database.models import Proposal, WorkspaceToken
        from utils.timeutils import utcnow_naive

        init_db(self.db.url)
        with get_session() as session:
            session.query(Proposal).delete()
            session.query(WorkspaceToken).delete()
        # See the identical comment in test_workspace_mcp.py: a reissued token
        # can carry a rowid an earlier test's token did, and the rate limiter
        # window is keyed by that id.
        self.app.state.workspace_auth.limiter.reset()

        self.read_token = ws.issue_token("claude-read", ["read"])
        self.propose_token = ws.issue_token("claude-propose", ["propose"])

        # A fresh cash Agentic ledger so the proposal has a book to size
        # against. The MCP tool sizes against the real clock (it takes no
        # injected `now`), so the ledger must be synced as of *now* or the
        # freshness guard would refuse it as stale.
        with get_session() as session:
            pf.synced_session(session, now=utcnow_naive())
            session.commit()

    def _call(self, token, arguments):
        return asyncio.run(ws.call_tool(self.live.base_url, token, "propose_order", arguments))

    def test_tool_is_listed_when_the_flag_is_on(self):
        listing = asyncio.run(ws.list_tools(self.live.base_url, self.propose_token))
        self.assertIn("propose_order", [t.name for t in listing.tools])

    def test_read_token_is_refused(self):
        result = self._call(
            self.read_token,
            {"ticker": "AMD", "entry": 100.0, "stop": 95.0, "risk_fraction": 0.005},
        )
        self.assertTrue(result.isError, _text(result))
        self.assertIn("insufficient_scope", _text(result))

    def test_propose_token_creates_a_row_and_places_nothing(self):
        result = self._call(
            self.propose_token,
            {"ticker": "AMD", "entry": 100.0, "stop": 95.0, "risk_fraction": 0.005},
        )
        self.assertFalse(result.isError, _text(result))
        payload = json.loads(_text(result))
        self.assertEqual(payload["status"], "proposed")
        self.assertFalse(payload["placed"])
        self.assertGreater(payload["quantity"], 0)

        # The row exists in the database.
        from database.db import get_session
        from database.models import Proposal

        with get_session() as session:
            row = session.get(Proposal, payload["proposal_id"])
            self.assertIsNotNone(row)
            self.assertEqual(row.requester_token_label, "claude-propose")

    def test_quantity_argument_is_refused(self):
        result = self._call(
            self.propose_token,
            {"ticker": "AMD", "entry": 100.0, "stop": 95.0, "risk_fraction": 0.005, "quantity": 10},
        )
        self.assertTrue(result.isError, _text(result))
        self.assertIn("quantity_not_accepted", _text(result))

    def test_risk_rejected_proposal_is_returned_not_hidden(self):
        result = self._call(
            self.propose_token,
            {"ticker": "AMD", "entry": 100.0, "stop": 95.0, "risk_fraction": 0.5},
        )
        self.assertFalse(result.isError, _text(result))
        payload = json.loads(_text(result))
        self.assertEqual(payload["status"], "risk_rejected")
        self.assertTrue(payload["rejection_reason"])


class ProposeOrderFlagOffTests(unittest.TestCase):
    """With PHASE6_EXECUTION_ENABLED off, the tool is not registered at all."""

    def setUp(self):
        self.db = TestDatabase("propose_off")
        self.addCleanup(self.db.cleanup)
        from database.db import init_db

        init_db(self.db.url)
        self.propose_token = ws.issue_token("claude-propose", ["propose"])
        app, _ = ws.build_app(self.db.url)  # phase6 defaults off
        self._live = ws.running(app)
        self.live = self._live.__enter__()
        self.addCleanup(lambda: self._live.__exit__(None, None, None))

    def test_the_flag_defaults_off(self):
        from config.settings import Settings

        self.assertFalse(Settings.model_fields["phase6_execution_enabled"].default)

    def test_the_tool_is_absent_from_tools_list(self):
        listing = asyncio.run(ws.list_tools(self.live.base_url, self.propose_token))
        self.assertNotIn("propose_order", [t.name for t in listing.tools])

    def test_calling_it_errors(self):
        result = asyncio.run(
            ws.call_tool(
                self.live.base_url,
                self.propose_token,
                "propose_order",
                {"ticker": "AMD", "entry": 100.0, "stop": 95.0, "risk_fraction": 0.005},
            )
        )
        self.assertTrue(result.isError)


if __name__ == "__main__":
    unittest.main()
