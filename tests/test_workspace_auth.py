"""Token issue/revoke, scopes, and rate limits (Spec K §4.1, §8).

Contains ``test_token_scopes`` and ``test_rate_limit_per_token`` from the Spec K
test plan.
"""

from __future__ import annotations

import unittest
from unittest import mock

import httpx

from tests import workspacefixture as ws
from tests.dbfixture import TestDatabase
from workspace import scopes as scope_module
from workspace import tokens
from workspace.auth import AuthError, WorkspaceAuth, argument_hash, authorize, require_scope
from workspace.ratelimit import RateLimitExceeded, RateLimiter


class ScopeModelTests(unittest.TestCase):
    def test_there_is_no_execute_scope(self):
        self.assertNotIn("execute", scope_module.SCOPES)
        self.assertEqual(
            scope_module.SCOPES, ("read", "research:write", "propose", "admin")
        )

    def test_unknown_scopes_are_refused_on_the_way_in_and_out(self):
        with self.assertRaises(scope_module.UnknownScope):
            scope_module.parse("read,execute")
        with self.assertRaises(scope_module.UnknownScope):
            scope_module.render(["execute"])

    def test_render_is_canonical(self):
        self.assertEqual(scope_module.render(["propose", "read", "read"]), "read,propose")

    def test_write_scopes_count_against_the_write_limit(self):
        self.assertEqual(scope_module.kind_for(scope_module.READ), "read")
        for scope in (scope_module.RESEARCH_WRITE, scope_module.PROPOSE, scope_module.ADMIN):
            self.assertEqual(scope_module.kind_for(scope), "write")


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("wstokens")
        self.addCleanup(self.db.cleanup)
        from database.db import init_db

        init_db(self.db.url)

    def _session(self):
        from database.db import get_session

        return get_session()

    def test_issue_returns_a_secret_that_is_not_stored(self):
        from database.models import WorkspaceToken

        with self._session() as session:
            secret = tokens.issue(session, "codex-laptop", ["read"])
        self.assertTrue(secret.startswith(tokens.TOKEN_PREFIX))
        with self._session() as session:
            row = session.query(WorkspaceToken).one()
            self.assertNotEqual(row.token_hash, secret)
            self.assertEqual(row.token_hash, tokens.hash_secret(secret))
            self.assertEqual(row.token_prefix, secret[: tokens.DISPLAY_PREFIX_LENGTH])
            self.assertEqual(row.scopes, "read")

    def test_authenticate_round_trips_and_records_use(self):
        with self._session() as session:
            secret = tokens.issue(session, "codex-laptop", ["read", "propose"])
        with self._session() as session:
            identity = tokens.authenticate(session, secret)
        self.assertIsNotNone(identity)
        self.assertEqual(identity.label, "codex-laptop")
        self.assertEqual(identity.scopes, ("read", "propose"))

        from database.models import WorkspaceToken

        with self._session() as session:
            self.assertIsNotNone(session.query(WorkspaceToken).one().last_used_at)

    def test_unknown_and_revoked_tokens_authenticate_to_nothing(self):
        with self._session() as session:
            secret = tokens.issue(session, "codex-laptop", ["read"])
        with self._session() as session:
            self.assertIsNone(tokens.authenticate(session, "swt_not-a-real-token"))
            self.assertIsNone(tokens.authenticate(session, ""))
        with self._session() as session:
            self.assertTrue(tokens.revoke(session, "codex-laptop"))
        with self._session() as session:
            self.assertIsNone(tokens.authenticate(session, secret))
            self.assertFalse(tokens.revoke(session, "codex-laptop"))

    def test_a_duplicate_label_is_refused(self):
        with self._session() as session:
            tokens.issue(session, "codex-laptop", ["read"])
        with self.assertRaises(tokens.TokenError):
            with self._session() as session:
                tokens.issue(session, "codex-laptop", ["read"])

    def test_a_token_needs_a_label_and_a_scope(self):
        with self.assertRaises(tokens.TokenError):
            with self._session() as session:
                tokens.issue(session, "  ", ["read"])
        with self.assertRaises(tokens.TokenError):
            with self._session() as session:
                tokens.issue(session, "empty", [])

    def test_two_tokens_never_collide(self):
        self.assertNotEqual(tokens.generate_secret(), tokens.generate_secret())

    def test_listing_never_shows_a_secret(self):
        with self._session() as session:
            secret = tokens.issue(session, "codex-laptop", ["read"], note="laptop")
        with self._session() as session:
            rows = tokens.listing(session)
        self.assertEqual(len(rows), 1)
        self.assertNotIn(secret, str(rows))
        self.assertEqual(rows[0]["prefix"], secret[: tokens.DISPLAY_PREFIX_LENGTH])


