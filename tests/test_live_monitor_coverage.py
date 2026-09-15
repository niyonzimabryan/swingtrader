"""Brief B — something watches a live Robinhood position (2026-09-15).

With ``EXECUTION_MODE=live`` and ``BROKER_PRIMARY=robinhood``, an owner-approved
entry became a real position that nothing reconciled: ``OrderMonitor`` filters
to Alpaca by design and ``PositionMonitor`` holds an Alpaca client by
construction, and ``main.py`` injected none of the reconcilers that its
docstring names as the mitigation.

These tests pin the wiring and, more importantly, its limits: the reconciler is
read-only, it never reaches a placement call, a broker it cannot reach is loud
rather than flat, and Alpaca/paper behaviour is what it was. The Robinhood side
runs through the real adapter over ``tests.robinhoodfixture``'s recorded-shape
transport, so the normalizers under test are the ones production runs. No
network, no live sender, no live broker.
"""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from database.db import get_session, init_db
from database.models import Ticker, Trade
from execution.ledger_reconciler import (
    LEDGER_MISMATCH,
    LEDGER_UNREACHABLE,
    BrokerPositionsUnavailable,
    LedgerReconciler,
    live_ledger_reconcilers,
)
from execution.position_monitor import PositionMonitor
from portfolio.paging import RecordingPager
from tests import robinhoodfixture as rh
from tests.dbfixture import TestDatabase
from tests.test_portfolio_import_graph import EQUITY_WRITE_TOOL_NAMES
from tests.test_strategy_lab_execution import ExecutionTestCase
from utils.timeutils import utcnow_naive

#: Every Robinhood MCP tool that places, reviews or cancels an equity order. The
#: monitor path must reach none of them (non-negotiable 1: no agent places an
#: order). Taken from the Spec L §8 guard rather than re-listed, so the two
#: cannot drift. The option and crypto write tools are deliberately *not* named
#: here: that same guard forbids those strings anywhere in the repository, which
#: is a stronger statement than any assertion this file could make.
WRITE_TOOLS = set(EQUITY_WRITE_TOOL_NAMES)

_SETTINGS = SimpleNamespace(monitor_broker_call_timeout_s=30, max_holding_days=20)


def open_trade(symbol: str, shares: int, *, broker: str, **overrides) -> int:
    """One active ``trades`` row, the way an approved entry leaves one."""
    with get_session() as session:
        ticker = session.query(Ticker).filter(Ticker.symbol == symbol).first()
        if ticker is None:
            ticker = Ticker(symbol=symbol, name=symbol, in_universe=False)
            session.add(ticker)
            session.flush()
        fields = dict(
            ticker_id=ticker.id,
            direction="long",
            entry_price=100.0,
            entry_date=utcnow_naive(),
            shares=shares,
            stop_loss=90.0,
            target_1=120.0,
            target_2=140.0,
            position_pct=1.0,
            status="open",
            setup_type="test",
            signal_scores="{}",
            regime_at_entry="unknown",
            broker=broker,
            execution_mode="live" if broker == "robinhood" else "paper",
        )
        fields.update(overrides)
        trade = Trade(**fields)
        session.add(trade)
        session.flush()
        return trade.id


