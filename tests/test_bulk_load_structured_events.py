from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from data.analog_ranker import (
    AnalogRanker,
    _type_match,
    compatible_event_types,
)
from data.event_discovery import (
    EARNINGS_BEAT_STRUCTURED,
    EARNINGS_MISS_STRUCTURED,
    EVENT_SUPPORTED_TYPES,
)
from data.event_extractor import make_dedupe_key
from database.db import get_session
from tests.dbfixture import init_test_db
from database.models import EventOutcome, HistoricalEvent
from scripts.bulk_load_structured_events import (
    StructuredEventLoader,
    build_earnings_candidate,
    cluster_grade_candidates,
)

TODAY = date(2025, 6, 1)


def _settings(**overrides):
    base = dict(fmp_api_key="", pattern_price_source="fmp")
    base.update(overrides)
    return SimpleNamespace(**base)


def _earnings_row(days_ago: int, actual: float, estimate: float, **extra) -> dict:
    row = {
        "symbol": "AAPL",
        "date": (TODAY - timedelta(days=days_ago)).isoformat(),
        "epsActual": actual,
        "epsEstimated": estimate,
    }
    row.update(extra)
    return row


def _grade_row(days_ago: int, action: str, firm: str = "JPM", new="Buy", prev="Hold") -> dict:
    return {
        "symbol": "AAPL",
        "date": (TODAY - timedelta(days=days_ago)).isoformat(),
        "action": action,
        "gradingCompany": firm,
        "newGrade": new,
        "previousGrade": prev,
    }


class RecordingOutcomeEngine:
    """No-network stand-in for EventOutcomeEngine."""

    def __init__(self):
        self.outcomes = 0
        self.contexts = 0

    def compute_outcome(self, event, session=None):
        self.outcomes += 1

    def compute_context(self, event, session=None, sector=""):
        self.contexts += 1


def _fetch(earnings=None, grades=None):
    def fetch(endpoint, params):
        if endpoint == "/earnings":
            return earnings or []
        if endpoint == "/grades":
            return grades or []
        return []

    return fetch


# --------------------------------------------------------------------------- #
# Pure builder tests (no DB)
# --------------------------------------------------------------------------- #


class EarningsMappingTests(unittest.TestCase):
    def test_beat_maps_to_structured_beat_with_positive_magnitude(self):
        row = _earnings_row(200, actual=2.18, estimate=1.95)
        candidate = build_earnings_candidate("AAPL", row, TODAY, years=2)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["event_type"], EARNINGS_BEAT_STRUCTURED)
        self.assertEqual(candidate["polarity"], "bullish")
        self.assertAlmostEqual(candidate["magnitude"], round((2.18 - 1.95) / 1.95 * 100, 2))
        self.assertGreater(candidate["magnitude"], 0)
        self.assertEqual(candidate["event_date"], (TODAY - timedelta(days=200)).isoformat())
        self.assertEqual(candidate["source_type"], "fmp_structured")
        # Content-derived date must be embedded so the extractor accepts it.
        self.assertIn(candidate["event_date"], candidate["evidence"])

    def test_miss_maps_to_structured_miss_with_negative_magnitude(self):
        row = _earnings_row(200, actual=1.50, estimate=2.00)
        candidate = build_earnings_candidate("AAPL", row, TODAY, years=2)
        self.assertEqual(candidate["event_type"], EARNINGS_MISS_STRUCTURED)
        self.assertEqual(candidate["polarity"], "bearish")
        self.assertLess(candidate["magnitude"], 0)

    def test_magnitude_clamped_for_near_zero_estimate(self):
        row = _earnings_row(200, actual=1.00, estimate=0.01)
        candidate = build_earnings_candidate("AAPL", row, TODAY, years=2)
        self.assertLessEqual(candidate["magnitude"], 100.0)

    def test_zero_estimate_skipped(self):
        self.assertIsNone(build_earnings_candidate("AAPL", _earnings_row(200, 1.0, 0.0), TODAY, 2))

    def test_missing_actual_skipped(self):
        row = {"date": (TODAY - timedelta(days=200)).isoformat(), "epsEstimated": 1.0}
        self.assertIsNone(build_earnings_candidate("AAPL", row, TODAY, 2))

    def test_pit_future_and_today_rejected(self):
        future = _earnings_row(-10, actual=2.0, estimate=1.0)  # 10 days in the future
        today_row = _earnings_row(0, actual=2.0, estimate=1.0)  # exactly today
        self.assertIsNone(build_earnings_candidate("AAPL", future, TODAY, 2))
        self.assertIsNone(build_earnings_candidate("AAPL", today_row, TODAY, 2))

    def test_outside_lookback_window_rejected(self):
        old = _earnings_row(days_ago=365 * 3, actual=2.0, estimate=1.0)
        self.assertIsNone(build_earnings_candidate("AAPL", old, TODAY, years=2))


