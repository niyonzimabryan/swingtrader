"""`portfolio_overview` over real streamable-HTTP MCP — the Phase 1 stop condition.

A uvicorn server on an ephemeral port, the official ``mcp`` client, a bearer
token, and a ledger filled by the real sync. Nothing is faked at the transport
layer, because streamable HTTP is part of what has to work: an ASGI-transport
shortcut would skip the thing the owner's four client configurations depend on.

What this asserts beyond "the call returns": that holdings arrive with their
provenance, that a stale ledger is flagged rather than hidden or raised, that an
option shows up as ``unsupported_instrument_present``, that a null basis stays
null over the wire, and that these tools are read-scoped.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import timedelta

from portfolio.paging import RecordingPager
from portfolio.sync import run_sync
from tests import portfoliofixture as fx
from tests import workspacefixture as ws
from tests.dbfixture import TestDatabase
from utils.timeutils import utcnow_naive


def _payload(result) -> dict:
    text = "".join(
        block.text for block in result.content if getattr(block, "type", "") == "text"
    )
    return json.loads(text)


class WorkspacePortfolioToolTests(unittest.TestCase):
    """One database and one live server for the whole class.

    Every test resyncs its own broker snapshot; `run_sync` reconciles fully
    against whatever the broker reports (Spec L), so replaying it against an
    already-synced ledger from a previous test leaves the same state a single
    fresh sync would — production runs this repeatedly against the same
    ledger for exactly that reason.
    """

    @classmethod
    def setUpClass(cls):
        cls.db = TestDatabase("workspace_portfolio")
        from database.db import init_db

        init_db(cls.db.url)
        cls.app, cls.settings = ws.build_app(cls.db.url)
        cls._live_ctx = ws.running(cls.app)
        cls.live = cls._live_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._live_ctx.__exit__(None, None, None)
        cls.db.cleanup()

    def setUp(self):
        from database.db import get_session, init_db
        from database.models import WorkspaceToken

        init_db(self.db.url)
        with get_session() as session:
            session.query(WorkspaceToken).delete()
        # See the identical comment in test_workspace_mcp.py: a reissued token
        # can carry a rowid an earlier test's token did, and the rate limiter
        # window is keyed by that id.
        self.app.state.workspace_auth.limiter.reset()

        self.read_token = ws.issue_token("claude-code", ["read"])
        self.admin_token = ws.issue_token("admin-only", ["admin"])

        # A ledger filled the way production fills it: the sync, not fixtures
        # inserted behind it. `as_of` is now, so the default run is fresh.
        self.synced_at = utcnow_naive()
        broker = fx.two_account_broker(
            as_of=self.synced_at,
            agentic_holdings=[
                fx.holding("AMD", 6, price=151.2, basis=852.9),
                fx.holding("TDW", 9, price=41.93),
                fx.holding(
                    "AMD",
                    -1,
                    price=215.0,
                    instrument_type="option",
                    notional=-14000.0,
                    detail={"option_type": "put", "strike_price": 140.0},
                ),
            ],
        )
        from database.db import get_session

        with get_session() as session:
            run_sync(session, broker, pager=RecordingPager(), now=self.synced_at)

    def _call(self, name, arguments=None, token=None):
        return asyncio.run(
            ws.call_tool(self.live.base_url, token or self.read_token, name, arguments)
        )

    # --- the stop condition -------------------------------------------------

    def test_portfolio_overview_returns_holdings_with_provenance_over_mcp(self):
        body = _payload(self._call("portfolio_overview"))

        symbols = {row["symbol"] for row in body["holdings"]}
        self.assertEqual(symbols, {"AMD", "TDW"})
        self.assertEqual(len(body["holdings"]), 4, "3 agentic rows + 1 primary row")

        provenance = body["provenance"]
        self.assertIsNotNone(provenance["as_of_utc"])
        self.assertFalse(provenance["stale"])
        self.assertEqual(provenance["data_quality"], "fresh")
        self.assertEqual(provenance["freshness_budget_minutes"], 60)
        self.assertEqual(provenance["sources"]["holdings"], "broker_sync")
        self.assertEqual(provenance["sources"]["exposure"], "derived")

        # Exposure spans both accounts, including the read-only one.
        amd = next(
            row
            for row in body["exposure"]["by_symbol"]
            if row["symbol"] == "AMD" and row["instrument_type"] == "equity"
        )
        self.assertEqual(amd["quantity"], 51.0)
        self.assertEqual(sorted(amd["accounts"]), ["Agentic", "Primary"])

        self.assertEqual(
            {a["label"]: a["agent_placeable"] for a in body["accounts"]},
            {"Agentic": True, "Primary": False},
        )

    def test_the_option_is_reported_not_omitted_over_the_wire(self):
        body = _payload(self._call("portfolio_overview"))
        unsupported = body["exposure"]["unsupported_instruments"]
        self.assertEqual(len(unsupported), 1)
        self.assertEqual(unsupported[0]["instrument_type"], "option")
        self.assertEqual(unsupported[0]["notional"], -14000.0)
        self.assertTrue(
            any("unsupported_instrument_present" in w for w in body["warnings"]),
            body["warnings"],
        )

    def test_a_null_basis_survives_the_wire_as_null(self):
        body = _payload(self._call("portfolio_overview"))
        tdw = next(row for row in body["holdings"] if row["symbol"] == "TDW")
        self.assertIsNone(tdw["cost_basis"])
        self.assertFalse(tdw["cost_basis_known"])

        detail = _payload(self._call("position_detail", {"symbol": "TDW"}))
        self.assertIsNone(detail["unrealized"])
        self.assertFalse(detail["cost_basis_known"])

    def test_a_stale_ledger_is_flagged_over_mcp_rather_than_hidden_or_raised(self):
        """Spec K §4.2: return the stale value **with the flag set**."""
        from database.db import get_session

        old = self.synced_at - timedelta(hours=6)
        with get_session() as session:
            run_sync(
                session,
                fx.two_account_broker(as_of=old),
                pager=RecordingPager(),
                now=old,
                force_mass_deletion=True,
            )

        result = self._call("portfolio_overview")
        self.assertFalse(getattr(result, "isError", False), "a stale ledger must not raise")
        body = _payload(result)
        self.assertTrue(body["provenance"]["stale"])
        self.assertEqual(body["provenance"]["data_quality"], "stale")
        self.assertGreater(body["provenance"]["age_minutes"], 60)
        self.assertTrue(body["holdings"], "the stale data is returned, not withheld")
        self.assertTrue(
            any("cannot trigger a sync" in w for w in body["warnings"]),
            "a stale response should name the sync that would fix it",
        )

    def test_position_detail_and_orders_open_answer_over_mcp(self):
        detail = _payload(self._call("position_detail", {"symbol": "AMD"}))
        self.assertTrue(detail["found"])
        self.assertEqual(detail["total_quantity"], 6 - 1 + 45)
        self.assertTrue(detail["tax_lots"])
        self.assertIn("provenance", detail)

        orders = _payload(self._call("orders_open"))
        self.assertEqual(len(orders["open_orders"]), 1)
        self.assertEqual(orders["open_orders"][0]["origin"], "external")
        self.assertEqual(orders["external_count"], 1)
        self.assertIn("provenance", orders)

    def test_an_unknown_symbol_answers_rather_than_erroring(self):
        detail = _payload(self._call("position_detail", {"symbol": "ZZZZ"}))
        self.assertFalse(detail["found"])
        self.assertEqual(detail["positions"], [])
        self.assertIn("provenance", detail)

    # --- scope and surface --------------------------------------------------

    def test_the_new_tools_are_advertised_and_marked_read_only(self):
        listing = asyncio.run(ws.list_tools(self.live.base_url, self.read_token))
        names = {tool.name for tool in listing.tools}
        self.assertTrue({"portfolio_overview", "position_detail", "orders_open"} <= names)
        for tool in listing.tools:
            if tool.name in {"portfolio_overview", "position_detail", "orders_open"}:
                self.assertIn("read-only", (tool.description or "").lower())

    def test_a_token_without_read_is_refused_on_every_new_tool(self):
        for name, arguments in (
            ("portfolio_overview", None),
            ("position_detail", {"symbol": "AMD"}),
            ("orders_open", None),
        ):
            with self.subTest(tool=name):
                result = self._call(name, arguments, token=self.admin_token)
                self.assertTrue(getattr(result, "isError", False), name)
                text = "".join(
                    block.text
                    for block in result.content
                    if getattr(block, "type", "") == "text"
                )
                self.assertIn("insufficient_scope", text)

    def test_health_reports_the_portfolio_sync_age(self):
        import httpx

        body = httpx.get(f"{self.live.base_url}/health").json()
        self.assertIn("portfolio_sync", body)
        self.assertIsNotNone(body["portfolio_sync"]["last_sync_age_seconds"])
        self.assertFalse(body["portfolio_sync"]["enabled"], "the flag defaults off")
        self.assertNotIn(
            "last_portfolio_sync_age (Spec L, Phase 1)", body["pending_checks"]
        )


if __name__ == "__main__":
    unittest.main()
