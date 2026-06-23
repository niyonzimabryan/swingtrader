from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents.base_agent import AgentOutput
from agents.pattern_agent import PatternAgent
from data.analog_ranker import AnalogRanker
from data.event_discovery import assert_outcome_neutral
from data.event_extractor import EventExtractor, EventValidationError, make_dedupe_key
from data.event_outcomes import EventOutcomeEngine, HistoricalMarketCapUnavailable, PriceBar
from data.peer_resolver import PeerResolver
from database.db import get_session, init_db
from database.models import EventOutcome, HistoricalEvent, PatternProviderCache, PatternSearchRun
from scoring.engine import ScoringEngine
from utils.perplexity_search_client import PERPLEXITY_SEARCH_URL, PerplexitySearchClient


def _settings(**overrides):
    base = dict(
        fmp_api_key="",
        gemini_api_key="",
        perplexity_api_key="",
        pattern_analog_engine_enabled=True,
        pattern_event_search_enabled=True,
        pattern_stage_wallclock_budget_s=1,
        pattern_cold_ticker_async_backfill=True,
        pattern_backfill_queue_path="",
        pattern_max_peer_count=8,
        pattern_peer_cache_ttl_days=30,
        pattern_min_total_matches=10,
        pattern_max_search_queries_per_catalyst=2,
        pattern_max_events_per_query=3,
        pattern_price_source="fmp",
        perplexity_search_enabled=True,
        perplexity_search_max_requests_per_run=1,
        pattern_event_cache_ttl_days=90,
        memo_threshold=0.55,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _agent(score, status=None, direction="bullish"):
    raw = {}
    if status is not None:
        raw["status"] = status
    return AgentOutput(
        agent_type="test",
        score=score,
        confidence=0.8,
        direction=direction,
        raw_data=raw,
    )


class HistoricalPatternEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        init_db(f"sqlite:///{self.db_path}")

    def tearDown(self):
        self.tmp.cleanup()

    def test_outcome_neutral_query_guard_rejects_biased_terms(self):
        self.assertEqual(assert_outcome_neutral('"AAPL" product launch announcement 2024'), '"AAPL" product launch announcement 2024')
        for query in [
            "AAPL shares rise after product launch",
            "MSFT stock reaction to Copilot",
            "NVDA surged after beating estimates",
            "TSLA worst performing after announcement",
        ]:
            with self.subTest(query=query):
                with self.assertRaises(ValueError):
                    assert_outcome_neutral(query)

    def test_peer_resolver_uses_fmp_fallback_for_non_manual_ticker(self):
        class FakeFmp:
            def get_stock_peers(self, ticker):
                return {"peersList": ["CLOV", "ALHC"]}

        with get_session() as session:
            resolver = PeerResolver(_settings(), manual_peers={}, session_factory=None, fmp_client=FakeFmp())
            result = resolver.resolve("OSCR", session=session)
            self.assertEqual(result["status"], "active")
            self.assertEqual([p["ticker"] for p in result["peers"][:2]], ["CLOV", "ALHC"])

    def test_event_date_must_come_from_content_not_provider_result_date(self):
        extractor = EventExtractor()
        with self.assertRaises(EventValidationError):
            extractor.normalize_candidate(
                {
                    "ticker": "AAPL",
                    "event_type": "product_launch",
                    "event_date": "2023-03-16",
                    "event_date_source": "provider_result",
                    "headline": "Apple product recap",
                    "summary": "Recap article without source date",
                    "source_url": "https://example.com/aapl",
                    "confidence": 0.8,
                },
                provider="perplexity",
                provider_result={"date": "2023-03-16"},
            )

    def test_cross_provider_dedup_merges_sources_and_redacts_payloads(self):
        extractor = EventExtractor()
        base = {
            "ticker": "MSFT",
            "event_type": "product_launch",
            "event_date": "2023-03-16",
            "event_date_source": "content",
            "headline": "Microsoft announces Copilot",
            "summary": "On March 16, 2023, Microsoft announced Copilot.",
            "evidence": "On March 16, 2023, Microsoft announced Copilot.",
            "source_url": "https://news.microsoft.com/copilot?apikey=pplx-abcdefghijklmnopqrstuvwxyz123456",
            "source_type": "company_ir",
            "confidence": 0.9,
        }
        with get_session() as session:
            event1, status1 = extractor.upsert_candidate(session, base, "gemini", "query", {"token": "pplx-abcdefghijklmnopqrstuvwxyz123456"})
            event2, status2 = extractor.upsert_candidate(
                session,
                {**base, "source_url": "https://example.com/story", "source_type": "news", "confidence": 0.7},
                "perplexity",
                "query",
            )
            session.flush()
            self.assertEqual(status1, "created")
            self.assertEqual(status2, "merged")
            self.assertEqual(event1.id, event2.id)
            raw = session.query(HistoricalEvent).one().raw_json
            self.assertIn("https://example.com/story", raw)
            self.assertNotIn("pplx-abcdefghijklmnopqrstuvwxyz123456", raw)

    def test_pit_context_uses_only_financials_filed_before_event(self):
        class FakePriceCache:
            def get_bars(self, ticker, start, end, session=None):
                return [
                    PriceBar(date(2023, 1, 1), 100, 101, 99, 100, 1000),
                    *[
                        PriceBar(date(2023, 1, min(day, 28)), 100 + day, 101 + day, 99 + day, 100 + day, 1000)
                        for day in range(2, 29)
                    ],
                ]

        engine = EventOutcomeEngine(_settings(fmp_api_key="x"), price_cache=FakePriceCache())

        def fake_fmp(endpoint, params):
            if endpoint == "/historical-market-capitalization":
                return [{"date": "2023-01-15", "marketCap": 100.0}]
            if endpoint == "/income-statement":
                return [
                    {"acceptedDate": "2023-02-01", "epsDiluted": 100, "revenue": 9999},
                    {"acceptedDate": "2023-01-10", "epsDiluted": 1, "revenue": 10},
                    {"acceptedDate": "2022-10-10", "epsDiluted": 2, "revenue": 20},
                    {"acceptedDate": "2022-07-10", "epsDiluted": 3, "revenue": 30},
                    {"acceptedDate": "2022-04-10", "epsDiluted": 4, "revenue": 40},
                ]
            if endpoint == "/balance-sheet-statement":
                return [{"acceptedDate": "2023-01-10", "totalDebt": 20, "cashAndCashEquivalents": 5}]
            return []

        engine._fmp_request = fake_fmp
        event = HistoricalEvent(
            ticker="PIT",
            event_type="product_launch",
            event_date=date(2023, 1, 15),
            headline="PIT launch",
            source_url="https://example.com",
            confidence=0.9,
            dedupe_key=make_dedupe_key("PIT", "product_launch", date(2023, 1, 15)),
        )
        with get_session() as session:
            session.add(event)
            session.flush()
            ctx = engine.compute_context(event, session=session)
            self.assertEqual(ctx.valuation_source_filing_date, date(2023, 1, 10))
            self.assertAlmostEqual(ctx.trailing_pe_ratio, 10.0)
            self.assertAlmostEqual(ctx.ev_sales, 1.15)
            self.assertFalse(hasattr(ctx, "fwd_pe_ratio"))
            self.assertFalse(hasattr(ctx, "short_interest_pct_float"))

    def test_missing_historical_market_cap_stops_pit_context(self):
        engine = EventOutcomeEngine(_settings(fmp_api_key="x"))
        engine._fmp_request = lambda endpoint, params: []
        event = HistoricalEvent(ticker="MISS", event_type="product_launch", event_date=date(2022, 1, 1), dedupe_key="x")
        with self.assertRaises(HistoricalMarketCapUnavailable):
            engine._pit_valuation("MISS", date(2022, 1, 1))

    def test_embedding_absent_fallback_ranks_candidate(self):
        with get_session() as session:
            event = HistoricalEvent(
                ticker="MSFT",
                event_type="product_launch",
                event_date=date(2023, 3, 16),
                headline="Microsoft announces Copilot AI product",
                summary="Microsoft announced Copilot on March 16, 2023.",
                source_url="https://news.microsoft.com",
                source_domain="news.microsoft.com",
                source_type="company_ir",
                confidence=0.9,
                dedupe_key=make_dedupe_key("MSFT", "product_launch", date(2023, 3, 16)),
            )
            session.add(event)
            session.flush()
            session.add(EventOutcome(event_id=event.id, ticker="MSFT", return_t10=5.0, return_t20=8.0, status="complete", matured_horizons_json='["t10","t20"]'))
            ranked = AnalogRanker(_settings()).rank(
                session,
                {"target_ticker": "AAPL", "setup_type": "product_launch", "catalyst_summary": "AI product launch"},
                {"peers": [{"ticker": "MSFT", "score": 0.8}]},
            )
            self.assertEqual(ranked["status"], "active")
            self.assertGreater(ranked["top_analogs"][0]["similarity_score"], 0)

    def test_cold_ticker_enqueues_backfill_and_returns_typed_status(self):
        queue_path = Path(self.tmp.name) / "queue.jsonl"
        settings = _settings(pattern_backfill_queue_path=str(queue_path))
        agent = PatternAgent(settings, anthropic_client=None)
        out = agent.analyze(
            "HNGE",
            catalyst_data={"catalyst_type": "product_launch", "catalyst_summary": "new product launch", "direction": "bullish"},
            catalyst_reasoning="new product launch",
        )
        self.assertIn(out.raw_data["status"], {"no_matches", "low_confidence_peers", "insufficient_forward_returns"})
        self.assertTrue(queue_path.exists())
        self.assertIn("HNGE", queue_path.read_text())

    def test_general_catalyst_does_not_route_to_earnings_when_engine_enabled(self):
        settings = _settings()
        agent = PatternAgent(settings, anthropic_client=None)
        with patch.object(agent, "_search_earnings_patterns", side_effect=AssertionError("earnings proxy used")):
            out = agent.analyze("AAPL", catalyst_data={"catalyst_summary": "general good news"})
        self.assertEqual(out.raw_data["status"], "unsupported")

    def test_pattern_raw_data_compatibility_keys_present(self):
        settings = _settings()
        agent = PatternAgent(settings, anthropic_client=None)
        out = agent.analyze("AAPL", catalyst_data={"catalyst_summary": "general good news"})
        self.assertIn("hs_count", out.raw_data)
        self.assertIn("highly_similar_count", out.raw_data)
        self.assertIn("most_similar", out.raw_data)
        self.assertIn("most_similar_instance", out.raw_data)

    def test_scoring_drops_inactive_pattern_weight(self):
        engine = ScoringEngine(_settings(), anthropic_client=None)
        catalyst = _agent(0.85)
        fundamental = _agent(0.65)
        web = _agent(0.75)
        unsupported = _agent(0.5, status="unsupported", direction="neutral")
        neutral_active = _agent(0.5, status="active", direction="neutral")

        unsupported_result = engine.score_opportunity("AAPL", catalyst, fundamental, unsupported, web, regime={})
        neutral_result = engine.score_opportunity("AAPL", catalyst, fundamental, neutral_active, web, regime={})
        expected_absent = round((0.85 * 0.35 + 0.65 * 0.25 + 0.75 * 0.20) / (0.35 + 0.25 + 0.20), 4)
        self.assertAlmostEqual(unsupported_result["raw_score"], expected_absent, places=4)
        self.assertGreaterEqual(unsupported_result["raw_score"], neutral_result["raw_score"])
        self.assertFalse(unsupported_result["signal_breakdown"]["pattern"]["counted"])

    def test_perplexity_search_budget_and_endpoint(self):
        client = PerplexitySearchClient(_settings(perplexity_api_key="pplx-test", perplexity_search_max_requests_per_run=0))
        self.assertEqual(PERPLEXITY_SEARCH_URL, "https://api.perplexity.ai/search")
        with self.assertRaises(RuntimeError):
            client.search("AAPL product launch", max_results=1)

    def test_persisted_search_run_redacts_key_shaped_tokens(self):
        with get_session() as session:
            run = PatternSearchRun(
                run_id="r1",
                ticker="AAPL",
                setup_type="product_launch",
                status="provider_error",
                provider_plan_json=json.dumps({"error": "[REDACTED]"}),
                queries_json=json.dumps(["AAPL product launch"]),
                result_counts_json=json.dumps({"raw": "[REDACTED]"}),
            )
            cache = PatternProviderCache(
                cache_key="k1",
                provider="perplexity_search",
                query="AAPL",
                filters_json=json.dumps({"Authorization": "[REDACTED]"}),
                result_json=json.dumps({"token": "[REDACTED]"}),
            )
            session.add_all([run, cache])
            session.flush()
            blob = run.provider_plan_json + run.queries_json + cache.filters_json + cache.result_json
            self.assertNotIn("pplx-", blob)


if __name__ == "__main__":
    unittest.main()
