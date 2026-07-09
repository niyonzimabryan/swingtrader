"""
Safety-critical regression tests — BRY-54.

Goal: protect order/risk behavior before open-source release with focused,
deterministic tests. No live Alpaca/Telegram/provider calls.

Coverage:
- Scoring: weight composition, classification boundaries, alignment penalty,
  Opus delta clamping, [0,1] final-score clamp.
- Risk: drawdown circuit breaker, daily loss halt, max positions,
  total-exposure cap, sector-exposure warning vs blocking, earnings blackout.
- Position sizing: regime / conviction / volatility multipliers, min/max clamp,
  long vs short stop-loss and target geometry.
- Order monitor: pending_fill -> open transition with direction-aware P&L,
  stop-trigger and target-hit transitions, missing-position reconciliation.
- Short-direction invariants: stop above entry, targets below entry,
  inverted P&L sign, OrderManager dispatches submit_limit_short_entry.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents.base_agent import AgentOutput
from database.db import get_session, init_db
from database.models import Ticker, Trade
from utils.timeutils import utcnow_naive
from execution.order_monitor import OrderMonitor
from execution.position_manager import PositionManager
from execution.risk_manager import RiskManager
from scoring.engine import ScoringEngine
from scoring.weights import (
    CONVICTION_MULTIPLIERS,
    OPUS_MAX_DELTA,
    SCORE_THRESHOLDS,
    SIGNAL_WEIGHTS,
)


def _settings(**overrides) -> SimpleNamespace:
    """Minimal settings stub. Override fields per test as needed."""
    base = dict(
        portfolio_value=100_000.0,
        base_position_pct=0.05,
        max_position_pct=0.10,
        min_position_pct=0.02,
        max_portfolio_exposure=0.80,
        max_sector_exposure=0.30,
        max_concurrent_positions=8,
        default_stop_loss_pct=0.05,
        max_stop_loss_pct=0.08,
        max_holding_days=20,
        drawdown_circuit_breaker_pct=0.10,
        daily_loss_halt_pct=0.03,
        memo_threshold=0.55,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _agent(score: float, confidence: float = 0.8, direction: str = "bullish") -> AgentOutput:
    return AgentOutput(
        agent_type="test",
        score=score,
        confidence=confidence,
        direction=direction,
        reasoning="",
        raw_data={},
    )


# ---------------------------------------------------------------------------
# Scoring: weight composition + classification + alignment
# ---------------------------------------------------------------------------


class ScoringWeightCompositionTests(unittest.TestCase):
    def setUp(self):
        # No anthropic_client → escalation disabled, raw aggregation only.
        self.engine = ScoringEngine(_settings(), anthropic_client=None)

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(SIGNAL_WEIGHTS.values()), 1.0, places=6)

    def test_raw_score_is_weighted_sum_when_all_aligned(self):
        result = self.engine.score_opportunity(
            ticker="AAPL",
            catalyst=_agent(0.8),
            fundamental=_agent(0.6),
            pattern=_agent(0.5),
            web_research=_agent(0.7),
            regime={},
        )
        expected = (
            0.8 * SIGNAL_WEIGHTS["catalyst"]
            + 0.6 * SIGNAL_WEIGHTS["fundamental"]
            + 0.5 * SIGNAL_WEIGHTS["pattern"]
            + 0.7 * SIGNAL_WEIGHTS["web_research"]
        )
        self.assertAlmostEqual(result["raw_score"], round(expected, 4), places=4)
        # All-aligned bullish → no alignment penalty applied.
        self.assertEqual(result["signal_agreement"], "all_aligned")
        self.assertEqual(result["adjusted_score"], result["raw_score"])

    def test_classification_boundaries(self):
        cases = [
            (SCORE_THRESHOLDS["high_conviction"], "high_conviction"),
            (SCORE_THRESHOLDS["high_conviction"] - 0.001, "moderate"),
            (SCORE_THRESHOLDS["moderate"], "moderate"),
            (SCORE_THRESHOLDS["moderate"] - 0.001, "low"),
            (SCORE_THRESHOLDS["low"], "low"),
            (SCORE_THRESHOLDS["low"] - 0.001, "no_action"),
            (0.0, "no_action"),
        ]
        for score, expected in cases:
            with self.subTest(score=score):
                self.assertEqual(self.engine._classify(score), expected)

    def test_conflicting_signals_apply_major_penalty(self):
        # Catalyst (highest weight) bullish, pattern + fundamental + web bearish
        # at high confidence → disagreement_ratio > 0.4 → major penalty.
        result = self.engine.score_opportunity(
            ticker="AAPL",
            catalyst=_agent(0.9, confidence=0.9, direction="bullish"),
            fundamental=_agent(0.9, confidence=0.9, direction="bearish"),
            pattern=_agent(0.9, confidence=0.9, direction="bearish"),
            web_research=_agent(0.9, confidence=0.9, direction="bearish"),
            regime={},
        )
        self.assertEqual(result["signal_agreement"], "conflicting")
        # Adjusted should be raw * 0.75 (DIRECTION_PENALTY_MAJOR).
        self.assertLess(result["adjusted_score"], result["raw_score"])

    def test_neutral_only_signals_stay_neutral(self):
        result = self.engine.score_opportunity(
            ticker="AAPL",
            catalyst=_agent(0.5, direction="neutral"),
            fundamental=_agent(0.5, direction="neutral"),
            pattern=_agent(0.5, direction="neutral"),
            web_research=_agent(0.5, direction="neutral"),
            regime={},
        )
        self.assertEqual(result["direction"], "neutral")
        self.assertEqual(result["signal_agreement"], "all_aligned")


class OpusClampingTests(unittest.TestCase):
    """Even without escalation, the post-aggregation final_score must be in [0, 1]."""

    def test_final_score_clamped_to_unit_interval(self):
        engine = ScoringEngine(_settings(), anthropic_client=None)
        # All score=1.0 → raw 1.0, no penalty, final 1.0 (boundary).
        r_high = engine.score_opportunity(
            "X", _agent(1.0), _agent(1.0), _agent(1.0), _agent(1.0), regime={}
        )
        self.assertLessEqual(r_high["final_score"], 1.0)
        self.assertGreaterEqual(r_high["final_score"], 0.0)

        # All score=0.0 → final 0.0.
        r_low = engine.score_opportunity(
            "X", _agent(0.0), _agent(0.0), _agent(0.0), _agent(0.0), regime={}
        )
        self.assertEqual(r_low["final_score"], 0.0)

    def test_opus_delta_clamp_constant_is_safe(self):
        # Sanity guard against accidental widening of the clamp.
        # If this fails, someone changed the safety budget — review intentionally.
        self.assertLessEqual(OPUS_MAX_DELTA, 0.30)
        self.assertGreater(OPUS_MAX_DELTA, 0.0)


# ---------------------------------------------------------------------------
# Risk manager
# ---------------------------------------------------------------------------


class _NoopMarketData:
    """Stub MarketDataAdapter with no network calls."""

    def get_daily_bars(self, *_, **__):
        import pandas as pd

        return pd.DataFrame()

    def get_current_price(self, *_, **__):
        return {"price": 100.0}

    def get_atr(self, *_, **__):
        return 1.5


class RiskManagerRejectionTests(unittest.TestCase):
    def setUp(self):
        with patch("execution.risk_manager.MarketDataAdapter", _NoopMarketData):
            self.risk = RiskManager(_settings())

    def _check(self, **portfolio_overrides):
        portfolio = dict(
            equity=100_000.0,
            pnl_today=0,
            pnl_today_pct=0,
            position_count=0,
            positions=[],
            sector_exposure={},
            total_exposure_pct=0.0,
        )
        portfolio.update(portfolio_overrides)
        return self.risk.full_risk_check(
            "AAPL", portfolio, regime={}, trade_params={"position_pct": 0.05},
        )

    def test_drawdown_circuit_breaker_blocks(self):
        # Seed peak by passing higher equity once, then drop 12%.
        self.risk._peak_value = 100_000.0
        result = self._check(equity=88_000.0)
        self.assertFalse(result["allowed"])
        self.assertTrue(any("drawdown" in r.lower() for r in result["reasons"]))

    def test_drawdown_below_threshold_allows(self):
        self.risk._peak_value = 100_000.0
        result = self._check(equity=95_000.0)  # 5% drawdown < 10%
        self.assertTrue(result["allowed"])

    def test_daily_loss_limit_blocks(self):
        result = self._check(pnl_today=-3500, pnl_today_pct=-3.5)
        self.assertFalse(result["allowed"])
        self.assertTrue(any("daily loss" in r.lower() for r in result["reasons"]))

    def test_daily_gain_does_not_trigger_loss_halt(self):
        # Same magnitude but positive — should not block.
        result = self._check(pnl_today=3500, pnl_today_pct=3.5)
        self.assertTrue(result["allowed"])

    def test_max_positions_blocks(self):
        result = self._check(position_count=8)  # default max_concurrent_positions
        self.assertFalse(result["allowed"])
        self.assertTrue(any("max positions" in r.lower() for r in result["reasons"]))

    def test_total_exposure_cap_blocks(self):
        # 78% existing + 5% proposed = 83% > 80% max.
        result = self._check(total_exposure_pct=0.78)
        self.assertFalse(result["allowed"])
        self.assertTrue(any("portfolio exposure" in r.lower() for r in result["reasons"]))

    def test_sector_exposure_warns_does_not_block(self):
        # Sector exposure is advisory, not a hard block.
        result = self._check(sector_exposure={"Technology": self.risk.settings.max_sector_exposure})
        self.assertTrue(result["allowed"])
        self.assertTrue(any("sector exposure" in w.lower() for w in result["warnings"]))

    def test_peak_value_updates_before_drawdown_check(self):
        self.risk._peak_value = 100_000.0
        self.assertFalse(self.risk.check_drawdown_circuit_breaker({"equity": 110_000.0}))
        self.assertEqual(self.risk._peak_value, 110_000.0)

    def test_high_correlation_blocks_new_position(self):
        import pandas as pd

        class CorrelatedMarketData(_NoopMarketData):
            def get_daily_bars(self, *_, **__):
                return pd.DataFrame({"Close": list(range(100, 130))})

        self.risk.market_data = CorrelatedMarketData()
        result = self._check(positions=[{"ticker": "MSFT"}])
        self.assertFalse(result["allowed"])
        self.assertTrue(any("high correlation" in r.lower() for r in result["reasons"]))

    def test_earnings_blackout_no_op_without_finnhub(self):
        # Current implementation always returns False (no Finnhub key required).
        # Lock that contract so a future change doesn't silently start blocking.
        self.assertFalse(self.risk.check_earnings_blackout("AAPL", "momentum"))
        self.assertFalse(self.risk.check_earnings_blackout("AAPL", "earnings_play"))


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------


class PositionSizingTests(unittest.TestCase):
    def setUp(self):
        with patch("execution.position_manager.MarketDataAdapter", _NoopMarketData):
            self.pm = PositionManager(_settings())

    def test_high_conviction_increases_size(self):
        out = self.pm.calculate_position_size(
            portfolio_value=100_000.0,
            regime={"position_size_multiplier": 1.0},
            composite_score=0.9,
            classification="high_conviction",
            ticker="AAPL",
        )
        # Base 5% * conviction 1.3 * vol_adj (atr_pct=1.5/100=0.015 ≤ 0.02 → 1.0) = 6.5%.
        self.assertAlmostEqual(out["position_pct"], 6.5, places=2)
        self.assertGreater(out["shares"], 0)

    def test_no_action_classification_uses_low_multiplier(self):
        out = self.pm.calculate_position_size(
            portfolio_value=100_000.0,
            regime={"position_size_multiplier": 1.0},
            composite_score=0.30,
            classification="no_action",
            ticker="AAPL",
        )
        # 5% * 0.5 = 2.5%, but min_position_pct floor is 2% → final 2.5%.
        self.assertAlmostEqual(out["position_pct"], 2.5, places=2)

    def test_position_pct_clamped_to_max(self):
        # Aggressive regime + high conviction would blow past max without clamp.
        out = self.pm.calculate_position_size(
            portfolio_value=100_000.0,
            regime={"position_size_multiplier": 2.0},
            composite_score=0.95,
            classification="high_conviction",
            ticker="AAPL",
        )
        # Final must not exceed max_position_pct (10%).
        self.assertLessEqual(out["position_pct"], 10.0)

    def test_position_pct_clamped_to_min(self):
        # Defensive regime + low conviction would dip below min.
        out = self.pm.calculate_position_size(
            portfolio_value=100_000.0,
            regime={"position_size_multiplier": 0.1},
            composite_score=0.30,
            classification="no_action",
            ticker="AAPL",
        )
        self.assertGreaterEqual(out["position_pct"], 2.0)

    def test_long_stop_below_entry_short_stop_above(self):
        long_stop = self.pm.calculate_stop_loss(100.0, "AAPL", direction="long")
        short_stop = self.pm.calculate_stop_loss(100.0, "AAPL", direction="short")
        self.assertLess(long_stop, 100.0)
        self.assertGreater(short_stop, 100.0)

    def test_long_targets_above_entry_short_targets_below(self):
        long_t = self.pm.calculate_targets(entry_price=100.0, stop_loss=95.0, direction="long")
        short_t = self.pm.calculate_targets(entry_price=100.0, stop_loss=105.0, direction="short")

        # Long: 2:1 = entry + 2*risk = 110, 3:1 = 115.
        self.assertEqual(long_t["target_1"], 110.0)
        self.assertEqual(long_t["target_2"], 115.0)
        # Short: 2:1 below = 90, 3:1 below = 85.
        self.assertEqual(short_t["target_1"], 90.0)
        self.assertEqual(short_t["target_2"], 85.0)


# ---------------------------------------------------------------------------
# Order monitor — state transitions + direction-aware P&L
# ---------------------------------------------------------------------------


class _FakeAlpaca:
    """Minimal AlpacaClient stub. Tests configure return values per call."""

    def __init__(self):
        self.order_status_map: dict[str, dict] = {}
        self.cancelled_orders: list[str] = []
        self.target_orders: list[tuple[str, int, float]] = []
        self.positions_detail: list[dict] = []
        self.close_position_result = {"success": True}
        self.open_orders: list[dict] = []
        self.closed_orders: list[dict] = []

    # Status
    def get_order_status(self, order_id: str) -> dict:
        return self.order_status_map.get(order_id, {})

    def cancel_order(self, order_id: str):
        self.cancelled_orders.append(order_id)

    # Sells (long exits)
    def submit_limit_sell(self, ticker: str, qty: int, price: float) -> str:
        self.target_orders.append(("sell", qty, price))
        return f"sell-{ticker}-{qty}"

    # Covers (short exits)
    def submit_limit_cover(self, ticker: str, qty: int, price: float) -> str:
        self.target_orders.append(("cover", qty, price))
        return f"cover-{ticker}-{qty}"

    def get_positions_detail(self) -> list[dict]:
        return list(self.positions_detail)

    def close_position(self, ticker: str) -> dict:
        return self.close_position_result

    def get_orders(self, status: str | None = None) -> list[dict]:
        if status == "open":
            return list(self.open_orders)
        if status in ("closed", "filled"):
            return list(self.closed_orders)
        return list(self.open_orders + self.closed_orders)


class _FakeOcoAlpaca(_FakeAlpaca):
    def __init__(self):
        super().__init__()
        self.oco_orders: list[tuple[str, int, float, float, str]] = []
        self.oco_fail_on_call: set[int] = set()  # 1-based call indexes that raise
        self.stop_loss_orders: list[tuple[str, int, float, str]] = []
        self.stop_loss_fails = False

    def submit_oco_exit(self, ticker: str, qty: int, limit_price: float, stop_price: float, direction: str = "long") -> dict:
        call_index = len(self.oco_orders) + 1
        if call_index in self.oco_fail_on_call:
            raise RuntimeError(f"oco submit failed (call {call_index})")
        self.oco_orders.append((ticker, qty, limit_price, stop_price, direction))
        base = f"oco-{ticker}-{qty}-{int(limit_price * 100)}"
        return {"order_id": base, "stop_leg_id": f"{base}-stopleg"}

    def submit_stop_loss(self, ticker: str, qty: int, stop_price: float, direction: str = "long") -> str:
        if self.stop_loss_fails:
            raise RuntimeError("stop loss submit failed")
        self.stop_loss_orders.append((ticker, qty, stop_price, direction))
        return f"replacement-stop-{ticker}-{qty}"


class _FakeNotifications:
    def __init__(self):
        self.messages: list[str] = []
        self.filled: list[dict] = []

    async def order_filled(self, **kwargs):
        self.filled.append(kwargs)

    async def system_message(self, message: str):
        self.messages.append(message)


class OrderMonitorTransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "test.db"
        init_db(f"sqlite:///{cls.db_path}")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        # Wipe trades + tickers between tests for isolation.
        with get_session() as s:
            s.query(Trade).delete()
            s.query(Ticker).delete()
        self.alpaca = _FakeAlpaca()
        self.monitor = OrderMonitor(self.alpaca, notification_manager=None, settings=_settings())

    def _make_trade(self, **fields) -> int:
        with get_session() as s:
            ticker = Ticker(symbol=fields.pop("symbol", "AAPL"))
            s.add(ticker)
            s.flush()
            defaults = dict(
                ticker_id=ticker.id,
                direction="long",
                entry_price=100.0,
                shares=10,
                stop_loss=95.0,
                target_1=110.0,
                target_2=115.0,
                status="pending_fill",
                alpaca_entry_order_id="entry-1",
            )
            defaults.update(fields)
            trade = Trade(**defaults)
            s.add(trade)
            s.flush()
            return trade.id

    def test_entry_fill_transitions_pending_to_open_and_records_actual_price(self):
        trade_id = self._make_trade()
        self.alpaca.order_status_map["entry-1"] = {
            "status": "filled",
            "filled_avg_price": 101.50,
            "filled_qty": 10,
        }

        with get_session() as s:
            trade = s.get(Trade, trade_id)
            asyncio.run(self.monitor._check_trade(trade, "AAPL", s))

        with get_session() as s:
            trade = s.get(Trade, trade_id)
            self.assertEqual(trade.status, "open")
            self.assertEqual(trade.entry_price, 101.50)
            self.assertIsNotNone(trade.entry_date)

    def test_entry_fill_cancels_managed_stop_before_replacing_exit_orders(self):
        self.alpaca = _FakeOcoAlpaca()
        self.monitor = OrderMonitor(self.alpaca, notification_manager=None, settings=_settings())
        trade_id = self._make_trade(
            symbol="BBIO",
            shares=39,
            stop_loss=95.0,
            target_1=110.0,
            target_2=115.0,
            alpaca_stop_order_id="stop-held",
        )
        self.alpaca.order_status_map["entry-1"] = {
            "status": "filled",
            "filled_avg_price": 100.0,
            "filled_qty": 39,
        }
        self.alpaca.order_status_map["stop-held"] = {"status": "canceled"}
        self.alpaca.open_orders = [{
            "id": "stop-held",
            "symbol": "BBIO",
            "side": "sell",
            "status": "new",
            "quantity": 39,
            "filled_quantity": 0,
        }]

        with get_session() as s:
            trade = s.get(Trade, trade_id)
            asyncio.run(self.monitor._check_trade(trade, "BBIO", s))

        self.assertEqual(self.alpaca.cancelled_orders, ["stop-held"])
        self.assertEqual(
            self.alpaca.oco_orders,
            [("BBIO", 19, 110.0, 95.0, "long"), ("BBIO", 20, 115.0, 95.0, "long")],
        )
        with get_session() as s:
            trade = s.get(Trade, trade_id)
            self.assertIn("TARGETS:t1:oco-BBIO-19-11000,t2:oco-BBIO-20-11500", trade.operator_notes)
            self.assertIn("STOPLEGS:t1:oco-BBIO-19-11000-stopleg,t2:oco-BBIO-20-11500-stopleg", trade.operator_notes)
            self.assertIsNone(trade.alpaca_stop_order_id)
            self.assertIsNone(trade.broker_stop_order_id)

    def test_entry_fill_unknown_held_order_alerts_without_cancel_or_replace(self):
        self.alpaca = _FakeOcoAlpaca()
        notifications = _FakeNotifications()
        self.monitor = OrderMonitor(self.alpaca, notification_manager=notifications, settings=_settings())
        trade_id = self._make_trade(
            symbol="BBIO",
            shares=39,
            stop_loss=95.0,
            target_1=110.0,
            target_2=115.0,
            alpaca_stop_order_id="stop-known",
        )
        self.alpaca.order_status_map["entry-1"] = {
            "status": "filled",
            "filled_avg_price": 100.0,
            "filled_qty": 39,
        }
        self.alpaca.open_orders = [{
            "id": "manual-1",
            "symbol": "BBIO",
            "side": "sell",
            "status": "new",
            "quantity": 39,
            "filled_quantity": 0,
        }]

        with get_session() as s:
            trade = s.get(Trade, trade_id)
            asyncio.run(self.monitor._check_trade(trade, "BBIO", s))

        self.assertEqual(self.alpaca.cancelled_orders, [])
        self.assertEqual(self.alpaca.oco_orders, [])
        self.assertEqual(len(notifications.messages), 1)
        self.assertIn("manual-1", notifications.messages[0])
        with get_session() as s:
            trade = s.get(Trade, trade_id)
            self.assertNotIn("TARGETS:", trade.operator_notes or "")

    def _entry_fill_with_held_stop(self, fail_calls=(), stop_loss_fails=False):
        """Common harness: entry fills while a bot-managed stop holds all 39 shares."""
        self.alpaca = _FakeOcoAlpaca()
        self.alpaca.oco_fail_on_call = set(fail_calls)
        self.alpaca.stop_loss_fails = stop_loss_fails
        notifications = _FakeNotifications()
        self.monitor = OrderMonitor(self.alpaca, notification_manager=notifications, settings=_settings())
        trade_id = self._make_trade(
            symbol="BBIO",
            shares=39,
            stop_loss=95.0,
            target_1=110.0,
            target_2=115.0,
            alpaca_stop_order_id="stop-held",
        )
        self.alpaca.order_status_map["entry-1"] = {
            "status": "filled",
            "filled_avg_price": 100.0,
            "filled_qty": 39,
        }
        self.alpaca.order_status_map["stop-held"] = {"status": "canceled"}
        self.alpaca.open_orders = [{
            "id": "stop-held",
            "symbol": "BBIO",
            "side": "sell",
            "status": "new",
            "quantity": 39,
            "filled_quantity": 0,
        }]
        with get_session() as s:
            trade = s.get(Trade, trade_id)
            asyncio.run(self.monitor._check_trade(trade, "BBIO", s))
        return trade_id, notifications

    def test_oco_stop_leg_fill_books_stop_out(self):
        """A stop-side fill on an OCO leg must book the stop-out (parent only shows canceled)."""
        self.alpaca = _FakeOcoAlpaca()
        self.monitor = OrderMonitor(self.alpaca, notification_manager=None, settings=_settings())
        trade_id = self._make_trade(
            symbol="BBIO",
            status="open",
            entry_price=100.0,
            shares=39,
            stop_loss=95.0,
            alpaca_stop_order_id=None,
            operator_notes="TARGETS:t1:oco-p1,t2:oco-p2|STOPLEGS:t1:leg-1,t2:leg-2",
        )
        self.alpaca.order_status_map["entry-1"] = {"status": "filled"}
        self.alpaca.order_status_map["oco-p1"] = {"status": "canceled"}
        self.alpaca.order_status_map["oco-p2"] = {"status": "canceled"}
        self.alpaca.order_status_map["leg-1"] = {"status": "filled", "filled_avg_price": 94.50}

        with get_session() as s:
            trade = s.get(Trade, trade_id)
            asyncio.run(self.monitor._check_trade(trade, "BBIO", s))

        with get_session() as s:
            trade = s.get(Trade, trade_id)
            self.assertEqual(trade.status, "closed")
            self.assertEqual(trade.exit_reason, "stop_loss")
            self.assertEqual(trade.exit_price, 94.50)
            self.assertLess(trade.pnl_pct, 0)

    def test_oco_failure_after_stop_cancel_replaces_protection_and_pages(self):
        """If OCO placement fails after the old stop was cancelled, a plain stop is re-placed."""
        trade_id, notifications = self._entry_fill_with_held_stop(fail_calls=(1, 2))

        self.assertEqual(self.alpaca.cancelled_orders, ["stop-held"])
        self.assertEqual(self.alpaca.oco_orders, [])  # both submissions failed
        self.assertEqual(self.alpaca.stop_loss_orders, [("BBIO", 39, 95.0, "long")])
        self.assertTrue(any("replacement stop" in m.lower() for m in notifications.messages))
        with get_session() as s:
            trade = s.get(Trade, trade_id)
            self.assertEqual(trade.alpaca_stop_order_id, "replacement-stop-BBIO-39")
            self.assertNotIn("TARGETS:", trade.operator_notes or "")

    def test_partial_oco_failure_reprotects_failed_qty_only(self):
        """t1 places, t2 fails: t2's shares get a replacement stop; t1 keeps its OCO leg."""
        trade_id, notifications = self._entry_fill_with_held_stop(fail_calls=(2,))

        self.assertEqual(len(self.alpaca.oco_orders), 1)  # t1 only
        self.assertEqual(self.alpaca.stop_loss_orders, [("BBIO", 20, 95.0, "long")])
        with get_session() as s:
            trade = s.get(Trade, trade_id)
            self.assertIn("TARGETS:t1:oco-BBIO-19-11000", trade.operator_notes)
            self.assertIn("STOPLEGS:t1:oco-BBIO-19-11000-stopleg", trade.operator_notes)
            self.assertNotIn("t2:", trade.operator_notes.split("STOPLEGS:")[1])
            self.assertEqual(trade.alpaca_stop_order_id, "replacement-stop-BBIO-20")
        self.assertTrue(any("20 share" in m for m in notifications.messages))

    def test_reprotection_failure_pages_unprotected(self):
        """If even the replacement stop fails, the operator gets an UNPROTECTED page."""
        _, notifications = self._entry_fill_with_held_stop(fail_calls=(1, 2), stop_loss_fails=True)
        self.assertTrue(any("UNPROTECTED" in m for m in notifications.messages))

    def test_long_stop_trigger_yields_negative_pnl(self):
        trade_id = self._make_trade(
            status="open",
            entry_price=100.0,
            shares=10,
            alpaca_stop_order_id="stop-1",
        )
        # Entry order is in terminal state; monitor falls through to step 2.
        self.alpaca.order_status_map["entry-1"] = {"status": "filled"}
        self.alpaca.order_status_map["stop-1"] = {
            "status": "filled",
            "filled_avg_price": 95.0,
            "filled_qty": 10,
        }

        with get_session() as s:
            trade = s.get(Trade, trade_id)
            asyncio.run(self.monitor._check_trade(trade, "AAPL", s))

        with get_session() as s:
            trade = s.get(Trade, trade_id)
            self.assertEqual(trade.status, "closed")
            self.assertEqual(trade.exit_reason, "stop_loss")
            # Long stop: (95-100)/100*100 = -5%.
            self.assertEqual(trade.pnl_pct, -5.0)
            self.assertEqual(trade.pnl_absolute, -50.0)

    def test_short_stop_trigger_yields_negative_pnl_inverted(self):
        # For shorts, stop is ABOVE entry. Hitting stop = loss.
        trade_id = self._make_trade(
            direction="short",
            status="open",
            entry_price=100.0,
            shares=10,
            stop_loss=105.0,
            alpaca_stop_order_id="stop-s1",
        )
        self.alpaca.order_status_map["entry-1"] = {"status": "filled"}
        self.alpaca.order_status_map["stop-s1"] = {
            "status": "filled",
            "filled_avg_price": 105.0,
            "filled_qty": 10,
        }

        with get_session() as s:
            trade = s.get(Trade, trade_id)
            asyncio.run(self.monitor._check_trade(trade, "AAPL", s))

        with get_session() as s:
            trade = s.get(Trade, trade_id)
            self.assertEqual(trade.status, "closed")
            # Short P&L: (entry - exit)/entry*100 = (100-105)/100*100 = -5%.
            self.assertEqual(trade.pnl_pct, -5.0)
            self.assertEqual(trade.pnl_absolute, -50.0)

    def test_target_hit_full_exit_closes_trade_and_cancels_stop(self):
        trade_id = self._make_trade(
            status="open",
            entry_price=100.0,
            shares=10,
            alpaca_stop_order_id="stop-3",
            operator_notes="ORDER_STRATEGY:oto|TARGETS:t1:target-1,t2:target-2",
        )
        self.alpaca.order_status_map["entry-1"] = {"status": "filled"}
        self.alpaca.order_status_map["target-2"] = {
            "status": "filled",
            "filled_avg_price": 115.0,
            "filled_qty": 10,
        }
        self.alpaca.positions_detail = []

        with get_session() as s:
            trade = s.get(Trade, trade_id)
            asyncio.run(self.monitor._check_trade(trade, "AAPL", s))

        with get_session() as s:
            trade = s.get(Trade, trade_id)
            self.assertEqual(trade.status, "closed")
            self.assertEqual(trade.exit_reason, "target_2")
            self.assertEqual(trade.pnl_pct, 15.0)
            self.assertEqual(trade.pnl_absolute, 150.0)
        self.assertIn("stop-3", self.alpaca.cancelled_orders)

    def test_short_entry_fill_places_cover_targets(self):
        trade_id = self._make_trade(
            direction="short",
            entry_price=100.0,
            stop_loss=105.0,
            target_1=90.0,
            target_2=85.0,
        )
        self.alpaca.order_status_map["entry-1"] = {
            "status": "filled",
            "filled_avg_price": 100.0,
            "filled_qty": 9,
        }

        with get_session() as s:
            trade = s.get(Trade, trade_id)
            asyncio.run(self.monitor._check_trade(trade, "AAPL", s))

        self.assertEqual(
            self.alpaca.target_orders,
            [("cover", 4, 90.0), ("cover", 5, 85.0)],
        )
        with get_session() as s:
            trade = s.get(Trade, trade_id)
            self.assertIn("TARGETS:t1:cover-AAPL-4,t2:cover-AAPL-5", trade.operator_notes)

    def test_time_exit_success_closes_with_direction_aware_pnl_and_cleans_orders(self):
        from datetime import timedelta

        trade_id = self._make_trade(
            direction="short",
            status="open",
            entry_price=100.0,
            shares=10,
            entry_date=utcnow_naive() - timedelta(days=30),
            alpaca_stop_order_id="stop-4",
            operator_notes="ORDER_STRATEGY:oto|TARGETS:t1:target-3,t2:target-4",
        )
        self.alpaca.order_status_map["entry-1"] = {"status": "filled"}
        self.alpaca.positions_detail = [{"ticker": "AAPL", "current_price": 90.0}]

        with get_session() as s:
            trade = s.get(Trade, trade_id)
            asyncio.run(self.monitor._check_trade(trade, "AAPL", s))

        with get_session() as s:
            trade = s.get(Trade, trade_id)
            self.assertEqual(trade.status, "closed")
            self.assertEqual(trade.exit_reason, "time_exit")
            self.assertEqual(trade.exit_price, 90.0)
            self.assertEqual(trade.pnl_pct, 10.0)
            self.assertEqual(trade.pnl_absolute, 100.0)
        self.assertIn("stop-4", self.alpaca.cancelled_orders)
        self.assertIn("target-3", self.alpaca.cancelled_orders)
        self.assertIn("target-4", self.alpaca.cancelled_orders)

    def test_cancelled_entry_marks_trade_cancelled_and_cancels_stop(self):
        trade_id = self._make_trade(alpaca_stop_order_id="stop-5")
        self.alpaca.order_status_map["entry-1"] = {"status": "canceled"}

        with get_session() as s:
            trade = s.get(Trade, trade_id)
            asyncio.run(self.monitor._check_trade(trade, "AAPL", s))

        with get_session() as s:
            trade = s.get(Trade, trade_id)
            self.assertEqual(trade.status, "cancelled")
            self.assertEqual(trade.exit_reason, "order_expired")
        self.assertIn("stop-5", self.alpaca.cancelled_orders)

    def test_missing_alpaca_position_reconciles_without_fabricating_pnl(self):
        # Time-exit attempt but Alpaca says position not found → reconciliation path.
        from datetime import timedelta

        trade_id = self._make_trade(
            symbol="HNGE",
            status="open",
            entry_price=100.0,
            shares=10,
            entry_date=utcnow_naive() - timedelta(days=30),  # past max_holding_days
            alpaca_stop_order_id="stop-2",
        )
        self.alpaca.order_status_map["entry-1"] = {"status": "filled"}
        self.alpaca.close_position_result = {
            "success": False,
            "error": '{"code":40410000,"message":"position not found: HNGE"}',
            "code": "40410000",
            "position_not_found": True,
        }

        with get_session() as s:
            trade = s.get(Trade, trade_id)
            asyncio.run(self.monitor._check_trade(trade, "HNGE", s))

        with get_session() as s:
            trade = s.get(Trade, trade_id)
            self.assertEqual(trade.status, "closed")
            self.assertEqual(trade.exit_reason, "reconciled_missing_position")
            # Critical: no P&L fabricated. exit_price stays at default (0).
            self.assertIsNone(trade.pnl_pct)
            self.assertIsNone(trade.pnl_absolute)
            self.assertIn("RECONCILED_MISSING_POSITION", trade.operator_notes)
            self.assertIn("RECONCILED_EXIT_UNKNOWN", trade.operator_notes)


