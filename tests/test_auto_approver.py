"""Spec I2 — paper autonomy sandbox.

The load-bearing test here is the HARD SAFETY GUARD: under any non-paper config
(live mode, ALLOW_LIVE_TRADING, Robinhood primary, a live-flagged broker) the
auto-approver must place ZERO orders. Plus cohort caps, tagging, and the
exploration path that reuses the human order flow.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from database import db as db_module
from database.db import get_session
from tests.dbfixture import init_test_db
from database.models import Memo, ScoredCandidate, Ticker, Trade
from execution.auto_approver import AutoApprover


def _settings(**overrides):
    base = dict(
        auto_approve_paper=True,
        execution_mode="paper",
        allow_live_trading=False,
        alpaca_paper_only=True,
        auto_approve_min_score=0.55,
        exploration_min_score=0.45,
        exploration_band_enabled=True,
        auto_max_concurrent_positions=8,
        auto_max_new_positions_per_scan=4,
        exploration_max_new_per_scan=2,
        exploration_position_pct_factor=0.5,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Active:
    def __init__(self, name="alpaca", live=False):
        self.name = name
        self.live_trading = live


class _Broker:
    def __init__(self, name="alpaca", live=False, positions=None):
        self.active = _Active(name, live)
        self._positions = positions or []

    def get_positions_detail(self):
        return list(self._positions)


class _OrderManager:
    """Records placements and creates a real Trade row so tagging can be asserted."""

    def __init__(self, ticker_id):
        self.ticker_id = ticker_id
        self.calls = []

    async def execute_approved_trade(self, memo_id, force_place=False):
        self.calls.append(memo_id)
        with get_session() as s:
            trade = Trade(
                ticker_id=self.ticker_id, memo_id=memo_id, direction="long",
                entry_price=100.0, shares=10, stop_loss=95.0, status="pending_fill",
                broker="alpaca", operator_notes="ORDER_STRATEGY:oto",
            )
            s.add(trade)
            s.flush()
            tid = trade.id
        return {"success": True, "trade_id": tid, "ticker": "AAA", "broker": "alpaca"}


class _MemoGen:
    def __init__(self, ticker_id):
        self.ticker_id = ticker_id
        self.created = []

    def create_exploration_memo(self, ticker, scoring_result, regime, position_pct_factor=1.0):
        self.created.append((ticker, position_pct_factor))
        with get_session() as s:
            m = Memo(
                ticker_id=self.ticker_id, composite_score=scoring_result.get("final_score", 0),
                status="auto_exploration", trade_params="{}",
            )
            s.add(m)
            s.flush()
            mid = m.id
        return {"memo_id": mid, "trade_params": {}, "direction": "long"}


class _Cand:
    def __init__(self, ticker, final_score, cohort, memo_id=None, ledger_id=None):
        self.ticker = ticker
        self.final_score = final_score
        self.cohort = cohort
        self.memo_id = memo_id
        self.ledger_id = ledger_id
        self.scoring_result = {"final_score": final_score, "direction": "bullish"}
        self.regime = {}
        self.auto_executed = False


class AutoApproverTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = init_test_db("auto")
        with get_session() as s:
            t = Ticker(symbol="AAA", sector="Tech", in_universe=True)
            s.add(t)
            s.flush()
            self.ticker_id = t.id
        self.order_manager = _OrderManager(self.ticker_id)
        self.memo_gen = _MemoGen(self.ticker_id)

    def tearDown(self):
        if db_module.engine is not None:
            db_module.engine.dispose()
        db_module.engine = None
        db_module.SessionLocal = None
        self.db.cleanup()
        self.tmp.cleanup()

    def _approver(self, settings, broker=None):
        return AutoApprover(settings, self.order_manager, self.memo_gen, broker or _Broker())

    def _seed_memo(self, score=0.60):
        with get_session() as s:
            m = Memo(ticker_id=self.ticker_id, composite_score=score, status="pending", trade_params="{}")
            s.add(m)
            s.flush()
            return m.id

    def _seed_ledger(self, cohort="memo"):
        with get_session() as s:
            row = ScoredCandidate(run_id="r", ticker="AAA", final_score=0.6, cohort=cohort)
            s.add(row)
            s.flush()
            return row.id


class HardSafetyGuardTests(AutoApproverTestBase):
    """Under any non-paper config: zero orders, zero trades."""

    def _assert_refused(self, settings, broker=None):
        cand = _Cand("AAA", 0.70, "memo", memo_id=self._seed_memo())
        result = asyncio.run(self._approver(settings, broker).run([cand]))
        self.assertTrue(result["refused"])
        self.assertEqual(result["placed"], 0)
        self.assertEqual(self.order_manager.calls, [])  # execute path never touched
        with get_session() as s:
            self.assertEqual(s.query(Trade).count(), 0)  # no order placed
        self.assertFalse(cand.auto_executed)

    def test_refuses_live_execution_mode(self):
        self._assert_refused(_settings(execution_mode="live"))

    def test_refuses_allow_live_trading_flag(self):
        self._assert_refused(_settings(allow_live_trading=True))

    def test_refuses_robinhood_primary(self):
        self._assert_refused(_settings(), broker=_Broker(name="robinhood"))

    def test_refuses_live_flagged_broker(self):
        self._assert_refused(_settings(), broker=_Broker(live=True))

    def test_refuses_when_paper_only_disabled(self):
        self._assert_refused(_settings(alpaca_paper_only=False))

    def test_is_paper_safe_true_only_for_paper_alpaca(self):
        self.assertTrue(self._approver(_settings()).is_paper_safe())


class CohortPlacementTests(AutoApproverTestBase):
    def test_disabled_flag_places_nothing(self):
        cand = _Cand("AAA", 0.70, "memo", memo_id=self._seed_memo())
        result = asyncio.run(self._approver(_settings(auto_approve_paper=False)).run([cand]))
        self.assertFalse(result["enabled"])
        self.assertEqual(result["placed"], 0)
        self.assertEqual(self.order_manager.calls, [])

    def test_memo_cohort_places_tags_and_marks_ledger(self):
        memo_id = self._seed_memo(0.62)
        ledger_id = self._seed_ledger("memo")
        cand = _Cand("AAA", 0.62, "memo", memo_id=memo_id, ledger_id=ledger_id)
        result = asyncio.run(self._approver(_settings()).run([cand]))

        self.assertEqual(result["placed"], 1)
        self.assertEqual(result["memo"], 1)
        self.assertEqual(self.order_manager.calls, [memo_id])
        self.assertTrue(cand.auto_executed)
        with get_session() as s:
            trade = s.query(Trade).first()
            self.assertIn("AUTO:memo", trade.operator_notes)
            self.assertEqual(s.query(Memo).filter_by(id=memo_id).first().status, "approved")
            row = s.query(ScoredCandidate).filter_by(id=ledger_id).first()
            self.assertTrue(row.paper_traded)
            self.assertEqual(row.cohort, "memo")

    def test_exploration_cohort_creates_memo_at_reduced_size(self):
        ledger_id = self._seed_ledger("exploration")
        cand = _Cand("AAA", 0.50, "exploration", memo_id=None, ledger_id=ledger_id)
        result = asyncio.run(self._approver(_settings()).run([cand]))

        self.assertEqual(result["exploration"], 1)
        self.assertEqual(self.memo_gen.created, [("AAA", 0.5)])  # reduced position factor
        with get_session() as s:
            trade = s.query(Trade).first()
            self.assertIn("AUTO:exploration", trade.operator_notes)
            self.assertTrue(s.query(ScoredCandidate).filter_by(id=ledger_id).first().paper_traded)

    def test_memo_cap_enforced(self):
        cands = [_Cand("AAA", 0.60 + i * 0.01, "memo", memo_id=self._seed_memo()) for i in range(3)]
        result = asyncio.run(self._approver(_settings(auto_max_new_positions_per_scan=2)).run(cands))
        self.assertEqual(result["memo"], 2)
        self.assertEqual(len(self.order_manager.calls), 2)

    def test_exploration_cap_enforced(self):
        cands = [_Cand("AAA", 0.46 + i * 0.01, "exploration", ledger_id=self._seed_ledger()) for i in range(3)]
        result = asyncio.run(self._approver(_settings(exploration_max_new_per_scan=1)).run(cands))
        self.assertEqual(result["exploration"], 1)

    def test_exploration_band_disabled_skips_exploration(self):
        cand = _Cand("AAA", 0.50, "exploration", ledger_id=self._seed_ledger())
        result = asyncio.run(self._approver(_settings(exploration_band_enabled=False)).run([cand]))
        self.assertEqual(result["exploration"], 0)
        self.assertEqual(self.order_manager.calls, [])

    def test_concurrent_cap_blocks_when_full(self):
        broker = _Broker(positions=[{"ticker": "XYZ"}] * 8)  # already at cap of 8
        cand = _Cand("AAA", 0.70, "memo", memo_id=self._seed_memo())
        result = asyncio.run(self._approver(_settings(), broker).run([cand]))
        self.assertEqual(result["placed"], 0)

    def test_skips_already_held_ticker(self):
        broker = _Broker(positions=[{"ticker": "AAA"}])
        cand = _Cand("AAA", 0.70, "memo", memo_id=self._seed_memo())
        result = asyncio.run(self._approver(_settings(), broker).run([cand]))
        self.assertEqual(result["placed"], 0)
        self.assertEqual(self.order_manager.calls, [])

    def test_memo_prioritized_over_exploration_for_slots(self):
        broker = _Broker(positions=[{"ticker": "XYZ"}] * 7)  # one slot left
        memo_cand = _Cand("AAA", 0.70, "memo", memo_id=self._seed_memo())
        expl_cand = _Cand("BBB", 0.50, "exploration", ledger_id=self._seed_ledger())
        result = asyncio.run(self._approver(_settings(), broker).run([expl_cand, memo_cand]))
        self.assertEqual(result["memo"], 1)
        self.assertEqual(result["exploration"], 0)  # no slots left after memo


if __name__ == "__main__":
    unittest.main()
