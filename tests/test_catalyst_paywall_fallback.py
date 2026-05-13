"""Verify catalyst agent enriches Sonnet input with full article body via the
article fetcher, and tags narrative_quality on the resulting AgentOutput."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from agents.catalyst_agent import CatalystAgent


class _FakeFetcher:
    def __init__(self, body, status):
        self._body = body
        self._status = status
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return self._body, self._status


class _FakeEscalation:
    def __init__(self):
        self.last_text = None
        self.haiku_returns = []

    def haiku_prescreen(self, text, ticker, company_context):
        return self.haiku_returns.pop(0)

    def sonnet_analyze(self, ticker, text, haiku_result, company_context):
        self.last_text = text
        return {
            "materiality": 0.7,
            "direction_confidence": 0.6,
            "direction": "bullish",
            "reasoning": "ok",
            "catalyst_type": "earnings_beat",
            "catalyst_summary": "beat",
            "catalyst_modifiers": [],
            "magnitude": 3,
            "expected_impact_pct": {},
            "time_horizon_days": 10,
            "counter_arguments": "",
            "risk_analysis": {},
        }


def _build_agent(fetcher, news_items, haiku_score=4):
    settings = SimpleNamespace(
        finnhub_api_key="x",
        catalyst_escalation_threshold=3,
        watchlist_haiku_threshold=2,
    )
    agent = CatalystAgent.__new__(CatalystAgent)
    agent.settings = settings
    agent.run_id = "test-run"
    agent.agent_type = "catalyst"
    agent.article_fetcher = fetcher
    agent.news_data = MagicMock()
    agent.news_data.get_company_news.return_value = news_items
    agent.sec_data = MagicMock()
    agent.sec_data.get_recent_filings.return_value = []
    agent.market_data = MagicMock()
    agent.market_data.get_current_price.return_value = {"price": 100, "change_pct": 1.2}
    esc = _FakeEscalation()
    esc.haiku_returns = [
        {"score": haiku_score, "category": "earnings", "summary": "beat", "direction": "bullish", "relevant": True}
    ]
    agent.escalation = esc
    agent._save_catalyst = lambda *a, **k: None
    return agent, esc


class CatalystPaywallFallbackTests(unittest.TestCase):
    def test_firecrawl_body_appended_to_sonnet_text(self):
        fetcher = _FakeFetcher(body="full article text ABC", status="firecrawl")
        items = [{"headline": "AAPL beat earnings", "summary": "EPS $1.20", "url": "https://example.com/1"}]
        agent, esc = _build_agent(fetcher, items)

        out = agent.analyze(ticker="AAPL", sector="Tech")

        self.assertEqual(fetcher.calls, ["https://example.com/1"])
        self.assertIn("full article text ABC", esc.last_text)
        self.assertIn("Full article body (firecrawl)", esc.last_text)
        self.assertEqual(out.raw_data["narrative_quality"], "firecrawl")

    def test_paywalled_status_propagates_when_fetch_fails(self):
        fetcher = _FakeFetcher(body=None, status="paywalled")
        items = [{"headline": "AAPL beat earnings", "summary": "EPS $1.20", "url": "https://paywalled.example/1"}]
        agent, esc = _build_agent(fetcher, items)

        out = agent.analyze(ticker="AAPL", sector="Tech")

        self.assertNotIn("Full article body", esc.last_text)
        self.assertEqual(out.raw_data["narrative_quality"], "skipped_paywall")

    def test_no_fetcher_means_headline_only(self):
        items = [{"headline": "AAPL beat earnings", "summary": "EPS $1.20", "url": "https://example.com/1"}]
        agent, esc = _build_agent(None, items)

        out = agent.analyze(ticker="AAPL", sector="Tech")

        self.assertNotIn("Full article body", esc.last_text)
        self.assertEqual(out.raw_data["narrative_quality"], "headline_only")


if __name__ == "__main__":
    unittest.main()
