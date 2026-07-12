"""Spec I1 — shadow calibration ledger.

Covers: a row per scored candidate with failure isolation; trading-day-aware
horizon maturity; the nightly returns job (matured-only, idempotent); and the
weekly calibration bucket math.
"""

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from database import db as db_module
from database.db import get_session, init_db
from database.models import ScoredCandidate
from data.event_outcomes import PriceBar
from tracking import shadow_ledger
from tracking.shadow_ledger import (
    calibration_report,
    compute_matured_returns,
    matured_horizons,
    record_scored_candidate,
    trading_days_between,
)


def _settings(**overrides):
    base = dict(
        auto_approve_min_score=0.55,
        exploration_min_score=0.45,
        shadow_returns_max_per_run=300,
        fmp_api_key="",
        pattern_price_source="yfinance",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _scoring_result(final=0.60, direction="bullish"):
    return {
        "final_score": final,
        "direction": direction,
        "signal_breakdown": {
            "catalyst": {"score": 0.7, "status": "ok"},
            "fundamental": {"score": 0.5, "status": "ok"},
            "pattern": {"score": 0.4, "status": "active"},
            "web_research": {"score": 0.6, "status": "ok"},
        },
    }


def _business_days(start: date, n: int) -> list[date]:
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


class FakePriceCache:
    """Returns a fixed trading-day bar series so ret_tN == N%."""

    def __init__(self, anchor: date, entry: float = 100.0, count: int = 30):
        self.bars = [
            PriceBar(date=d, open=entry, high=entry, low=entry,
                     close=round(entry * (1 + 0.01 * i), 4), volume=1000)
            for i, d in enumerate(_business_days(anchor, count))
        ]

    def get_bars(self, ticker, start, end, session=None):
        return list(self.bars)


class TradingDayMaturityTests(unittest.TestCase):
    def test_trading_days_between_skips_weekends(self):
        # Mon 2026-02-02 → Fri 2026-02-06 = Tue,Wed,Thu,Fri = 4 trading days.
        self.assertEqual(trading_days_between(date(2026, 2, 2), date(2026, 2, 6)), 4)

    def test_matured_horizons_only_returns_elapsed(self):
        matured = matured_horizons(datetime(2026, 2, 2, 12, 0), today=date(2026, 2, 6))
        self.assertEqual(matured, [1, 3])  # 4 trading days → t1,t3; not t5/t10/t20

    def test_matured_horizons_all_when_far_past(self):
        matured = matured_horizons(datetime(2026, 2, 2), today=date(2026, 4, 1))
        self.assertEqual(matured, [1, 3, 5, 10, 20])


class LedgerWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        init_db(f"sqlite:///{Path(self.tmp.name) / 'ledger.db'}")

    def tearDown(self):
        if db_module.engine is not None:
            db_module.engine.dispose()
        db_module.engine = None
        db_module.SessionLocal = None
        self.tmp.cleanup()

    def test_records_row_with_signal_breakdown_and_cohort(self):
        rid = record_scored_candidate(
            _settings(), run_id="scan-1", ticker="nvda", source="tier2_gemini",
            scoring_result=_scoring_result(final=0.60), regime={"regime": "risk-on"},
            memo_generated=True, entry_price=250.0, suggested_stop=240.0, target_1=270.0,
        )
        self.assertIsNotNone(rid)
        with get_session() as s:
            row = s.query(ScoredCandidate).filter_by(id=rid).first()
            self.assertEqual(row.ticker, "NVDA")
            self.assertEqual(row.cohort, "memo")  # 0.60 >= 0.55
            self.assertEqual(row.regime, "risk-on")
            self.assertEqual(row.catalyst_score, 0.7)
            self.assertEqual(row.pattern_status, "active")
            self.assertEqual(row.entry_price, 250.0)
            self.assertTrue(row.memo_generated)
            self.assertFalse(row.paper_traded)

    def test_cohort_bands(self):
        for final, expected in [(0.70, "memo"), (0.50, "exploration"), (0.30, "below")]:
            rid = record_scored_candidate(
                _settings(), run_id="r", ticker="AAA", source="discovery",
                scoring_result=_scoring_result(final=final), regime={}, memo_generated=False,
                entry_price=10.0,
            )
            with get_session() as s:
                self.assertEqual(s.query(ScoredCandidate).filter_by(id=rid).first().cohort, expected)

    def test_write_failure_is_isolated_returns_none(self):
        # A DB failure must never propagate into the pipeline.
        with patch("tracking.shadow_ledger.get_session", side_effect=RuntimeError("db down")):
            rid = record_scored_candidate(
                _settings(), run_id="r", ticker="AAA", source="x",
                scoring_result=_scoring_result(), regime={}, memo_generated=False, entry_price=10.0,
            )
        self.assertIsNone(rid)


class NightlyReturnsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        init_db(f"sqlite:///{Path(self.tmp.name) / 'returns.db'}")

    def tearDown(self):
        if db_module.engine is not None:
            db_module.engine.dispose()
        db_module.engine = None
        db_module.SessionLocal = None
        self.tmp.cleanup()

    def _seed(self, scored_at: datetime, entry=100.0, ticker="AAA"):
        with get_session() as s:
            row = ScoredCandidate(
                run_id="r", ticker=ticker, scored_at=scored_at, source="tier2_gemini",
                final_score=0.6, direction="bullish", entry_price=entry, cohort="memo",
            )
            s.add(row)
            s.flush()
            return row.id

    def test_fills_only_matured_horizons(self):
        anchor = date(2026, 2, 2)
        # Only 4 trading days elapsed → t1,t3 mature; t5/t10/t20 stay null.
        rid = self._seed(datetime(2026, 2, 2, 12, 0))
        summary = compute_matured_returns(
            _settings(), today=date(2026, 2, 6), price_cache=FakePriceCache(anchor),
        )
        self.assertEqual(summary["rows_updated"], 1)
        with get_session() as s:
            row = s.query(ScoredCandidate).filter_by(id=rid).first()
            self.assertEqual(row.ret_t1, 1.0)
            self.assertEqual(row.ret_t3, 3.0)
            self.assertIsNone(row.ret_t5)
            self.assertIsNone(row.ret_t20)
            self.assertIsNotNone(row.returns_computed_at)

    def test_idempotent_second_run_no_change(self):
        anchor = date(2026, 2, 2)
        rid = self._seed(datetime(2026, 2, 2, 12, 0))
        cache = FakePriceCache(anchor)
        compute_matured_returns(_settings(), today=date(2026, 4, 1), price_cache=cache)
        with get_session() as s:
            first = s.query(ScoredCandidate).filter_by(id=rid).first()
            snapshot = (first.ret_t1, first.ret_t5, first.ret_t20, first.returns_computed_at)
            self.assertEqual((first.ret_t1, first.ret_t5, first.ret_t20), (1.0, 5.0, 20.0))
        # Second run: fully matured + filled → nothing to do.
        summary = compute_matured_returns(_settings(), today=date(2026, 4, 1), price_cache=cache)
        self.assertEqual(summary["rows_updated"], 0)
        with get_session() as s:
            again = s.query(ScoredCandidate).filter_by(id=rid).first()
            self.assertEqual((again.ret_t1, again.ret_t5, again.ret_t20), (1.0, 5.0, 20.0))

    def test_respects_max_per_run(self):
        for i in range(3):
            self._seed(datetime(2026, 2, 2, 12, 0), ticker=f"T{i}")
        summary = compute_matured_returns(
            _settings(), today=date(2026, 4, 1), max_per_run=2,
            price_cache=FakePriceCache(date(2026, 2, 2)),
        )
        self.assertEqual(summary["processed"], 2)

    def test_unmatured_rows_are_skipped(self):
        # Scored today → no horizon matured yet → nothing computed.
        rid = self._seed(datetime.combine(date.today(), datetime.min.time()))
        summary = compute_matured_returns(
            _settings(), today=date.today(), price_cache=FakePriceCache(date.today()),
        )
        self.assertEqual(summary["rows_updated"], 0)
        with get_session() as s:
            self.assertIsNone(s.query(ScoredCandidate).filter_by(id=rid).first().ret_t1)


class CalibrationReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        init_db(f"sqlite:///{Path(self.tmp.name) / 'cal.db'}")
        rows = [
            # (final_score, ret_t10, direction)
            (0.60, 5.0, "bullish"),
            (0.58, -2.0, "bullish"),
            (0.50, 3.0, "bearish"),
            (0.20, None, "bullish"),   # unmatured → excluded
        ]
        with get_session() as s:
            for score, ret, direction in rows:
                s.add(ScoredCandidate(
                    run_id="r", ticker="AAA", scored_at=datetime(2026, 2, 2),
                    final_score=score, direction=direction, ret_t10=ret, cohort="memo",
                ))

    def tearDown(self):
        if db_module.engine is not None:
            db_module.engine.dispose()
        db_module.engine = None
        db_module.SessionLocal = None
        self.tmp.cleanup()

    def test_buckets_and_win_rates(self):
        report = calibration_report()
        self.assertEqual(report["total_matured"], 3)  # None ret_t10 excluded
        buckets = {b["label"]: b for b in report["buckets"]}
        self.assertEqual(buckets["0.55-0.65"]["count"], 2)
        self.assertEqual(buckets["0.55-0.65"]["win_rate"], 50.0)  # one +5, one -2
        self.assertEqual(buckets["0.45-0.55"]["count"], 1)
        self.assertEqual(buckets["0.45-0.55"]["win_rate"], 100.0)
        self.assertEqual(report["by_direction"]["bearish"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