class TokenScopeEnforcementTests(unittest.TestCase):
    """``test_token_scopes``: a read token is refused on the write tools."""

    def setUp(self):
        self.identity = tokens.TokenIdentity(token_id=1, label="read-only", scopes=("read",))
        self.auth = WorkspaceAuth(
            settings=mock.Mock(workspace_api_enabled=True),
            limiter=RateLimiter(),
            session_factory=None,
        )

    def test_a_read_token_is_refused_on_research_write_and_propose_order(self):
        for tool in ("research_write", "thesis_review", "journal_append", "propose_order"):
            with self.subTest(tool=tool):
                with self.assertRaises(AuthError) as caught:
                    authorize(self.auth, self.identity, tool, {"ticker": "AAPL"})
                self.assertEqual(caught.exception.status, 403)
                self.assertEqual(caught.exception.code, "insufficient_scope")

    def test_a_read_token_is_allowed_on_the_read_tools(self):
        for tool, scope in scope_module.TOOL_SCOPES.items():
            if scope != scope_module.READ:
                continue
            with self.subTest(tool=tool):
                self.auth.limiter.reset()
                self.assertIs(authorize(self.auth, self.identity, tool, None), self.identity)

    def test_a_write_token_carries_read_only_if_it_was_granted_read(self):
        writer = tokens.TokenIdentity(token_id=2, label="w", scopes=("research:write",))
        with self.assertRaises(AuthError):
            authorize(self.auth, writer, "research_get", None)
        self.assertIs(authorize(self.auth, writer, "research_write", None), writer)

    def test_a_tool_with_no_declared_scope_is_a_server_error_not_an_allow(self):
        with self.assertRaises(AuthError) as caught:
            authorize(self.auth, self.identity, "tool_from_the_future", None)
        self.assertEqual(caught.exception.status, 500)

    def test_require_scope_rejects_a_scope_that_does_not_exist(self):
        with self.assertRaises(AuthError):
            require_scope(self.identity, "execute")

    def test_every_spec_k_tool_has_a_declared_scope(self):
        """Spec K §4.2's table, so a phase cannot ship a write tool at `read`."""
        expected_writes = {"research_write", "thesis_review", "journal_append", "propose_order"}
        writes = {
            name
            for name, scope in scope_module.TOOL_SCOPES.items()
            if scope in scope_module.WRITE_SCOPES
        }
        self.assertEqual(writes, expected_writes)
        self.assertEqual(scope_module.TOOL_SCOPES["propose_order"], scope_module.PROPOSE)


