"""Integration: _process_scan_item wiring (I0 funnel flags + I1 ledger row).

Proves a scored candidate produces exactly one ledger row with the right cohort,
and that the catalyst gate short-circuits before scoring (no ledger row).
"""

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from database import db as db_module
from database.db import get_session, init_db
from database.models import ScoredCandidate
from data.event_outcomes import PriceBar
from orchestrator.pipeline import ScanTickerItem, TradingPipeline


class _Agent:
    def __init__(self, score=0.8, direction="bullish"):
        self.score = score
        self.direction = direction
        self.confidence = 0.8
        self.reasoning = ""
        self.raw_data = {}


class _FakePriceCache:
    def get_bars(self, ticker, start, end, session=None):
        return [PriceBar(date=date(2026, 2, 2), open=10, high=10, low=10, close=10.0, volume=1)]


def _result(final, meets):
    return {
        "final_score": final,
        "direction": "bullish",
        "meets_memo_threshold": meets,
        "signal_breakdown": {
            "catalyst": {"score": 0.7, "status": "ok"},
            "fundamental": {"score": 0.5, "status": "ok"},
            "pattern": {"score": 0.4, "status": "active"},
            "web_research": {"score": 0.6, "status": "ok"},
        },
    }


class ProcessScanItemLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        init_db(f"sqlite:///{Path(self.tmp.name) / 'scan.db'}")
        p = TradingPipeline.__new__(TradingPipeline)
        p.settings = SimpleNamespace(
            memo_threshold=0.55, auto_approve_min_score=0.55, exploration_min_score=0.45,
        )
        p._price_cache = _FakePriceCache()
        p.deep_research_agent = None
        p._ensure_ticker = lambda ticker: None
        p._get_portfolio_context = lambda: ""
        p._run_post_catalyst_agents = lambda **kw: (_Agent(), _Agent(), _Agent(), {}, 1)
        self.pipeline = p

    def tearDown(self):
        if db_module.engine is not None:
            db_module.engine.dispose()
        db_module.engine = None
        db_module.SessionLocal = None
        self.tmp.cleanup()

    def _item(self):
        return ScanTickerItem(ticker="NVDA", sector="Tech", source="tier2_gemini", haiku_threshold=0)

    def test_catalyst_gate_fail_writes_no_ledger_row(self):
        self.pipeline.catalyst_agent = SimpleNamespace(analyze=lambda **kw: _Agent(score=0.1))
        outcome = self.pipeline._process_scan_item(self._item(), regime={}, run_id="scan-1")
        self.assertFalse(outcome.catalyst_gate_passed)
        self.assertFalse(outcome.scored)
        self.assertIsNone(outcome.ledger_id)
        with get_session() as s:
            self.assertEqual(s.query(ScoredCandidate).count(), 0)

    def test_scored_memo_candidate_writes_ledger_and_memo(self):
        self.pipeline.catalyst_agent = SimpleNamespace(analyze=lambda **kw: _Agent(score=0.8))
        self.pipeline.scoring_engine = SimpleNamespace(
            score_opportunity=lambda *a, **k: _result(0.62, True)
        )
        self.pipeline.memo_generator = SimpleNamespace(
            generate=lambda *a, **k: {
                "memo_id": 7, "composite_score": 0.62,
                "trade_params": {"entry_price": 250.0, "stop_loss": 240.0, "target_1": 270.0},
                "opus_evaluation": {},
            }
        )
        outcome = self.pipeline._process_scan_item(self._item(), regime={"regime": "risk-on"}, run_id="scan-1")

        self.assertTrue(outcome.catalyst_gate_passed)
        self.assertTrue(outcome.scored)
        self.assertEqual(outcome.cohort, "memo")
        self.assertEqual(outcome.memo_id, 7)
        self.assertIsNotNone(outcome.ledger_id)
        with get_session() as s:
            row = s.query(ScoredCandidate).filter_by(id=outcome.ledger_id).first()
            self.assertEqual(row.ticker, "NVDA")
            self.assertEqual(row.cohort, "memo")
            self.assertEqual(row.entry_price, 250.0)  # from memo trade_params, no price fetch
            self.assertTrue(row.memo_generated)
            self.assertEqual(row.run_id, "scan-1")

    def test_scored_below_threshold_records_ledger_without_memo(self):
        self.pipeline.catalyst_agent = SimpleNamespace(analyze=lambda **kw: _Agent(score=0.8))
        self.pipeline.scoring_engine = SimpleNamespace(
            score_opportunity=lambda *a, **k: _result(0.30, False)
        )
        self.pipeline.memo_generator = SimpleNamespace(generate=lambda *a, **k: {})
        outcome = self.pipeline._process_scan_item(self._item(), regime={}, run_id="scan-2")

        self.assertTrue(outcome.scored)
        self.assertIsNone(outcome.memo_id)
        self.assertEqual(outcome.cohort, "below")
        with get_session() as s:
            row = s.query(ScoredCandidate).filter_by(id=outcome.ledger_id).first()
            self.assertFalse(row.memo_generated)
            self.assertEqual(row.cohort, "below")
            # entry price resolved from the (fake) price cache close.
            self.assertEqual(row.entry_price, 10.0)


if __name__ == "__main__":
    unittest.main()
