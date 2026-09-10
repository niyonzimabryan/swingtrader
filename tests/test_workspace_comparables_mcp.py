"""`compare_setups` and `cohort_detail` over streamable-HTTP MCP (Spec N §11).

The Phase 3c stop condition, end to end and on whichever engine the run
targets: a real uvicorn server, the official ``mcp`` client, a bearer token, a
`quick` answer, a `full` answer, a stored query id, and a citation that
resolves through the seam.

Nothing is faked at the transport layer, and nothing is faked at the data
layer either — the cohorts are built from rows written into the test database
by `tests/cohortfixture.py`.
"""

from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from tests import cohortfixture as cf
from tests import workspacefixture as ws
from tests.dbfixture import TestDatabase

from workspace import scopes as scope_module
from workspace.tools import COMPARABLE_TOOLS, REGISTERED_TOOLS, tools_for


def _text(result) -> str:
    return "".join(
        block.text for block in result.content if getattr(block, "type", "") == "text"
    )


def _payload(result) -> dict:
    return json.loads(_text(result))


class ComparableToolsTestCase(unittest.TestCase):
    """A live workspace with the Spec N flag on, over a seeded world."""

    def setUp(self):
        self.db = TestDatabase("compare_setups")
        self.addCleanup(self.db.cleanup)

        from database.db import get_session, init_db

        init_db(self.db.url)
        with get_session() as session:
            self.world = cf.seed_world(session)

        self.read_token = ws.issue_token("claude-code", ["read"])
        self.admin_token = ws.issue_token("admin-only", ["admin"])

        settings = cf.settings_for(self.world, self.db.url)
        from workspace.app import create_app

        self._live = ws.running(create_app(settings))
        self.live = self._live.__enter__()
        self.addCleanup(lambda: self._live.__exit__(None, None, None))
        self.settings = settings

    def call(self, name, arguments=None, token=None):
        return asyncio.run(ws.call_tool(
            self.live.base_url, token or self.read_token, name, arguments or {}
        ))