class RateLimitTests(unittest.TestCase):
    def test_the_read_limit_is_sixty_a_minute_and_the_write_limit_ten(self):
        limiter = RateLimiter()
        self.assertEqual(limiter.limit_for("read"), 60)
        self.assertEqual(limiter.limit_for("write"), 10)

    def test_the_sixty_first_read_in_a_minute_is_refused_with_a_retry_after(self):
        limiter = RateLimiter()
        for _ in range(60):
            limiter.check(token_id=1, kind="read")
        with self.assertRaises(RateLimitExceeded) as caught:
            limiter.check(token_id=1, kind="read")
        self.assertEqual(caught.exception.limit, 60)
        self.assertGreaterEqual(caught.exception.retry_after, 1)
        self.assertLessEqual(caught.exception.retry_after, 60)

    def test_the_eleventh_write_in_a_minute_is_refused(self):
        limiter = RateLimiter()
        for _ in range(10):
            limiter.check(token_id=1, kind="write")
        with self.assertRaises(RateLimitExceeded):
            limiter.check(token_id=1, kind="write")

    def test_limits_are_per_token_and_per_kind(self):
        limiter = RateLimiter()
        for _ in range(60):
            limiter.check(token_id=1, kind="read")
        limiter.check(token_id=2, kind="read")
        limiter.check(token_id=1, kind="write")

    def test_the_window_rolls_over(self):
        now = [1000.0]
        limiter = RateLimiter(clock=lambda: now[0])
        for _ in range(60):
            limiter.check(token_id=1, kind="read")
        with self.assertRaises(RateLimitExceeded):
            limiter.check(token_id=1, kind="read")
        now[0] += 61
        limiter.check(token_id=1, kind="read")


class RateLimitOverHttpTests(unittest.TestCase):
    """``test_rate_limit_per_token``, through the real service."""

    def setUp(self):
        self.db = TestDatabase("wsrate")
        self.addCleanup(self.db.cleanup)
        from database.db import init_db

        init_db(self.db.url)
        self.token = ws.issue_token("busy-agent", ["read"])
        self.other = ws.issue_token("calm-agent", ["read"])

        app, _ = ws.build_app(self.db.url, workspace_read_rate_limit_per_minute=5)
        self._live = ws.running(app)
        self.live = self._live.__enter__()
        self.addCleanup(lambda: self._live.__exit__(None, None, None))

    def _get(self, token):
        return httpx.get(
            f"{self.live.base_url}/v1/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )

    def test_the_call_past_the_limit_is_refused_with_a_retry_after(self):
        for _ in range(5):
            self.assertEqual(self._get(self.token).status_code, 200)
        refused = self._get(self.token)
        self.assertEqual(refused.status_code, 429)
        self.assertEqual(refused.json()["error"], "rate_limited")
        self.assertIn("Retry-After", refused.headers)
        self.assertGreaterEqual(int(refused.headers["Retry-After"]), 1)

        # The limit is per token, not per service.
        self.assertEqual(self._get(self.other).status_code, 200)


class CallLogTests(unittest.TestCase):
    """Spec K §4.1: token label, tool name, argument hash — and not the arguments."""

    def test_the_log_line_carries_the_label_the_tool_and_an_argument_hash(self):
        identity = tokens.TokenIdentity(token_id=1, label="codex-laptop", scopes=("read",))
        auth = WorkspaceAuth(
            settings=mock.Mock(workspace_api_enabled=True),
            limiter=RateLimiter(),
            session_factory=None,
        )
        with mock.patch("workspace.auth.log") as logger:
            authorize(auth, identity, "whoami", {"ticker": "SECRET-TICKER"})
        logger.info.assert_called_once()
        _, kwargs = logger.info.call_args
        self.assertEqual(kwargs["token_label"], "codex-laptop")
        self.assertEqual(kwargs["tool"], "whoami")
        self.assertEqual(kwargs["argument_hash"], argument_hash({"ticker": "SECRET-TICKER"}))
        self.assertNotIn("SECRET-TICKER", str(kwargs))

    def test_the_argument_hash_is_order_independent_and_collision_resistant(self):
        self.assertEqual(argument_hash({"a": 1, "b": 2}), argument_hash({"b": 2, "a": 1}))
        self.assertNotEqual(argument_hash({"a": 1}), argument_hash({"a": 2}))


if __name__ == "__main__":
    unittest.main()
