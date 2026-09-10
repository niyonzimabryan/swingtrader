"""Spec L §8 rows that live on the proposal side (Phase 6, Spec L §6).

The rows exercised here, all against the ledger produced by the real
``run_sync`` path and a fake citation resolver (the Spec N §8 seam):

* ``test_agent_cannot_set_quantity``
* ``test_risk_fraction_capped``
* ``test_percentage_input_rejected``
* ``test_citation_must_be_full_ok_recent``
* ``test_nonpositive_lower_bound_goes_discretionary``
* ``test_discretionary_budget_is_separate``
* ``test_protected_entries_are_whole_shares`` (the zero-share refusal half)
* ``test_proposal_on_readonly_account_rejected``
* ``test_risk_caps_span_all_accounts``
* ``test_stale_ledger_refuses_proposal``

Each refusal is asserted to be a *returned* ``risk_rejected`` row with a reason,
not a dropped idea (Spec L §6.4), except the two that are malformed input
(a quantity, a short side) and refuse before a row exists.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from database.db import get_session
from portfolio import evidence, proposals
from portfolio.sizing import (
    EVIDENCE_SIZED_TO_ZERO,
    PERCENTAGE_INPUT,
    RISK_FRACTION_ABOVE_HARD_CAP,
    ZERO_SHARES,
)
from tests import comparablesfixture as cf
from tests import proposalfixture as pf
from tests.dbfixture import TestDatabase


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("proposals")
        self.addCleanup(self.db.cleanup)
        from database.db import init_db

        init_db(self.db.url)
        self.now = pf.NOW

    def _sync(self, **kwargs):
        with get_session() as session:
            pf.synced_session(session, now=self.now, **kwargs)
            session.commit()

    def _propose(self, *, resolver=None, settings=None, **kwargs):
        params = dict(
            ticker="AMD",
            entry=100.0,
            stop=95.0,
            risk_fraction=0.005,
            settings=settings or pf.settings(),
            owner_id="99887766",
            now=self.now,
            resolver=resolver or pf.resolver_for({}),
        )
        params.update(kwargs)
        with get_session() as session:
            row = proposals.create_proposal(session, **params)
            payload = proposals.proposal_payload(row)
            session.commit()
            return payload

    # -- inputs that are not a proposal ------------------------------------

    def test_agent_cannot_set_quantity(self):
        self._sync()
        with self.assertRaises(proposals.ProposalRefused) as ctx:
            self._propose(quantity=10)
        self.assertEqual(ctx.exception.code, "quantity_not_accepted")

    def test_short_side_refused_before_a_row(self):
        self._sync()
        with self.assertRaises(proposals.ProposalRefused) as ctx:
            self._propose(side="short")
        self.assertEqual(ctx.exception.code, proposals.UNSUPPORTED_SIDE)

    # -- risk_fraction validation ------------------------------------------

    def test_risk_fraction_capped(self):
        """Above the hard cap is refused, not clamped (Spec L §6.6)."""
        self._sync()
        payload = self._propose(risk_fraction=0.02)  # 2% > 1% hard cap
        self.assertEqual(payload["status"], "risk_rejected")
        self.assertEqual(payload["rejection_code"], RISK_FRACTION_ABOVE_HARD_CAP)
        # Not clamped: the effective fraction is not silently set to the cap.
        self.assertEqual(payload["quantity"], 0)

    def test_percentage_input_rejected(self):
        """0.5 meaning 0.5% is refused; 0.005 is accepted (Spec L §8)."""
        self._sync()
        rejected = self._propose(risk_fraction=0.5)
        self.assertEqual(rejected["status"], "risk_rejected")
        self.assertEqual(rejected["rejection_code"], PERCENTAGE_INPUT)

        accepted = self._propose(risk_fraction=0.005)
        self.assertEqual(accepted["status"], "proposed")

    # -- the citation rule --------------------------------------------------

    def test_citation_must_be_full_ok_recent(self):
        """quick / insufficient / inconclusive / stale / other-ticker → discretionary.

        In advisory mode the proposal is not refused (nothing sizes to zero on
        evidence); it is re-labelled discretionary with the reason. The evidenced
        *label* is what must be withheld from each of these, which is the rule
        the row records.
        """
        self._sync()

        # A real quick answer and a real insufficient answer, straight from the
        # engine fixtures: these exercise the genuine assert_citable predicate.
        quick, refused = cf.quick_answer(), cf.refused_answer()
        # Constructed full answers for the cases that need a ticker and a date.
        other_ticker = pf.cited_answer(ticker="NVDA")
        inconclusive = pf.cited_answer(status="inconclusive")
        stale = pf.cited_answer(as_of=(self.now.date() - timedelta(days=20)))

        cases = {
            "quick@x": (quick, evidence.NOT_CITABLE),
            "insufficient@x": (refused, evidence.NOT_CITABLE),
            "inconclusive@x": (inconclusive, evidence.NOT_OK),
            "other@x": (other_ticker, evidence.WRONG_TICKER),
            "stale@x": (stale, evidence.STALE_CITATION),
        }
        resolver = pf.resolver_for({k: v[0] for k, v in cases.items()})
        for answer_id, (_, expected_code) in cases.items():
            with self.subTest(answer_id=answer_id):
                payload = self._propose(cohort_answer_id=answer_id, resolver=resolver)
                self.assertEqual(payload["budget"], "discretionary")
                self.assertIn(expected_code, payload["evidence"]["reason"])
                # Still a live proposal, not suppressed.
                self.assertEqual(payload["status"], "proposed")

    def test_citation_must_be_full_ok_recent_strict_refuses(self):
        """In strict mode the same non-citation is a risk_rejected refusal."""
        self._sync()
        resolver = pf.resolver_for({"other@x": pf.cited_answer(ticker="NVDA")})
        payload = self._propose(
            cohort_answer_id="other@x",
            resolver=resolver,
            settings=pf.settings(evidence_gate_mode="strict"),
        )
        self.assertEqual(payload["status"], "risk_rejected")
        self.assertEqual(payload["rejection_code"], proposals.EVIDENCE_GATE_REFUSED)

    def test_evidenced_citation_scales_and_draws_evidenced_budget(self):
        """A full/ok/same-ticker/recent citation with LB>0 is evidenced, m=LB/PE."""
        self._sync()
        answer = pf.cited_answer(ticker="AMD", policy_net=0.08, policy_lb=0.04)
        resolver = pf.resolver_for({"amd@1": answer})
        payload = self._propose(cohort_answer_id="amd@1", resolver=resolver)
        self.assertEqual(payload["status"], "proposed")
        self.assertEqual(payload["budget"], "evidenced")
        self.assertAlmostEqual(payload["evidence"]["m"], 0.5, places=6)
        # effective = risk_fraction * m = 0.005 * 0.5 = 0.0025
        self.assertAlmostEqual(payload["risk_fraction_effective"], 0.0025, places=6)

    def test_nonpositive_lower_bound_goes_discretionary(self):
        """LB<=0: advisory re-labels discretionary and prints the bound; strict → zero."""
        self._sync()
        answer = pf.cited_answer(ticker="AMD", policy_net=0.08, policy_lb=-0.01)
        resolver = pf.resolver_for({"amd@neg": answer})

        advisory = self._propose(cohort_answer_id="amd@neg", resolver=resolver)
        self.assertEqual(advisory["budget"], "discretionary")
        self.assertEqual(advisory["status"], "proposed")
        self.assertEqual(advisory["evidence"]["lower_bound"], -0.01)
        self.assertIn(evidence.NONPOSITIVE_LOWER_BOUND, advisory["evidence"]["reason"])

        strict = self._propose(
            cohort_answer_id="amd@neg",
            resolver=resolver,
            settings=pf.settings(evidence_gate_mode="strict"),
        )
        self.assertEqual(strict["status"], "risk_rejected")
        self.assertEqual(strict["rejection_code"], EVIDENCE_SIZED_TO_ZERO)

    # -- the two budgets ----------------------------------------------------

    def test_discretionary_budget_is_separate(self):
        """An uncited proposal cannot exceed the discretionary per-trade cap.

        With entry $100, stop $95 (risk/share $5) and equity ~$100k, a
        risk_fraction of 1% asks for $1000 of risk = 200 shares. The
        discretionary per-trade cap of 0.25% caps effective risk at $250 = 50
        shares. So an uncited proposal at the hard-cap fraction still sizes to
        the discretionary budget, never the evidenced one.
        """
        self._sync()
        payload = self._propose(risk_fraction=0.01)  # at the hard cap, uncited
        self.assertEqual(payload["budget"], "discretionary")
        self.assertEqual(payload["status"], "proposed")
        # 0.25% of 100k / $5 per share = 50 shares.
        self.assertEqual(payload["quantity"], 50)
        self.assertAlmostEqual(payload["risk_fraction_effective"], 0.0025, places=6)
        self.assertTrue(payload["caps"]["budget_risk_cap"]["bound"])

    def test_discretionary_daily_notional_caps_the_book(self):
        """Discretionary daily notional bounds the size across proposals in a day."""
        self._sync()
        # One share of headroom: daily notional of $100 at $100 entry = 1 share.
        settings = pf.settings(discretionary_daily_notional=100.0)
        payload = self._propose(risk_fraction=0.01, settings=settings)
        self.assertEqual(payload["quantity"], 1)
        self.assertTrue(payload["caps"]["daily_notional_cap"]["bound"])

    # -- whole shares / zero-share refusal ---------------------------------

    def test_zero_share_size_is_risk_rejected(self):
        """A size that rounds to zero whole shares is refused, not rounded up."""
        self._sync(settled_cash=100_000.0)
        # Tiny fraction: 0.00001 * 100k = $1 of risk / $5 per share = 0.2 → 0.
        payload = self._propose(risk_fraction=0.00001)
        self.assertEqual(payload["status"], "risk_rejected")
        self.assertEqual(payload["rejection_code"], ZERO_SHARES)
        self.assertEqual(payload["quantity"], 0)

    # -- accounts -----------------------------------------------------------

    def test_proposal_on_readonly_account_rejected(self):
        """No agent_placeable account → risk_rejected with the reason."""
        # Sync a book whose only account is read-only by making the Agentic one
        # disabled is overkill; instead assert the guard fires when placement
        # would target a non-placeable account. Here we simulate by disabling
        # the placeable account through a settings-independent path: the
        # read_context picks the first placeable account, and with none it
        # refuses.
        with get_session() as session:
            # Sync only the read-only primary account.
            from portfolio.records import BrokerSnapshot, AccountSnapshot
            from execution.brokers.fake import FakeBroker, FakeAccount
            from portfolio.records import CashRecord

            b = FakeBroker(
                accounts=[FakeAccount(record=pf.PRIMARY, cash=CashRecord(settled_cash=50_000.0))],
                as_of_utc=self.now,
            )
            from portfolio.sync import run_sync

            run_sync(session, b, now=self.now)
            session.commit()
        payload = self._propose()
        self.assertEqual(payload["status"], "risk_rejected")
        self.assertEqual(payload["rejection_code"], proposals.NO_PLACEABLE_ACCOUNT)

    def test_risk_caps_span_all_accounts(self):
        """A holding in the read-only primary account counts against concentration.

        Concentration cap is 10% of equity. If the primary account already holds
        enough AMD to be at the cap, the Agentic-account proposal in AMD is bound
        by concentration — proving the cap counts the combined book (Spec L §5.1).
        """
        # Primary holds $9,900 of AMD; equity ~ $100k cash + $9,900 = $109,900;
        # 10% cap = $10,990, headroom $1,090 = 10 shares at $100.
        self._sync(primary_holdings=[pf.holding("AMD", 99, 100.0)])
        payload = self._propose(risk_fraction=0.01)  # would want 50 disc. shares
        self.assertEqual(payload["status"], "proposed")
        conc = payload["caps"]["concentration_cap"]
        self.assertTrue(conc["bound"])
        self.assertGreater(conc["existing_value"], 0.0)
        # Bound below the discretionary 50-share size.
        self.assertLess(payload["quantity"], 50)

    def test_concentration_cap_ignores_other_names(self):
        """A read-only holding in a *different* name does not bind this proposal."""
        self._sync(primary_holdings=[pf.holding("TSLA", 99, 100.0)])
        payload = self._propose(risk_fraction=0.01)
        self.assertFalse(payload["caps"]["concentration_cap"]["bound"])

    # -- freshness ----------------------------------------------------------

    def test_stale_ledger_refuses_proposal(self):
        """A ledger older than the freshness budget refuses with the age."""
        self._sync()
        stale_now = self.now + timedelta(minutes=120)  # budget is 60
        payload = self._propose(now=stale_now)
        self.assertEqual(payload["status"], "risk_rejected")
        self.assertEqual(payload["rejection_code"], "stale_ledger")

    # -- the risk_rejected contract ----------------------------------------

    def test_risk_rejected_is_returned_with_reason_and_no_approval(self):
        """A refused proposal is a row with a reason and no approval reference."""
        self._sync()
        payload = self._propose(risk_fraction=0.5)
        self.assertEqual(payload["status"], "risk_rejected")
        self.assertTrue(payload["rejection_reason"])
        self.assertIsNone(payload["approval_expires_at"])
        self.assertFalse(payload["placed"])


class UnsettledCashTests(unittest.TestCase):
    """The cash Agentic account refuses a proposal needing T+1 proceeds."""

    def setUp(self):
        self.db = TestDatabase("proposals_cash")
        self.addCleanup(self.db.cleanup)
        from database.db import init_db

        init_db(self.db.url)
        self.now = pf.NOW

    def test_unsettled_cash_rejected_with_settlement_date(self):
        settles_on = (self.now.date() + timedelta(days=1))
        with get_session() as session:
            pf.synced_session(
                session,
                now=self.now,
                settled_cash=100.0,
                unsettled_cash=100_000.0,
                pending=[pf.pending_settlement(100_000.0, settles_on)],
            )
            session.commit()
            row = proposals.create_proposal(
                session,
                ticker="AMD",
                entry=100.0,
                stop=95.0,
                risk_fraction=0.005,
                settings=pf.settings(),
                owner_id="99887766",
                now=self.now,
                resolver=pf.resolver_for({}),
            )
            payload = proposals.proposal_payload(row)
            session.commit()
        self.assertEqual(payload["status"], "risk_rejected")
        self.assertEqual(payload["rejection_code"], "unsettled_cash")
        self.assertIn(settles_on.isoformat(), payload["rejection_reason"])


if __name__ == "__main__":
    unittest.main()