class RegistrationTests(ComparableToolsTestCase):
    def test_mcp_lists_the_comparable_tools_when_the_flag_is_on(self):
        listing = asyncio.run(ws.list_tools(self.live.base_url, self.read_token))
        names = sorted(tool.name for tool in listing.tools)
        self.assertEqual(names, sorted(REGISTERED_TOOLS + COMPARABLE_TOOLS))

    def test_health_advertises_what_is_actually_registered(self):
        body = httpx.get(f"{self.live.base_url}/health").json()
        self.assertEqual(
            sorted(body["mcp"]["tools"]), sorted(REGISTERED_TOOLS + COMPARABLE_TOOLS)
        )
        self.assertTrue(body["comparable_setups_enabled"])

    def test_both_tools_are_read_scoped_and_enforce_it(self):
        """A tool that forgot `authorize_call` would answer the admin token."""
        for name in COMPARABLE_TOOLS:
            with self.subTest(tool=name):
                self.assertEqual(scope_module.TOOL_SCOPES[name], scope_module.READ)
                result = self.call(name, token=self.admin_token)
                self.assertTrue(result.isError, f"{name} answered an unscoped token")
                self.assertIn("insufficient_scope", _text(result))

    def test_no_tool_can_place_an_order(self):
        """The Spec L §6 rule, restated where a new tool surface lands."""
        import workspace.tools as tool_module

        with open(tool_module.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for banned in ("submit_order", "place_order", "execution.brokers", "alpaca"):
            self.assertNotIn(banned, source)
        self.assertNotIn("execute", scope_module.SCOPES)


class CompareSetupsTests(ComparableToolsTestCase):
    def test_compare_setups_answers_quick_with_a_stored_query_id(self):
        result = self.call("compare_setups", {
            "setup": "gap_and_go_v1",
            "as_of": self.world.as_of.isoformat(),
            "depth": "quick",
        })
        self.assertFalse(result.isError, _text(result))
        payload = _payload(result)

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["depth"], "quick")
        self.assertIsNotNone(payload["query_id"])
        self.assertFalse(payload["citable"], "a quick answer must not be citable")

        answer = payload["answer"]
        self.assertGreaterEqual(answer["n_distinct_dates"], 20)
        self.assertGreaterEqual(answer["n_matured"], 30)
        self.assertIn(answer["evidence_tier"], ("clean_pit", "vendor_pit"))
        self.assertTrue(answer["horizons"])
        for horizon in answer["horizons"]:
            self.assertIn("headline", horizon)
            self.assertIn("lower", horizon["headline"])
            self.assertIn("upper", horizon["headline"])

        provenance = payload["provenance"]
        self.assertEqual(
            provenance["price_snapshot"]["slug"], self.world.context.price_snapshot_slug
        )
        self.assertTrue(provenance["price_snapshot"]["delisting_audit_recorded"])
        self.assertEqual(provenance["query_id"], payload["query_id"])
        self.assertEqual(
            provenance["universe"]["slug"], self.world.context.universe_slug
        )
        self.assertTrue(provenance["universe"]["point_in_time"])

        # The query really is stored, and the trial count really is stored.
        from database.db import get_session

        from comparables import registry

        with get_session() as session:
            row = registry.query(session, payload["query_id"])
            self.assertIsNotNone(row)
            self.assertEqual(row.requester_label, "claude-code")
            self.assertEqual(row.setup_hash, payload["setup_hash"])
            self.assertEqual(row.trials_against_this_pattern, 1)

    def test_compare_setups_answers_full_and_the_answer_is_citable(self):
        result = self.call("compare_setups", {
            "setup": "gap_and_go_v1",
            "parameters": {"horizons_sessions": [5, 10]},
            "as_of": self.world.as_of.isoformat(),
            "depth": "full",
        })
        self.assertFalse(result.isError, _text(result))
        payload = _payload(result)
        self.assertEqual(payload["depth"], "full")
        self.assertIn(payload["status"], ("ok", "inconclusive"))
        self.assertTrue(payload["citable"])
        self.assertTrue(payload["citation_id"].startswith("cohort:"))

        answer = payload["answer"]
        for field in ("cost_model", "regime_breakdown", "stability", "null_tests",
                      "n_eff", "delisting_rate", "policy", "shrinkage_reason"):
            self.assertIn(field, answer, f"a full answer must carry {field}")

        # And it resolves through the citation seam, which is what
        # `journal_append` will call.
        from database.db import get_session

        from comparables import citations

        with get_session() as session:
            resolved = citations.resolve(session, payload["citation_id"])
            self.assertTrue(resolved.citable)
            self.assertEqual(resolved.setup_hash, payload["setup_hash"])
            self.assertEqual(resolved.query_id, payload["query_id"])
            self.assertEqual(resolved.answer["depth"], "full")

    def test_the_same_question_twice_is_served_from_the_cache_byte_for_byte(self):
        args = {
            "setup": "gap_and_go_v1",
            "as_of": self.world.as_of.isoformat(),
            "depth": "quick",
        }
        first = _payload(self.call("compare_setups", args))
        second = _payload(self.call("compare_setups", args))
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(first["answer"], second["answer"])
        self.assertEqual(first["citation_id"], second["citation_id"])

    def test_a_below_floor_cohort_is_rendered_as_a_refusal(self):
        """`insufficient` is a valid, expected and frequently-correct answer."""
        result = self.call("compare_setups", {
            "setup": "gap_and_go_v1",
            "parameters": {"gap_pct": 4.9},
            "as_of": self.world.sessions[90].isoformat(),
            "depth": "quick",
        })
        self.assertFalse(result.isError, _text(result))
        payload = _payload(result)
        self.assertEqual(payload["status"], "insufficient")
        self.assertFalse(payload["citable"])
        self.assertIsNotNone(payload["query_id"])
        self.assertTrue(payload["answer"]["refusal_reason"])
        # A refusal carries no statistic to round into a hedge.
        self.assertNotIn("horizons", payload["answer"])

    def test_a_pending_plane_setup_refuses_and_says_which(self):
        payload = _payload(self.call("compare_setups", {
            "setup": "insider_cluster_v1",
            "as_of": self.world.as_of.isoformat(),
        }))
        self.assertEqual(payload["status"], "pending_plane")
        self.assertIn("Form 4", payload["refusal_reason"])
        self.assertIn("not an `insufficient`", payload["note"])

    def test_an_explicit_setup_spec_is_accepted_and_counted(self):
        spec = {
            "slug": "ad_hoc_gap_v1",
            "version": "1.0.0",
            "conditions": [
                {"fact": "gap_pct", "op": ">", "value": 3.5},
                {"fact": "dollar_volume_20d", "op": ">", "value": 5_000_000.0},
            ],
            "universe": "liquid_us_equity_v1",
            "horizons_sessions": [5, 10],
            "execution_policy": "event_swing_14cal_v1",
            "match_covariates": ["liquidity_decile", "realized_vol_decile"],
            "lookback_years": 5,
        }
        first = _payload(self.call("compare_setups", {
            "setup": "gap_and_go_v1", "as_of": self.world.as_of.isoformat(),
        }))
        second = _payload(self.call("compare_setups", {
            "setup_spec": spec, "as_of": self.world.as_of.isoformat(),
        }))
        # A free-form request counts as a trial against the nearest family (§7),
        # and the family is derived, so it lands in the right one by itself.
        self.assertEqual(second["family_slug"], first["family_slug"])
        self.assertEqual(second["answer"]["trials_against_this_pattern"], 2)

    def test_an_unknown_setup_names_the_roster(self):
        result = self.call("compare_setups", {"setup": "not_a_setup_v1"})
        self.assertTrue(result.isError)
        self.assertIn("gap_and_go_v1", _text(result))

    def test_a_bad_as_of_is_refused_rather_than_guessed(self):
        result = self.call("compare_setups", {
            "setup": "gap_and_go_v1", "as_of": "last tuesday",
        })
        self.assertTrue(result.isError)
        self.assertIn("invalid_argument", _text(result))

    def test_an_unknown_parameter_is_refused_not_ignored(self):
        result = self.call("compare_setups", {
            "setup": "gap_and_go_v1", "parameters": {"vibes": 3},
        })
        self.assertTrue(result.isError)
        self.assertIn("invalid_argument", _text(result))


