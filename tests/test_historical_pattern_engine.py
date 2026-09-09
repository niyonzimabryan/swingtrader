from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents.base_agent import AgentOutput
from agents.pattern_agent import PatternAgent
from config.settings import resolve_backfill_queue_path
from data.analog_ranker import AnalogRanker
from data.event_discovery import EventDiscoveryEngine, assert_outcome_neutral
from data.event_extractor import EventExtractor, EventValidationError, make_dedupe_key
from data.event_outcomes import EventOutcomeEngine, HistoricalMarketCapUnavailable, PriceBar, is_event_mature
from data.peer_resolver import PeerResolver
from database.db import get_session
from database.models import EventContext, EventOutcome, HistoricalEvent, PatternProviderCache, PatternSearchRun
from tests.dbfixture import init_test_db
from memo.templates.ic_memo import format_memo_plain
from scoring.engine import ScoringEngine
from scripts.backfill_historical_events import drain_queue
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
        self.db = init_test_db("patterns")

    def tearDown(self):
        self.db.cleanup()
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

    def test_compute_outcome_idempotent_with_pending_unflushed_outcome(self):
        """Double-compute (inline + backfill loop) must not violate the event_id
        UNIQUE constraint. App sessions run autoflush=False, so the second call
        must see the first call's PENDING outcome (prod crash 2026-07-09)."""
        class FakePriceCache:
            def get_bars(self, ticker, start, end, session=None):
                return [
                    PriceBar(date(2023, 1, day), 100 + day, 101 + day, 99 + day, 100 + day, 1000)
                    for day in range(1, 28)
                ]

        engine = EventOutcomeEngine(_settings(fmp_api_key=""), price_cache=FakePriceCache())
        event = HistoricalEvent(
            ticker="DUPE",
            event_type="product_launch",
            event_date=date(2023, 1, 5),
            headline="Dupe launch",
            source_url="https://example.com",
            confidence=0.9,
            dedupe_key=make_dedupe_key("DUPE", "product_launch", date(2023, 1, 5)),
        )
        with get_session() as session:
            session.add(event)
            session.flush()
            first = engine.compute_outcome(event, session=session)
            # No flush in between — second compute must find the pending row.
            second = engine.compute_outcome(event, session=session)
            self.assertIs(first, second)
            session.flush()  # would raise IntegrityError before the fix
            count = session.query(EventOutcome).filter_by(event_id=event.id).count()
            self.assertEqual(count, 1)

    def test_price_cache_write_idempotent_with_pending_unflushed_row(self):
        """Two outcome computations sharing a price window must not double-insert
        the provider-cache row (prod crash 2026-07-09: UNIQUE pattern_provider_cache.cache_key)."""
        from data.event_outcomes import PriceHistoryCache

        cache = PriceHistoryCache(_settings(fmp_api_key=""))
        payload = [{"date": "2023-01-05", "close": 100.0}]
        cache._fetch_yfinance = lambda ticker, start, end: payload
        with get_session() as session:
            bars1 = cache.get_bars("DUPE", date(2023, 1, 1), date(2023, 1, 31), session=session)
            # No flush in between — second read must hit the PENDING cache row.
            bars2 = cache.get_bars("DUPE", date(2023, 1, 1), date(2023, 1, 31), session=session)
            self.assertEqual(len(bars1), 1)
            self.assertEqual(len(bars2), 1)
            session.flush()  # would raise IntegrityError before the fix
            count = session.query(PatternProviderCache).filter(
                PatternProviderCache.cache_key.like("price:%DUPE%")
            ).count()
            self.assertEqual(count, 1)

    def test_extractor_dedupes_pending_unflushed_event_in_same_batch(self):
        """The same candidate stored twice in one run (before any flush) must merge
        into one pending event, not violate the dedupe_key UNIQUE constraint."""
        extractor = EventExtractor()
        candidate = {
            "ticker": "MSFT",
            "event_type": "product_launch",
            "event_date": "2023-03-16",
            "event_date_source": "content",
            "event_timing": "unknown",
            "polarity": "bullish",
            "magnitude": 0.7,
            "headline": "Microsoft announces Copilot",
            "summary": "On March 16, 2023, Microsoft announced Copilot.",
            "evidence": "On March 16, 2023, Microsoft announced Copilot.",
            "source_url": "https://news.microsoft.com/copilot",
            "confidence": 0.9,
            "_provider": "gemini",
            "_provider_result": {"grounded": True, "queries": ["q"], "sources": ["s"]},
        }
        with get_session() as session:
            e1, s1 = extractor.upsert_candidate(session, dict(candidate), "gemini", "q", {})
            e2, s2 = extractor.upsert_candidate(session, dict(candidate), "gemini", "q", {})
            session.flush()  # would raise IntegrityError on dedupe_key if dedupe missed
            count = session.query(HistoricalEvent).filter_by(ticker="MSFT").count()
            self.assertEqual(count, 1)

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
        # time_budget_exhausted is a valid typed status here: on slow CI runners the
        # 45s pattern wall-clock budget can expire before discovery is attempted.
        self.assertIn(
            out.raw_data["status"],
            {"no_matches", "low_confidence_peers", "insufficient_forward_returns", "time_budget_exhausted"},
        )
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

    # ── A1: LEFT-JOIN partial ranking ─────────────────────────────

    def _add_event(self, session, ticker, event_date, source_type="company_ir", event_type="product_launch"):
        event = HistoricalEvent(
            ticker=ticker,
            event_type=event_type,
            event_date=event_date,
            headline=f"{ticker} {event_type} on {event_date}",
            summary=f"{ticker} {event_type}.",
            source_url=f"https://example.com/{ticker}/{event_date}",
            source_domain="example.com",
            source_type=source_type,
            confidence=0.9,
            dedupe_key=make_dedupe_key(ticker, event_type, event_date),
        )
        session.add(event)
        session.flush()
        return event

    def test_outcomeless_events_surface_as_partial_not_hidden(self):
        # Regression for the INNER-JOIN bug: an event with NO EventOutcome row must
        # still rank (as partial), not vanish → 'no_matches'.
        with get_session() as session:
            self._add_event(session, "MSFT", date(2023, 3, 16))
            ranked = AnalogRanker(_settings()).rank(
                session,
                {"target_ticker": "MSFT", "setup_type": "product_launch", "catalyst_summary": "product launch"},
                {"peers": []},
            )
        self.assertEqual(ranked["summary_stats"]["total_instances"], 1)
        self.assertEqual(ranked["status"], "insufficient_forward_returns")
        self.assertEqual(ranked["top_analogs"][0]["outcome_status"], "partial")
        self.assertIsNone(ranked["top_analogs"][0]["return_t10"])

    def test_left_join_counts_mature_and_immature_together(self):
        with get_session() as session:
            mature = self._add_event(session, "MSFT", date(2023, 3, 16))
            self._add_event(session, "MSFT", date(2023, 6, 1))  # outcome-less
            session.add(
                EventOutcome(
                    event_id=mature.id, ticker="MSFT", return_t10=5.0, return_t20=8.0,
                    status="complete", matured_horizons_json='["t10","t20"]',
                )
            )
            session.flush()
            ranked = AnalogRanker(_settings()).rank(
                session,
                {"target_ticker": "MSFT", "setup_type": "product_launch", "catalyst_summary": "product launch"},
                {"peers": []},
            )
        self.assertEqual(ranked["status"], "active")  # >=1 matured horizon
        self.assertEqual(ranked["summary_stats"]["total_instances"], 2)  # both counted
        statuses = {a["outcome_status"] for a in ranked["top_analogs"]}
        self.assertIn("partial", statuses)
        self.assertIn("complete", statuses)

    # ── A1: inline outcome computation cap + maturity ─────────────

    def test_inline_outcome_computation_caps_and_skips_immature(self):
        class _CountingOutcomeEngine:
            def __init__(self):
                self.calls = []

            def compute_outcome(self, event, session=None):
                self.calls.append(event.ticker)

        counter = _CountingOutcomeEngine()
        engine = EventDiscoveryEngine(
            _settings(pattern_inline_outcome_max_per_scan=2), outcome_engine=counter
        )
        old = date.today() - timedelta(days=400)
        recent = date.today()
        events = [SimpleNamespace(ticker="RECENT", event_date=recent)] + [
            SimpleNamespace(ticker=f"M{i}", event_date=old) for i in range(5)
        ]
        computed = engine._compute_inline_outcomes(session=None, events=events)
        self.assertEqual(computed, 2)  # capped
        self.assertEqual(len(counter.calls), 2)
        self.assertNotIn("RECENT", counter.calls)  # immature skipped
        self.assertTrue(is_event_mature(old))
        self.assertFalse(is_event_mature(recent))

    def test_inline_outcome_disabled_when_cap_zero(self):
        class _Boom:
            def compute_outcome(self, event, session=None):
                raise AssertionError("should not compute when cap is 0")

        engine = EventDiscoveryEngine(_settings(pattern_inline_outcome_max_per_scan=0), outcome_engine=_Boom())
        events = [SimpleNamespace(ticker="M", event_date=date.today() - timedelta(days=400))]
        self.assertEqual(engine._compute_inline_outcomes(session=None, events=events), 0)

    # ── A2: backfill queue path resolution + consumer drain ───────

    def test_backfill_queue_path_resolves_to_db_dir(self):
        settings = _settings(
            database_url=f"sqlite:///{self.tmp.name}/sub/swing_trader.db",
            pattern_backfill_queue_path=".pattern_backfill_queue.jsonl",
        )
        resolved = resolve_backfill_queue_path(settings)
        self.assertEqual(str(resolved), f"{self.tmp.name}/sub/.pattern_backfill_queue.jsonl")
        # Absolute paths are preserved regardless of the DB dir.
        abs_path = f"{self.tmp.name}/abs.jsonl"
        settings_abs = _settings(database_url="sqlite:///whatever.db", pattern_backfill_queue_path=abs_path)
        self.assertEqual(str(resolve_backfill_queue_path(settings_abs)), abs_path)

    def test_drain_queue_consumes_capped_batch_and_keeps_remainder(self):
        queue_path = Path(self.tmp.name) / "queue.jsonl"
        entries = [
            {"ticker": t, "setup_type": "product_launch", "catalyst_summary": "launch"}
            for t in ("AAA", "BBB", "CCC")
        ]
        queue_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        # No provider keys → discovery is a no-op; this exercises queue mechanics only.
        settings = _settings(
            pattern_backfill_queue_path=str(queue_path),
            pattern_backfill_max_tickers_per_run=2,
            gemini_api_key="",
            perplexity_api_key="",
        )
        summary = drain_queue(settings)
        self.assertEqual(summary["tickers"], 2)
        remaining = [line for line in queue_path.read_text().splitlines() if line.strip()]
        self.assertEqual(len(remaining), 1)
        self.assertIn("CCC", remaining[0])

    def test_drain_queue_dedupes_repeated_enqueues(self):
        # Cold tickers re-enqueue every scan; duplicates must not eat the cap.
        queue_path = Path(self.tmp.name) / "queue_dupes.jsonl"
        entries = [
            {"ticker": "AAA", "setup_type": "product_launch", "catalyst_summary": "launch"},
            {"ticker": "AAA", "setup_type": "product_launch", "catalyst_summary": "launch"},
            {"ticker": "BBB", "setup_type": "m_and_a", "catalyst_summary": "deal"},
            {"ticker": "AAA", "setup_type": "product_launch", "catalyst_summary": "launch"},
            {"ticker": "CCC", "setup_type": "m_and_a", "catalyst_summary": "deal"},
        ]
        queue_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        settings = _settings(
            pattern_backfill_queue_path=str(queue_path),
            pattern_backfill_max_tickers_per_run=2,
            gemini_api_key="",
            perplexity_api_key="",
        )
        summary = drain_queue(settings)
        # Cap of 2 drains the two unique keys AAA+BBB, not AAA twice.
        self.assertEqual(summary["tickers"], 2)
        remaining = [line for line in queue_path.read_text().splitlines() if line.strip()]
        self.assertEqual(len(remaining), 1)
        self.assertIn("CCC", remaining[0])

    # ── A3: grounding + future-date guards ────────────────────────

    def _discovery_request(self, ticker="MSFT"):
        return {"target_ticker": ticker, "setup_type": "product_launch", "catalyst_summary": "product launch", "peers": []}

    def _valid_event_candidate(self):
        return {
            "ticker": "MSFT",
            "event_type": "product_launch",
            "event_date": "2023-03-16",
            "event_date_source": "content",
            "event_timing": "unknown",
            "polarity": "bullish",
            "magnitude": 0.7,
            "headline": "Microsoft announces Copilot",
            "summary": "On March 16, 2023, Microsoft announced Copilot.",
            "evidence": "On March 16, 2023, Microsoft announced Copilot.",
            "source_url": "https://news.microsoft.com/copilot",
            "source_type": "company_ir",
            "confidence": 0.9,
        }

    def test_ungrounded_gemini_call_rejects_all_candidates(self):
        candidate = self._valid_event_candidate()

        class _FakeWeb:
            def search_and_analyze_json_with_grounding(self, *a, **k):
                # Even though the model returned an event, an ungrounded search is not evidence.
                return {"_grounding": {"grounded": False, "queries": [], "sources": []}, "events": [dict(candidate)]}

        engine = EventDiscoveryEngine(
            _settings(pattern_max_search_queries_per_catalyst=1, pattern_inline_outcome_max_per_scan=0),
            web_search_client=_FakeWeb(),
        )
        with get_session() as session:
            out = engine.discover_and_store(session, self._discovery_request(), run_id="ung")
            self.assertEqual(out["status"], "no_matches")
            self.assertEqual(len(out["events"]), 0)
            self.assertEqual(session.query(HistoricalEvent).count(), 0)

    def test_grounded_gemini_call_stores_event(self):
        candidate = self._valid_event_candidate()

        class _FakeWeb:
            def search_and_analyze_json_with_grounding(self, *a, **k):
                return {
                    "_grounding": {"grounded": True, "queries": ["q"], "sources": [{"title": "t", "uri": "https://news.microsoft.com"}]},
                    "events": [dict(candidate)],
                }

        engine = EventDiscoveryEngine(
            _settings(pattern_max_search_queries_per_catalyst=1, pattern_inline_outcome_max_per_scan=0),
            web_search_client=_FakeWeb(),
        )
        with get_session() as session:
            out = engine.discover_and_store(session, self._discovery_request(), run_id="grd")
            self.assertEqual(out["status"], "active")
            self.assertEqual(session.query(HistoricalEvent).count(), 1)

    def test_future_dated_event_is_rejected(self):
        future = date.today() + timedelta(days=30)
        candidate = {
            "ticker": "CPRT",
            "event_type": "product_launch",
            "event_date": future.isoformat(),
            "event_date_source": "content",
            "headline": f"CPRT event dated {future.isoformat()}",
            "summary": f"On {future.isoformat()} CPRT will do something.",
            "evidence": f"On {future.isoformat()} CPRT will do something.",
            "source_url": "https://example.com/cprt",
            "source_type": "news",
            "confidence": 0.9,
        }
        with self.assertRaises(EventValidationError) as ctx:
            EventExtractor().normalize_candidate(candidate, provider="gemini")
        self.assertEqual(str(ctx.exception), "future_date")

    # ── A5: typed provider_error + time_budget_exhausted ──────────

    def test_gemini_provider_exception_surfaces_provider_error(self):
        class _RaisingWeb:
            def search_and_analyze_json_with_grounding(self, *a, **k):
                raise RuntimeError("boom pplx-abcdefghijklmnopqrstuvwxyz123456")

        engine = EventDiscoveryEngine(
            _settings(pattern_max_search_queries_per_catalyst=1, pattern_inline_outcome_max_per_scan=0),
            web_search_client=_RaisingWeb(),
        )
        with get_session() as session:
            out = engine.discover_and_store(session, {"target_ticker": "AAPL", "setup_type": "product_launch", "catalyst_summary": "launch", "peers": []}, run_id="err")
            self.assertEqual(out["status"], "provider_error")
            run = session.query(PatternSearchRun).filter_by(run_id="err").one()
            self.assertEqual(run.status, "provider_error")
            blob = (run.error or "") + run.result_counts_json + run.provider_plan_json
            self.assertNotIn("pplx-abcdefghijklmnopqrstuvwxyz123456", blob)

    def test_scoring_drops_provider_error_and_budget_statuses(self):
        engine = ScoringEngine(_settings(), anthropic_client=None)
        catalyst = _agent(0.85)
        fundamental = _agent(0.65)
        web = _agent(0.75)
        expected_absent = round((0.85 * 0.35 + 0.65 * 0.25 + 0.75 * 0.20) / (0.35 + 0.25 + 0.20), 4)
        for status in ("provider_error", "time_budget_exhausted"):
            with self.subTest(status=status):
                # score 0.2 would drag the composite down if it were counted.
                pattern = _agent(0.2, status=status, direction="neutral")
                result = engine.score_opportunity("AAPL", catalyst, fundamental, pattern, web, regime={})
                self.assertAlmostEqual(result["raw_score"], expected_absent, places=4)
                self.assertFalse(result["signal_breakdown"]["pattern"]["counted"])

    def test_time_budget_exhausted_status_renders_in_memo(self):
        memo_data = {
            "ticker": "ACME",
            "direction": "long",
            "composite_score": 0.7,
            "classification": "moderate",
            "generated_at": "2026-07-08T12:00",
            "thesis": "t",
            "catalyst": {"catalyst_type": "product", "catalyst_summary": "launch", "materiality": 0.6, "direction_confidence": 0.6},
            "fundamental": {"quality_score": 0.7, "valuation_score": 0.6, "growth_score": 0.6, "balance_sheet_score": 0.7},
            "pattern": {"status": "time_budget_exhausted", "reasoning": "budget spent"},
            "web_research": {"status": "stub"},
            "opus_evaluation": {"recommendation": "pass", "conviction": "low", "key_risk": "x", "stress_test": "ok", "reasoning": "r"},
        }
        out = format_memo_plain(memo_data)
        self.assertIn("hit the live time budget", out)

    # ── A4: FMP screener endpoint rename ──────────────────────────

    def test_fmp_screener_uses_company_screener_endpoint(self):
        calls = []

        def fake_fmp(endpoint, params=None):
            calls.append(endpoint)
            if endpoint == "/company-screener":
                return [{"symbol": "PEER1", "sector": "Technology", "industry": "Software", "marketCap": 1e9, "exchangeShortName": "NASDAQ"}]
            return None  # old /stock-screener path 404s → None

        resolver = PeerResolver(_settings(fmp_api_key="x"), manual_peers={}, session_factory=None)
        resolver._fmp_request = fake_fmp
        profile = {"sector": "Technology", "industry": "Software", "exchangeShortName": "NASDAQ", "mktCap": 1e9}
        peers = resolver._fmp_screener_peers("AAA", profile)
        self.assertIn("/company-screener", calls)
        self.assertNotIn("/stock-screener", calls)
        self.assertTrue(any(p.ticker == "PEER1" for p in peers))

    def test_fmp_screener_404_degrades_gracefully(self):
        resolver = PeerResolver(_settings(fmp_api_key="x"), manual_peers={}, session_factory=None)
        resolver._fmp_request = lambda endpoint, params=None: None  # simulate 404 → None
        profile = {"sector": "Technology", "industry": "Software", "exchangeShortName": "NASDAQ", "mktCap": 1e9}
        self.assertEqual(resolver._fmp_screener_peers("AAA", profile), [])


if __name__ == "__main__":
    unittest.main()
