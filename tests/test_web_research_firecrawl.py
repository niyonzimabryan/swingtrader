"""Verify the web research agent injects scraped Firecrawl markdown into the
LLM prompt and records scraped source URLs in the agent output."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from agents.web_research_agent import WebResearchAgent


class _FakeFirecrawl:
    def __init__(self, body_by_url):
        self._body_by_url = body_by_url
        self.is_available = True
        self.calls = []

    def scrape(self, url):
        self.calls.append(url)
        return self._body_by_url.get(url)


class _FakeWebSearch:
    def __init__(self):
        self.last_user_prompt = None

    def search_and_analyze_json(self, system_prompt, user_prompt, model=None, max_searches=8, max_tokens=6144):
        self.last_user_prompt = user_prompt
        return {
            "synthesis": "ok",
            "catalyst_context": "c",
            "competitive_dynamics": "d",
            "management_signals": "m",
            "bull_bear_debate": "bb",
            "institutional_positioning": "ip",
            "information_score": 0.6,
            "confidence": 0.7,
            "direction": "bullish",
            "key_finding": "kf",
            "sources_summary": "s",
            "source_urls": ["https://x"],
        }


def _build_agent(firecrawl=None):
    settings = SimpleNamespace(
        web_search_provider="anthropic",
        web_research_max_searches=4,
        web_research_cache_enabled=False,
        web_research_cache_ttl_hours=24,
        analyst_model="claude-sonnet-4-6",
        scoring_model="claude-opus-4-6",
        filter_model="claude-haiku-4-5-20251001",
    )
    agent = WebResearchAgent.__new__(WebResearchAgent)
    agent.settings = settings
    agent.run_id = "test-run"
    agent.agent_type = "web_research"
    agent.web_search_client = _FakeWebSearch()
    agent.firecrawl_client = firecrawl
    return agent


class WebResearchFirecrawlTests(unittest.TestCase):
    def test_scraped_markdown_inserted_into_user_prompt(self):
        firecrawl = _FakeFirecrawl({"https://example.com/1": "scraped markdown body XYZ"})
        agent = _build_agent(firecrawl)
        catalyst_data = {
            "source": "https://example.com/1",
            "catalyst_summary": "beat",
            "catalyst_type": "earnings",
        }

        out = agent.analyze(
            ticker="AAPL", sector="Tech", catalyst_data=catalyst_data,
            catalyst_reasoning="EPS beat", direction_hint="bullish",
        )

        self.assertEqual(firecrawl.calls, ["https://example.com/1"])
        self.assertIn("PRE-FETCHED SOURCES", agent.web_search_client.last_user_prompt)
        self.assertIn("scraped markdown body XYZ", agent.web_search_client.last_user_prompt)
        self.assertEqual(out.raw_data["scraped_source_urls"], ["https://example.com/1"])

    def test_no_scrape_when_firecrawl_unavailable(self):
        agent = _build_agent(firecrawl=None)
        catalyst_data = {"source": "https://example.com/1"}

        out = agent.analyze(
            ticker="AAPL", sector="Tech", catalyst_data=catalyst_data,
            catalyst_reasoning="", direction_hint="bullish",
        )

        self.assertNotIn("PRE-FETCHED SOURCES", agent.web_search_client.last_user_prompt)
        self.assertEqual(out.raw_data["scraped_source_urls"], [])

    def test_collect_source_urls_handles_missing_and_invalid(self):
        urls = WebResearchAgent._collect_source_urls({
            "source": "not-a-url",
            "source_urls": ["https://a", "ftp://b", "https://a"],
        })
        self.assertEqual(urls, ["https://a"])


if __name__ == "__main__":
    unittest.main()
