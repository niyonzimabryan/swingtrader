import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from bot.daily_digest import DailyDigest
from bot.weekly_report import WeeklyReport
from database import db as db_module
from database.db import get_session, init_db
from database.models import Memo, Ticker, Trade
from execution.order_manager import OrderManager
from orchestrator.pipeline import TradingPipeline
from screening.gemini_screener import GeminiBatchResult, GeminiScreenResult


class PipelineScanListTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = TradingPipeline.__new__(TradingPipeline)
        self.pipeline.settings = SimpleNamespace(
            watchlist_haiku_threshold=2,
            catalyst_escalation_threshold=3,
        )

    def test_build_scan_list_excludes_screened_non_escalated_and_keeps_unscreened_fallback(self):
        structured_result = SimpleNamespace(
            flagged=[
                SimpleNamespace(symbol="AAPL", catalysts=["earnings"], change_pct=None, volume_ratio=None, earnings_date=None, sector="Technology"),
                SimpleNamespace(symbol="MSFT", catalysts=["guidance"], change_pct=None, volume_ratio=None, earnings_date=None, sector="Technology"),
                SimpleNamespace(symbol="NVDA", catalysts=["momentum"], change_pct=None, volume_ratio=None, earnings_date=None, sector="Technology"),
            ]
        )
        gemini_result = GeminiBatchResult(
            results=[
                GeminiScreenResult(ticker="AAPL", score=0.32, summary="Not compelling", escalate=False),
                GeminiScreenResult(ticker="MSFT", score=0.81, summary="High conviction", escalate=True),
            ],
            escalated=["MSFT"],
            total_screened=2,
        )
        discovery_output = SimpleNamespace(tickers=[])

        with patch("orchestrator.pipeline.get_watchlist", return_value=[]):
            scan_list = TradingPipeline._build_scan_list(
                self.pipeline,
                discovery_output,
                structured_result,
                gemini_result,
            )

        self.assertEqual([item.ticker for item in scan_list], ["MSFT", "NVDA"])
        self.assertEqual(scan_list[0].source, "tier2_gemini")
        self.assertEqual(scan_list[1].source, "tier1_scan")

    def test_build_scan_list_skips_universe_fallback_when_disabled(self):
        discovery_output = SimpleNamespace(tickers=[])
        structured_result = SimpleNamespace(flagged=[])

        with patch("orchestrator.pipeline.get_watchlist", return_value=[]):
            scan_list = TradingPipeline._build_scan_list(
                self.pipeline,
                discovery_output,
                structured_result,
                GeminiBatchResult(),
                allow_universe_fallback=False,
            )

        self.assertEqual(scan_list, [])


class _FakeAlpaca:
    def get_account_info(self):
        return {
            "equity": 100_000.0,
            "cash": 50_000.0,
            "pnl_today": 250.0,
            "pnl_today_pct": 0.25,
        }

    def get_positions_detail(self):
        return [
            {
                "ticker": "AAPL",
                "qty": 10,
                "entry_price": 100.0,
                "current_price": 105.0,
                "market_value": 1_050.0,
                "pnl_abs": 50.0,
                "pnl_pct": 5.0,
                "side": "long",
            }
        ]


class ReportingSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        init_db(f"sqlite:///{self.db_path}")
        self.settings = SimpleNamespace(max_holding_days=20, anthropic_api_key="test-key")

        et_now = datetime.now(ZoneInfo("America/New_York"))
        days_since_monday = et_now.weekday()
        week_end = et_now.replace(hour=23, minute=59, second=59, microsecond=0) - timedelta(days=max(0, days_since_monday - 4))
        week_start = (week_end - timedelta(days=4)).replace(hour=0, minute=0, second=0, microsecond=0)
        now = (week_start + timedelta(days=2, hours=12)).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        with get_session() as session:
            ticker = Ticker(symbol="AAPL", sector="Technology", in_universe=True)
            session.add(ticker)
            session.flush()

            session.add_all(
                [
                    Memo(
                        ticker_id=ticker.id,
                        composite_score=0.8,
                        classification="high_conviction",
                        status="approved",
                        created_at=now,
                    ),
                    Memo(
                        ticker_id=ticker.id,
                        composite_score=0.3,
                        classification="no_action",
                        status="rejected",
                        created_at=now,
                    ),
                    Memo(
                        ticker_id=ticker.id,
                        composite_score=0.5,
                        classification="moderate",
                        status="watchlisted",
                        created_at=now,
                    ),
                ]
            )

            session.add(
                Trade(
                    ticker_id=ticker.id,
                    direction="long",
                    entry_price=100.0,
                    entry_date=now,
                    shares=10,
                    stop_loss=95.0,
                    target_1=108.0,
                    target_2=112.0,
                    position_pct=5.0,
                    status="open",
                )
            )
            session.add(
                Trade(
                    ticker_id=ticker.id,
                    direction="long",
                    entry_price=90.0,
                    exit_price=102.5,
                    entry_date=now,
                    exit_date=now,
                    shares=8,
                    stop_loss=85.0,
                    target_1=98.0,
                    target_2=104.0,
                    position_pct=4.0,
                    status="closed",
                    exit_reason="target_2",
                    pnl_pct=13.89,
                    pnl_absolute=100.0,
                )
            )

    def tearDown(self):
        if db_module.engine is not None:
            db_module.engine.dispose()
        db_module.engine = None
        db_module.SessionLocal = None
        self.temp_dir.cleanup()

    def test_daily_digest_uses_current_memo_timestamp_field(self):
        current_now = datetime.utcnow()
        with get_session() as session:
            for memo in session.query(Memo).all():
                memo.created_at = current_now
            for trade in session.query(Trade).all():
                trade.entry_date = current_now
                if trade.status == "closed":
                    trade.exit_date = current_now

        digest = DailyDigest(_FakeAlpaca(), notification_manager=None, settings=self.settings)
        text = digest._build_digest()

        self.assertIn("New memos: `3`", text)
        self.assertIn("Trades executed: `2`", text)

    def test_weekly_report_uses_current_trade_and_memo_schema(self):
        report = WeeklyReport(_FakeAlpaca(), notification_manager=None, settings=self.settings)
        data = report._gather_data()

        self.assertEqual(data["approved"], 1)
        self.assertEqual(data["passed"], 1)
        self.assertEqual(data["watchlisted"], 1)
        self.assertEqual(data["realized_pnl"], 100.0)
        self.assertEqual(data["wins"], 1)
        self.assertEqual(data["losses"], 0)


class _FakeRiskManager:
    def full_risk_check(self, *args, **kwargs):
        return {"allowed": True, "warnings": []}


class _FakeProtectedEntryAlpaca:
    def __init__(self):
        self.calls = []

    def get_account_info(self):
        return {"equity": 100_000.0, "cash": 100_000.0, "pnl_today": 0.0, "pnl_today_pct": 0.0}

    def get_positions_detail(self):
        return []

    def submit_protected_limit_entry(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "entry_order_id": "entry-1",
            "stop_order_id": "stop-1",
            "order_strategy": "oto",
            "stop_price": kwargs["stop_price"],
        }


class OrderExecutionFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "orders.db"
        init_db(f"sqlite:///{self.db_path}")
        with get_session() as session:
            ticker = Ticker(symbol="MSFT", sector="Technology", in_universe=True)
            session.add(ticker)
            session.flush()
            session.add(
                Memo(
                    ticker_id=ticker.id,
                    composite_score=0.82,
                    classification="high_conviction",
                    trade_params=json.dumps(
                        {
                            "shares": 12,
                            "entry_price": 250.0,
                            "stop_loss": 242.5,
                            "target_1": 262.5,
                            "target_2": 270.0,
                            "position_pct": 5.0,
                            "direction": "long",
                        }
                    ),
                    signal_breakdown=json.dumps({"catalyst": 0.8}),
                    status="approved",
                    created_at=datetime.utcnow(),
                )
            )

        self.settings = SimpleNamespace(portfolio_value=100_000.0)
        self.alpaca = _FakeProtectedEntryAlpaca()
        self.manager = OrderManager(
            self.settings,
            self.alpaca,
            _FakeRiskManager(),
            position_manager=SimpleNamespace(),
        )

    def tearDown(self):
        if db_module.engine is not None:
            db_module.engine.dispose()
        db_module.engine = None
        db_module.SessionLocal = None
        self.temp_dir.cleanup()

    def test_execute_approved_trade_records_pending_fill_until_entry_fills(self):
        result = asyncio.run(self.manager.execute_approved_trade(1))

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "pending_fill")
        self.assertEqual(len(self.alpaca.calls), 1)

        with get_session() as session:
            trade = session.query(Trade).first()
            self.assertIsNotNone(trade)
            self.assertEqual(trade.status, "pending_fill")
            self.assertIsNone(trade.entry_date)
            self.assertEqual(trade.alpaca_entry_order_id, "entry-1")
            self.assertEqual(trade.alpaca_stop_order_id, "stop-1")
