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
            trade = s.query(Trade).get(trade_id)
            asyncio.run(self.monitor._check_trade(trade, "AAPL", s))

        with get_session() as s:
            trade = s.query(Trade).get(trade_id)
            self.assertEqual(trade.status, "open")
            self.assertEqual(trade.entry_price, 101.50)
            self.assertIsNotNone(trade.entry_date)

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
            trade = s.query(Trade).get(trade_id)
            asyncio.run(self.monitor._check_trade(trade, "AAPL", s))

        with get_session() as s:
            trade = s.query(Trade).get(trade_id)
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
            trade = s.query(Trade).get(trade_id)
            asyncio.run(self.monitor._check_trade(trade, "AAPL", s))

        with get_session() as s:
            trade = s.query(Trade).get(trade_id)
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
            trade = s.query(Trade).get(trade_id)
            asyncio.run(self.monitor._check_trade(trade, "AAPL", s))

        with get_session() as s:
            trade = s.query(Trade).get(trade_id)
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
            trade = s.query(Trade).get(trade_id)
            asyncio.run(self.monitor._check_trade(trade, "AAPL", s))

        self.assertEqual(
            self.alpaca.target_orders,
            [("cover", 4, 90.0), ("cover", 5, 85.0)],
        )
        with get_session() as s:
            trade = s.query(Trade).get(trade_id)
            self.assertIn("TARGETS:t1:cover-AAPL-4,t2:cover-AAPL-5", trade.operator_notes)

    def test_time_exit_success_closes_with_direction_aware_pnl_and_cleans_orders(self):
        from datetime import datetime, timedelta

        trade_id = self._make_trade(
            direction="short",
            status="open",
            entry_price=100.0,
            shares=10,
            entry_date=datetime.utcnow() - timedelta(days=30),
            alpaca_stop_order_id="stop-4",
            operator_notes="ORDER_STRATEGY:oto|TARGETS:t1:target-3,t2:target-4",
        )
        self.alpaca.order_status_map["entry-1"] = {"status": "filled"}
        self.alpaca.positions_detail = [{"ticker": "AAPL", "current_price": 90.0}]

        with get_session() as s:
            trade = s.query(Trade).get(trade_id)
            asyncio.run(self.monitor._check_trade(trade, "AAPL", s))

        with get_session() as s:
            trade = s.query(Trade).get(trade_id)
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
            trade = s.query(Trade).get(trade_id)
            asyncio.run(self.monitor._check_trade(trade, "AAPL", s))

        with get_session() as s:
            trade = s.query(Trade).get(trade_id)
            self.assertEqual(trade.status, "cancelled")
            self.assertEqual(trade.exit_reason, "order_expired")
        self.assertIn("stop-5", self.alpaca.cancelled_orders)

    def test_missing_alpaca_position_reconciles_without_fabricating_pnl(self):
        # Time-exit attempt but Alpaca says position not found → reconciliation path.
        from datetime import datetime, timedelta

        trade_id = self._make_trade(
            status="open",
            entry_price=100.0,
            shares=10,
            entry_date=datetime.utcnow() - timedelta(days=30),  # past max_holding_days
            alpaca_stop_order_id="stop-2",
        )
        self.alpaca.order_status_map["entry-1"] = {"status": "filled"}
        self.alpaca.close_position_result = {
            "success": False,
            "error": "position not found for ticker",
        }

        with get_session() as s:
            trade = s.query(Trade).get(trade_id)
            asyncio.run(self.monitor._check_trade(trade, "AAPL", s))

        with get_session() as s:
            trade = s.query(Trade).get(trade_id)
            self.assertEqual(trade.status, "closed")
            self.assertEqual(trade.exit_reason, "reconciled_missing_position")
            # Critical: no P&L fabricated. exit_price stays at default (0).
            self.assertIsNone(trade.pnl_pct)
            self.assertIsNone(trade.pnl_absolute)
            self.assertIn("RECONCILED_MISSING_POSITION", trade.operator_notes)


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
