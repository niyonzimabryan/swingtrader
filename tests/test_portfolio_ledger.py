"""The Spec L §8 read-side rows: nulls, options, staleness, settlement, wash sales.

These are the rules a portfolio answer has to obey before anyone acts on it:
an unknown basis stays unknown, an option is never dropped, exposure spans every
account, a stale ledger refuses to feed a proposal, and unsettled cash is not
spendable. Each one is a way a plausible-looking answer can be wrong.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta

from portfolio import ledger
from portfolio.dividends import DividendDeclaration, reconstruct_dividends, total_reconstructed
from portfolio.exposure import UNSUPPORTED_INSTRUMENT_WARNING, aggregate_exposure
from portfolio.freshness import StaleLedger, provenance, require_fresh
from portfolio.guards import (
    ACCOUNT_NOT_PLACEABLE,
    STALE_LEDGER,
    UNSETTLED_CASH,
    UnknownCostBasis,
    check_account_placeable,
    check_ledger_fresh,
    check_settled_cash,
    require_cost_basis,
)
from portfolio.paging import RecordingPager
from portfolio.settlement import settlement_date, settlement_view
from portfolio.sync import run_sync
from portfolio.wash_sale import (
    WASH_SALE_WINDOW_DAYS,
    RealizedLoss,
    flag_wash_sale_window,
    in_window,
)
from tests import portfoliofixture as fx
from tests.dbfixture import TestDatabase


class LedgerReadTests(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("portfolio_ledger")
        self.addCleanup(self.db.cleanup)
        from database.db import init_db

        init_db(self.db.url)

    def _sync(self, broker=None, **kwargs):
        from database.db import get_session

        with get_session() as session:
            return run_sync(session, broker or fx.two_account_broker(), pager=RecordingPager(), **kwargs)

    def _overview(self, *, now=None, **kwargs):
        from database.db import get_session

        with get_session() as session:
            return ledger.portfolio_overview(session, now=now or fx.NOW, **kwargs)

    def _detail(self, symbol, *, now=None):
        from database.db import get_session

        with get_session() as session:
            return ledger.position_detail(session, symbol, now=now or fx.NOW)

    # --- Spec L §8: test_unknown_basis_is_null_not_zero ---------------------

    def test_unknown_basis_is_null_not_zero(self):
        """Consumers of a null basis flag or raise; none of them computes."""
        self._sync()
        overview = self._overview()
        tdw = next(h for h in overview["holdings"] if h["symbol"] == "TDW")
        self.assertIsNone(tdw["cost_basis"])
        self.assertIsNone(tdw["average_cost"])
        self.assertFalse(tdw["cost_basis_known"])
        self.assertTrue(
            any("unknown_cost_basis" in w for w in overview["warnings"]),
            overview["warnings"],
        )

        detail = self._detail("TDW")
        self.assertIsNone(detail["unrealized"])
        self.assertIsNone(detail["cost_basis"])
        self.assertFalse(detail["cost_basis_known"])
        # A lot with no basis reports it as unknown too, not as a zero lot.
        lot = next(lot for lot in detail["tax_lots"] if lot["broker_lot_id"] == "lot-4b71")
        self.assertIsNone(lot["cost_basis"])
        self.assertFalse(lot["cost_basis_known"])

        # And the position that *does* have one still computes normally, so the
        # rule is about unknown values rather than about giving up.
        amd = self._detail("AMD")
        self.assertTrue(amd["cost_basis_known"])
        self.assertIsNotNone(amd["unrealized"])

    def test_require_cost_basis_raises_rather_than_returning_zero(self):
        with self.assertRaises(UnknownCostBasis):
            require_cost_basis(None, symbol="TDW")
        # Zero is a real basis (a gifted or written-down lot) and is returned.
        self.assertEqual(require_cost_basis(0.0, symbol="TDW"), 0.0)
        self.assertEqual(require_cost_basis(852.9, symbol="AMD"), 852.9)

    # --- Spec L §8: test_options_never_silently_omitted ---------------------

    def test_options_never_silently_omitted(self):
        """A synced option renders unsupported_instrument_present with notional."""
        broker = fx.two_account_broker(
            agentic_holdings=[
                fx.holding("AMD", 6, price=151.2, basis=852.9),
                fx.holding(
                    "AMD",
                    -1,
                    price=215.0,
                    instrument_type="option",
                    notional=-14000.0,
                    detail={"option_type": "put", "strike_price": 140.0},
                ),
            ]
        )
        self._sync(broker)
        overview = self._overview()

        symbols = [h["symbol"] for h in overview["holdings"]]
        self.assertIn("AMD", symbols)
        options = [h for h in overview["holdings"] if h["instrument_type"] == "option"]
        self.assertEqual(len(options), 1, "the option was dropped from the holdings list")
        self.assertEqual(options[0]["notional"], -14000.0)

        unsupported = overview["exposure"]["unsupported_instruments"]
        self.assertEqual(len(unsupported), 1)
        self.assertEqual(unsupported[0]["instrument_type"], "option")
        self.assertEqual(unsupported[0]["notional"], -14000.0)
        self.assertEqual(unsupported[0]["detail"]["option_type"], "put")
        self.assertTrue(
            any(UNSUPPORTED_INSTRUMENT_WARNING in w for w in overview["warnings"]),
            overview["warnings"],
        )
        self.assertIn("14,000", " ".join(overview["warnings"]))

        detail = self._detail("AMD")
        self.assertTrue(
            any(UNSUPPORTED_INSTRUMENT_WARNING in w for w in detail["warnings"]),
            detail["warnings"],
        )

    # --- Spec L §8: test_risk_caps_span_all_accounts (read half) ------------

    def test_risk_caps_span_all_accounts(self):
        """Exposure counts the read-only account's holding in the same name."""
        self._sync()
        overview = self._overview()

        amd = next(
            row
            for row in overview["exposure"]["by_symbol"]
            if row["symbol"] == "AMD" and row["instrument_type"] == "equity"
        )
        self.assertEqual(amd["quantity"], 51.0, "6 in the Agentic account + 45 in the primary")
        self.assertEqual(sorted(amd["accounts"]), ["Agentic", "Primary"])
        self.assertAlmostEqual(amd["value"], (6 + 45) * 151.2, places=2)

        labels = {a["label"] for a in overview["accounts"]}
        self.assertEqual(labels, {"Agentic", "Primary"})
        self.assertEqual(
            {a["label"]: a["agent_placeable"] for a in overview["accounts"]},
            {"Agentic": True, "Primary": False},
            "a cap computed over the placeable account alone would miss the "
            "primary account's 45 shares of the same name",
        )
        self.assertGreater(overview["exposure"]["weights"]["AMD"], 0.5)

    def test_exposure_by_narrative_tag_spans_accounts_too(self):
        from database.db import get_session
        from database.models import ExposureTag

        self._sync()
        with get_session() as session:
            session.add(ExposureTag(symbol="AMD", tag="ai-infra", source="dossier"))
        overview = self._overview()
        self.assertIn("ai-infra", overview["exposure"]["by_tag"])
        self.assertAlmostEqual(
            overview["exposure"]["by_tag"]["ai-infra"], round((6 + 45) * 151.2, 2), places=2
        )

    # --- Spec K §4.2 / Spec L §4: provenance and staleness ------------------

    def test_every_read_tool_returns_provenance_and_flags_staleness(self):
        self._sync()
        fresh = self._overview(now=fx.NOW + timedelta(minutes=10))
        self.assertFalse(fresh["provenance"]["stale"])
        self.assertEqual(fresh["provenance"]["data_quality"], "fresh")
        self.assertIn("holdings", fresh["provenance"]["sources"])

        stale = self._overview(now=fx.NOW + timedelta(minutes=90))
        self.assertTrue(stale["provenance"]["stale"])
        self.assertEqual(stale["provenance"]["data_quality"], "stale")
        self.assertAlmostEqual(stale["provenance"]["age_minutes"], 90.0, places=1)
        # Flagged, not hidden: the data is still there.
        self.assertTrue(stale["holdings"])
        self.assertTrue(any("stale" in w for w in stale["provenance"]["warnings"]))

        for payload in (self._detail("AMD"), stale):
            self.assertIn("provenance", payload)
            self.assertTrue(payload["provenance"]["sources"])

    def test_an_unsynced_ledger_says_unknown_rather_than_empty(self):
        overview = self._overview()
        self.assertEqual(overview["provenance"]["data_quality"], "unknown")
        self.assertTrue(overview["provenance"]["stale"])
        self.assertIsNone(overview["provenance"]["as_of_utc"])
        self.assertEqual(overview["holdings"], [])

    def test_the_ledger_is_as_fresh_as_its_stalest_account(self):
        """One healthy account must not mask another that has stopped syncing."""
        self._sync()
        later = fx.NOW + timedelta(hours=3)
        broken = fx.two_account_broker(as_of=later)
        broken.accounts[0].error = "upstream 503"
        self._sync(broken, now=later)

        overview = self._overview(now=later)
        self.assertTrue(overview["provenance"]["stale"])
        self.assertTrue(
            any("account_sync_error" in w for w in overview["warnings"]), overview["warnings"]
        )

    # --- Spec L §8: test_stale_ledger_refuses_proposal ----------------------

    def test_stale_ledger_refuses_proposal(self):
        """A proposal path refuses stale holdings with the age; a read serves them."""
        self._sync()
        stale_now = fx.NOW + timedelta(minutes=61)

        with self.assertRaises(StaleLedger) as caught:
            require_fresh(fx.NOW, stale_now)
        message = str(caught.exception)
        self.assertIn("stale_ledger", message)
        self.assertIn("61.0 minutes", message)
        self.assertIn("60 minutes", message)

        rejection = check_ledger_fresh(fx.NOW, stale_now)
        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.code, STALE_LEDGER)
        self.assertAlmostEqual(rejection.detail["age_minutes"], 61.0, places=1)
        self.assertEqual(rejection.as_dict()["status"], "risk_rejected")

        # Inside the budget it does not refuse.
        self.assertIsNone(check_ledger_fresh(fx.NOW, fx.NOW + timedelta(minutes=59)))
        # A ledger that has never synced refuses too, and says which case it is.
        never = check_ledger_fresh(None, stale_now)
        self.assertIn("never been synced", never.reason)

        # And the same moment still *reads*, with the flag set — the two halves
        # of the freshness contract point in opposite directions on purpose.
        overview = self._overview(now=stale_now)
        self.assertTrue(overview["provenance"]["stale"])
        self.assertTrue(overview["holdings"])

    # --- Spec L §8: test_proposal_on_readonly_account_rejected --------------

    def test_proposal_on_readonly_account_rejected(self):
        """agent_placeable=False lands a proposal in risk_rejected with the reason."""
        self._sync()
        from database.db import get_session
        from database.models import BrokerageAccount
        from sqlalchemy import select

        with get_session() as session:
            accounts = {
                a.label: type("A", (), {
                    "label": a.label,
                    "broker": a.broker,
                    "agent_placeable": a.agent_placeable,
                    "external_account_id": a.external_account_id,
                })()
                for a in session.execute(select(BrokerageAccount)).scalars()
            }

        rejection = check_account_placeable(accounts["Primary"])
        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.code, ACCOUNT_NOT_PLACEABLE)
        self.assertIn("Primary", rejection.reason)
        self.assertIn("read-only", rejection.reason)
        self.assertEqual(rejection.as_dict()["status"], "risk_rejected")

        self.assertIsNone(check_account_placeable(accounts["Agentic"]))

    # --- Spec L §8: test_unsettled_cash_rejected / test_t1_settlement_modelled

    def test_unsettled_cash_rejected(self):
        """A proposal needing T+1 proceeds is refused with the settlement date."""
        self._sync()
        overview = self._overview()
        agentic_cash = next(row for row in overview["cash"] if row["account"] == "Agentic")
        self.assertEqual(agentic_cash["settled_cash"], 80.0)
        self.assertEqual(agentic_cash["unsettled_cash"], 145.0)
        self.assertEqual(agentic_cash["spendable_today"], 80.0)
        self.assertEqual(
            agentic_cash["pending_settlements"],
            [{"settles_on": "2026-09-10", "amount": 145.0}],
        )

        rejection = check_settled_cash(
            200.0,
            settled_cash=80.0,
            unsettled_cash=145.0,
            pending_settlements=[{"amount": 145.0, "settles_on": "2026-09-10"}],
            on_date=date(2026, 9, 9),
        )
        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.code, UNSETTLED_CASH)
        self.assertEqual(rejection.detail["settlement_date"], "2026-09-10")
        self.assertIn("2026-09-10", rejection.reason)
        self.assertIn("T+1", rejection.reason)

        # Within settled cash it is not refused.
        self.assertIsNone(
            check_settled_cash(
                50.0,
                settled_cash=80.0,
                unsettled_cash=145.0,
                pending_settlements=[{"amount": 145.0, "settles_on": "2026-09-10"}],
                on_date=date(2026, 9, 9),
            )
        )

    def test_t1_settlement_modelled(self):
        """Proceeds from a close are unavailable to a same-day proposal."""
        sale_day = date(2026, 9, 9)
        settles = settlement_date(sale_day)
        self.assertEqual(settles, date(2026, 9, 10))

        view = settlement_view(
            settled_cash=80.0,
            unsettled_cash=145.0,
            pending_settlements=[{"amount": 145.0, "settles_on": settles.isoformat()}],
            as_of_date=sale_day,
        )
        self.assertEqual(view.available, 80.0, "unsettled proceeds are not spendable the same day")
        self.assertEqual(view.earliest_settlement_for(200.0), settles)

        # The next day they are.
        next_day = settlement_view(
            settled_cash=225.0,
            unsettled_cash=0.0,
            pending_settlements=[{"amount": 145.0, "settles_on": settles.isoformat()}],
            as_of_date=settles,
        )
        self.assertEqual(next_day.available, 225.0)
        self.assertEqual(next_day.pending_by_date, {})

        # A Friday sale settles Monday, and a sale before a holiday skips it.
        self.assertEqual(settlement_date(date(2026, 9, 11)), date(2026, 9, 14))
        self.assertEqual(settlement_date(date(2026, 9, 4)), date(2026, 9, 8))

    def test_an_amount_that_never_settles_says_so_rather_than_naming_a_date(self):
        rejection = check_settled_cash(
            10_000.0,
            settled_cash=80.0,
            unsettled_cash=145.0,
            pending_settlements=[{"amount": 145.0, "settles_on": "2026-09-10"}],
            on_date=date(2026, 9, 9),
        )
        self.assertIsNone(rejection.detail["settlement_date"])
        self.assertIn("not enough even once", rejection.reason)


