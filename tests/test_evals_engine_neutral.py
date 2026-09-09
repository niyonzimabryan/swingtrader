"""The eval harness reads the outcomes database on whichever engine it is on.

``evals/pnl_monitor.py`` opened the production ``.db`` file with ``sqlite3``
directly. Phase 0a flagged it as cutover work: after the Postgres cutover there
is no file to open, and the rollback tripwire would simply stop running — the
worst failure mode for a monitor, because nothing goes red.

This test runs it against the engine the suite targets, so the Postgres matrix
entry is what proves the fix.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from evals import pnl_monitor
from tests.dbfixture import TestDatabase


class DatabaseUrlTests(unittest.TestCase):
    def test_a_bare_path_still_means_sqlite(self):
        self.assertEqual(
            pnl_monitor.database_url("/data/swing_trader.db"),
            "sqlite:////data/swing_trader.db",
        )

    def test_a_url_is_passed_through(self):
        url = "postgresql+psycopg://user:pw@host:5432/railway"
        self.assertEqual(pnl_monitor.database_url(url), url)


class RealizedPnlTests(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("evals")
        self.addCleanup(self.db.cleanup)

        from database.db import get_session, init_db
        from database.models import Memo, Ticker, Trade

        init_db(self.db.url)
        with get_session() as session:
            session.add(Ticker(symbol="AAPL"))
        with get_session() as session:
            session.add(
                Memo(
                    ticker_id=1,
                    direction="long",
                    composite_score=0.8,
                    classification="high_conviction",
                    opus_critique=json.dumps({"opus_evaluation": {"conviction": "high"}}),
                )
            )
        with get_session() as session:
            for index in range(20):
                session.add(
                    Trade(
                        ticker_id=1,
                        memo_id=1,
                        status="closed",
                        pnl_pct=0.05,
                        pnl_absolute=100,
                        entry_date=datetime(2026, 5, 1, 14, 30),
                        exit_date=datetime(2026, 5, 10, 20, 0),
                        exit_reason="target_1",
                    )
                )
            for index in range(20):
                session.add(
                    Trade(
                        ticker_id=1,
                        memo_id=1,
                        status="closed",
                        pnl_pct=-0.05,
                        pnl_absolute=-100,
                        entry_date=datetime(2026, 9, 1, 14, 30),
                        exit_date=datetime(2026, 9, 10, 20, 0),
                        exit_reason="stop_loss",
                    )
                )

    def test_the_monitor_reads_the_current_engine(self):
        outcomes = pnl_monitor.realized_pnl(self.db.url)
        self.assertEqual(len(outcomes), 40)
        self.assertEqual(outcomes[0].conviction, "high")
        # Dates arrive as YYYY-MM-DD whichever driver returned them.
        self.assertEqual(outcomes[0].entry_date, "2026-05-01")

    def test_the_rollback_tripwire_still_fires(self):
        outcomes = pnl_monitor.realized_pnl(self.db.url)
        check = pnl_monitor.regression_check(outcomes, swap_date="2026-08-01")
        self.assertEqual(check["n_before"], 20)
        self.assertEqual(check["n_after"], 20)
        self.assertTrue(check["rollback_suggested"])


if __name__ == "__main__":
    unittest.main()
