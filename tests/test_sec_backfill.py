"""The Phase 3a backfill job: idempotency and the coverage checkpoint.

``scripts/sec_backfill.py`` is what produces the coverage numbers Phase 3a
reports. Both properties that make those numbers meaningful are asserted here:
a re-run writes nothing new, and the report counts what was actually written
rather than what was requested.

Runs offline against ``tests/fixtures/sec/`` through the job's own
``--fixtures`` replay mode, so the path under test is the one that runs in
production minus the transport.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

from database.db import get_session
from database.models import SourceObservation
from filings.coverage_universe import COVERAGE_UNIVERSE_50
from filings.errors import PlaneDisabled
from scripts.sec_backfill import (
    BackfillReport,
    _fixture_client,
    format_report,
    main,
    run_backfill,
)
from tests.dbfixture import init_test_db
from tests.test_sec_minimal import (
    CIK_DUAL_CLASS,
    CIK_GAP,
    CIK_TAG_MIGRATION,
    DisabledSettings,
    EnabledSettings,
    FIXTURES,
)

TICKERS = ["TAGM", "GAPC", "DUAL"]


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.db = init_test_db("sec_backfill")
        self.addCleanup(self.db.cleanup)
        self.client = _fixture_client(FIXTURES)
        self.addCleanup(self.client.close)

    def _run(self, tickers=TICKERS, since=None, settings=None):
        with get_session() as session:
            return run_backfill(
                session,
                self.client,
                tickers,
                settings=settings or EnabledSettings(),
                since=since,
            )

    def test_the_job_is_idempotent(self):
        first = self._run()
        self.assertEqual(first.companies_ingested, 3)
        self.assertGreater(first.rows_written, 0)
        self.assertEqual(first.rows_duplicate, 0)

        with get_session() as session:
            after_first = session.query(SourceObservation).count()

        second = self._run()
        self.assertEqual(second.rows_written, 0)
        self.assertEqual(second.rows_duplicate, first.rows_written)

        with get_session() as session:
            self.assertEqual(session.query(SourceObservation).count(), after_first)

    def test_the_report_carries_the_three_checkpoint_numbers(self):
        report = self._run()

        # Continuous revenue AND EPS: GAPC has a real revenue gap, the other two
        # are continuous (TAGM only because the alias map bridged its migration).
        self.assertEqual(report.companies_with_continuous_revenue_and_eps, 2)
        self.assertEqual(report.companies_with_share_count, 3)
        self.assertEqual(report.companies_with_8k_item_202, 3)
        self.assertGreater(report.eight_k_total, report.eight_k_item_202)
        self.assertEqual(report.unresolved_accessions, 0)
        self.assertIn("tag_migration", report.alert_counts)
        self.assertIn("series_gap", report.alert_counts)

        self.assertAlmostEqual(report.continuous_series_rate, 2 / 3, places=3)
        self.assertAlmostEqual(report.share_count_rate, 1.0, places=3)

    def test_an_unknown_ticker_is_a_failure_not_a_silent_skip(self):
        report = self._run(tickers=["TAGM", "NOSUCHTICKER"])
        self.assertEqual(report.companies_ingested, 1)
        self.assertEqual(report.companies_failed, 1)
        failure = next(row for row in report.per_company if row.get("error"))
        self.assertIn("no CIK", failure["error"])

    def test_since_narrows_what_is_written(self):
        everything = self._run()
        with get_session() as session:
            session.query(SourceObservation).delete()

        narrowed = self._run(since=date(2019, 6, 1))
        self.assertLess(narrowed.rows_written, everything.rows_written)
        self.assertGreater(narrowed.rows_written, 0)

    def test_the_flag_gates_the_job(self):
        with self.assertRaises(PlaneDisabled):
            self._run(settings=DisabledSettings())
        with get_session() as session:
            self.assertEqual(session.query(SourceObservation).count(), 0)

    def test_the_formatted_report_states_its_denominators(self):
        text = format_report(self._run())
        self.assertIn("continuous revenue AND EPS", text)
        self.assertIn("share-count coverage", text)
        self.assertIn("8-K Item 2.02", text)
        self.assertIn("/3", text)


class BackfillCliTests(unittest.TestCase):
    def setUp(self):
        self.db = init_test_db("sec_backfill_cli")
        self.addCleanup(self.db.cleanup)
        self._env = dict(os.environ)
        os.environ["DATABASE_URL"] = self.db.url
        os.environ["PLANE_SEC_MINIMAL_ENABLED"] = "true"
        os.environ["SEC_USER_AGENT"] = "SwingTrader Test suite@example.invalid"
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_cli_runs_against_fixtures_and_writes_a_json_report(self):
        # Never the repository root: on the Postgres matrix entry the test
        # database has no file path to hang a scratch file off.
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        report_path = Path(scratch.name) / "coverage.json"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = main(
                [
                    "--tickers", *TICKERS,
                    "--fixtures", str(FIXTURES),
                    "--report", str(report_path),
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertIn("SEC minimal plane", buffer.getvalue())

        payload = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["companies_ingested"], 3)
        self.assertEqual(len(payload["per_company"]), 3)

    def test_dry_run_writes_nothing(self):
        with redirect_stdout(io.StringIO()):
            main(["--tickers", "TAGM", "--fixtures", str(FIXTURES), "--dry-run"])
        with get_session() as session:
            self.assertEqual(session.query(SourceObservation).count(), 0)


class CoverageUniverseTests(unittest.TestCase):
    def test_the_checkpoint_list_is_fifty_distinct_tickers(self):
        self.assertEqual(len(COVERAGE_UNIVERSE_50), 50)
        self.assertEqual(len(set(COVERAGE_UNIVERSE_50)), 50)

    def test_report_rates_are_safe_on_an_empty_run(self):
        empty = BackfillReport(since=None)
        self.assertEqual(empty.continuous_series_rate, 0.0)
        self.assertEqual(empty.item_202_filing_rate, 0.0)


if __name__ == "__main__":
    unittest.main()