class LedgerReconcilerTests(unittest.TestCase):
    """The comparison itself: what it finds, what it says, what it never does."""

    def setUp(self):
        self.db = TestDatabase("live_monitor_coverage")
        self.addCleanup(self.db.cleanup)
        init_db(self.db.url)
        self.pager = RecordingPager()

    def reconciler(self, **kwargs):
        broker = kwargs.pop("broker", None) or rh.fixture_broker(**kwargs)
        self.broker = broker
        return LedgerReconciler(broker, pager=self.pager)

    def kinds(self, report) -> list[str]:
        return [f.kind for f in report.findings]

    # -- the gap this closes ------------------------------------------------ #

    def test_a_live_robinhood_position_is_reconciled(self):
        """Fails against today's code, where nothing asks Robinhood anything."""
        open_trade("AMD", 6, broker="robinhood")
        open_trade("TDW", 9, broker="robinhood")

        report = self.reconciler()()

        self.assertEqual(self.kinds(report), ["matched", "matched"])
        self.assertTrue(report.ok)
        self.assertEqual(self.pager.events(), [])

    def test_a_quantity_divergence_pages_and_says_what_diverged(self):
        trade_id = open_trade("AMD", 10, broker="robinhood")
        open_trade("TDW", 9, broker="robinhood")

        report = self.reconciler()()

        self.assertEqual([f.kind for f in report.mismatches], ["quantity_mismatch"])
        self.assertEqual(self.pager.events(), [LEDGER_MISMATCH])
        _, detail = self.pager.pages[-1]
        self.assertEqual(detail["ticker"], "AMD")
        self.assertEqual(detail["trade_id"], trade_id)
        self.assertEqual(detail["reason_code"], "quantity_mismatch")
        self.assertEqual(detail["expected_quantity"], 10.0)
        self.assertEqual(detail["broker_quantity"], 6.0)
        self.assertIn("Do not place a compensating order", detail["recovery"])
        self.assertNotIn(rh.AGENTIC_ACCOUNT, str(detail))

    def test_a_position_missing_at_the_broker_pages(self):
        open_trade("NVDA", 5, broker="robinhood")

        report = self.reconciler()()

        self.assertIn("missing_at_broker", [f.kind for f in report.mismatches])
        detail = self.pager.pages[0][1]
        self.assertEqual(detail["reason_code"], "missing_at_broker")
        self.assertIn("the broker reports no position", detail["recovery"])

    def test_a_broker_position_no_open_trade_accounts_for_pages(self):
        report = self.reconciler()()

        self.assertEqual(
            sorted(f.ticker for f in report.mismatches), ["AMD", "TDW"]
        )
        self.assertEqual(
            {f.kind for f in report.mismatches}, {"unexpected_at_broker"}
        )

    def test_an_alpaca_row_is_not_part_of_the_robinhood_pass(self):
        """Broker isolation: the paper ledger is not evidence about Robinhood."""
        open_trade("AMD", 6, broker="alpaca")

        report = self.reconciler()()

        self.assertEqual({f.kind for f in report.mismatches}, {"unexpected_at_broker"})
        self.assertNotIn("missing_at_broker", self.kinds(report))

    def test_a_standing_divergence_pages_once_then_again_when_it_recurs(self):
        open_trade("AMD", 10, broker="robinhood")
        open_trade("TDW", 9, broker="robinhood")
        reconciler = self.reconciler()

        reconciler()
        reconciler()
        self.assertEqual(self.pager.events(), [LEDGER_MISMATCH])

        with get_session() as session:
            row = session.query(Trade).join(Ticker).filter(Ticker.symbol == "AMD").one()
            row.shares = 6
        reconciler()  # clean pass
        with get_session() as session:
            row = session.query(Trade).join(Ticker).filter(Ticker.symbol == "AMD").one()
            row.shares = 10
        reconciler()
        self.assertEqual(self.pager.events(), [LEDGER_MISMATCH, LEDGER_MISMATCH])

    # -- absence of data is not absence of exposure ------------------------- #

    def test_a_broker_error_is_loud_and_never_reads_as_flat(self):
        open_trade("AMD", 6, broker="robinhood")
        reconciler = self.reconciler(failing={"get_equity_positions": "mcp transport down"})

        with self.assertRaises(BrokerPositionsUnavailable):
            reconciler()

        self.assertEqual(self.pager.events(), [LEDGER_UNREACHABLE])
        detail = self.pager.pages[0][1]
        self.assertIn("NOT a report that the account is flat", detail["recovery"])
        self.assertNotIn(LEDGER_MISMATCH, self.pager.events())

    def test_an_adapter_with_no_account_number_fails_closed(self):
        """The adapter returns [] with no account configured — that is not flat."""
        broker = rh.fixture_broker()
        broker.account_number = ""
        reconciler = LedgerReconciler(broker, pager=self.pager)
        open_trade("AMD", 6, broker="robinhood")

        with self.assertRaises(BrokerPositionsUnavailable):
            reconciler()

        self.assertEqual(self.pager.events(), [LEDGER_UNREACHABLE])
        self.assertEqual(broker.transport.calls, [])

    def test_an_adapter_returning_none_fails_closed(self):
        broker = rh.fixture_broker()
        broker.get_positions_detail = lambda: None
        reconciler = LedgerReconciler(broker, pager=self.pager)

        with self.assertRaises(BrokerPositionsUnavailable):
            reconciler()
        self.assertEqual(self.pager.events(), [LEDGER_UNREACHABLE])

    def test_an_outage_pages_once_and_pages_again_after_it_clears(self):
        open_trade("AMD", 6, broker="robinhood")
        open_trade("TDW", 9, broker="robinhood")
        reconciler = self.reconciler(failing={"get_equity_positions": "down"})
        for _ in range(3):
            with self.assertRaises(BrokerPositionsUnavailable):
                reconciler()
        self.assertEqual(self.pager.events(), [LEDGER_UNREACHABLE])

        reconciler.broker.transport.failing = {}
        reconciler()  # recovered
        reconciler.broker.transport.failing = {"get_equity_positions": "down again"}
        with self.assertRaises(BrokerPositionsUnavailable):
            reconciler()
        self.assertEqual(
            self.pager.events(), [LEDGER_UNREACHABLE, LEDGER_UNREACHABLE]
        )

    # -- non-negotiable 1 --------------------------------------------------- #

    def test_the_reconciler_reaches_only_a_read_tool(self):
        open_trade("AMD", 1, broker="robinhood")  # a divergence, to exercise paging
        reconciler = self.reconciler()

        reconciler()

        called = reconciler.broker.transport.tools_called()
        self.assertEqual(called, {"get_equity_positions"})
        self.assertEqual(called & WRITE_TOOLS, set())

    def test_the_reconciler_writes_no_database_row(self):
        open_trade("AMD", 10, broker="robinhood")

        self.reconciler()()

        with get_session() as session:
            rows = session.query(Trade).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].shares, 10)  # not "corrected" to the broker's 6
            self.assertEqual(rows[0].status, "open")


