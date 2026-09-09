"""Spec M §3-§4: the domain rules, on whichever engine the run targets.

The names from the Spec M §8 test plan are kept verbatim where they exist, and
the Phase 2 goal's names alongside them where the two differ, so a reader
checking either list finds the row.
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from sqlalchemy import text

from database.models import Thesis
from research_workspace import store, trust
from research_workspace.errors import ResearchRefused
from tests.dbfixture import TestDatabase
from utils.timeutils import utcnow_naive

SEC = {"url": "https://www.sec.gov/Archives/edgar/data/2488/000000248826000012.htm",
       "tier": trust.PRIMARY_REGULATOR, "title": "AMD 10-Q", "as_of": "2026-08-01"}
NEWS = {"url": "https://example.test/story", "tier": trust.NEWS, "title": "A story"}
OWN = {"url": "cohort://gap-and-go@abc", "tier": trust.INTERNAL_ANALYSIS}

PRICE_INVALIDATOR = {
    "description": "closes below $82 for three sessions",
    "type": "price_level",
    "params": {"operator": "below", "price": 82.0, "consecutive_sessions": 3},
}


class ResearchStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("research")
        self.addCleanup(self.db.cleanup)
        from database.db import get_session, init_db

        init_db(self.db.url)
        self._get_session = get_session
        self._ctx = get_session()
        self.session = self._ctx.__enter__()
        self.addCleanup(lambda: self._ctx.__exit__(None, None, None))

    def a_thesis(self, **overrides) -> Thesis:
        kwargs = dict(
            ticker="AMD",
            title="MI400 ramp re-rates the datacenter segment",
            claim="Datacenter revenue doubles by FY27 on MI400 volume.",
            bear_case="Supply is allocated to a single hyperscaler that can walk.",
            bear_case_author="thesis-critic",
        )
        kwargs.update(overrides)
        return store.create_thesis(self.session, **kwargs)

    def arm(self, thesis, **overrides):
        params = dict(PRICE_INVALIDATOR)
        params.update(overrides)
        return store.add_invalidator(self.session, thesis, **params)

    def state_probability(self, thesis, probability=0.6, days=200):
        return store.set_probability(
            self.session,
            thesis,
            probability,
            resolution_at=utcnow_naive().date() + timedelta(days=days),
            observable="FY27 datacenter revenue in the 10-K",
        )


class InvalidatorDisciplineTests(ResearchStoreTestCase):
    def test_invalidator_required_for_active(self):
        """Spec M §8 `test_thesis_requires_invalidator`: draft -> active is refused."""
        thesis = self.a_thesis()
        self.state_probability(thesis)
        with self.assertRaises(ResearchRefused) as caught:
            store.activate(self.session, thesis)
        self.assertEqual(caught.exception.code, "thesis_not_activatable")
        self.assertIn("no invalidator", caught.exception.message)
        self.assertEqual(thesis.status, "draft")

    # The Spec M §8 name for the same assertion.
    test_thesis_requires_invalidator = test_invalidator_required_for_active

    def test_machine_checkable_required(self):
        """All-qualitative invalidators do not open the gate (Spec M §4 rule 2)."""
        thesis = self.a_thesis()
        self.state_probability(thesis)
        store.add_invalidator(
            self.session,
            thesis,
            description="the AI capex narrative breaks",
            type="qualitative",
        )
        with self.assertRaises(ResearchRefused) as caught:
            store.activate(self.session, thesis)
        self.assertIn("no machine-checkable invalidator", caught.exception.message)
        self.assertEqual(thesis.status, "draft")
        self.assertEqual(thesis.machine_checkable_invalidators, 0)

        self.arm(thesis)
        store.activate(self.session, thesis)
        self.assertEqual(thesis.status, "active")
        self.assertEqual(thesis.machine_checkable_invalidators, 1)

    test_requires_machine_checkable = test_machine_checkable_required

    def test_active_requires_probability(self):
        """No stated probability, or no resolution_at, means no `active`."""
        thesis = self.a_thesis()
        self.arm(thesis)
        with self.assertRaises(ResearchRefused) as caught:
            store.activate(self.session, thesis)
        self.assertIn("no stated probability", caught.exception.message)

        store.set_probability(self.session, thesis, 0.6)
        with self.assertRaises(ResearchRefused) as caught:
            store.activate(self.session, thesis)
        self.assertIn("no resolution_at", caught.exception.message)
        self.assertEqual(thesis.status, "draft")

        thesis.resolution_at = date(2027, 3, 31)
        store.activate(self.session, thesis)
        self.assertEqual(thesis.status, "active")

    def test_active_requires_a_bear_case(self):
        thesis = self.a_thesis(bear_case="")
        self.arm(thesis)
        self.state_probability(thesis)
        with self.assertRaises(ResearchRefused) as caught:
            store.activate(self.session, thesis)
        self.assertIn("no bear case", caught.exception.message)

    def test_a_probability_outside_zero_to_one_is_refused(self):
        thesis = self.a_thesis()
        for bad in (1.4, -0.1, "sixty percent"):
            with self.subTest(probability=bad):
                with self.assertRaises(ResearchRefused):
                    store.set_probability(self.session, thesis, bad)

    def test_the_database_refuses_an_active_thesis_that_is_not_falsifiable(self):
        """The CHECK constraint, not the code path: a hand-written UPDATE.

        The application refuses first and with a readable reason; this asserts
        the backstop underneath it exists on whichever engine the run targets.
        """
        thesis = self.a_thesis()
        self.state_probability(thesis)
        self.session.flush()
        with self.assertRaises(Exception):
            self.session.execute(
                text("UPDATE theses SET status='active' WHERE id = :id"),
                {"id": thesis.id},
            )
            self.session.flush()
        self.session.rollback()

    def test_post_hoc_invalidator_flagged(self):
        """One added after the position opens is kept, flagged, and excluded."""
        opened = utcnow_naive() - timedelta(days=3)
        thesis = self.a_thesis(
            position_opened_at=opened, linked_position_ref="rh:AMD:2026-09-06"
        )
        before = store.add_invalidator(
            self.session,
            thesis,
            description="set before entry",
            type="price_level",
            params={"operator": "below", "price": 82.0},
            now=opened - timedelta(days=1),
        )
        after = self.arm(thesis, description="closes below $70 for two sessions")

        self.assertFalse(before.post_hoc)
        self.assertTrue(after.post_hoc)
        # Kept and counted for falsifiability; excluded from the honesty metrics.
        self.assertEqual(thesis.machine_checkable_invalidators, 2)

        from research_workspace import metrics

        honest = metrics.honesty_metrics(self.session)
        self.assertEqual(honest["invalidators"]["post_hoc_excluded"], 1)
        self.assertEqual(honest["invalidators"]["counted"], 1)

    def test_an_invalidator_with_unusable_parameters_is_refused_at_write_time(self):
        thesis = self.a_thesis()
        cases = [
            ("price_level", {"operator": "below"}),          # no price
            ("price_level", {"operator": "sideways", "price": 1}),
            ("metric_threshold", {"fact_type": "gross_margin", "operator": "below"}),
            ("time_decay", {}),                               # no horizon at all
            ("event", {}),                                    # no event key
            ("nonsense", {}),
        ]
        for type_, params in cases:
            with self.subTest(type=type_, params=params):
                with self.assertRaises(ResearchRefused):
                    store.add_invalidator(
                        self.session,
                        thesis,
                        description="d",
                        type=type_,
                        params=params,
                    )

    def test_an_invalidator_without_a_description_is_refused(self):
        thesis = self.a_thesis()
        with self.assertRaises(ResearchRefused):
            store.add_invalidator(
                self.session,
                thesis,
                description="  ",
                type="price_level",
                params={"operator": "below", "price": 82.0},
            )


class DossierSectionTests(ResearchStoreTestCase):
    def test_sections_are_append_only(self):
        first = store.write_section(
            self.session, "AMD", "business_model", "Sells CPUs and GPUs.",
            sources=[SEC], author="human", human_authored=True,
        )
        second = store.write_section(
            self.session, "AMD", "business_model", "Sells CPUs, GPUs and DPUs.",
            sources=[SEC], author="human", human_authored=True,
        )
        history = store.section_history(self.session, "AMD", "business_model")

        self.assertEqual([r.id for r in history], [first.id, second.id])
        self.assertEqual(history[0].body_md, "Sells CPUs and GPUs.")
        self.assertEqual(second.supersedes_id, first.id)
        self.assertTrue(first.superseded)
        current = store.current_sections(self.session, "AMD")
        self.assertEqual([r.id for r in current], [second.id])

    def test_unsourced_section_warned(self):
        """A section with no source is flagged, not refused (Spec M §3)."""
        section = store.write_section(
            self.session, "AMD", "notes", "Bryan thinks the ramp slips.",
            sources=[], author="human", human_authored=True,
        )
        self.assertTrue(section.unsourced)

        from research_workspace import render

        rendered = render.section_payload(section, horizon_days=90)
        self.assertTrue(rendered["unsourced"])
        self.assertIn("unsourced", rendered["warnings"][0])

    test_unsourced_is_flagged = test_unsourced_section_warned

    def test_research_write_refuses_all_untrusted(self):
        """Spec P §5: all-untrusted sources need a human to claim authorship."""
        with self.assertRaises(ResearchRefused) as caught:
            store.write_section(
                self.session, "AMD", "notes", "A news story said so.",
                sources=[NEWS], author="claude-opus-4-6",
            )
        self.assertEqual(caught.exception.code, "all_sources_untrusted")

        # A human asserting authorship may write it.
        allowed = store.write_section(
            self.session, "AMD", "notes", "Bryan's read of the story.",
            sources=[NEWS], author="human", human_authored=True,
        )
        self.assertTrue(allowed.human_authored)
        self.assertEqual(trust.content_trust(allowed.sources), trust.UNTRUSTED)

        # One accountable source alongside makes it a synthesis, not a
        # laundered body — and a filing on its own is fine for a model to
        # write from, which is the `company-researcher` brief (Spec P §4).
        mixed = store.write_section(
            self.session, "AMD", "risks", "Filing plus the story.",
            sources=[NEWS, SEC], author="claude-opus-4-6",
        )
        self.assertFalse(mixed.unsourced)
        filing_only = store.write_section(
            self.session, "AMD", "capital_structure", "Converts due 2029.",
            sources=[SEC], author="claude-opus-4-6",
        )
        # Written, and still marked as resting on text someone else wrote.
        self.assertEqual(trust.content_trust(filing_only.sources), trust.UNTRUSTED)

    def test_a_vendor_only_section_needs_a_human(self):
        """A section whose whole basis is a vendor number is the "model
        produces a statistic" hazard wearing a citation."""
        with self.assertRaises(ResearchRefused) as caught:
            store.write_section(
                self.session, "AMD", "notes", "Market cap is $260bn.",
                sources=[{"url": "https://vendor.test/x", "tier": trust.VENDOR_DATA}],
                author="claude-opus-4-6",
            )
        self.assertEqual(caught.exception.code, "all_sources_untrusted")

    def test_a_model_authored_section_is_visibly_distinct(self):
        model = store.write_section(
            self.session, "AMD", "risks", "Model prose.", sources=[SEC],
            author="claude-opus-4-6",
        )
        human = store.write_section(
            self.session, "AMD", "notes", "Human prose.", sources=[SEC],
            author="human",
        )
        self.assertEqual(model.author_kind, "model")
        self.assertEqual(human.author_kind, "human")

    def test_an_unknown_source_tier_is_refused_rather_than_defaulted(self):
        with self.assertRaises(trust.UnknownTier):
            store.write_section(
                self.session, "AMD", "notes", "x",
                sources=[{"url": "https://example.test", "tier": "vibes"}],
            )

    def test_a_stale_section_is_marked(self):
        section = store.write_section(
            self.session, "AMD", "notes", "old", sources=[SEC], human_authored=True,
        )
        section.created_at = utcnow_naive() - timedelta(days=120)
        self.session.flush()
        self.assertTrue(store.is_stale(section, horizon_days=90))
        self.assertFalse(store.is_stale(section, horizon_days=365))