class CohortDetailTests(ComparableToolsTestCase):
    def setUp(self):
        super().setUp()
        self.answer = _payload(self.call("compare_setups", {
            "setup": "earnings_sue_seasonal_v1",
            "parameters": {"horizons_sessions": [5, 10]},
            "as_of": self.world.as_of.isoformat(),
            "depth": "quick",
        }))

    def test_cohort_detail_lists_the_constituents_with_provenance_and_outcomes(self):
        payload = _payload(self.call(
            "cohort_detail", {"query_id": self.answer["query_id"]}
        ))
        self.assertEqual(payload["query_id"], self.answer["query_id"])
        self.assertEqual(payload["setup_hash"], self.answer["setup_hash"])
        self.assertTrue(payload["events"])

        for event in payload["events"]:
            self.assertIn(
                event["provenance_class"],
                ("observed_live", "vendor_pit", "archival_reconstructed"),
            )
            self.assertIn("known_at_utc", event)
            self.assertTrue(event["covariates"])
            for horizon, outcome in event["outcomes_by_horizon"].items():
                if outcome["matured"]:
                    self.assertIn("car", outcome)
                    self.assertIn("raw_return", outcome)
                else:
                    self.assertTrue(outcome["censored_reason"])

        self.assertEqual(
            sum(payload["provenance_mix"].values()), len(payload["events"])
        )

    def test_the_delisted_member_is_visible_with_its_terminal_outcome(self):
        payload = _payload(self.call(
            "cohort_detail", {"query_id": self.answer["query_id"]}
        ))
        delisted = [
            e for e in payload["events"]
            if e["ticker"] == self.world.delisted_ticker
        ]
        self.assertTrue(delisted, "the delisted name is invisible in the detail")
        for event in delisted:
            self.assertEqual(event["terminal"]["reason"], "performance_nasdaq")
            self.assertTrue(event["terminal"]["resolved"])
            self.assertAlmostEqual(event["terminal"]["terminal_return"], -0.55)

    def test_the_excluded_candidates_are_listed_with_their_reasons(self):
        payload = _payload(self.call(
            "cohort_detail", {"query_id": self.answer["query_id"]}
        ))
        reasons = {e["reason"] for e in payload["excluded"]}
        self.assertIn("market_cap_no_share_source", reasons)

    def test_cohort_detail_resolves_a_citation_id_too(self):
        payload = _payload(self.call(
            "cohort_detail", {"citation_id": self.answer["citation_id"]}
        ))
        self.assertEqual(payload["query_id"], self.answer["query_id"])

    def test_cohort_detail_needs_one_of_the_two_identifiers(self):
        result = self.call("cohort_detail", {})
        self.assertTrue(result.isError)
        self.assertIn("invalid_argument", _text(result))

    def test_an_unknown_query_id_says_so(self):
        result = self.call("cohort_detail", {"query_id": 999_999})
        self.assertTrue(result.isError)
        self.assertIn("no stored query", _text(result))