class _RecordingAlpaca:
    """A paper broker that answers reads and records every call it was asked for."""

    def __init__(self, positions=()):
        self.positions = list(positions)
        self.calls: list[str] = []

    def get_positions_detail(self):
        self.calls.append("get_positions_detail")
        return list(self.positions)

    def get_account_info(self):
        self.calls.append("get_account_info")
        return {"equity": 100_000.0, "pnl_today_pct": 0.0}

    def __getattr__(self, item):  # every write method, caught rather than guessed
        if item.startswith("_"):
            raise AttributeError(item)

        def _refuse(*args, **kwargs):
            raise AssertionError(f"the monitor called {item!r} on the broker")

        self.calls.append(item)
        return _refuse


class MonitorTickTests(unittest.IsolatedAsyncioTestCase):
    """What the 60-second tick does, with and without reconcilers injected."""

    def setUp(self):
        self.db = TestDatabase("live_monitor_tick")
        self.addCleanup(self.db.cleanup)
        init_db(self.db.url)
        self.pager = RecordingPager()
        self.nm = AsyncMock()

    def monitor(self, alpaca, **kwargs):
        return PositionMonitor(alpaca, self.nm, _SETTINGS, **kwargs)

    async def test_the_reconcilers_run_even_when_the_paper_account_is_flat(self):
        """The bug in one line: they used to sit after `if not positions: return`."""
        ran = []
        monitor = self.monitor(_RecordingAlpaca([]), execution_reconcilers=(lambda: ran.append(1),))

        await monitor._check_positions()

        self.assertEqual(ran, [1])

    async def test_the_reconcilers_run_when_the_paper_broker_itself_fails(self):
        ran = []

        class _Broken(_RecordingAlpaca):
            def get_positions_detail(self):
                raise RuntimeError("alpaca down")

        monitor = self.monitor(_Broken(), execution_reconcilers=(lambda: ran.append(1),))

        with self.assertRaises(RuntimeError):
            await monitor._check_positions()

        self.assertEqual(ran, [1])

    async def test_a_live_robinhood_position_is_covered_by_the_tick(self):
        open_trade("AMD", 10, broker="robinhood")
        open_trade("TDW", 9, broker="robinhood")
        broker = rh.fixture_broker()
        monitor = self.monitor(
            _RecordingAlpaca([]),
            execution_reconcilers=(LedgerReconciler(broker, pager=self.pager),),
        )

        await monitor._check_positions()

        self.assertEqual(self.pager.events(), [LEDGER_MISMATCH])
        self.assertEqual(self.pager.pages[0][1]["ticker"], "AMD")
        self.assertEqual(broker.transport.tools_called(), {"get_equity_positions"})

    async def test_nothing_in_the_tick_places_modifies_or_cancels_anything(self):
        open_trade("AMD", 10, broker="robinhood")
        open_trade("NVDA", 4, broker="alpaca", stop_loss=200.0)
        broker = rh.fixture_broker()
        alpaca = _RecordingAlpaca(
            [{"ticker": "NVDA", "qty": 4, "entry_price": 150.0, "current_price": 100.0}]
        )
        monitor = self.monitor(
            alpaca, execution_reconcilers=(LedgerReconciler(broker, pager=self.pager),)
        )

        await monitor._check_positions()

        self.assertEqual(alpaca.calls, ["get_positions_detail"])
        self.assertEqual(broker.transport.tools_called(), {"get_equity_positions"})
        self.assertEqual(broker.transport.tools_called() & WRITE_TOOLS, set())

    async def test_a_stalled_reconciler_does_not_stall_the_loop(self):
        settings = SimpleNamespace(monitor_broker_call_timeout_s=0.05, max_holding_days=20)

        def stalls():
            time.sleep(0.4)

        monitor = PositionMonitor(
            _RecordingAlpaca([]), self.nm, settings, execution_reconcilers=(stalls,)
        )
        started = time.monotonic()
        await monitor._check_positions()
        self.assertLess(time.monotonic() - started, 0.35)

    async def test_the_loop_keeps_ticking_and_the_heartbeat_stays_fresh(self):
        settings = SimpleNamespace(monitor_broker_call_timeout_s=0.05, max_holding_days=20)

        def explodes():
            raise RuntimeError("robinhood unreachable")

        monitor = PositionMonitor(
            _RecordingAlpaca([]), self.nm, settings, execution_reconcilers=(explodes,)
        )
        monitor._is_market_hours = lambda: True
        monitor._running = True

        async def stop_after_one(_seconds):
            monitor._running = False

        with patch("execution.position_monitor.asyncio.sleep", stop_after_one):
            await monitor._monitor_loop()

        self.assertIsNotNone(monitor.last_tick)


class AlpacaRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Paper behaviour is what it was. Asserted, not assumed."""

    def setUp(self):
        self.db = TestDatabase("live_monitor_alpaca")
        self.addCleanup(self.db.cleanup)
        init_db(self.db.url)
        self.nm = AsyncMock()

    async def test_an_alpaca_position_still_flows_exactly_as_it_does_today(self):
        trade_id = open_trade("NVDA", 4, broker="alpaca", entry_price=150.0, stop_loss=140.0)
        alpaca = _RecordingAlpaca(
            [{"ticker": "NVDA", "qty": 4, "entry_price": 150.0, "current_price": 130.0}]
        )
        monitor = PositionMonitor(alpaca, self.nm, _SETTINGS)

        await monitor._check_positions()

        self.nm.position_stop_breached.assert_awaited_once()
        kwargs = self.nm.position_stop_breached.await_args.kwargs
        self.assertEqual(kwargs["ticker"], "NVDA")
        self.assertEqual(kwargs["trade_id"], trade_id)
        with get_session() as session:
            row = session.get(Trade, trade_id)
            self.assertEqual(row.peak_price, 130.0)  # the tick's own bookkeeping

    async def test_an_injected_reconciler_does_not_change_the_alpaca_pass(self):
        trade_id = open_trade("NVDA", 4, broker="alpaca", entry_price=150.0, stop_loss=140.0)
        alpaca = _RecordingAlpaca(
            [{"ticker": "NVDA", "qty": 4, "entry_price": 150.0, "current_price": 130.0}]
        )
        broker = rh.fixture_broker()
        monitor = PositionMonitor(
            alpaca,
            self.nm,
            _SETTINGS,
            execution_reconcilers=(LedgerReconciler(broker, pager=RecordingPager()),),
        )

        await monitor._check_positions()

        self.nm.position_stop_breached.assert_awaited_once()
        self.assertEqual(
            self.nm.position_stop_breached.await_args.kwargs["trade_id"], trade_id
        )


class StrategyLabPaperArmTests(ExecutionTestCase):
    """A paper arm is unaffected: the live pass reads a different ledger.

    ``ExecutionTestCase`` builds the whole chain a ``strategy_trades`` row hangs
    off — version, experiment, arm, snapshot, decision — and approves one
    execution against a fake paper broker. The live ledger reconciler must leave
    every bit of that alone: it compares ``trades`` rows at one named broker,
    and a Strategy Lab execution is neither.
    """

    def test_a_paper_arm_execution_is_untouched_by_the_live_ledger_pass(self):
        self.build()
        card = self.propose()
        self.approve(card)
        before = self.status(card.execution_id)

        report = LedgerReconciler(rh.fixture_broker(), pager=RecordingPager())()

        self.assertEqual(self.status(card.execution_id), before)
        self.assertEqual([f for f in report.findings if f.execution_id], [])
        self.assertIsNone(self.entry_block())  # no entry is blocked by this pass


class WiringTests(unittest.TestCase):
    """`live_ledger_reconcilers` decides coverage from the execution mode."""

    def _pipeline(self, mode: str):
        from execution.brokers.factory import BrokerRouter

        settings = SimpleNamespace(execution_mode=mode)
        paper = SimpleNamespace(name="alpaca", account_number="paper")
        primary = paper if mode == "paper" else SimpleNamespace(
            name="robinhood", account_number="****4021"
        )
        pipeline = SimpleNamespace(
            paper_broker=paper,
            primary_broker=primary,
            broker=BrokerRouter(settings, paper, primary),
        )
        return pipeline, settings

    def test_paper_mode_injects_nothing(self):
        pipeline, settings = self._pipeline("paper")
        self.assertEqual(live_ledger_reconcilers(pipeline, settings), ())

    def test_live_mode_injects_one_bound_to_the_concrete_primary_broker(self):
        pipeline, settings = self._pipeline("live")

        reconcilers = live_ledger_reconcilers(pipeline, settings, pager=RecordingPager())

        self.assertEqual(len(reconcilers), 1)
        self.assertIs(reconcilers[0].broker, pipeline.primary_broker)
        self.assertIsNot(reconcilers[0].broker, pipeline.broker)  # never the router
        self.assertEqual(reconcilers[0].broker_name, "robinhood")

    def test_a_live_mode_primary_that_is_the_paper_broker_injects_nothing(self):
        """BROKER_PRIMARY=alpaca with EXECUTION_MODE=live: already covered."""
        from execution.brokers.factory import BrokerRouter

        settings = SimpleNamespace(execution_mode="live")
        paper = SimpleNamespace(name="alpaca", account_number="paper")
        pipeline = SimpleNamespace(
            paper_broker=paper,
            primary_broker=paper,
            broker=BrokerRouter(settings, paper, paper),
        )
        self.assertEqual(live_ledger_reconcilers(pipeline, settings), ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
