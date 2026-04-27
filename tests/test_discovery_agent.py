from types import SimpleNamespace
from unittest.mock import patch

from agents.discovery_agent import DiscoveryAgent


class _FakeWebSearchClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def search_and_analyze_json(self, system_prompt, user_prompt, model=None, max_searches=10, max_tokens=8192):
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "model": model,
                "max_searches": max_searches,
                "max_tokens": max_tokens,
            }
        )
        return self.result


def _build_agent(result):
    settings = SimpleNamespace(
        discovery_model="claude-sonnet-4-6",
        web_search_provider="gemini",
        gemini_discovery_model="gemini-3.1-pro-preview",
        discovery_max_tickers=12,
        discovery_max_searches=8,
        discovery_thinking_budget=0,
        discovery_output_max_tokens=8192,
    )
    web_search = _FakeWebSearchClient(result)
    agent = DiscoveryAgent(settings, anthropic_client=None, web_search_client=web_search)
    return agent, web_search


def test_recover_partial_results_salvages_complete_tickers_from_truncated_output():
    agent, _ = _build_agent({"tickers": []})
    raw_text = """I'll search for ideas now.
```json
{
  "tickers": [
    {
      "ticker": "ORCL",
      "catalyst_summary": "Oracle beat and raised guidance.",
      "catalyst_type": "earnings_surprise",
      "relevance_score": 0.95,
      "direction_hint": "bullish",
      "discovery_context": "Oracle reported a strong earnings beat with cloud revenue accelerating."
    },
    {
      "ticker": "MDB",
      "catalyst_summary": "MongoDB sold off on weak guidance.",
      "catalyst_type": "earnings_surprise",
      "relevance_score": 0.84,
      "direction_hint": "bearish",
      "discovery_context": "MongoDB's guidance disappointed the market despite strong current-quarter results."
    },
    {
      "ticker": "ADBE",
      "catalyst_summary": "Adobe reports after the bell",
"""

    recovered = agent._recover_partial_results(raw_text, "claude-sonnet-4-6")

    assert [ticker.ticker for ticker in recovered.tickers] == ["ORCL", "MDB"]
    assert recovered.tickers[0].direction_hint == "bullish"
    assert recovered.tickers[1].direction_hint == "bearish"


def test_discover_recovers_from_truncated_json_and_uses_larger_output_budget():
    raw_text = """Research complete.
{
  "tickers": [
    {
      "ticker": "CF",
      "catalyst_summary": "Fertilizer names are reacting to supply risk.",
      "catalyst_type": "sector_catalyst",
      "relevance_score": 0.9,
      "direction_hint": "bullish",
      "discovery_context": "CF Industries is benefiting from a supply shock in fertilizer markets."
    },
    {
      "ticker": "DISC",
      "catalyst_summary": "Bitopertin approval window is now open.",
      "catalyst_type": "product_regulatory",
      "relevance_score": 0.83,
      "direction_hint": "bullish",
      "discovery_context": "Disc Medicine has a near-term FDA catalyst for bitopertin."
    }
"""
    agent, web_search = _build_agent(
        {"error": "Failed to parse JSON response", "raw": raw_text}
    )

    with patch.object(agent, "_save_discoveries", return_value=None):
        output = agent.discover(regime={"regime": "neutral"})

    assert [ticker.ticker for ticker in output.tickers] == ["CF", "DISC"]
    assert web_search.calls[0]["max_tokens"] == 8192
    assert web_search.calls[0]["max_searches"] == 8
    assert web_search.calls[0]["model"] == "gemini-3.1-pro-preview"
