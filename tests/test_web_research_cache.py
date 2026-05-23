"""Regression tests for BRY-66 web research cost controls."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agents.web_research_agent import WebResearchAgent
from config.settings import Settings
from database.db import init_db


class _CountingWebSearch:
    def __init__(self):
        self.calls = []

    def search_and_analyze_json(self, system_prompt, user_prompt, model=None, max_searches=8, max_tokens=6144):
        self.calls.append({
            "model": model,
            "max_searches": max_searches,
            "user_prompt": user_prompt,
        })
        return {
            "synthesis": f"research call {len(self.calls)}",
            "catalyst_context": "c",
            "competitive_dynamics": "d",
            "management_signals": "m",
            "bull_bear_debate": "bb",
            "institutional_positioning": "ip",
            "information_score": 0.61,
            "confidence": 0.72,
            "direction": "bullish",
            "key_finding": "kf",
            "sources_summary": "s",
            "source_urls": ["https://example.com/source"],
        }


def _settings(db_path, **overrides):
    base = {
        "web_search_provider": "anthropic",
        "web_research_max_searches": 5,
        "web_research_cache_enabled": True,
        "web_research_cache_ttl_hours": 24,
        "database_url": f"sqlite:///{db_path}",
        "analyst_model": "claude-sonnet-4-6",
        "scoring_model": "claude-opus-4-6",
        "filter_model": "claude-haiku-4-5-20251001",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _agent(settings, web_search):
    agent = WebResearchAgent.__new__(WebResearchAgent)
    agent.settings = settings
    agent.run_id = "test-run"
    agent.agent_type = "web_research"
    agent.web_search_client = web_search
    agent.firecrawl_client = None
    return agent


class WebResearchCostControlTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        init_db(f"sqlite:///{self.db_path}")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_default_web_research_search_cap_is_bry66_recommendation(self):
        default = Settings.model_fields["web_research_max_searches"].default
        self.assertEqual(default, 5)

    def test_same_day_web_research_cache_hits_skip_provider_call(self):
        web_search = _CountingWebSearch()
        agent = _agent(_settings(self.db_path), web_search)
        catalyst_data = {"catalyst_summary": "earnings beat", "catalyst_type": "earnings"}

        first = agent.analyze(
            ticker="AAPL",
            sector="Tech",
            catalyst_data=catalyst_data,
            catalyst_reasoning="EPS beat",
            direction_hint="bullish",
        )
        second = agent.analyze(
            ticker="AAPL",
            sector="Tech",
            catalyst_data=catalyst_data,
            catalyst_reasoning="EPS beat",
            direction_hint="bullish",
        )

        self.assertEqual(len(web_search.calls), 1)
        self.assertEqual(first.reasoning, "research call 1")
        self.assertEqual(second.reasoning, "research call 1")
        self.assertEqual(first.raw_data["cache_status"], "miss")
        self.assertEqual(second.raw_data["cache_status"], "hit")

    def test_cache_key_includes_catalyst_hash(self):
        web_search = _CountingWebSearch()
        agent = _agent(_settings(self.db_path), web_search)

        agent.analyze(ticker="AAPL", catalyst_data={"catalyst_summary": "earnings beat"})
        agent.analyze(ticker="AAPL", catalyst_data={"catalyst_summary": "analyst downgrade"})

        self.assertEqual(len(web_search.calls), 2)

    def test_cache_can_be_disabled(self):
        web_search = _CountingWebSearch()
        agent = _agent(_settings(self.db_path, web_research_cache_enabled=False), web_search)
        catalyst_data = {"catalyst_summary": "earnings beat"}

        first = agent.analyze(ticker="MSFT", catalyst_data=catalyst_data)
        second = agent.analyze(ticker="MSFT", catalyst_data=catalyst_data)

        self.assertEqual(len(web_search.calls), 2)
        self.assertEqual(first.raw_data["cache_status"], "disabled")
        self.assertEqual(second.raw_data["cache_status"], "disabled")


if __name__ == "__main__":
    unittest.main()