class UpgradeClusteringTests(unittest.TestCase):
    def test_two_upgrades_in_window_form_one_cluster(self):
        rows = [_grade_row(100, "upgrade", firm="JPM"), _grade_row(98, "upgrade", firm="MS")]
        clusters = cluster_grade_candidates("AAPL", rows, TODAY, years=2)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["event_type"], "analyst_upgrade_cluster")
        self.assertEqual(clusters[0]["polarity"], "bullish")
        self.assertEqual(clusters[0]["magnitude"], 2.0)
        # Dated at the cluster start (earliest action).
        self.assertEqual(clusters[0]["event_date"], (TODAY - timedelta(days=100)).isoformat())

    def test_isolated_single_upgrade_skipped(self):
        rows = [_grade_row(100, "upgrade"), _grade_row(50, "upgrade")]  # 50 days apart
        self.assertEqual(cluster_grade_candidates("AAPL", rows, TODAY, years=2), [])

    def test_downgrade_cluster_maps_to_bearish(self):
        rows = [_grade_row(80, "downgrade", firm="GS"), _grade_row(78, "downgrade", firm="BofA")]
        clusters = cluster_grade_candidates("AAPL", rows, TODAY, years=2)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["event_type"], "analyst_downgrade_cluster")
        self.assertEqual(clusters[0]["polarity"], "bearish")

    def test_mixed_directions_do_not_cross_cluster(self):
        rows = [
            _grade_row(100, "upgrade"),
            _grade_row(99, "downgrade"),
            _grade_row(98, "upgrade"),
        ]
        # One upgrade + one downgrade each within window of the other same-dir action?
        # 2 upgrades (100, 98) within 2 days -> one upgrade cluster; single downgrade skipped.
        clusters = cluster_grade_candidates("AAPL", rows, TODAY, years=2)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["event_type"], "analyst_upgrade_cluster")

    def test_hold_action_ignored(self):
        rows = [_grade_row(100, "hold"), _grade_row(99, "hold")]
        self.assertEqual(cluster_grade_candidates("AAPL", rows, TODAY, years=2), [])


# --------------------------------------------------------------------------- #
# Loader / DB tests
# --------------------------------------------------------------------------- #


class LoaderStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = init_test_db("bulkload")

    def tearDown(self):
        self.db.cleanup()
        self.tmp.cleanup()

    def _loader(self, earnings=None, grades=None, outcome_engine=None):
        return StructuredEventLoader(
            _settings(),
            outcome_engine=outcome_engine,
            fetch=_fetch(earnings=earnings, grades=grades),
        )

    def test_events_stored_with_structured_source_type(self):
        earnings = [_earnings_row(200, 2.18, 1.95), _earnings_row(300, 1.5, 2.0)]
        loader = self._loader(earnings=earnings, outcome_engine=RecordingOutcomeEngine())
        with get_session() as session:
            summary = loader.load(session, ["AAPL"], classes=("earnings",), years=2, today=TODAY)
            source_types = [r.source_type for r in session.query(HistoricalEvent).all()]
        self.assertEqual(summary["events_stored"], 2)
        self.assertEqual(len(source_types), 2)
        # Structured source type must survive validation (not coerced to "other").
        self.assertTrue(all(st == "fmp_structured" for st in source_types))

    def test_outcomes_computed_for_mature_events(self):
        engine = RecordingOutcomeEngine()
        loader = self._loader(earnings=[_earnings_row(200, 2.18, 1.95)], outcome_engine=engine)
        with get_session() as session:
            summary = loader.load(session, ["AAPL"], classes=("earnings",), years=2, today=TODAY)
        self.assertEqual(summary["outcomes_computed"], 1)
        self.assertEqual(engine.outcomes, 1)

    def test_idempotent_rerun_does_not_duplicate(self):
        earnings = [_earnings_row(200, 2.18, 1.95), _earnings_row(300, 1.5, 2.0)]
        grades = [_grade_row(120, "upgrade", firm="JPM"), _grade_row(118, "upgrade", firm="MS")]
        loader = self._loader(earnings=earnings, grades=grades, outcome_engine=RecordingOutcomeEngine())
        with get_session() as session:
            first = loader.load(session, ["AAPL"], years=2, today=TODAY)
        with get_session() as session:
            second = loader.load(session, ["AAPL"], years=2, today=TODAY)
            count = session.query(HistoricalEvent).count()
        self.assertEqual(first["events_stored"], 3)  # 2 earnings + 1 upgrade cluster
        self.assertEqual(second["events_stored"], 0)
        self.assertEqual(second["skipped_dupes"], 3)
        self.assertEqual(count, 3)

    def test_dry_run_stores_nothing(self):
        earnings = [_earnings_row(200, 2.18, 1.95)]
        grades = [_grade_row(120, "upgrade", firm="JPM"), _grade_row(118, "upgrade", firm="MS")]
        loader = self._loader(earnings=earnings, grades=grades)
        with get_session() as session:
            summary = loader.load(session, ["AAPL"], years=2, dry_run=True, today=TODAY)
            count = session.query(HistoricalEvent).count()
        self.assertEqual(count, 0)
        self.assertEqual(summary["events_stored"], 0)
        self.assertEqual(summary["would_store"], 2)

    def test_pit_future_and_today_not_stored(self):
        earnings = [_earnings_row(-5, 2.0, 1.0), _earnings_row(0, 2.0, 1.0), _earnings_row(200, 2.0, 1.0)]
        loader = self._loader(earnings=earnings, outcome_engine=RecordingOutcomeEngine())
        with get_session() as session:
            summary = loader.load(session, ["AAPL"], classes=("earnings",), years=2, today=TODAY)
            count = session.query(HistoricalEvent).count()
        self.assertEqual(count, 1)  # only the 200-days-ago row
        self.assertEqual(summary["events_stored"], 1)


# --------------------------------------------------------------------------- #
# H2 taxonomy / fallback-matching tests
# --------------------------------------------------------------------------- #


class TaxonomyFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = init_test_db("bulkload")

    def tearDown(self):
        self.db.cleanup()
        self.tmp.cleanup()

    def test_structured_types_are_supported(self):
        self.assertIn(EARNINGS_BEAT_STRUCTURED, EVENT_SUPPORTED_TYPES)
        self.assertIn(EARNINGS_MISS_STRUCTURED, EVENT_SUPPORTED_TYPES)

    def test_compatible_types_add_fallback_for_guidance_classes(self):
        self.assertEqual(
            compatible_event_types("earnings_beat_guide_up"),
            ["earnings_beat_guide_up", EARNINGS_BEAT_STRUCTURED],
        )
        self.assertEqual(compatible_event_types("earnings_miss"), ["earnings_miss", EARNINGS_MISS_STRUCTURED])
        # Non-earnings classes unchanged.
        self.assertEqual(compatible_event_types("product_launch"), ["product_launch"])

    def test_type_match_discounts_structured_fallback(self):
        self.assertEqual(_type_match("earnings_beat_guide_up", "earnings_beat_guide_up"), 1.0)
        self.assertEqual(_type_match("earnings_beat_guide_up", EARNINGS_BEAT_STRUCTURED), 0.85)
        self.assertEqual(_type_match("earnings_beat_guide_up", "product_launch"), 0.0)

    def _store_event(self, session, event_type, ticker="AAPL", days_ago=200):
        event_date = date.today() - timedelta(days=days_ago)
        event = HistoricalEvent(
            ticker=ticker,
            event_type=event_type,
            event_date=event_date,
            event_subtype="earnings_surprise",
            headline="AAPL earnings beat estimates",
            summary="AAPL reported an EPS beat.",
            source_url="https://financialmodelingprep.com/stable/earnings?symbol=AAPL",
            source_type="fmp_structured",
            confidence=0.9,
            dedupe_key=make_dedupe_key(ticker, event_type, event_date),
        )
        session.add(event)
        session.flush()
        session.add(
            EventOutcome(
                event_id=event.id,
                ticker=ticker,
                return_t10=6.0,
                return_t20=9.0,
                status="complete",
                matured_horizons_json='["t10","t20"]',
            )
        )
        return event

    def test_guidance_request_finds_structured_analog(self):
        with get_session() as session:
            self._store_event(session, EARNINGS_BEAT_STRUCTURED)
            ranked = AnalogRanker(_settings()).rank(
                session,
                {"target_ticker": "AAPL", "setup_type": "earnings_beat_guide_up", "catalyst_summary": "earnings beat"},
                {"peers": []},
            )
        self.assertEqual(ranked["status"], "active")
        self.assertEqual(len(ranked["top_analogs"]), 1)
        self.assertEqual(ranked["top_analogs"][0]["event_type"], EARNINGS_BEAT_STRUCTURED)

    def test_exact_class_ranks_above_structured_fallback(self):
        with get_session() as session:
            self._store_event(session, "earnings_beat_guide_up", days_ago=200)
            self._store_event(session, EARNINGS_BEAT_STRUCTURED, days_ago=200)
            ranked = AnalogRanker(_settings()).rank(
                session,
                {"target_ticker": "AAPL", "setup_type": "earnings_beat_guide_up", "catalyst_summary": "earnings beat"},
                {"peers": []},
            )
        by_type = {a["event_type"]: a["similarity_score"] for a in ranked["top_analogs"]}
        self.assertIn("earnings_beat_guide_up", by_type)
        self.assertIn(EARNINGS_BEAT_STRUCTURED, by_type)
        self.assertGreater(by_type["earnings_beat_guide_up"], by_type[EARNINGS_BEAT_STRUCTURED])

    def test_structured_types_never_offered_to_live_classifier(self):
        from unittest.mock import patch

        from agents.pattern_agent import PatternAgent

        captured = {}

        class FakeClient:
            def analyze_json(self, model, system, prompt, max_tokens=300):
                captured["prompt"] = prompt
                return {"setup_type": "earnings_beat_guide_up", "confidence": 0.9}

        agent = PatternAgent(
            _settings(fmp_api_key="", pattern_analog_engine_enabled=True), anthropic_client=FakeClient()
        )
        with patch("agents.pattern_agent.get_model", return_value="m"):
            agent._decompose_setup("AAPL", "general_positive_catalyst", {"catalyst_summary": "beat"}, "beat")
        # Guidance-specific classes remain offered; structured classes never do.
        self.assertIn("earnings_beat_guide_up", captured["prompt"])
        self.assertNotIn(EARNINGS_BEAT_STRUCTURED, captured["prompt"])
        self.assertNotIn(EARNINGS_MISS_STRUCTURED, captured["prompt"])

    def test_non_earnings_class_unaffected(self):
        with get_session() as session:
            # A structured earnings event must NOT surface for a product_launch request.
            self._store_event(session, EARNINGS_BEAT_STRUCTURED)
            ranked = AnalogRanker(_settings()).rank(
                session,
                {"target_ticker": "AAPL", "setup_type": "product_launch", "catalyst_summary": "launch"},
                {"peers": []},
            )
        self.assertEqual(ranked["status"], "no_matches")


if __name__ == "__main__":
    unittest.main()
