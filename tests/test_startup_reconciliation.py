"""Startup position visibility: the async seam, the loud failure, and the hazard.

Three things are asserted here and they are separate defects that arrived
together on 2026-09-15, when ``EXECUTION_MODE`` was flipped to ``live`` with
``BROKER_PRIMARY=robinhood``:

1. **The seam.** ``main._reconcile_startup_positions`` used to call a
   synchronous broker from ``main()``'s running event loop. The Robinhood
   adapter's ``_call_tool_sync`` calls ``asyncio.run()``, which is illegal
   there, so every boot logged
   ``asyncio.run() cannot be called from a running event loop`` and the process
   started with no view of the account. ``StartupReconciliationSeamTests``
   drives the real shape — a broker whose read genuinely calls ``asyncio.run``
   — from inside a running loop, and fails against the pre-fix code.

2. **The failure mode.** It was swallowed into a ``log.warning``, which is why
   nobody noticed for as long as it took to switch to live trading.
   ``StartupReconciliationFailureTests`` asserts the behaviour — a page on the
   configured channel and an ``error`` line — not that some log call happened.

3. **The hazard.** A ledger that has never synced reports zero exposure, and
   zero exposure is what an empty account reports too. The direction of that
   error is permissive: unseen exposure reads as no exposure, and the
   concentration and sector caps are computed over it.
   ``BlankVersusEmptyLedgerTests`` pins that the two states are distinguishable
   through the production guard, using ``run_sync`` to produce both rather than
   hand-built ORM rows.

No network, no live sender, no live broker: the Robinhood adapter appears only
with its one network method substituted, which is what ``tests/robinhoodfixture``
already does.
"""

from __future__ import annotations

import asyncio
import unittest
import warnings
from datetime import timedelta
from types import SimpleNamespace

from database.db import get_session
from database.models import Ticker, Trade
from portfolio import paging
from portfolio.guards import STALE_LEDGER, check_ledger_fresh
from portfolio.paging import RecordingPager
from portfolio.proposals import read_context
from portfolio.sync import run_sync
from tests import portfoliofixture as fx
from tests.dbfixture import TestDatabase

from main import _reconcile_startup_positions


class _RecordingLog:
    """A logger that keeps what it was told, by level.

    The level matters to the assertions: the whole point of this change is that
    a startup that cannot see the account stops being a ``warning``.
    """

    def __init__(self):
        self.infos: list[tuple[str, dict]] = []
        self.warnings: list[tuple[str, dict]] = []
        self.errors: list[tuple[str, dict]] = []

    def info(self, event, **fields):
        self.infos.append((event, fields))

    def warning(self, event, **fields):
        self.warnings.append((event, fields))

    def error(self, event, **fields):
        self.errors.append((event, fields))

    def events(self, bucket) -> list[str]:
        return [event for event, _ in bucket]


class _AsyncioRunBroker:
    """A broker shaped like ``RobinhoodMCPBroker``: its read calls ``asyncio.run``.

    This is the whole regression in one class. Nothing else about the adapter
    matters here — what matters is that the synchronous read bottoms out in
    ``asyncio.run``, which raises ``RuntimeError`` on a thread that already has
    a running loop and returns normally on one that does not.
    """

    name = "robinhood"
    account_number = "rh-account-1"

    def __init__(self, positions=None):
        self.positions = positions if positions is not None else [
            {
                "ticker": "NOW",
                "qty": 2,
                "entry_price": 100.0,
                "market_value": 210.0,
                "side": "long",
            }
        ]
        self.calls = 0

    def get_positions_detail(self):
        self.calls += 1

        async def _fetch():
            return self.positions

        return asyncio.run(_fetch())


class _RaisingBroker:
    name = "robinhood"
    account_number = "rh-account-1"

    def get_positions_detail(self):
        raise RuntimeError("Robinhood MCP get_equity_positions failed: 401 unauthorized")


class _HangingBroker:
    name = "robinhood"
    account_number = "rh-account-1"

    def __init__(self, seconds: float = 2.0):
        self.seconds = seconds

    def get_positions_detail(self):
        import time

        time.sleep(self.seconds)
        return []


def _settings(**overrides):
    base = {"execution_mode": "live", "monitor_broker_call_timeout_s": 30}
    base.update(overrides)
    return SimpleNamespace(**base)


