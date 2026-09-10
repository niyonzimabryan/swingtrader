"""The Spec L §8 sync rows: append-only, never zeroes, fails closed, ingests externals.

Every test here drives ``portfolio.sync.run_sync`` against a broker — the fake
one for the failure policies it can express, and the *real* Robinhood adapter
replayed over recorded-shape fixtures for the parsing. Nothing constructs ORM
rows by hand: a test that did would prove the reader works and say nothing
about the writer, which is the half these rows are about.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from sqlalchemy import select

from database.models import BrokerageAccount, BrokerOrder, CashBalance, Holding, TaxLot
from portfolio import paging
from portfolio.paging import RecordingPager
from portfolio.sync import mass_deletion_check, run_sync
from tests import portfoliofixture as fx
from tests.dbfixture import TestDatabase
from tests.robinhoodfixture import fixture_broker


class PortfolioSyncTests(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("portfolio_sync")
        self.addCleanup(self.db.cleanup)
        from database.db import init_db

        init_db(self.db.url)
        self.pager = RecordingPager()

    def _session(self):
        from database.db import get_session

        return get_session()

    def _sync(self, broker, **kwargs):
        with self._session() as session:
            return run_sync(session, broker, pager=self.pager, **kwargs)

    def _holdings(self, *, current_only=True):
        """Holdings as plain dicts.

        ``get_session`` closes the session on exit, so an ORM row read outside
        the block raises ``DetachedInstanceError``. Reading the columns here
        keeps every assertion below about values rather than about SQLAlchemy's
        identity map.
        """
        with self._session() as session:
            statement = select(Holding)
            if current_only:
                statement = statement.where(Holding.superseded_at.is_(None))
            return [
                {
                    "symbol": h.symbol,
                    "account_id": h.account_id,
                    "quantity": h.quantity,
                    "last_price": h.last_price,
                    "cost_basis": h.cost_basis,
                    "instrument_type": h.instrument_type,
                    "sync_id": h.sync_id,
                    "superseded_at": h.superseded_at,
                    "superseded_by_sync_id": h.superseded_by_sync_id,
                }
                for h in session.execute(statement.order_by(Holding.id)).scalars()
            ]

    def _account(self, label: str) -> dict:
        with self._session() as session:
            row = session.execute(
                select(BrokerageAccount).where(BrokerageAccount.label == label)
            ).scalar_one()
            return {
                "id": row.id,
                "external_account_id": row.external_account_id,
                "account_type": row.account_type,
                "agent_placeable": row.agent_placeable,
                "last_sync_error": row.last_sync_error,
                "last_sync_error_at": row.last_sync_error_at,
                "capabilities": row.capabilities,
                "capabilities_probed_at": row.capabilities_probed_at,
            }

    # --- Spec L §8: test_sync_is_append_only -------------------------------

    def test_sync_is_append_only(self):
        """Historical holdings remain queryable after a later sync."""
        first = self._sync(fx.two_account_broker())
        self.assertTrue(first.ok, first.as_dict())
        first_rows = self._holdings()
        self.assertEqual({h["symbol"] for h in first_rows}, {"AMD", "TDW"})

        later = fx.NOW + timedelta(hours=1)
        second = self._sync(
            fx.two_account_broker(
                as_of=later,
                agentic_holdings=[
                    fx.holding("AMD", 6, price=160.0, basis=852.9),
                    fx.holding("TDW", 4, price=41.93),
                ],
            ),
            now=later,
        )
        self.assertTrue(second.ok, second.as_dict())

        current = self._holdings()
        agentic_id = first_rows[0]["account_id"]
        amd = next(h for h in current if h["symbol"] == "AMD" and h["account_id"] == agentic_id)
        self.assertEqual(amd["last_price"], 160.0)

        everything = self._holdings(current_only=False)
        self.assertGreater(len(everything), len(current))
        superseded = [h for h in everything if h["superseded_at"] is not None]
        self.assertTrue(superseded, "the first sync's rows were not retained")
        # The point of the append: the earlier values are still readable, and
        # they still say what they said.
        old_amd = next(h for h in superseded if h["symbol"] == "AMD")
        self.assertEqual(old_amd["last_price"], 151.2)
        self.assertEqual(old_amd["superseded_by_sync_id"], second.sync_id)
        self.assertNotEqual(first.sync_id, second.sync_id)

        # Every row a run wrote shares that run's sync_id.
        self.assertEqual(
            {h["sync_id"] for h in current if h["symbol"] in {"AMD", "TDW"}},
            {second.sync_id},
        )

    # --- Spec L §8: test_sync_never_zeroes_on_error ------------------------

    def test_sync_never_zeroes_on_error(self):
        """A broker exception leaves prior rows and sets the account stale."""
        self._sync(fx.two_account_broker())
        before = {(h["symbol"], h["quantity"]) for h in self._holdings()}
        self.assertTrue(before)

        broken = fx.two_account_broker()
        broken.accounts[0].error = "upstream 503 from get_equity_positions"
        broken.accounts[0].holdings = ()
        result = self._sync(broken, now=fx.NOW + timedelta(hours=1))

        self.assertFalse(result.ok)
        after = {(h["symbol"], h["quantity"]) for h in self._holdings()}
        self.assertEqual(
            {row for row in after if row[0] in {"AMD", "TDW"}},
            {row for row in before if row[0] in {"AMD", "TDW"}},
            "an unreadable account must leave its previous rows exactly as they were",
        )
        self.assertNotIn(0.0, {q for _, q in after})

        account = self._account("Agentic")
        self.assertIn("503", account["last_sync_error"])
        self.assertIsNotNone(account["last_sync_error_at"])
        self.assertIn(paging.SYNC_ACCOUNT_FAILED, self.pager.events())

    def test_a_whole_broker_failure_writes_nothing_and_pages(self):
        self._sync(fx.two_account_broker())
        before = len(self._holdings(current_only=False))

        result = self._sync(fx.two_account_broker(fail_with="connection reset"))
        self.assertFalse(result.ok)
        self.assertIn("connection reset", result.error)
        self.assertEqual(len(self._holdings(current_only=False)), before)
        self.assertIn(paging.SYNC_FAILED, self.pager.events())

    # --- Spec L §8: test_mass_deletion_fails_closed ------------------------

    def test_mass_deletion_fails_closed(self):
        """A sync dropping >50% of holdings pages and writes nothing."""
        self._sync(
            fx.two_account_broker(
                agentic_holdings=[
                    fx.holding("AMD", 6, price=151.2, basis=852.9),
                    fx.holding("TDW", 9, price=41.93, basis=354.96),
                    fx.holding("MSFT", 2, price=528.0, basis=804.0),
                    fx.holding("NVDA", 1, price=145.0, basis=150.0),
                ]
            )
        )
        agentic_id = self._account("Agentic")["id"]
        before = [
            (h["symbol"], h["sync_id"])
            for h in self._holdings()
            if h["account_id"] == agentic_id
        ]
        self.assertEqual(len({s for s, _ in before}), 4)

        later = fx.NOW + timedelta(hours=1)
        result = self._sync(
            fx.two_account_broker(
                as_of=later, agentic_holdings=[fx.holding("AMD", 6, price=151.2, basis=852.9)]
            ),
            now=later,
        )

        agentic = next(a for a in result.accounts if a.account_key.endswith("4021"))
        self.assertTrue(agentic.blocked, result.as_dict())
        self.assertEqual(agentic.holdings_written, 0)
        self.assertIn("TDW:equity", agentic.dropped_symbols)
        self.assertIn(paging.MASS_DELETION_BLOCKED, self.pager.events())

        after = [
            (h["symbol"], h["sync_id"])
            for h in self._holdings()
            if h["account_id"] == agentic_id
        ]
        self.assertEqual(
            sorted(after),
            sorted(before),
            "nothing may be written for the account whose check failed closed",
        )

        # And the account that was fine still synced: failing closed is
        # per-account, not a whole-run abort.
        primary = next(a for a in result.accounts if a.account_key.endswith("7788"))
        self.assertTrue(primary.ok)

    def test_mass_deletion_can_be_forced_by_a_human(self):
        self._sync(
            fx.two_account_broker(
                agentic_holdings=[fx.holding("AMD", 6), fx.holding("TDW", 9), fx.holding("MSFT", 2)]
            )
        )
        later = fx.NOW + timedelta(hours=1)
        result = self._sync(
            fx.two_account_broker(as_of=later, agentic_holdings=[fx.holding("AMD", 6)]),
            now=later,
            force_mass_deletion=True,
        )
        self.assertTrue(result.ok, result.as_dict())
        current = {h["symbol"] for h in self._holdings() if h["symbol"] != "MSFT"}
        self.assertIn("AMD", current)

    def test_a_trim_is_not_a_deletion(self):
        """Quantity falling to a fraction of a position must not trip the check."""
        current = [
            type("Row", (), {"symbol": "AMD", "instrument_type": "equity"})(),
            type("Row", (), {"symbol": "TDW", "instrument_type": "equity"})(),
        ]
        incoming = [fx.holding("AMD", 1), fx.holding("TDW", 1)]
        blocked, dropped, fraction = mass_deletion_check(current, incoming)
        self.assertFalse(blocked)
        self.assertEqual(dropped, ())
        self.assertEqual(fraction, 0.0)

    def test_an_empty_ledger_is_not_a_mass_deletion(self):
        blocked, dropped, fraction = mass_deletion_check([], [])
        self.assertFalse(blocked)
        self.assertEqual(fraction, 0.0)

    # --- Spec L §8: test_external_orders_ingested --------------------------

    def test_external_orders_ingested(self):
        """An order placed outside this system appears with origin='external'."""
        self._sync(fx.two_account_broker())
        with self._session() as session:
            orders = [
                {
                    "broker_order_id": o.broker_order_id,
                    "origin": o.origin,
                    "execution_id": o.execution_id,
                    "status": o.status,
                    "time_in_force": o.time_in_force,
                }
                for o in session.execute(select(BrokerOrder)).scalars()
            ]
        self.assertTrue(orders)
        order = next(o for o in orders if o["broker_order_id"] == "ord-7731")
        self.assertEqual(order["origin"], "external")
        self.assertIsNone(order["execution_id"])
        self.assertEqual(order["status"], "queued")
        self.assertEqual(order["time_in_force"], "gtc")

    def test_re_syncing_an_order_updates_it_rather_than_duplicating_it(self):
        self._sync(fx.two_account_broker())
        later = fx.NOW + timedelta(hours=1)
        broker = fx.two_account_broker(as_of=later)
        filled = broker.accounts[0].orders[0]
        broker.accounts[0].orders = (
            type(filled)(
                **{
                    **filled.__dict__,
                    "status": "filled",
                    "filled_quantity": 2.0,
                    "average_fill_price": 147.9,
                }
            ),
        )
        self._sync(broker, now=later)
        with self._session() as session:
            rows = [
                (o.status, o.origin)
                for o in session.execute(
                    select(BrokerOrder).where(BrokerOrder.broker_order_id == "ord-7731")
                ).scalars()
            ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], ("filled", "external"))

    # --- the ledger tables the sync fills ----------------------------------

    def test_lots_and_cash_are_written_with_the_run_s_sync_id(self):
        result = self._sync(fx.two_account_broker())
        with self._session() as session:
            lots = [(lot.sync_id, lot.booking_method) for lot in session.execute(select(TaxLot)).scalars()]
            cash = [
                (row.sync_id, row.settled_cash, row.unsettled_cash, len(row.pending_settlements))
                for row in session.execute(select(CashBalance)).scalars()
            ]
        self.assertEqual({sync_id for sync_id, _ in lots}, {result.sync_id})
        self.assertEqual({sync_id for sync_id, *_ in cash}, {result.sync_id})
        self.assertEqual({method for _, method in lots}, {"STRICT"})
        agentic_cash = next(row for row in cash if row[1] == 80.0)
        self.assertEqual(agentic_cash[2], 145.0)
        self.assertEqual(agentic_cash[3], 1)

    def test_capabilities_are_recorded_on_the_account(self):
        self._sync(fx.two_account_broker())
        account = self._account("Agentic")
        self.assertFalse(account["capabilities"]["can_place_attached_stop"])
        self.assertTrue(account["capabilities"]["can_place_standalone_gtc_stop"])
        self.assertIsNotNone(account["capabilities_probed_at"])
        self.assertTrue(account["agent_placeable"])
        self.assertEqual(account["account_type"], "cash")


class RobinhoodFixtureSyncTests(unittest.TestCase):
    """The real adapter's normalizers, replayed over recorded-shape fixtures.

    Recorded shape, not a live recording — see
    ``tests/fixtures/robinhood/README.md``. What this establishes is that the
    key aliases, the string-typed numbers, the nesting, and the missing fields
    are handled; it does not establish anything about Robinhood's live values.
    """

    def setUp(self):
        self.db = TestDatabase("robinhood_sync")
        self.addCleanup(self.db.cleanup)
        from database.db import init_db

        init_db(self.db.url)

    def test_the_adapter_reads_both_accounts_through_the_real_normalizers(self):
        broker = fixture_broker()
        from database.db import get_session

        with get_session() as session:
            result = run_sync(session, broker, pager=RecordingPager(), now=datetime(2026, 9, 9, 14, 0))
        self.assertTrue(result.ok, result.as_dict())

        with get_session() as session:
            accounts = [
                (a.external_account_id, a.agent_placeable, a.account_type)
                for a in session.execute(select(BrokerageAccount)).scalars()
            ]
            holdings = [
                (h.symbol, h.instrument_type)
                for h in session.execute(
                    select(Holding).where(Holding.superseded_at.is_(None))
                ).scalars()
            ]

        self.assertEqual(
            {number: placeable for number, placeable, _ in accounts},
            {"****4021": True, "****7788": False},
        )
        self.assertEqual(
            {number: kind for number, _, kind in accounts},
            {"****4021": "cash", "****7788": "margin"},
        )
        self.assertEqual({symbol for symbol, _ in holdings}, {"AMD", "TDW", "MSFT"})
        self.assertIn("option", {kind for _, kind in holdings})

    def test_only_read_tools_are_called(self):
        broker = fixture_broker()
        broker.fetch_ledger_snapshot()
        called = broker.transport.tools_called()
        self.assertTrue(called)
        for name in called:
            self.assertTrue(
                name.startswith("get_"),
                f"the sync called {name!r}; only get_* read tools are permitted.",
            )

    def test_an_account_whose_options_cannot_be_read_is_marked_stale_not_emptied(self):
        """Options failing must not produce a book that silently lacks them."""
        broker = fixture_broker(failing={"get_option_positions": "upstream 500"})
        snapshot = broker.fetch_ledger_snapshot()
        agentic = next(a for a in snapshot.accounts if a.account.external_account_id == "****4021")
        self.assertFalse(agentic.ok)
        self.assertIn("500", agentic.error)
        self.assertEqual(agentic.holdings, ())


if __name__ == "__main__":
    unittest.main()