class JournalTests(ResearchStoreTestCase):
    def test_a_decision_to_pass_is_recorded(self):
        entry = store.journal_append(
            self.session,
            decision="passed",
            tickers="AMD",
            note_md="Cohort was insufficient; waiting for the next print.",
        )
        self.assertEqual(entry.budget, "discretionary")
        self.assertEqual(entry.tickers, "AMD")
        self.assertEqual(entry.outcome_recorded_at, None)

    def test_evidenced_budget_requires_a_citation(self):
        with self.assertRaises(ResearchRefused) as caught:
            store.journal_append(
                self.session, decision="opened", tickers=["AMD"], budget="evidenced"
            )
        self.assertEqual(caught.exception.code, "evidenced_needs_citation")

    def test_the_journal_freezes_the_thesis_it_cited(self):
        thesis = self.a_thesis()
        self.arm(thesis)
        self.state_probability(thesis)
        store.activate(self.session, thesis)
        entry = store.journal_append(
            self.session, decision="opened", tickers=["AMD"], thesis=thesis
        )
        frozen = entry.thesis_hash
        self.assertTrue(frozen)

        store.set_probability(self.session, thesis, 0.4, reason="ramp slipped")
        self.assertNotEqual(store.thesis_hash(self.session, thesis), frozen)
        self.assertEqual(entry.thesis_hash, frozen)


class SearchTests(ResearchStoreTestCase):
    def test_search_spans_dossiers_theses_and_decisions(self):
        store.write_section(
            self.session, "AMD", "business_model", "MI400 accelerators.",
            sources=[SEC], human_authored=True,
        )
        self.a_thesis()
        store.journal_append(
            self.session, decision="passed", tickers=["AMD"],
            note_md="Waiting on the MI400 ramp.",
        )
        kinds = {hit.kind for hit in store.search(self.session, "MI400")}
        self.assertEqual(kinds, {"dossier_section", "thesis", "decision"})

    def test_an_empty_query_is_refused(self):
        with self.assertRaises(ResearchRefused):
            store.search(self.session, "   ")


if __name__ == "__main__":
    unittest.main()