class WashSaleTests(unittest.TestCase):
    """Spec L §8: test_wash_sale_window_flagged."""

    def setUp(self):
        self.loss = RealizedLoss(
            symbol="AMD",
            sale_date=date(2026, 8, 1),
            amount=-212.40,
            account="Agentic",
            figi="BBG000BBQCY0",
        )

    def test_wash_sale_window_flagged(self):
        twenty_days = flag_wash_sale_window(
            symbol="AMD",
            purchase_date=date(2026, 8, 21),
            realized_losses=[self.loss],
            figi="BBG000BBQCY0",
        )
        self.assertTrue(twenty_days.wash_sale_window)
        payload = twenty_days.as_dict()
        self.assertEqual(payload["matches"][0]["confidence"], "high")
        self.assertEqual(payload["matches"][0]["days_from_sale"], 20)
        self.assertFalse(payload["is_determination"])

        forty_days = flag_wash_sale_window(
            symbol="AMD",
            purchase_date=date(2026, 9, 10),
            realized_losses=[self.loss],
            figi="BBG000BBQCY0",
        )
        self.assertFalse(forty_days.wash_sale_window)
        self.assertEqual(forty_days.matches, ())

    def test_the_window_is_61_days_and_inclusive_at_both_ends(self):
        self.assertEqual(WASH_SALE_WINDOW_DAYS, 61)
        sale = date(2026, 8, 1)
        self.assertTrue(in_window(sale, date(2026, 8, 31)))   # +30
        self.assertFalse(in_window(sale, date(2026, 9, 1)))   # +31
        self.assertTrue(in_window(sale, date(2026, 7, 2)))    # -30
        self.assertFalse(in_window(sale, date(2026, 7, 1)))   # -31
        self.assertTrue(in_window(sale, sale))                # the sale day

    def test_a_different_share_class_is_possible_not_high(self):
        loss = RealizedLoss(
            symbol="GOOG",
            sale_date=date(2026, 8, 1),
            amount=-100.0,
            figi="BBG009S39JX6",
            issuer_id="alphabet",
            security_class="C",
        )
        flag = flag_wash_sale_window(
            symbol="GOOGL",
            purchase_date=date(2026, 8, 10),
            realized_losses=[loss],
            figi="BBG009S3NB30",
            issuer_id="alphabet",
            security_class="A",
        )
        self.assertTrue(flag.wash_sale_window)
        self.assertEqual(flag.matches[0]["confidence"], "possible")
        self.assertIn("different security class", flag.matches[0]["basis"])

    def test_the_flag_never_emits_a_determination_and_states_its_coverage(self):
        flag = flag_wash_sale_window(
            symbol="AMD", purchase_date=date(2026, 8, 21), realized_losses=[self.loss]
        )
        payload = flag.as_dict()
        self.assertFalse(payload["is_determination"])
        notes = " ".join(payload["notes"])
        self.assertIn("facts-and-circumstances", notes)
        self.assertIn("Publication 550", notes)
        self.assertIn("another broker", notes)

    def test_an_ira_purchase_is_flagged_separately(self):
        flag = flag_wash_sale_window(
            symbol="AMD",
            purchase_date=date(2026, 8, 21),
            realized_losses=[self.loss],
            account_type="ira",
        )
        payload = flag.as_dict()
        self.assertTrue(payload["ira_purchase"])
        self.assertIn("permanently disallowed", " ".join(payload["notes"]))


