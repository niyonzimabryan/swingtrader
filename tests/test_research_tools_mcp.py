"""The five Spec M tools answer over streamable-HTTP MCP, with scope enforcement.

Same harness as Phase 0b: a real uvicorn server, the official ``mcp`` client, a
bearer token, over loopback. Nothing is faked at the transport layer, because
the transport is part of what the phase claims to have delivered.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import date, timedelta

import httpx

from research_workspace import citations
from tests import workspacefixture as ws
from tests.dbfixture import TestDatabase
from workspace.research_tools import RESEARCH_TOOLS
from workspace.tools import REGISTERED_TOOLS

SEC = {
    "url": "https://www.sec.gov/Archives/edgar/data/2488/x.htm",
    "tier": "primary_regulator",
    "title": "AMD 10-Q",
}
NEWS = {"url": "https://news.test/story", "tier": "news", "title": "A story"}


def text_of(result) -> str:
    return "".join(
        block.text for block in result.content if getattr(block, "type", "") == "text"
    )


def payload_of(result) -> dict:
    return json.loads(text_of(result))


#: Minimal valid arguments per tool, for the scope sweep. A tool called with
#: arguments missing would be refused by schema validation before its body ran,
#: which would make the scope assertion vacuous.
MINIMAL_ARGUMENTS = {
    "research_get": {"ticker": "AMD"},
    "research_search": {"query": "AMD"},
    "research_write": {"kind": "research_question", "question": "why?"},
    "thesis_review": {"thesis_id": 1, "verdict": "hold"},
    "journal_append": {"decision": "passed", "tickers": ["AMD"]},
}


class ResearchToolsTestCase(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("research_tools")
        self.addCleanup(self.db.cleanup)

        from database.db import init_db

        init_db(self.db.url)
        self.read_token = ws.issue_token("claude-code", ["read"])
        self.write_token = ws.issue_token("claude-web", ["read", "research:write"])
        self.admin_token = ws.issue_token("admin-only", ["admin"])

        app, self.settings = ws.build_app(self.db.url, research_workspace_enabled=True)
        self._live = ws.running(app)
        self.live = self._live.__enter__()
        self.addCleanup(lambda: self._live.__exit__(None, None, None))
        self.addCleanup(citations.clear_answer_resolver)

    def call(self, tool, arguments=None, token=None):
        return asyncio.run(
            ws.call_tool(self.live.base_url, token or self.write_token, tool, arguments)
        )

    def ok(self, tool, arguments=None, token=None) -> dict:
        result = self.call(tool, arguments, token)
        self.assertFalse(result.isError, text_of(result))
        return payload_of(result)

    def refused(self, tool, arguments=None, token=None) -> str:
        result = self.call(tool, arguments, token)
        self.assertTrue(result.isError, f"{tool} answered when it should refuse")
        return text_of(result)

    def a_live_thesis(self) -> int:
        written = self.ok(
            "research_write",
            {
                "kind": "thesis",
                "ticker": "AMD",
                "title": "MI400 ramp re-rates datacenter",
                "claim": "Datacenter revenue doubles by FY27.",
                "bear_case": "One hyperscaler can walk.",
                "bear_case_author": "thesis-critic",
                "author": "human",
            },
        )
        thesis_id = written["thesis"]["thesis_id"]
        self.ok(
            "research_write",
            {
                "kind": "invalidator",
                "thesis_id": thesis_id,
                "description": "closes below $82 for three sessions",
                "invalidator_type": "price_level",
                "params": {"operator": "below", "price": 82.0, "consecutive_sessions": 3},
                "author": "human",
            },
        )
        self.ok(
            "research_write",
            {
                "kind": "thesis",
                "thesis_id": thesis_id,
                "probability": 0.6,
                "resolution_at": (date.today() + timedelta(days=300)).isoformat(),
                "resolution_observable": "FY27 datacenter revenue in the 10-K",
                "status": "active",
                "author": "human",
            },
        )
        return thesis_id


class SurfaceTests(ResearchToolsTestCase):
    def test_the_five_tools_are_advertised_with_the_flag_on(self):
        listing = asyncio.run(ws.list_tools(self.live.base_url, self.read_token))
        names = sorted(tool.name for tool in listing.tools)
        self.assertEqual(names, sorted(REGISTERED_TOOLS + RESEARCH_TOOLS))

    def test_health_reports_the_same_surface(self):
        body = httpx.get(f"{self.live.base_url}/health").json()
        self.assertEqual(
            sorted(body["mcp"]["tools"]), sorted(REGISTERED_TOOLS + RESEARCH_TOOLS)
        )
        self.assertTrue(body["research_workspace_enabled"])

    def test_a_read_token_is_refused_on_every_write_tool(self):
        for tool in ("research_write", "thesis_review", "journal_append"):
            with self.subTest(tool=tool):
                message = self.refused(
                    tool, MINIMAL_ARGUMENTS[tool], token=self.read_token
                )
                self.assertIn("insufficient_scope", message)
                self.assertIn("research:write", message)

    def test_a_read_token_may_read(self):
        payload = self.ok("research_get", {"ticker": "AMD"}, token=self.read_token)
        self.assertEqual(payload["ticker"], "AMD")

    def test_every_research_tool_authorises_before_it_answers(self):
        """The admin token carries no scope any of these require."""
        for tool in RESEARCH_TOOLS:
            with self.subTest(tool=tool):
                message = self.refused(
                    tool, MINIMAL_ARGUMENTS[tool], token=self.admin_token
                )
                self.assertIn("insufficient_scope", message)


class FlagOffTests(unittest.TestCase):
    def test_the_tools_are_not_advertised_with_the_flag_off(self):
        db = TestDatabase("research_tools_off")
        self.addCleanup(db.cleanup)
        from database.db import init_db

        init_db(db.url)
        token = ws.issue_token("claude-code", ["read", "research:write"])
        app, _ = ws.build_app(db.url)  # research_workspace_enabled defaults false

        with ws.running(app) as live:
            listing = asyncio.run(ws.list_tools(live.base_url, token))
            self.assertEqual(
                sorted(t.name for t in listing.tools), sorted(REGISTERED_TOOLS)
            )
            body = httpx.get(f"{live.base_url}/health").json()
            self.assertFalse(body["research_workspace_enabled"])


class ReadToolTests(ResearchToolsTestCase):
    def test_research_get_returns_a_provenance_block(self):
        self.a_live_thesis()
        payload = self.ok("research_get", {"ticker": "AMD"})
        provenance = payload["provenance"]

        self.assertIn("as_of_utc", provenance)
        self.assertIn("theses", provenance["sources"])
        self.assertIn("data_quality", provenance)
        self.assertFalse(provenance["stale"])
        self.assertEqual(len(payload["theses"]), 1)
        self.assertEqual(payload["theses"][0]["status"], "active")
        self.assertEqual(
            [i["type"] for i in payload["theses"][0]["invalidators"]], ["price_level"]
        )

    def test_unsourced_and_untrusted_sections_are_flagged_in_the_response(self):
        self.ok(
            "research_write",
            {
                "kind": "dossier_section", "ticker": "AMD", "section_key": "notes",
                "body_md": "No source for this.", "sources": [], "author": "human",
                "human_authored": True,
            },
        )
        self.ok(
            "research_write",
            {
                "kind": "dossier_section", "ticker": "AMD",
                "section_key": "business_model", "body_md": "From the 10-Q.",
                "sources": [SEC], "author": "claude-opus-4-6",
            },
        )
        payload = self.ok("research_get", {"ticker": "AMD"})
        by_key = {s["section_key"]: s for s in payload["sections"]}

        self.assertTrue(by_key["notes"]["unsourced"])
        self.assertIn("unsourced", by_key["notes"]["warnings"][0])
        self.assertEqual(by_key["business_model"]["content_trust"], "untrusted")
        self.assertEqual(payload["provenance"]["content_trust"], "untrusted")

    def test_untrusted_bodies_are_delimited_with_a_per_response_nonce(self):
        self.ok(
            "research_write",
            {
                "kind": "dossier_section", "ticker": "AMD",
                "section_key": "business_model", "body_md": "From the 10-Q.",
                "sources": [SEC], "author": "claude-opus-4-6",
            },
        )
        first = self.ok("research_get", {"ticker": "AMD"})
        second = self.ok("research_get", {"ticker": "AMD"})

        nonce = first["provenance"]["untrusted_delimiter_nonce"]
        self.assertTrue(nonce)
        body = first["sections"][0]["body_md"]
        self.assertTrue(body.startswith(f"<<untrusted:{nonce}>>"))
        self.assertIn("From the 10-Q.", body)
        # Two responses never share a nonce (Spec P §5).
        self.assertNotEqual(nonce, second["provenance"]["untrusted_delimiter_nonce"])

    def test_research_search_spans_the_workspace(self):
        self.a_live_thesis()
        self.ok(
            "journal_append",
            {"decision": "passed", "tickers": ["AMD"], "note_md": "MI400 ramp watch."},
        )
        payload = self.ok("research_search", {"query": "MI400"})
        self.assertEqual({hit["kind"] for hit in payload["hits"]}, {"thesis", "decision"})
        self.assertIn("hits", payload["provenance"]["sources"])

    def test_an_empty_search_is_refused_readably(self):
        self.assertIn("empty_query", self.refused("research_search", {"query": "  "}))


class WriteToolTests(ResearchToolsTestCase):
    def test_research_write_refuses_all_untrusted(self):
        """Spec P §5, over the wire."""
        message = self.refused(
            "research_write",
            {
                "kind": "dossier_section", "ticker": "AMD", "section_key": "notes",
                "body_md": "A news story said so.", "sources": [NEWS],
                "author": "claude-opus-4-6",
            },
        )
        self.assertIn("all_sources_untrusted", message)

        allowed = self.ok(
            "research_write",
            {
                "kind": "dossier_section", "ticker": "AMD", "section_key": "notes",
                "body_md": "Bryan's read of the story.", "sources": [NEWS],
                "author": "human", "human_authored": True,
            },
        )
        self.assertTrue(allowed["section"]["human_authored"])

    def test_a_thesis_cannot_be_activated_without_its_invalidator(self):
        written = self.ok(
            "research_write",
            {
                "kind": "thesis", "ticker": "AMD", "title": "Bare thesis",
                "claim": "c", "bear_case": "b", "author": "human",
            },
        )
        message = self.refused(
            "research_write",
            {
                "kind": "thesis",
                "thesis_id": written["thesis"]["thesis_id"],
                "probability": 0.6,
                "resolution_at": (date.today() + timedelta(days=90)).isoformat(),
                "status": "active",
            },
        )
        self.assertIn("thesis_not_activatable", message)
        self.assertIn("no invalidator", message)

    def test_a_qualitative_only_thesis_cannot_be_activated(self):
        written = self.ok(
            "research_write",
            {
                "kind": "thesis", "ticker": "AMD", "title": "Feelings",
                "claim": "c", "bear_case": "b", "author": "human",
            },
        )
        thesis_id = written["thesis"]["thesis_id"]
        attached = self.ok(
            "research_write",
            {
                "kind": "invalidator", "thesis_id": thesis_id,
                "description": "the narrative breaks", "invalidator_type": "qualitative",
            },
        )
        self.assertIn(
            "no machine-checkable invalidator",
            " ".join(attached["activation_blockers"]),
        )
        message = self.refused(
            "research_write",
            {
                "kind": "thesis", "thesis_id": thesis_id, "probability": 0.6,
                "resolution_at": (date.today() + timedelta(days=90)).isoformat(),
                "status": "active",
            },
        )
        self.assertIn("no machine-checkable invalidator", message)

    def test_thesis_review_records_a_verdict_and_touches_no_position(self):
        thesis_id = self.a_live_thesis()
        payload = self.ok(
            "thesis_review",
            {"thesis_id": thesis_id, "verdict": "weakened", "note": "ramp slipping"},
        )
        self.assertEqual(payload["thesis"]["status"], "weakened")
        self.assertIn("never a position", payload["position_effect"])

    def test_an_unknown_verdict_is_refused_with_the_valid_ones(self):
        thesis_id = self.a_live_thesis()
        message = self.refused(
            "thesis_review", {"thesis_id": thesis_id, "verdict": "sell"}
        )
        self.assertIn("unknown_verdict", message)
        self.assertIn("invalidated", message)


class JournalToolTests(ResearchToolsTestCase):
    def test_journal_refuses_uncitable_answer(self):
        """A `quick` answer cannot back a decision (Spec N §8, Spec L §6.6)."""
        from tests.comparablesfixture import quick_answer, refused_answer, full_answer

        citations.register_answer_resolver(
            {
                "quick@1": quick_answer(),
                "insufficient@1": refused_answer(),
                "full-ok@1": full_answer(status="ok"),
                "full-inconclusive@1": full_answer(status="inconclusive"),
            }.get
        )

        for answer_id in ("quick@1", "insufficient@1", "full-inconclusive@1"):
            with self.subTest(answer_id=answer_id):
                message = self.refused(
                    "journal_append",
                    {
                        "decision": "opened", "tickers": ["AMD"],
                        "budget": "evidenced", "cohort_answer_id": answer_id,
                    },
                )
                self.assertIn("uncitable_cohort_answer", message)

        payload = self.ok(
            "journal_append",
            {
                "decision": "opened", "tickers": ["AMD"], "budget": "evidenced",
                "cohort_answer_id": "full-ok@1", "note_md": "Sized on the cohort.",
            },
        )
        self.assertEqual(payload["entry"]["budget"], "evidenced")
        self.assertTrue(payload["entry"]["cohort_answer_id"])
        self.assertTrue(payload["entry"]["cohort_evidence_hash"])

    def test_an_unresolvable_citation_is_refused_rather_than_trusted(self):
        message = self.refused(
            "journal_append",
            {
                "decision": "opened", "tickers": ["AMD"], "budget": "evidenced",
                "cohort_answer_id": "does-not-exist@9",
            },
        )
        self.assertIn("unresolvable_cohort_answer", message)
        self.assertIn("Phase 3", message)

    def test_the_evidenced_budget_cannot_be_self_awarded(self):
        message = self.refused(
            "journal_append",
            {"decision": "opened", "tickers": ["AMD"], "budget": "evidenced"},
        )
        self.assertIn("evidenced_needs_citation", message)

    def test_a_decision_to_pass_is_recorded_as_discretionary(self):
        payload = self.ok(
            "journal_append",
            {
                "decision": "passed", "tickers": ["AMD"],
                "note_md": "Cohort insufficient; waiting for the next print.",
            },
        )
        self.assertEqual(payload["entry"]["decision"], "passed")
        self.assertEqual(payload["entry"]["budget"], "discretionary")
        self.assertIsNone(payload["entry"]["outcome"]["recorded_at"])


if __name__ == "__main__":
    unittest.main()
