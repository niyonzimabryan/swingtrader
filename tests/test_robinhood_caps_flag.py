"""ROBINHOOD_NOTIONAL_CAPS_ENABLED: the owner's switch for the Robinhood caps.

Owner ruling 2026-09-13. Default `true` keeps today's behaviour byte for byte —
`tests/test_pipeline_reports_execution.py` owns those assertions and is not
touched here. What this file asserts is the flag: that `false` lifts exactly the
three notional/position-count ceilings and lifts nothing else, and that a
settings object predating the flag still gets the caps.
"""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from config.settings import Settings
from database import db as db_module
from database.db import get_session
from database.models import Memo, Ticker, Trade
from execution.brokers.base import BrokerOrderRequest
from execution import order_manager as order_manager_module
from execution.order_manager import OrderManager
from tests.dbfixture import init_test_db
from tests.test_pipeline_reports_execution import _FakeRiskManager, _FakeRobinhoodBroker
from utils.timeutils import utcnow_naive


class RobinhoodNotionalCapsFlagTests(unittest.TestCase):
    def setUp(self):
        self.db = init_test_db("robinhood_caps_flag")
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
                    created_at=utcnow_naive(),
                )
            )

    def tearDown(self):
        if db_module.engine is not None:
            db_module.engine.dispose()
        db_module.engine = None
        db_module.SessionLocal = None
        self.db.cleanup()

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
            robinhood_notional_caps_enabled=True,
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

    @staticmethod
    def _over_cap_request():
        """$1,000 of exposure — 200x the $5 per-order cap."""
        return BrokerOrderRequest(
            symbol="NVDA",
            side="buy",
            order_type="market",
            dollar_amount=1000.0,
            requested_notional=1000.0,
            direction="long",
        )

    # --- the default ---

    def test_setting_defaults_to_enabled(self):
        self.assertIs(
            Settings.model_fields["robinhood_notional_caps_enabled"].default, True
        )

    def test_settings_without_the_flag_are_treated_as_enabled(self):
        """A settings object built before this flag existed keeps the caps."""
        settings = self._settings()
        del settings.robinhood_notional_caps_enabled
        manager = OrderManager(
            settings,
            alpaca=SimpleNamespace(),
            risk_manager=_FakeRiskManager(),
            position_manager=SimpleNamespace(),
            broker=_FakeRobinhoodBroker(),
        )
        self.assertTrue(manager._robinhood_caps_enabled())
        result = manager._check_runtime_limits(
            "robinhood", "NVDA", self._over_cap_request(), positions=[]
        )
        self.assertFalse(result["allowed"])

    # --- caps enabled: today's behaviour ---

    def test_caps_enabled_refuses_an_over_cap_order(self):
        manager = self._manager()
        result = manager._check_runtime_limits(
            "robinhood", "NVDA", self._over_cap_request(), positions=[]
        )
        self.assertFalse(result["allowed"])
        self.assertTrue(any("per-order cap" in r for r in result["reasons"]))

    def test_caps_enabled_still_clamps_the_built_order(self):
        manager = self._manager()
        request = manager._build_order_request(
            broker_name="robinhood",
            ticker="NVDA",
            trade_params={"shares": 10, "entry_price": 100.0, "direction": "long"},
            account={},
            positions=[],
        )
        self.assertEqual(request.dollar_amount, 5.0)

    def test_caps_enabled_refuses_over_max_open_positions(self):
        manager = self._manager()
        positions = [{"ticker": t} for t in ("AAPL", "MSFT", "AMD")]
        request = BrokerOrderRequest(
            symbol="NVDA",
            side="buy",
            order_type="market",
            dollar_amount=1.0,
            requested_notional=1.0,
            direction="long",
        )
        result = manager._check_runtime_limits(
            "robinhood", "NVDA", request, positions=positions
        )
        self.assertFalse(result["allowed"])
        self.assertTrue(any("max open positions" in r for r in result["reasons"]))

    # --- caps disabled: the three ceilings lift ---

    def test_caps_disabled_allows_an_over_cap_order(self):
        manager = self._manager(robinhood_notional_caps_enabled=False)
        result = manager._check_runtime_limits(
            "robinhood", "NVDA", self._over_cap_request(), positions=[]
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result.get("reasons", []), [])

    def test_caps_disabled_allows_over_max_open_positions(self):
        manager = self._manager(robinhood_notional_caps_enabled=False)
        positions = [{"ticker": t} for t in ("AAPL", "MSFT", "AMD")]
        request = BrokerOrderRequest(
            symbol="NVDA",
            side="buy",
            order_type="market",
            dollar_amount=1.0,
            requested_notional=1.0,
            direction="long",
        )
        result = manager._check_runtime_limits(
            "robinhood", "NVDA", request, positions=positions
        )
        self.assertTrue(result["allowed"])

    def test_caps_disabled_does_not_clamp_the_built_order(self):
        manager = self._manager(robinhood_notional_caps_enabled=False)
        request = manager._build_order_request(
            broker_name="robinhood",
            ticker="NVDA",
            trade_params={"shares": 10, "entry_price": 100.0, "direction": "long"},
            account={},
            positions=[],
        )
        self.assertEqual(request.dollar_amount, 1000.0)
        self.assertEqual(request.requested_notional, 1000.0)

    def test_caps_disabled_places_the_unclamped_order_end_to_end(self):
        broker = _FakeRobinhoodBroker()
        manager = self._manager(broker=broker, robinhood_notional_caps_enabled=False)

        result = asyncio.run(manager.execute_approved_trade(1))

        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(result["broker"], "robinhood")
        self.assertEqual(broker.reviewed[0].dollar_amount, 1000.0)
        self.assertEqual(broker.placed[0].requested_notional, 1000.0)
        with get_session() as session:
            trade = session.query(Trade).first()
            self.assertEqual(trade.requested_notional, 1000.0)

    def test_caps_disabled_logs_a_warning_per_order(self):
        manager = self._manager(robinhood_notional_caps_enabled=False)
        with patch.object(order_manager_module, "log") as fake_log:
            manager._build_order_request(
                broker_name="robinhood",
                ticker="NVDA",
                trade_params={"shares": 10, "entry_price": 100.0, "direction": "long"},
                account={},
                positions=[],
            )
        fake_log.warning.assert_called_once()
        event, kwargs = fake_log.warning.call_args[0][0], fake_log.warning.call_args[1]
        self.assertEqual(event, "robinhood_notional_caps_disabled")
        self.assertEqual(kwargs["ticker"], "NVDA")
        self.assertEqual(kwargs["notional"], 1000.0)

    def test_caps_enabled_logs_no_such_warning(self):
        manager = self._manager()
        with patch.object(order_manager_module, "log") as fake_log:
            manager._build_order_request(
                broker_name="robinhood",
                ticker="NVDA",
                trade_params={"shares": 10, "entry_price": 100.0, "direction": "long"},
                account={},
                positions=[],
            )
        fake_log.warning.assert_not_called()

    # --- caps disabled: what must stay enforced ---

    def test_caps_disabled_still_enforces_allowed_symbols(self):
        manager = self._manager(
            robinhood_notional_caps_enabled=False,
            robinhood_allowed_symbols="AAPL,MSFT",
        )
        result = manager._check_runtime_limits(
            "robinhood", "NVDA", self._over_cap_request(), positions=[]
        )
        self.assertFalse(result["allowed"])
        self.assertTrue(
            any("ROBINHOOD_ALLOWED_SYMBOLS" in r for r in result["reasons"])
        )

    def test_caps_disabled_still_enforces_blocked_symbols(self):
        manager = self._manager(
            robinhood_notional_caps_enabled=False,
            robinhood_blocked_symbols="NVDA",
        )
        result = manager._check_runtime_limits(
            "robinhood", "NVDA", self._over_cap_request(), positions=[]
        )
        self.assertFalse(result["allowed"])
        self.assertTrue(
            any("ROBINHOOD_BLOCKED_SYMBOLS" in r for r in result["reasons"])
        )

    def test_caps_disabled_still_refuses_a_short(self):
        manager = self._manager(robinhood_notional_caps_enabled=False)
        with self.assertRaises(ValueError):
            manager._build_order_request(
                broker_name="robinhood",
                ticker="NVDA",
                trade_params={"shares": 10, "entry_price": 100.0, "direction": "short"},
                account={},
                positions=[],
            )

    # --- caps disabled: other brokers are untouched ---

    def test_caps_disabled_does_not_change_a_non_robinhood_broker(self):
        manager = self._manager(robinhood_notional_caps_enabled=False)
        request = manager._build_order_request(
            broker_name="alpaca",
            ticker="NVDA",
            trade_params={"shares": 10, "entry_price": 100.0, "direction": "long"},
            account={},
            positions=[],
        )
        self.assertEqual(request.quantity, 10)
        self.assertEqual(request.limit_price, 100.0)
        self.assertEqual(request.requested_notional, 1000.0)

        positions = [{"ticker": t} for t in ("AAPL", "MSFT", "AMD", "TSLA")]
        result = manager._check_runtime_limits(
            "alpaca", "NVDA", request, positions=positions
        )
        self.assertEqual(result, {"allowed": True, "request": request})


if __name__ == "__main__":
    unittest.main()