class DividendReconstructionTests(unittest.TestCase):
    """Spec L §8: test_dividends_reconstructed_flagged."""

    def test_dividends_reconstructed_flagged(self):
        declaration = DividendDeclaration(
            symbol="MSFT",
            ex_date=date(2026, 8, 20),
            record_date=date(2026, 8, 21),
            pay_date=date(2026, 9, 11),
            amount_per_share=0.83,
            source="market_data_feed",
        )
        flows = reconstruct_dividends(
            [declaration], quantity_on=lambda symbol, account, on: 22.0, accounts=("Primary",)
        )
        self.assertEqual(len(flows), 1)
        flow = flows[0].as_dict()
        self.assertTrue(flow["reconstructed"])
        self.assertAlmostEqual(flow["gross_amount"], 18.26, places=2)
        self.assertIn(
            "Robinhood", " ".join(flow["warnings"]),
            "the flag has to say why it is a reconstruction, not just that it is one",
        )
        self.assertTrue(total_reconstructed(flows)["reconstructed"])

    def test_a_quantity_the_ledger_cannot_supply_produces_no_row_rather_than_a_zero(self):
        declaration = DividendDeclaration(
            symbol="MSFT", ex_date=date(2026, 8, 20), amount_per_share=0.83
        )
        self.assertEqual(
            reconstruct_dividends([declaration], quantity_on=lambda *a: None), ()
        )
        self.assertEqual(
            reconstruct_dividends([declaration], quantity_on=lambda *a: 0.0), ()
        )

    def test_a_missing_record_date_is_warned_about_rather_than_assumed_away(self):
        declaration = DividendDeclaration(
            symbol="MSFT", ex_date=date(2026, 8, 20), amount_per_share=0.83
        )
        flows = reconstruct_dividends([declaration], quantity_on=lambda *a: 10.0)
        self.assertIn("no record date", " ".join(flows[0].warnings))


