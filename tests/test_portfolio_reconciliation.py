"""Reconciliation pages when the broker and this system disagree (Spec L §4.3).

Detection only: ``tracking/position_reconciliation.py`` already owns repair, and
a sync that silently rewrote trade rows would make the ledger's history depend
on which of the two jobs ran first.
"""

from __future__ import annotations

import unittest

from database.models import Ticker, Trade
from portfolio import reconcile
from portfolio.paging import RECONCILIATION_REQUIRED, RecordingPager
from portfolio.sync import run_sync
from tests import portfoliofixture as fx
from tests.dbfixture import TestDatabase
from utils.timeutils import utcnow_naive


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("portfolio_reconcile")
        self.addCleanup(self.db.cleanup)
        from database.db import init_db

        init_db(self.db.url)
        self.pager = RecordingPager()

    def _open_trade(self, symbol: str, broker: str = "fake"):
        from database.db import get_session

        with get_session() as session:
            ticker = Ticker(symbol=symbol, name=symbol, in_universe=False)
            session.add(ticker)
            session.flush()
            session.add(
                Trade(
                    ticker_id=ticker.id,
                    direction="long",
                    entry_price=10.0,
                    entry_date=utcnow_naive(),
                    shares=1,
                    stop_loss=9.0,
                    target_1=12.0,
                    target_2=14.0,
                    position_pct=1.0,
                    status="open",
                    setup_type="test",
                    signal_scores="{}",
                    regime_at_entry="unknown",
                    broker=broker,
                )
            )

    def _sync(self, broker=None):
        from database.db import get_session

        with get_session() as session:
            return run_sync(
                session,
                broker or fx.two_account_broker(),
                pager=self.pager,
                reconciler=reconcile.detect_mismatches,
            )

    def test_a_position_the_broker_does_not_report_raises_reconciliation_required(self):
        self._open_trade("NVDA")
        result = self._sync()
        self.assertIn(RECONCILIATION_REQUIRED, result.pages)
        page = next(p for e, p in self.pager.pages if e == RECONCILIATION_REQUIRED)
        kinds = {m["kind"] for m in page["mismatches"]}
        self.assertIn(reconcile.MISSING_AT_BROKER, kinds)
        self.assertIn("NVDA", {m["symbol"] for m in page["mismatches"]})

    def test_a_matching_position_does_not_page(self):
        self._open_trade("AMD")
        result = self._sync(
            fx.two_account_broker(
                agentic_holdings=[fx.holding("AMD", 6, price=151.2, basis=852.9)],
                primary_holdings=[],
            )
        )
        self.assertNotIn(RECONCILIATION_REQUIRED, result.pages)

    def test_an_unknown_position_pages_only_in_an_agent_placeable_account(self):
        """Every position in a read-only account is one the owner opened.

        Reporting each of them would page on the normal case, and a page that
        fires on the normal case is one nobody reads within a week.
        """
        result = self._sync(
            fx.two_account_broker(
                agentic_holdings=[],
                primary_holdings=[fx.holding("MSFT", 22, price=528.0, basis=8846.0)],
            )
        )
        self.assertNotIn(RECONCILIATION_REQUIRED, result.pages)

        self.pager.pages.clear()
        result = self._sync(
            fx.two_account_broker(
                agentic_holdings=[fx.holding("MSFT", 2, price=528.0, basis=1056.0)],
                primary_holdings=[],
            )
        )
        page = next(p for e, p in self.pager.pages if e == RECONCILIATION_REQUIRED)
        self.assertEqual(
            {m["kind"] for m in page["mismatches"]}, {reconcile.UNKNOWN_TO_SYSTEM}
        )

    def test_a_failing_reconciler_does_not_fail_the_sync(self):
        from database.db import get_session

        def explode(session, account, holdings):
            raise RuntimeError("reconciler is broken")

        with get_session() as session:
            result = run_sync(
                session, fx.two_account_broker(), pager=self.pager, reconciler=explode
            )
        self.assertTrue(result.ok, result.as_dict())


if __name__ == "__main__":
    unittest.main()
