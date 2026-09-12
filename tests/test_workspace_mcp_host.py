"""The MCP endpoint answers on the host the workspace is actually served on.

``FastMCP``'s ``host`` argument defaults to ``127.0.0.1``, and on that default
the SDK auto-enables DNS-rebinding protection allowing only loopback ``Host``
headers. The workspace never binds loopback in production — uvicorn serves it
behind Railway's edge — but the guard fired regardless and answered every
``/mcp`` request on the public domain with ``421 Invalid Host header``, while
``/health`` and ``/v1/...`` kept working because they are ordinary FastAPI
routes. The whole agent tool surface was unreachable and the suite did not
notice, because ``tests/workspacefixture.py`` runs the server on ``127.0.0.1``
and so every existing MCP test sends a loopback ``Host``.

That is the gap these tests close: they set ``Host`` explicitly rather than
inheriting loopback, so a regression shows up here rather than in production.
"""

from __future__ import annotations

import unittest

from starlette.testclient import TestClient

from tests import workspacefixture as ws
from tests.dbfixture import TestDatabase

PUBLIC_HOST = "workspace-production-6e7b.up.railway.app"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "host-header-tests", "version": "1"},
    },
}


class McpHostHeaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = TestDatabase("workspace_host")
        from database.db import init_db

        init_db(cls.db.url)

    @classmethod
    def tearDownClass(cls):
        cls.db.cleanup()

    def _post_initialize(self, app, token: str, host: str):
        with TestClient(app) as client:
            return client.post(
                "/mcp",
                json=INITIALIZE,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                    "Host": host,
                },
            )

    def test_configured_public_host_is_accepted(self):
        """The deployed domain must not be refused by the transport guard."""
        app, _ = ws.build_app(
            self.db.url, workspace_base_url=f"https://{PUBLIC_HOST}"
        )
        token = ws.issue_token("host-accepted", ["read"])

        response = self._post_initialize(app, token, PUBLIC_HOST)

        self.assertNotEqual(
            response.status_code,
            421,
            "the public host is refused; the MCP surface is unreachable",
        )
        self.assertEqual(response.status_code, 200)

    def test_loopback_still_accepted_for_local_development(self):
        app, _ = ws.build_app(
            self.db.url, workspace_base_url=f"https://{PUBLIC_HOST}"
        )
        token = ws.issue_token("host-loopback", ["read"])

        response = self._post_initialize(app, token, "127.0.0.1:8000")

        self.assertEqual(response.status_code, 200)

    def test_unrelated_host_is_refused_when_base_url_is_configured(self):
        """Protection stays on — this is a corrected allowlist, not a removed one."""
        app, _ = ws.build_app(
            self.db.url, workspace_base_url=f"https://{PUBLIC_HOST}"
        )
        token = ws.issue_token("host-refused", ["read"])

        response = self._post_initialize(app, token, "attacker.example.com")

        self.assertEqual(response.status_code, 421)

    def test_no_base_url_does_not_refuse_every_request(self):
        """With no host to name, refusing everything is the same outage again."""
        app, _ = ws.build_app(self.db.url, workspace_base_url="")
        token = ws.issue_token("host-no-base-url", ["read"])

        response = self._post_initialize(app, token, PUBLIC_HOST)

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