class FlagOffTests(unittest.TestCase):
    """`COMPARABLE_SETUPS_ENABLED` defaults false, and false means absent."""

    def setUp(self):
        self.db = TestDatabase("compare_off")
        self.addCleanup(self.db.cleanup)
        from database.db import init_db

        init_db(self.db.url)
        self.token = ws.issue_token("claude-code", ["read"])
        app, _ = ws.build_app(self.db.url)
        self._live = ws.running(app)
        self.live = self._live.__enter__()
        self.addCleanup(lambda: self._live.__exit__(None, None, None))

    def test_the_flag_defaults_off(self):
        from config.settings import Settings

        self.assertFalse(Settings.model_fields["comparable_setups_enabled"].default)

    def test_the_tools_are_not_registered_at_all(self):
        """An unregistered tool is a clearer refusal than a registered one that
        answers "disabled", and `tools/list` should describe what the server can
        actually do."""
        listing = asyncio.run(ws.list_tools(self.live.base_url, self.token))
        names = {tool.name for tool in listing.tools}
        self.assertEqual(names, set(REGISTERED_TOOLS))
        for name in COMPARABLE_TOOLS:
            self.assertNotIn(name, names)

    def test_calling_an_unregistered_tool_is_an_error_not_an_empty_answer(self):
        result = asyncio.run(ws.call_tool(
            self.live.base_url, self.token, "compare_setups", {}
        ))
        self.assertTrue(result.isError)

    def test_health_reports_the_flag_and_the_smaller_surface(self):
        body = httpx.get(f"{self.live.base_url}/health").json()
        self.assertFalse(body["comparable_setups_enabled"])
        self.assertEqual(body["mcp"]["tools"], list(REGISTERED_TOOLS))

    def test_tools_for_is_the_one_place_the_surface_is_decided(self):
        class Off:
            comparable_setups_enabled = False

        class On:
            comparable_setups_enabled = True

        self.assertEqual(tools_for(None), REGISTERED_TOOLS)
        self.assertEqual(tools_for(Off()), REGISTERED_TOOLS)
        self.assertEqual(tools_for(On()), REGISTERED_TOOLS + COMPARABLE_TOOLS)


class CikMapTests(unittest.TestCase):
    def test_a_malformed_entry_raises_rather_than_being_skipped(self):
        from workspace.tools import parse_cik_map

        self.assertEqual(parse_cik_map(""), ())
        self.assertEqual(
            parse_cik_map("aapl:0000320193, MSFT:0000789019"),
            (("AAPL", "0000320193"), ("MSFT", "0000789019")),
        )
        with self.assertRaises(ValueError):
            parse_cik_map("AAPL")


if __name__ == "__main__":
    unittest.main()