class ExposureUnitTests(unittest.TestCase):
    def test_gross_and_net_separate_a_short_from_a_long(self):
        rows = [
            _Row("AMD", 10, 1000.0),
            _Row("SPY", -5, -2500.0),
        ]
        report = aggregate_exposure(rows, cash_total=500.0)
        self.assertEqual(report.long_exposure, 1000.0)
        self.assertEqual(report.short_exposure, 2500.0)
        self.assertEqual(report.gross_exposure, 3500.0)
        self.assertEqual(report.net_exposure, -1500.0)
        self.assertEqual(report.total_value, -1000.0)

    def test_a_symbol_with_no_sector_is_reported_as_unknown_not_dropped(self):
        report = aggregate_exposure([_Row("AMD", 10, 1000.0)], sectors={})
        self.assertEqual(set(report.by_sector), {"unknown"})
        self.assertEqual(report.by_sector["unknown"], 1000.0)

    def test_weights_are_of_gross_exposure_so_a_cash_heavy_book_still_has_a_largest(self):
        report = aggregate_exposure([_Row("AMD", 10, 1000.0)], cash_total=99_000.0)
        self.assertEqual(report.weights["AMD"], 1.0)
        self.assertEqual(report.largest_position_weight, 1.0)
        self.assertEqual(report.concentration_hhi, 1.0)

    def test_provenance_is_a_complete_block_even_when_nothing_is_known(self):
        block = provenance(None, datetime(2026, 9, 9)).as_dict()
        self.assertEqual(
            set(block),
            {
                "as_of_utc",
                "stale",
                "data_quality",
                "age_minutes",
                "freshness_budget_minutes",
                "sources",
                "warnings",
            },
        )
        self.assertTrue(block["stale"])
        self.assertEqual(block["data_quality"], "unknown")


class _Row:
    """A holding-shaped object, for the pure exposure unit tests."""

    def __init__(self, symbol, quantity, market_value, instrument_type="equity", account_id=1):
        self.symbol = symbol
        self.quantity = quantity
        self.market_value = market_value
        self.instrument_type = instrument_type
        self.account_id = account_id
        self.instrument_detail = {}
        self.notional = market_value
        self.last_price = None


if __name__ == "__main__":
    unittest.main()
