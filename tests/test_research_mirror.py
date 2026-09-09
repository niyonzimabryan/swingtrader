"""Spec M §5 and §8: the mirror round-trips permitted content and withholds the rest.

Three rows live here:

``test_mirror_roundtrip_permitted_content``
    Postgres → Markdown → ``--import`` → Postgres is lossless for permitted
    content, and re-exporting produces the same bytes.
``test_mirror_withholds_by_provenance``
    A news-tier section is exported as a marker carrying its Postgres id, and
    the import preserves the database original rather than blanking it.
``test_offline_read``
    With no database at all, the Markdown alone answers "what is my thesis on
    X and what would change my mind".
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from research_workspace import mirror, offline, store, trust
from research_workspace.errors import ResearchRefused
from tests.dbfixture import TestDatabase
from utils.timeutils import utcnow_naive

SEC = {
    "url": "https://www.sec.gov/Archives/edgar/data/2488/x.htm",
    "tier": trust.PRIMARY_REGULATOR,
    "title": "AMD 10-Q",
    "as_of": "2026-08-01",
}
NEWS = {"url": "https://news.test/mi400", "tier": trust.NEWS, "title": "MI400 story"}
COHORT = {"url": "cohort://gap-and-go@ab12", "tier": trust.INTERNAL_ANALYSIS}

FIXED_NOW = datetime(2026, 9, 9, 12, 0, 0)


class MirrorTestCase(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("research_mirror")
        self.addCleanup(self.db.cleanup)
        from database.db import get_session, init_db

        init_db(self.db.url)
        self._ctx = get_session()
        self.session = self._ctx.__enter__()
        self.addCleanup(lambda: self._ctx.__exit__(None, None, None))

        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "research"

    def seed(self):
        store.write_section(
            self.session, "AMD", "business_model",
            "Designs CPUs and GPUs; MI400 is the datacenter accelerator line.",
            sources=[SEC], author="human", human_authored=True,
            company_name="Advanced Micro Devices", sector="Semiconductors",
        )
        store.write_section(
            self.session, "AMD", "competitive_position",
            "Second source to NVIDIA in accelerators; cohort gap-and-go@ab12 "
            "shows a 3.1% median 20-session excess return.",
            sources=[SEC, COHORT], author="claude-opus-4-6",
        )
        store.write_section(
            self.session, "AMD", "notes", "Bryan's own read, no source.",
            sources=[], author="human", human_authored=True,
        )
        thesis = store.create_thesis(
            self.session, ticker="AMD", title="MI400 ramp re-rates datacenter",
            claim="Datacenter revenue doubles by FY27.",
            bear_case="Supply is allocated to one hyperscaler that can walk.",
            bear_case_author="thesis-critic",
            argument=[{"n": 1, "claim": "Backlog is booked", "evidence": ["10-Q p.14"]}],
        )
        store.set_probability(
            self.session, thesis, 0.6,
            resolution_at=utcnow_naive().date() + timedelta(days=300),
            observable="FY27 datacenter revenue in the 10-K",
        )
        store.add_invalidator(
            self.session, thesis,
            description="closes below $82 for three sessions",
            type="price_level",
            params={"operator": "below", "price": 82.0, "consecutive_sessions": 3},
        )
        store.add_invalidator(
            self.session, thesis, description="the AI capex narrative breaks",
            type="qualitative",
        )
        store.activate(self.session, thesis)
        store.journal_append(
            self.session, decision="opened", tickers=["AMD"], thesis=thesis,
            note_md="Opened on the ramp; invalidators set first.",
            sizing_rationale="0.5% risk, stop below the base.",
            expected_holding_days=60,
        )
        store.add_question(
            self.session, "What share of MI400 supply is committed to one customer?",
            ticker="AMD",
        )
        return thesis

    def withheld_section(self):
        return store.write_section(
            self.session, "AMD", "market_chatter",
            "A news story said the ramp is ahead of schedule.",
            sources=[NEWS], author="human", human_authored=True,
        )


class ExportTests(MirrorTestCase):
    def test_the_export_writes_the_four_shapes_spec_m_names(self):
        thesis = self.seed()
        report = mirror.export_all(self.session, self.root, now=FIXED_NOW)

        self.assertTrue((self.root / "companies" / "AMD.md").exists())
        self.assertTrue((self.root / "questions.md").exists())
        self.assertEqual(len(list((self.root / "journal").glob("*.md"))), 1)
        thesis_files = list((self.root / "theses").glob("*.md"))
        self.assertEqual(len(thesis_files), 1)
        self.assertRegex(thesis_files[0].name, r"^\d{4}-\d{2}-amd-[a-z0-9-]+\.md$")
        self.assertEqual(report.withheld_sections, [])

    def test_every_file_carries_front_matter_and_the_do_not_edit_banner(self):
        self.seed()
        mirror.export_all(self.session, self.root, now=FIXED_NOW)
        for path in sorted(self.root.rglob("*.md")):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                front, _ = mirror.split_front_matter(text)
                self.assertIn("generated_at", front)
                self.assertIn("mirror_version", front)
                self.assertIn("Do not hand-edit", text)

    def test_the_thesis_file_carries_the_invalidators_and_the_probability(self):
        thesis = self.seed()
        mirror.export_all(self.session, self.root, now=FIXED_NOW)
        path = next((self.root / "theses").glob("*.md"))
        front, body = mirror.split_front_matter(path.read_text(encoding="utf-8"))

        self.assertEqual(front["thesis_id"], thesis.id)
        self.assertEqual(front["status"], "active")
        self.assertEqual(front["probability"], 0.6)
        self.assertEqual(front["resolution_at"], thesis.resolution_at.isoformat())
        self.assertEqual([i["type"] for i in front["invalidators"]],
                         ["price_level", "qualitative"])
        self.assertIn("What would change my mind", body)

    def test_an_unsourced_section_carries_its_warning_into_the_repo(self):
        self.seed()
        mirror.export_all(self.session, self.root, now=FIXED_NOW)
        text = (self.root / "companies" / "AMD.md").read_text(encoding="utf-8")
        self.assertIn("**unsourced**", text)


class WithholdingTests(MirrorTestCase):
    def test_mirror_withholds_by_provenance(self):
        self.seed()
        section = self.withheld_section()
        report = mirror.export_all(self.session, self.root, now=FIXED_NOW)

        text = (self.root / "companies" / "AMD.md").read_text(encoding="utf-8")
        self.assertIn(
            f"<!-- withheld: news-derived, see dossier_sections/{section.id} -->", text
        )
        self.assertNotIn("A news story said the ramp is ahead of schedule.", text)
        self.assertNotIn("news.test", text)
        self.assertEqual(report.withheld_sections, [section.id])

        # And the import preserves the Postgres original rather than blanking it.
        imported = mirror.import_all(self.session, self.root)
        self.assertEqual(imported.withheld_preserved, [section.id])
        self.assertEqual(imported.revisions, [])

        current = store.current_section(
            self.session, section.dossier_id, "market_chatter"
        )
        self.assertEqual(current.id, section.id)
        self.assertEqual(
            current.body_md, "A news story said the ramp is ahead of schedule."
        )

    def test_a_vendor_sourced_section_is_withheld_too(self):
        self.seed()
        section = store.write_section(
            self.session, "AMD", "valuation", "EV/S is 9.1x on vendor data.",
            sources=[{"url": "https://vendor.test/s", "tier": trust.VENDOR_DATA}],
            author="human", human_authored=True,
        )
        mirror.export_all(self.session, self.root, now=FIXED_NOW)
        text = (self.root / "companies" / "AMD.md").read_text(encoding="utf-8")
        self.assertIn(f"see dossier_sections/{section.id}", text)
        self.assertNotIn("9.1x", text)

    def test_a_section_citing_a_filing_and_a_cohort_is_not_withheld(self):
        """The filter withholds by tier, not by nervousness."""
        self.seed()
        mirror.export_all(self.session, self.root, now=FIXED_NOW)
        text = (self.root / "companies" / "AMD.md").read_text(encoding="utf-8")
        self.assertIn("Second source to NVIDIA in accelerators", text)
        self.assertIn("cohort://gap-and-go@ab12", text)


class RoundTripTests(MirrorTestCase):
    def test_mirror_roundtrip_permitted_content(self):
        """Postgres → Markdown → --import → Postgres, lossless for permitted content."""
        self.seed()
        self.withheld_section()
        mirror.export_all(self.session, self.root, now=FIXED_NOW)

        before = {
            s.section_key: (s.id, s.body_md, s.sources)
            for s in store.current_sections(self.session, "AMD")
        }
        revisions_before = len(store.section_history(self.session, "AMD", "business_model"))

        report = mirror.import_all(self.session, self.root)

        self.assertEqual(report.revisions, [], report.refusals)
        self.assertEqual(report.refusals, [])
        self.assertEqual(report.unchanged, 3)  # the three permitted sections

        after = {
            s.section_key: (s.id, s.body_md, s.sources)
            for s in store.current_sections(self.session, "AMD")
        }
        self.assertEqual(before, after)
        self.assertEqual(
            len(store.section_history(self.session, "AMD", "business_model")),
            revisions_before,
        )

        # Re-exporting produces the same bytes.
        first = (self.root / "companies" / "AMD.md").read_text(encoding="utf-8")
        mirror.export_all(self.session, self.root, now=FIXED_NOW)
        self.assertEqual((self.root / "companies" / "AMD.md").read_text(encoding="utf-8"), first)

    def test_a_hand_edited_section_becomes_an_append_only_revision(self):
        self.seed()
        mirror.export_all(self.session, self.root, now=FIXED_NOW)
        path = self.root / "companies" / "AMD.md"
        original = store.current_section(
            self.session, store.get_dossier(self.session, "AMD").id, "business_model"
        )
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "MI400 is the datacenter accelerator line.",
                "MI400 is the datacenter accelerator line, shipping since Q2.",
            ),
            encoding="utf-8",
        )

        report = mirror.import_all(self.session, self.root)
        self.assertEqual(len(report.revisions), 1)

        history = store.section_history(self.session, "AMD", "business_model")
        self.assertEqual(len(history), 2)
        self.assertIn("shipping since Q2", history[-1].body_md)
        self.assertEqual(history[-1].supersedes_id, original.id)
        # The prior body is still readable. Nothing was overwritten.
        self.assertNotIn("shipping since Q2", history[0].body_md)
        # And the sources survived the round trip.
        self.assertEqual([s["url"] for s in history[-1].sources], [SEC["url"]])

    def test_a_hand_edited_thesis_status_is_refused_by_name(self):
        thesis = self.seed()
        mirror.export_all(self.session, self.root, now=FIXED_NOW)
        path = next((self.root / "theses").glob("*.md"))
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "status: active", "status: invalidated", 1
            ),
            encoding="utf-8",
        )

        report = mirror.import_all(self.session, self.root)
        self.assertEqual(len(report.refusals), 1)
        self.assertIn("'status' was hand-edited", report.refusals[0])
        self.assertIn("thesis_review", report.refusals[0])
        self.assertEqual(thesis.status, "active")

    def test_a_hand_edited_bear_case_is_applied(self):
        thesis = self.seed()
        mirror.export_all(self.session, self.root, now=FIXED_NOW)
        path = next((self.root / "theses").glob("*.md"))
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "Supply is allocated to one hyperscaler that can walk.",
                "Supply is allocated to one hyperscaler, and CoWoS is the real cap.",
            ),
            encoding="utf-8",
        )
        report = mirror.import_all(self.session, self.root)
        self.assertEqual(report.theses_updated, [thesis.id])
        self.assertIn("CoWoS", thesis.bear_case)

    def test_a_file_without_front_matter_is_refused(self):
        (self.root / "companies").mkdir(parents=True)
        (self.root / "companies" / "AMD.md").write_text("just prose", encoding="utf-8")
        with self.assertRaises(ResearchRefused):
            mirror.import_all(self.session, self.root)


class OfflineReadTests(MirrorTestCase):
    def test_offline_read(self):
        """With no database, the Markdown alone answers thesis and invalidators."""
        self.seed()
        mirror.export_all(self.session, self.root, now=FIXED_NOW)

        # Everything below this line uses files only.
        view = offline.current_view(self.root, "AMD")
        self.assertEqual(view["ticker"], "AMD")
        self.assertEqual(len(view["theses"]), 1)
        recorded = view["theses"][0]
        self.assertEqual(recorded["title"], "MI400 ramp re-rates datacenter")
        self.assertEqual(recorded["status"], "active")
        self.assertEqual(recorded["probability"], 0.6)
        self.assertIn("FY27 datacenter revenue", recorded["resolution_observable"])

        changes = offline.what_would_change_my_mind(self.root, "AMD")
        self.assertEqual(
            changes,
            [
                "price_level: closes below $82 for three sessions",
                "qualitative: the AI capex narrative breaks",
            ],
        )

    def test_the_offline_reader_touches_no_database_module(self):
        """The fallback is worthless if it needs the thing that is down.

        Parsed imports rather than a substring scan: the module's own docstring
        says the word "database", and a test that reads prose rather than
        imports would fail on the sentence explaining why it passes.
        """
        import ast

        tree = ast.parse(Path(offline.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(
            imported & {"database", "sqlalchemy", "httpx", "requests", "psycopg",
                        "workspace", "research_workspace"},
            set(),
        )

    def test_an_empty_mirror_answers_honestly_rather_than_raising(self):
        view = offline.current_view(self.root, "AMD")
        self.assertEqual(view["theses"], [])
        self.assertEqual(offline.what_would_change_my_mind(self.root, "AMD"), [])


if __name__ == "__main__":
    unittest.main()