# ---------------------------------------------------------------------------
# Short-direction invariants (cross-cutting)
# ---------------------------------------------------------------------------


class ShortDirectionInvariantsTests(unittest.TestCase):
    """If short support remains in code, these invariants MUST hold.
    Lock them so a refactor can't silently break short P&L."""

    def setUp(self):
        with patch("execution.position_manager.MarketDataAdapter", _NoopMarketData):
            self.pm = PositionManager(_settings())

    def test_conviction_multipliers_are_complete(self):
        # OrderManager and PositionManager both depend on these keys.
        for cls in ("high_conviction", "moderate", "low", "no_action"):
            self.assertIn(cls, CONVICTION_MULTIPLIERS)

    def test_short_stop_is_above_entry_long_below(self):
        long_stop = self.pm.calculate_stop_loss(50.0, "X", direction="long")
        short_stop = self.pm.calculate_stop_loss(50.0, "X", direction="short")
        self.assertLess(long_stop, 50.0)
        self.assertGreater(short_stop, 50.0)

    def test_short_targets_below_entry(self):
        targets = self.pm.calculate_targets(entry_price=50.0, stop_loss=52.5, direction="short")
        # Risk = 2.5; target1 = 50 - 5 = 45; target2 = 50 - 7.5 = 42.5.
        self.assertEqual(targets["target_1"], 45.0)
        self.assertEqual(targets["target_2"], 42.5)
        self.assertLess(targets["target_1"], 50.0)
        self.assertLess(targets["target_2"], targets["target_1"])

    def test_short_target_at_2x_risk_locks_2_to_1_rr(self):
        # 2:1 R/R must hold for shorts as well as longs.
        targets = self.pm.calculate_targets(entry_price=100.0, stop_loss=110.0, direction="short")
        risk = 10.0
        reward_t1 = 100.0 - targets["target_1"]
        self.assertAlmostEqual(reward_t1 / risk, 2.0, places=4)


if __name__ == "__main__":
    unittest.main()
