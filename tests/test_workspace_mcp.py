"""The workspace answers /health and `whoami` over streamable-HTTP MCP.

The Phase 0b stop condition, end to end and on whichever engine the run targets:
a real uvicorn server, the official ``mcp`` client, a bearer token, a tool call.
Nothing here is faked at the transport layer.
"""

from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from tests import workspacefixture as ws
from tests.dbfixture import TestDatabase
from workspace import scopes as scope_module
from workspace.tools import REGISTERED_TOOLS


def _text(result) -> str:
    return "".join(
        block.text for block in result.content if getattr(block, "type", "") == "text"
    )


class WorkspaceServiceTests(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("workspace")
        self.addCleanup(self.db.cleanup)

        from database.db import init_db

        init_db(self.db.url)
        self.read_token = ws.issue_token("claude-code", ["read"])
        self.admin_token = ws.issue_token("admin-only", ["admin"])
        self.revoked_token = ws.issue_token("retired-laptop", ["read"])

        from database.db import get_session
        from workspace import tokens

        with get_session() as session:
            tokens.revoke(session, "retired-laptop")

        app, self.settings = ws.build_app(self.db.url)
        self._live = ws.running(app)
        self.live = self._live.__enter__()
        self.addCleanup(lambda: self._live.__exit__(None, None, None))

    # --- /health -----------------------------------------------------------

    def test_health_is_unauthenticated_and_reports_the_migration_revision(self):
        response = httpx.get(f"{self.live.base_url}/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["database"]["reachable"])
        self.assertEqual(body["database"]["backend"], self.db.backend)
        self.assertIsNotNone(body["database"]["migration_revision"])
        self.assertEqual(body["mcp"]["tools"], list(REGISTERED_TOOLS))

    # --- MCP ---------------------------------------------------------------

    def test_mcp_lists_exactly_the_registered_tools(self):
        listing = asyncio.run(ws.list_tools(self.live.base_url, self.read_token))
        self.assertEqual(
            sorted(tool.name for tool in listing.tools), sorted(REGISTERED_TOOLS)
        )

    def test_whoami_over_streamable_http_returns_the_token_identity(self):
        result = asyncio.run(
            ws.call_tool(self.live.base_url, self.read_token, "whoami")
        )
        self.assertFalse(result.isError, _text(result))
        payload = json.loads(_text(result))
        self.assertEqual(payload["token_label"], "claude-code")
        self.assertEqual(payload["scopes"], ["read"])
        self.assertFalse(payload["execute_scope_exists"])

    def test_mcp_without_a_token_is_refused(self):
        response = httpx.post(
            f"{self.live.base_url}/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "missing_token")
        self.assertIn("WWW-Authenticate", response.headers)

    def test_a_revoked_token_is_refused(self):
        response = httpx.get(
            f"{self.live.base_url}/v1/whoami",
            headers={"Authorization": f"Bearer {self.revoked_token}"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "invalid_token")

    def test_every_registered_tool_authorises_before_it_answers(self):
        """A tool that forgets ``authorize_call`` fails here.

        The admin token carries no scope that any registered tool requires, so
        every tool must refuse it. A tool that skipped authorisation would
        answer instead.
        """
        for name in REGISTERED_TOOLS:
            with self.subTest(tool=name):
                self.assertNotEqual(
                    scope_module.TOOL_SCOPES[name],
                    scope_module.ADMIN,
                    "this test needs a scope the admin token does not carry",
                )
                result = asyncio.run(
                    ws.call_tool(self.live.base_url, self.admin_token, name)
                )
                self.assertTrue(result.isError, f"{name} answered an unscoped token")
                self.assertIn("insufficient_scope", _text(result))

    # --- REST --------------------------------------------------------------

    def test_v1_whoami_matches_the_mcp_tool(self):
        response = httpx.get(
            f"{self.live.base_url}/v1/whoami",
            headers={"Authorization": f"Bearer {self.read_token}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["token_label"], "claude-code")

    def test_v1_without_a_token_is_refused(self):
        response = httpx.get(f"{self.live.base_url}/v1/whoami")
        self.assertEqual(response.status_code, 401)


class WorkspaceFlagOffTests(unittest.TestCase):
    """WORKSPACE_API_ENABLED defaults false, and false means false."""

    def setUp(self):
        self.db = TestDatabase("workspace_off")
        self.addCleanup(self.db.cleanup)
        from database.db import init_db

        init_db(self.db.url)
        self.token = ws.issue_token("codex-laptop", ["read"])

        app, _ = ws.build_app(self.db.url, workspace_api_enabled=False)
        self._live = ws.running(app)
        self.live = self._live.__enter__()
        self.addCleanup(lambda: self._live.__exit__(None, None, None))

    def test_the_flag_defaults_off(self):
        from config.settings import Settings

        self.assertFalse(Settings.model_fields["workspace_api_enabled"].default)

    def test_health_still_answers_so_a_deploy_is_observable(self):
        response = httpx.get(f"{self.live.base_url}/health")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["workspace_api_enabled"])

    def test_authenticated_surfaces_are_closed(self):
        for path in ("/v1/whoami", "/mcp"):
            with self.subTest(path=path):
                response = httpx.post(
                    f"{self.live.base_url}{path}",
                    headers={"Authorization": f"Bearer {self.token}"},
                    json={},
                )
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["error"], "workspace_api_disabled")


class OAuthStubTests(unittest.TestCase):
    """OAuth 2.1 is designed, not implemented (workspace/oauth.py).

    The flag defaults off and, when on, publishes only the protected-resource
    metadata document. Advertising a token endpoint that 404s would be worse
    than advertising nothing, so this asserts the shape the docs promise.
    """

    def setUp(self):
        self.db = TestDatabase("workspace_oauth")
        self.addCleanup(self.db.cleanup)
        from database.db import init_db

        init_db(self.db.url)

    def test_the_oauth_flag_defaults_off_and_publishes_nothing(self):
        from config.settings import Settings

        self.assertFalse(Settings.model_fields["workspace_oauth_enabled"].default)
        app, _ = ws.build_app(self.db.url)
        with ws.running(app) as live:
            response = httpx.get(
                f"{live.base_url}/.well-known/oauth-protected-resource"
            )
        self.assertEqual(response.status_code, 404)

    def test_with_the_flag_on_the_metadata_names_the_same_four_scopes(self):
        app, _ = ws.build_app(
            self.db.url,
            workspace_oauth_enabled=True,
            workspace_base_url="https://workspace.example",
        )
        with ws.running(app) as live:
            response = httpx.get(
                f"{live.base_url}/.well-known/oauth-protected-resource"
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["resource"], "https://workspace.example/mcp")
        self.assertEqual(body["scopes_supported"], list(scope_module.SCOPES))
        self.assertNotIn("execute", body["scopes_supported"])
        self.assertIn("not_implemented", body["x_status"])


if __name__ == "__main__":
    unittest.main()