class StartupReconciliationSeamTests(unittest.TestCase):
    """The regression: reconcile from inside a running loop and survive."""

    def setUp(self):
        self.db = TestDatabase("startup_reconcile")
        self.addCleanup(self.db.cleanup)
        from database.db import init_db

        init_db(self.db.url)

    def test_reconciles_a_robinhood_shaped_broker_from_a_running_event_loop(self):
        broker = _AsyncioRunBroker()
        pipeline = SimpleNamespace(
            paper_broker=None, broker=SimpleNamespace(active=broker)
        )
        log = _RecordingLog()
        pager = RecordingPager()

        async def _boot():
            # `asyncio.run` gives this coroutine a running loop, exactly as
            # `main()` does. Against the pre-fix code the broker read raises
            # RuntimeError here and the position is never seen.
            await _reconcile_startup_positions(pipeline, _settings(), log, pager=pager)

        asyncio.run(_boot())

        self.assertEqual(broker.calls, 1)
        self.assertEqual(pager.events(), [], "a successful read must not page")
        self.assertEqual(log.errors, [])

    def test_the_reconciliation_result_reaches_the_ledger(self):
        pipeline = SimpleNamespace(
            paper_broker=None, broker=SimpleNamespace(active=_AsyncioRunBroker())
        )
        log = _RecordingLog()

        asyncio.run(
            _reconcile_startup_positions(
                pipeline, _settings(), log, pager=RecordingPager()
            )
        )

        with get_session() as session:
            trade = (
                session.query(Trade)
                .join(Ticker)
                .filter(Ticker.symbol == "NOW", Trade.status == "open")
                .first()
            )
            self.assertIsNotNone(trade, "the startup read must create a trade row")
            self.assertEqual(trade.broker, "robinhood")
            self.assertEqual(trade.execution_mode, "live")
            self.assertEqual(trade.broker_account_id, "rh-account-1")

        events = [event for event, _ in log.infos]
        self.assertIn("startup_positions_reconciled", events)

    def test_paper_broker_path_is_unchanged(self):
        """A plain synchronous Alpaca-shaped broker still reconciles, as paper."""

        class _PaperBroker:
            name = "alpaca"
            account_number = ""

            def get_positions_detail(self):
                return [
                    {
                        "ticker": "AAPL",
                        "qty": 10,
                        "entry_price": 100.0,
                        "market_value": 1_050.0,
                        "side": "long",
                    }
                ]

        pipeline = SimpleNamespace(
            paper_broker=_PaperBroker(), broker=SimpleNamespace(active=None)
        )

        asyncio.run(
            _reconcile_startup_positions(
                pipeline, _settings(), _RecordingLog(), pager=RecordingPager()
            )
        )

        with get_session() as session:
            trade = (
                session.query(Trade)
                .join(Ticker)
                .filter(Ticker.symbol == "AAPL", Trade.status == "open")
                .first()
            )
            self.assertIsNotNone(trade)
            self.assertEqual(trade.broker, "alpaca")
            self.assertEqual(trade.execution_mode, "paper")


