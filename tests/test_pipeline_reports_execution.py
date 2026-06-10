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
from database.models import Memo, OrderEvent, Ticker, Trade
from execution.brokers.base import BrokerOrderRequest, BrokerOrderResult, BrokerOrderReview
from execution.order_manager import OrderManager
from execution.order_monitor import OrderMonitor
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


class _FakeRobinhoodBroker:
    name = "robinhood"
    supports_fractional = True
    supports_order_review = True
    live_trading = True

    def __init__(self, warnings=None):
        self.account_number = "RH123456"
        self.reviewed = []
        self.placed = []
        self.warnings = warnings or []

    def get_account_info(self):
        return {"equity": 25.0, "cash": 25.0, "pnl_today": 0.0, "pnl_today_pct": 0.0}

    def get_positions_detail(self):
        return []

    def review_order(self, order):
        self.reviewed.append(order)
        return BrokerOrderReview(
            broker="robinhood",
            request=order,
            approved=True,
            warnings=list(self.warnings),
            estimated_notional=order.requested_notional,
            raw={"ok": True, "warnings": self.warnings},
        )

    def place_order(self, review):
        self.placed.append(review.request)
        return BrokerOrderResult(
            broker="robinhood",
            success=True,
            order_id="rh-order-1",
            status="submitted",
            order_strategy="robinhood_mcp",
            raw={"id": "rh-order-1"},
        )


class RobinhoodOrderExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "robinhood.db"
        init_db(f"sqlite:///{self.db_path}")
        with get_session() as session:
            ticker = Ticker(symbol="NVDA", sector="Technology", in_universe=True)
            session.add(ticker)
            session.flush()
            session.add(
                Memo(
                    ticker_id=ticker.id,
                    composite_score=0.86,
                    classification="high_conviction",
                    trade_params=json.dumps(
                        {
                            "shares": 10,
                            "entry_price": 100.0,
                            "stop_loss": 95.0,
                            "target_1": 110.0,
                            "target_2": 115.0,
                            "position_pct": 5.0,
                            "direction": "long",
                        }
                    ),
                    signal_breakdown=json.dumps({"catalyst": 0.9}),
                    status="approved",
                    created_at=datetime.utcnow(),
                )
            )

    def tearDown(self):
        if db_module.engine is not None:
            db_module.engine.dispose()
        db_module.engine = None
        db_module.SessionLocal = None
        self.temp_dir.cleanup()

    def _settings(self, **overrides):
        base = dict(
            portfolio_value=100_000.0,
            execution_mode="live",
            allow_live_trading=True,
            robinhood_order_type="market",
            robinhood_market_hours="regular_hours",
            robinhood_max_order_notional=5.0,
            robinhood_max_daily_notional=10.0,
            robinhood_max_open_positions=3,
            robinhood_allowed_symbols="",
            robinhood_blocked_symbols="",
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_robinhood_live_order_is_capped_and_audited(self):
        broker = _FakeRobinhoodBroker()
        manager = OrderManager(
            self._settings(),
            alpaca=SimpleNamespace(),
            risk_manager=_FakeRiskManager(),
            position_manager=SimpleNamespace(),
            broker=broker,
        )

        result = asyncio.run(manager.execute_approved_trade(1))

        self.assertTrue(result["success"])
        self.assertEqual(result["broker"], "robinhood")
        self.assertEqual(broker.reviewed[0].dollar_amount, 5.0)
        self.assertEqual(broker.placed[0].requested_notional, 5.0)

        with get_session() as session:
            trade = session.query(Trade).first()
            events = session.query(OrderEvent).order_by(OrderEvent.id).all()
            self.assertEqual(trade.broker, "robinhood")
            self.assertEqual(trade.broker_order_id, "rh-order-1")
            self.assertEqual(trade.requested_notional, 5.0)
            self.assertEqual([e.event_type for e in events], ["review", "placed"])

    def test_robinhood_review_only_does_not_place(self):
        broker = _FakeRobinhoodBroker()
        manager = OrderManager(
            self._settings(execution_mode="review_only", allow_live_trading=False),
            alpaca=SimpleNamespace(),
            risk_manager=_FakeRiskManager(),
            position_manager=SimpleNamespace(),
            broker=broker,
        )

        result = asyncio.run(manager.execute_approved_trade(1))

        self.assertTrue(result["success"])
        self.assertTrue(result["review_only"])
        self.assertEqual(len(broker.placed), 0)
        with get_session() as session:
            trade = session.query(Trade).first()
            self.assertEqual(trade.status, "reviewed")
            self.assertEqual(trade.broker_order_strategy, "review_only")


class _MissingPositionAlpaca:
    def close_position(self, ticker):
        return {"success": False, "error": '{"code":40410000,"message":"position not found: ORCL"}'}

    def cancel_order(self, order_id):
        return None


class OrderMonitorReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "monitor.db"
        init_db(f"sqlite:///{self.db_path}")
        with get_session() as session:
            ticker = Ticker(symbol="ORCL", sector="Technology", in_universe=True)
            session.add(ticker)
            session.flush()
            session.add(
                Trade(
                    ticker_id=ticker.id,
                    direction="long",
                    entry_price=100.0,
                    entry_date=datetime.utcnow() - timedelta(days=45),
                    shares=5,
                    stop_loss=92.0,
                    target_1=108.0,
                    target_2=115.0,
                    position_pct=2.5,
                    status="open",
                )
            )

        self.monitor = OrderMonitor(
            _MissingPositionAlpaca(),
            notification_manager=None,
            settings=SimpleNamespace(max_holding_days=20),
        )

    def tearDown(self):
        if db_module.engine is not None:
            db_module.engine.dispose()
        db_module.engine = None
        db_module.SessionLocal = None
        self.temp_dir.cleanup()

    def test_time_exit_reconciles_trade_when_alpaca_position_is_missing(self):
        asyncio.run(self.monitor._check_open_trades())

        with get_session() as session:
            trade = session.query(Trade).first()
            self.assertEqual(trade.status, "closed")
            self.assertEqual(trade.exit_reason, "reconciled_missing_position")
            self.assertIsNotNone(trade.exit_date)
            self.assertIsNone(trade.pnl_absolute)
            self.assertIn("RECONCILED_MISSING_POSITION", trade.operator_notes)


class _RacingRobinhoodBroker(_FakeRobinhoodBroker):
    """Simulates a competing placement consuming the daily budget while THIS
    order is still under (slow) review — exercises the in-lock cap re-check."""

    def review_order(self, order):
        with get_session() as session:
            session.add(
                OrderEvent(
                    broker="robinhood", event_type="placed", status="pending_fill",
                    notional=8.0, raw_payload="{}", created_at=datetime.utcnow(),
                )
            )
        return super().review_order(order)


class _TimeoutThenFoundRobinhoodBroker(_FakeRobinhoodBroker):
    def __init__(self):
        super().__init__()
        self.ref_id = ""

    def place_order(self, review):
        self.ref_id = review.request.client_context["ref_id"]
        raise TimeoutError("read timed out after placement")

    def find_order_by_ref_id(self, ref_id):
        if ref_id == self.ref_id:
            return {
                "order_id": "rh-reconciled-1",
                "ref_id": ref_id,
                "status": "submitted",
                "symbol": "NVDA",
            }
        return None


class _LeakyRobinhoodBroker(_FakeRobinhoodBroker):
    def review_order(self, order):
        self.reviewed.append(order)
        return BrokerOrderReview(
            broker="robinhood",
            request=order,
            approved=True,
            estimated_notional=order.requested_notional,
            raw={
                "access_token": "secret-access",
                "account_number": "RH123456",
                "nested": {"refresh_token": "secret-refresh"},
            },
        )

    def place_order(self, review):
        self.placed.append(review.request)
        return BrokerOrderResult(
            broker="robinhood",
            success=True,
            order_id="rh-order-1",
            status="submitted",
            order_strategy="robinhood_mcp",
            raw={
                "id": "rh-order-1",
                "headers": {"Authorization": "Bearer secret-access"},
                "account_id": "RH123456",
            },
        )


class _RecordingAlpaca:
    """Records every order-status lookup so we can assert which trades the
    monitor actually touched."""

    def __init__(self):
        self.status_calls = []

    def get_order_status(self, order_id):
        self.status_calls.append(order_id)
        return None  # not filled -> monitor returns early


class RobinhoodLiveSafetyRegressionTests(unittest.TestCase):
    """Regression coverage for the live-trading safety fixes C1/C2/C3/H1."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "rh_safety.db"
        init_db(f"sqlite:///{self.db_path}")
        with get_session() as session:
            ticker = Ticker(symbol="NVDA", sector="Technology", in_universe=True)
            session.add(ticker)
            session.flush()
            session.add(
                Memo(
                    ticker_id=ticker.id,
                    composite_score=0.86,
                    classification="high_conviction",
                    trade_params=json.dumps(
                        {
                            "shares": 10, "entry_price": 100.0, "stop_loss": 95.0,
                            "target_1": 110.0, "target_2": 115.0, "position_pct": 5.0,
                            "direction": "long",
                        }
                    ),
                    signal_breakdown=json.dumps({"catalyst": 0.9}),
                    status="approved",
                    created_at=datetime.utcnow(),
                )
            )

    def tearDown(self):
        if db_module.engine is not None:
            db_module.engine.dispose()
        db_module.engine = None
        db_module.SessionLocal = None
        self.temp_dir.cleanup()

    def _settings(self, **overrides):
        base = dict(
            portfolio_value=100_000.0,
            execution_mode="live",
            allow_live_trading=True,
            robinhood_order_type="market",
            robinhood_market_hours="regular_hours",
            robinhood_max_order_notional=5.0,
            robinhood_max_daily_notional=10.0,
            robinhood_max_open_positions=3,
            robinhood_allowed_symbols="",
            robinhood_blocked_symbols="",
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def _manager(self, broker=None, **overrides):
        return OrderManager(
            self._settings(**overrides),
            alpaca=SimpleNamespace(),
            risk_manager=_FakeRiskManager(),
            position_manager=SimpleNamespace(),
            broker=broker or _FakeRobinhoodBroker(),
        )

    # --- C2: per-order notional cap now covers limit orders ---
    def test_limit_order_over_per_order_cap_is_rejected(self):
        manager = self._manager(robinhood_order_type="limit")
        req = BrokerOrderRequest(
            symbol="NVDA", side="buy", order_type="limit",
            quantity=2, limit_price=100.0, requested_notional=200.0, direction="long",
        )
        result = manager._check_runtime_limits("robinhood", "NVDA", req, positions=[])
        self.assertFalse(result["allowed"])
        self.assertTrue(any("per-order cap" in r for r in result["reasons"]))

    def test_effective_notional_uses_quantity_times_price_for_limit(self):
        manager = self._manager()
        limit_req = BrokerOrderRequest(
            symbol="NVDA", side="buy", order_type="limit",
            quantity=3, limit_price=4.0, requested_notional=999.0, direction="long",
        )
        self.assertEqual(manager._effective_notional(limit_req), 12.0)
        market_req = BrokerOrderRequest(
            symbol="NVDA", side="buy", order_type="market",
            dollar_amount=5.0, requested_notional=5.0, direction="long",
        )
        self.assertEqual(manager._effective_notional(market_req), 5.0)

    # --- C3: daily cap counts prior placements + atomic re-check under lock ---
    def test_daily_cap_counts_prior_placed_events(self):
        with get_session() as session:
            session.add(
                OrderEvent(
                    broker="robinhood", event_type="placed", status="pending_fill",
                    notional=8.0, raw_payload="{}", created_at=datetime.utcnow(),
                )
            )
        manager = self._manager()
        req = BrokerOrderRequest(
            symbol="NVDA", side="buy", order_type="market",
            dollar_amount=5.0, requested_notional=5.0, direction="long",
        )
        result = manager._check_runtime_limits("robinhood", "NVDA", req, positions=[])
        self.assertFalse(result["allowed"])
        self.assertTrue(any("daily notional cap" in r for r in result["reasons"]))

    def test_inflight_placement_rechecks_daily_cap_under_lock(self):
        broker = _RacingRobinhoodBroker()
        manager = self._manager(broker=broker)
        result = asyncio.run(manager.execute_approved_trade(1))
        self.assertFalse(result["success"])
        self.assertIn("daily notional cap", result["error"])
        self.assertEqual(len(broker.placed), 0)

    # --- C1: order monitor manages only Alpaca trades ---
    def test_order_monitor_skips_robinhood_trades(self):
        with get_session() as session:
            nvda = session.query(Ticker).filter_by(symbol="NVDA").first()
            session.add(
                Trade(
                    ticker_id=nvda.id, direction="long", entry_price=100.0,
                    shares=5, stop_loss=95.0, status="pending_fill",
                    broker="alpaca", broker_order_id="alp-1",
                )
            )
            session.add(
                Trade(
                    ticker_id=nvda.id, direction="long", entry_price=100.0,
                    shares=5, stop_loss=95.0, status="pending_fill",
                    broker="robinhood", broker_order_id="rh-9",
                )
            )
        recorder = _RecordingAlpaca()
        monitor = OrderMonitor(
            recorder, notification_manager=None, settings=SimpleNamespace(max_holding_days=20)
        )
        asyncio.run(monitor._check_open_trades())
        self.assertEqual(recorder.status_calls, ["alp-1"])

    def test_placement_timeout_records_unknown_and_reconciles_by_ref_id(self):
        broker = _TimeoutThenFoundRobinhoodBroker()
        manager = self._manager(broker=broker)

        result = asyncio.run(manager.execute_approved_trade(1))

        self.assertTrue(result["success"])
        self.assertTrue(result["placement_reconciled"])
        self.assertEqual(result["entry_order_id"], "rh-reconciled-1")
        with get_session() as session:
            events = session.query(OrderEvent).order_by(OrderEvent.id).all()
            self.assertEqual([e.event_type for e in events], ["review", "placement_unknown", "placed"])
            self.assertIn("read timed out after placement", events[1].raw_payload)

    def test_persisted_robinhood_payloads_are_redacted(self):
        broker = _LeakyRobinhoodBroker()
        manager = self._manager(broker=broker)

        result = asyncio.run(manager.execute_approved_trade(1))

        self.assertTrue(result["success"])
        with get_session() as session:
            trade = session.query(Trade).first()
            events = session.query(OrderEvent).order_by(OrderEvent.id).all()
            persisted = "\n".join([trade.order_review_json, *[e.raw_payload for e in events]])
            self.assertNotIn("secret-access", persisted)
            self.assertNotIn("secret-refresh", persisted)
            self.assertNotIn("RH123456", persisted)
            self.assertIn("****3456", persisted)


class BrokerSwitchSafetyTests(unittest.TestCase):
    """H1: selecting Robinhood must always drop execution to review_only,
    never inheriting a prior live mode."""

    def test_broker_robinhood_forces_review_only_from_live(self):
        from bot.handlers import commands as commands_module
        from bot.auth import init_auth
        from unittest.mock import AsyncMock

        init_auth("99")
        calls = {}

        class _Pipeline:
            def __init__(self):
                self.settings = SimpleNamespace(execution_mode="live")

            def configure_broker(self, primary=None, execution_mode=None, robinhood_account_number=None):
                calls["primary"] = primary
                calls["execution_mode"] = execution_mode

        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=99),
            effective_user=SimpleNamespace(id=99),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        context = SimpleNamespace(bot_data={"pipeline": _Pipeline()}, args=["robinhood"])

        with patch.object(commands_module, "_broker_summary", lambda p: "ok"):
            asyncio.run(commands_module.broker_command(update, context))

        self.assertEqual(calls["primary"], "robinhood")
        self.assertEqual(calls["execution_mode"], "review_only")
