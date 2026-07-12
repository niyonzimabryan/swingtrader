"""Spec I1/I2 — weekly report calibration section + cohort P&L split.

Also locks the operator-noise guard: exploration-cohort sentinel memos never
inflate the operator-facing memo count.
"""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from database import db as db_module
from database.db import get_session, init_db
from database.models import Memo, ScoredCandidate, Ticker, Trade
from bot.weekly_report import WeeklyReport

ET = ZoneInfo("America/New_York")


class _FakeAlpaca:
    def get_account_info(self):
        return {"equity": 100_000.0, "cash": 50_000.0, "pnl_today": 0.0, "pnl_today_pct": 0.0}

    def get_positions_detail(self):
        return []


def _mid_week_utc() -> datetime:
    et_now = datetime.now(ET)
    days_since_monday = et_now.weekday()
    week_end = et_now.replace(hour=23, minute=59, second=59, microsecond=0) - timedelta(
        days=max(0, days_since_monday - 4)
    )
    week_start = (week_end - timedelta(days=4)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (week_start + timedelta(days=2, hours=12)).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


class WeeklyCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        init_db(f"sqlite:///{Path(self.tmp.name) / 'weekly.db'}")
        self.settings = SimpleNamespace(max_holding_days=20, anthropic_api_key="test-key")
        now = _mid_week_utc()
        with get_session() as s:
            ticker = Ticker(symbol="AAA", sector="Tech", in_universe=True)
            s.add(ticker)
            s.flush()
            tid = ticker.id

            # Matured ledger rows for the calibration curve (window-independent).
            for score, ret in [(0.60, 5.0), (0.58, -2.0), (0.50, 3.0)]:
                s.add(ScoredCandidate(
                    run_id="r", ticker="AAA", scored_at=now, final_score=score,
                    direction="bullish", ret_t10=ret, cohort="memo",
                ))

            # One normal memo + one exploration sentinel in-window.
            s.add(Memo(ticker_id=tid, composite_score=0.6, status="approved", created_at=now))
            s.add(Memo(ticker_id=tid, composite_score=0.5, status="auto_exploration", created_at=now))

            # Closed auto-tagged trades this week for the cohort P&L split.
            s.add(Trade(
                ticker_id=tid, direction="long", entry_price=100.0, exit_price=110.0,
                entry_date=now, exit_date=now, shares=10, status="closed",
                exit_reason="target_1", pnl_absolute=100.0, operator_notes="ORDER_STRATEGY:oto|AUTO:memo",
            ))
            s.add(Trade(
                ticker_id=tid, direction="long", entry_price=50.0, exit_price=48.0,
                entry_date=now, exit_date=now, shares=10, status="closed",
                exit_reason="stop_loss", pnl_absolute=-20.0, operator_notes="ORDER_STRATEGY:oto|AUTO:exploration",
            ))

    def tearDown(self):
        if db_module.engine is not None:
            db_module.engine.dispose()
        db_module.engine = None
        db_module.SessionLocal = None
        self.tmp.cleanup()

    def test_gather_data_includes_calibration_and_cohort(self):
        data = WeeklyReport(_FakeAlpaca(), notification_manager=None, settings=self.settings)._gather_data()

        cal = data["calibration"]
        self.assertEqual(cal["total_matured"], 3)
        buckets = {b["label"]: b for b in cal["buckets"]}
        self.assertEqual(buckets["0.55-0.65"]["count"], 2)
        self.assertEqual(buckets["0.45-0.55"]["count"], 1)

        self.assertEqual(data["cohort_pnl"]["memo"], {"count": 1, "pnl": 100.0})
        self.assertEqual(data["cohort_pnl"]["exploration"], {"count": 1, "pnl": -20.0})

        # Exploration sentinel memo must NOT inflate the operator-facing count.
        self.assertEqual(data["total_memos"], 1)

    def test_format_message_renders_sections(self):
        report = WeeklyReport(_FakeAlpaca(), notification_manager=None, settings=self.settings)
        data = report._gather_data()
        text = report._format_message(data, narrative="")

        self.assertIn("CALIBRATION", text)
        self.assertIn("0.55-0.65", text)
        self.assertIn("AUTO COHORT P&L", text)


if __name__ == "__main__":
    unittest.main()