class StartupReconciliationFailureTests(unittest.TestCase):
    """Loud, not fatal — and "loud" is asserted as behaviour, not as a log call."""

    def setUp(self):
        self.db = TestDatabase("startup_reconcile_fail")
        self.addCleanup(self.db.cleanup)
        from database.db import init_db

        init_db(self.db.url)

    def _run(self, broker, **settings_overrides):
        pipeline = SimpleNamespace(
            paper_broker=None, broker=SimpleNamespace(active=broker)
        )
        log = _RecordingLog()
        pager = RecordingPager()
        asyncio.run(
            _reconcile_startup_positions(
                pipeline, _settings(**settings_overrides), log, pager=pager
            )
        )
        return log, pager

    def test_a_failing_broker_does_not_crash_the_boot_and_pages(self):
        log, pager = self._run(_RaisingBroker())

        self.assertEqual(
            pager.events(),
            [paging.STARTUP_RECONCILIATION_FAILED],
            "the owner's channel must be told the process is blind to the account",
        )
        _event, detail = pager.pages[0]
        self.assertEqual(detail["broker"], "robinhood")
        self.assertEqual(detail["execution_mode"], "live")
        self.assertIn("401 unauthorized", detail["error"])
        self.assertIn("scripts.portfolio_sync", detail["recovery"])

    def test_the_failure_is_an_error_line_not_a_warning(self):
        log, _pager = self._run(_RaisingBroker())

        self.assertEqual(
            [event for event, _ in log.errors],
            [paging.STARTUP_RECONCILIATION_FAILED],
        )
        self.assertEqual(
            log.warnings,
            [],
            "a warning is what let this fail silently through the switch to live",
        )

    def test_a_pager_that_itself_fails_still_does_not_crash_the_boot(self):
        def _broken_pager(event, detail):
            raise RuntimeError("smtp is down")

        pipeline = SimpleNamespace(
            paper_broker=None, broker=SimpleNamespace(active=_RaisingBroker())
        )
        log = _RecordingLog()

        asyncio.run(
            _reconcile_startup_positions(pipeline, _settings(), log, pager=_broken_pager)
        )

        events = [event for event, _ in log.errors]
        self.assertIn(paging.STARTUP_RECONCILIATION_FAILED, events)
        self.assertIn("startup_reconciliation_page_failed", events)

    def test_a_hanging_broker_is_bounded_and_reported_as_a_timeout(self):
        """The bound the bare call never had: a stalled broker cannot hang the boot."""
        log, pager = self._run(_HangingBroker(seconds=2.0), monitor_broker_call_timeout_s=0.2)

        self.assertEqual(pager.events(), [paging.STARTUP_RECONCILIATION_FAILED])
        _event, detail = pager.pages[0]
        self.assertIn("timed out", detail["error"])

    def test_one_broker_failing_does_not_stop_the_other(self):
        class _PaperBroker:
            name = "alpaca"
            account_number = ""

            def get_positions_detail(self):
                return [
                    {
                        "ticker": "AAPL",
                        "qty": 10,
                        "entry_price": 100.0,
                        "market_value": 1_050.0,
                        "side": "long",
                    }
                ]

        pipeline = SimpleNamespace(
            paper_broker=_PaperBroker(),
            broker=SimpleNamespace(active=_RaisingBroker()),
        )
        pager = RecordingPager()
        asyncio.run(
            _reconcile_startup_positions(pipeline, _settings(), _RecordingLog(), pager=pager)
        )

        self.assertEqual(pager.events(), [paging.STARTUP_RECONCILIATION_FAILED])
        with get_session() as session:
            trade = (
                session.query(Trade)
                .join(Ticker)
                .filter(Ticker.symbol == "AAPL")
                .first()
            )
            self.assertIsNotNone(trade, "the paper read must still have run")


class BlankVersusEmptyLedgerTests(unittest.TestCase):
    """Unknown exposure and no exposure are the same number and different states.

    Both ledgers below report ``equity == 0``. One of them has never been read
    successfully; the other has been read and is genuinely flat. If a cap could
    not tell them apart it would size a trade against a book it cannot see.
    """

    def setUp(self):
        self.db = TestDatabase("blank_vs_empty")
        self.addCleanup(self.db.cleanup)
        from database.db import init_db

        init_db(self.db.url)

    def _sync(self, broker):
        with get_session() as session:
            return run_sync(session, broker, pager=RecordingPager(), now=fx.NOW)

    def _context(self):
        with get_session() as session:
            return read_context(session, now=fx.NOW)

    def test_a_never_synced_ledger_is_zero_and_refuses(self):
        # The production shape of the 2026-09-15 outage: the account is known,
        # its read failed, so nothing was written and nothing was zeroed.
        broker = fx.two_account_broker()
        broker.accounts[0].error = (
            "RobinhoodMCPError: Robinhood MCP get_equity_tax_lots failed"
        )
        broker.accounts[1].error = "RobinhoodMCPError: Robinhood MCP get_accounts failed"
        result = self._sync(broker)
        self.assertFalse(result.ok)

        context = self._context()
        self.assertEqual(context.equity, 0.0)
        self.assertIsNone(
            context.as_of_utc,
            "a never-synced ledger has no as_of, which is what the guard reads",
        )

        rejection = check_ledger_fresh(context.as_of_utc, fx.NOW)
        self.assertIsNotNone(rejection, "a blank ledger must refuse to size a trade")
        self.assertEqual(rejection.code, STALE_LEDGER)
        self.assertIn("never been synced", rejection.reason)

    def test_a_genuinely_empty_ledger_is_zero_and_serves(self):
        broker = fx.two_account_broker(agentic_holdings=(), primary_holdings=())
        result = self._sync(broker)
        self.assertTrue(result.ok, result.as_dict())

        context = self._context()
        self.assertEqual(
            context.equity, 1225.0, "settled + unsettled cash is exposure the caps see"
        )
        self.assertIsNotNone(context.as_of_utc)
        self.assertIsNone(
            check_ledger_fresh(context.as_of_utc, fx.NOW),
            "a fresh, flat account is a real answer and must not refuse",
        )

    def test_a_ledger_that_stopped_syncing_refuses_once_it_goes_stale(self):
        """The other direction of the same hazard: stale is not fresh-and-flat."""
        self._sync(fx.two_account_broker(agentic_holdings=(), primary_holdings=()))
        context = self._context()

        self.assertIsNone(check_ledger_fresh(context.as_of_utc, fx.NOW))
        rejection = check_ledger_fresh(context.as_of_utc, fx.NOW + timedelta(hours=4))
        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.code, STALE_LEDGER)


class RobinhoodErrorSurfaceTests(unittest.TestCase):
    """``_call_tool_sync``: say what actually went wrong, and refuse the loop.

    The ledger held ``last_sync_error = "RobinhoodMCPError: Robinhood MCP
    get_equity_tax_lots failed: unhandled errors in a TaskGroup (1
    sub-exception)"``. That string names no cause: it is ``str()`` of the
    ``BaseExceptionGroup`` anyio raises around the MCP transport, and the
    sub-exception — the actual diagnosis — was discarded on the way out.
    """

    def _broker(self):
        from execution.brokers.robinhood import RobinhoodMCPBroker

        # Inert: no token store is configured under the test guard, and
        # `_call_tool` is replaced below in every test, so nothing dials out.
        return RobinhoodMCPBroker(
            SimpleNamespace(
                robinhood_mcp_url="https://example.invalid/mcp",
                robinhood_account_number="",
            )
        )

    def test_the_real_sub_exception_survives_the_task_group(self):
        from execution.brokers.robinhood import RobinhoodMCPError

        broker = self._broker()

        async def _call_tool(name, arguments):
            raise BaseExceptionGroup(
                "unhandled errors in a TaskGroup",
                [ConnectionError("Server disconnected without sending a response")],
            )

        broker._call_tool = _call_tool

        with self.assertRaises(RobinhoodMCPError) as caught:
            broker._call_tool_sync("get_equity_tax_lots", {"account_number": "x"})

        message = str(caught.exception)
        self.assertIn("get_equity_tax_lots", message)
        self.assertIn("ConnectionError", message)
        self.assertIn("Server disconnected", message)

    def test_a_nested_group_is_walked_to_its_leaves(self):
        from execution.brokers.robinhood import RobinhoodMCPError

        broker = self._broker()

        async def _call_tool(name, arguments):
            raise BaseExceptionGroup(
                "unhandled errors in a TaskGroup",
                [
                    BaseExceptionGroup(
                        "unhandled errors in a TaskGroup",
                        [ValueError("token refresh rejected")],
                    )
                ],
            )

        broker._call_tool = _call_tool

        with self.assertRaises(RobinhoodMCPError) as caught:
            broker._call_tool_sync("get_portfolio", {})

        self.assertIn("token refresh rejected", str(caught.exception))

    def test_a_plain_exception_is_reported_unchanged_in_substance(self):
        from execution.brokers.robinhood import RobinhoodMCPError

        broker = self._broker()

        async def _call_tool(name, arguments):
            raise TimeoutError("read timed out")

        broker._call_tool = _call_tool

        with self.assertRaises(RobinhoodMCPError) as caught:
            broker._call_tool_sync("get_accounts", {})

        self.assertIn("TimeoutError", str(caught.exception))
        self.assertIn("read timed out", str(caught.exception))

    def test_calling_from_a_running_loop_names_the_fix_and_leaks_no_coroutine(self):
        from execution.brokers.robinhood import RobinhoodMCPError

        broker = self._broker()
        called = []

        async def _call_tool(name, arguments):  # pragma: no cover - must not run
            called.append(name)
            return {}

        broker._call_tool = _call_tool

        async def _on_the_loop():
            with self.assertRaises(RobinhoodMCPError) as caught:
                broker._call_tool_sync("get_equity_positions", {})
            return caught.exception

        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            error = asyncio.run(_on_the_loop())

        self.assertIn("running event loop", str(error))
        self.assertIn("call_with_timeout", str(error))
        self.assertEqual(called, [])
        self.assertEqual(
            [w for w in seen if "never awaited" in str(w.message)],
            [],
            "the coroutine must not be constructed just to be dropped",
        )


if __name__ == "__main__":
    unittest.main()
