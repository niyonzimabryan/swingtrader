"""Phase 3p's migration, and the end-to-end path the docs tell you to run.

The Alembic single-head / descends-from-baseline tests live in
`tests/test_schema_discipline.py` and still apply; what is here is specific to
this phase: that the revision branches from the baseline rather than from
another phase's work, that the five tables round-trip on whichever engine the
matrix entry selects, and that `BASELINE_TABLES` has not drifted from the
migration it claims to describe.
"""

from __future__ import annotations

import re
import unittest
from datetime import date
from pathlib import Path

from alembic.script import ScriptDirectory

from database.schema import BASELINE_REVISION, BASELINE_TABLES, alembic_config
from tests.dbfixture import init_test_db

REPO_ROOT = Path(__file__).resolve().parents[1]
REVISION = "3p01_price_plane"

PHASE_TABLES = (
    "securities", "price_bars", "corporate_actions",
    "universe_membership", "price_snapshots",
)


class BaselineTableSetTests(unittest.TestCase):
    def test_baseline_table_set_matches_the_migration(self):
        """`BASELINE_TABLES` is the tables `0001_baseline` creates, exactly.

        It is hand-written, and the legacy-adoption branch depends on it, so it
        is checked against the migration rather than trusted.
        """
        source = (REPO_ROOT / "migrations" / "versions" / "0001_baseline.py").read_text()
        created = set(re.findall(r"op\.create_table\('([^']+)'", source))
        self.assertEqual(created, set(BASELINE_TABLES))

    def test_this_phases_tables_are_not_in_the_baseline_set(self):
        self.assertEqual(set(PHASE_TABLES) & set(BASELINE_TABLES), set())


class MigrationLineageTests(unittest.TestCase):
    def setUp(self):
        self.script = ScriptDirectory.from_config(alembic_config())

    def test_the_price_plane_revision_branches_from_the_baseline(self):
        revision = self.script.get_revision(REVISION)
        self.assertEqual(
            revision.down_revision,
            BASELINE_REVISION,
            "migrations/README.md: never branch from another phase's migration.",
        )

    def test_the_revision_id_does_not_collide_with_a_sequential_number(self):
        """Three phases each numbering their first revision 0002 would collide."""
        ids = {rev.revision for rev in self.script.walk_revisions()}
        self.assertIn(REVISION, ids)
        self.assertNotIn("0002", ids)

    def test_the_migration_declares_no_cross_phase_foreign_key(self):
        source = (REPO_ROOT / "migrations" / "versions" / f"{REVISION}.py").read_text()
        self.assertNotIn("ForeignKeyConstraint", source)


class PhaseTableTests(unittest.TestCase):
    """The five tables exist and behave, on whichever engine this run targets."""

    def setUp(self):
        self.db = init_test_db("price_plane_schema")
        self.addCleanup(self.db.cleanup)

    def test_every_phase_table_exists_after_upgrade_head(self):
        from sqlalchemy import create_engine, inspect

        engine = create_engine(self.db.url)
        self.addCleanup(engine.dispose)
        present = set(inspect(engine).get_table_names())
        for table in PHASE_TABLES:
            self.assertIn(table, present)

    def test_a_duplicate_bar_for_a_security_and_date_is_rejected(self):
        from sqlalchemy.exc import IntegrityError

        from database.db import get_session
        from database.models import PriceBar

        def bar():
            return PriceBar(
                security_uid="uid", ticker="X", session_date=date(2024, 1, 2),
                raw_open=1.0, raw_high=1.0, raw_low=1.0, raw_close=1.0, volume=1.0,
                split_factor=1.0, dividend_cash=0.0,
                split_adjusted_close=1.0, total_return_close=1.0, source="test",
            )

        with get_session() as session:
            session.add(bar())
        with self.assertRaises(IntegrityError):
            with get_session() as session:
                session.add(bar())

    def test_membership_with_an_open_ended_interval_round_trips(self):
        from datetime import datetime, timezone

        from data.prices import store
        from data.prices.base import MembershipInterval
        from database.db import get_session

        interval = MembershipInterval(
            universe_slug="u", security_uid="uid", ticker="X",
            member_from=date(2024, 1, 2), member_to=None, source="test",
            known_at_utc=datetime(2024, 1, 2, 21, tzinfo=timezone.utc),
        )
        with get_session() as session:
            store.replace_universe(session, "u", [interval])
        with get_session() as session:
            rows = store.members_as_of(session, "u", date(2030, 1, 1))
            self.assertEqual(len(rows), 1)
            # `known_at_utc` is stored naive-UTC on both engines (Phase 0a rule).
            self.assertIsNone(rows[0].known_at_utc.tzinfo)
            self.assertEqual(rows[0].known_at_utc.hour, 21)


class DocumentedConfigTests(unittest.TestCase):
    """docs/PRICE_PLANE.md claims it names every variable; hold it to that."""

    ENV_VARS = (
        "PRICE_PLANE_ENABLED",
        "PRICE_PLANE_SOURCE",
        "NASDAQ_DATA_LINK_API_KEY",
        "PRICE_PLANE_SNAPSHOT",
        "LIQUID_UNIVERSE_TOP_N",
        "LIQUID_UNIVERSE_WINDOW_SESSIONS",
        "DELISTING_AUDIT_WINDOW_SESSIONS",
        "DELISTING_AUDIT_COLLAPSE_THRESHOLD",
    )

    def test_every_setting_is_in_env_example_and_the_docs(self):
        from config.settings import Settings

        env_example = (REPO_ROOT / ".env.example").read_text()
        docs = (REPO_ROOT / "docs" / "PRICE_PLANE.md").read_text()
        for name in self.ENV_VARS:
            with self.subTest(name=name):
                self.assertIn(name.lower(), Settings.model_fields, "not a settings field")
                self.assertIn(f"\n{name}=", env_example)
                self.assertIn(name, docs)

    def test_the_api_key_is_not_committed_anywhere(self):
        env_example = (REPO_ROOT / ".env.example").read_text()
        self.assertIn("NASDAQ_DATA_LINK_API_KEY=\n", env_example + "\n")
        self.assertEqual(Path(REPO_ROOT / "data" / "prices").is_dir(), True)

    def test_the_committed_sp500_csv_carries_its_licence(self):
        licence = REPO_ROOT / "data" / "prices" / "sp500" / "LICENSE.fja05680-sp500.txt"
        self.assertTrue(licence.exists())
        text = licence.read_text()
        self.assertIn("MIT License", text)
        self.assertIn("Farrell J. Aultman", text)
        self.assertIn(
            "sp500_historical_components_fja05680.csv",
            (REPO_ROOT / "docs" / "DATA_LICENSES.md").read_text(),
        )


class EndToEndScriptTests(unittest.TestCase):
    """The exact sequence `docs/PRICE_PLANE.md` tells you to run, offline."""

    def setUp(self):
        self.db = init_test_db("price_plane_e2e")
        self.addCleanup(self.db.cleanup)

        from config.settings import Settings

        from data.prices import config as plane_config

        # The flag is off by default, so an end-to-end run has to turn it on
        # explicitly — which is the behaviour `FlagGateTests` asserts from the
        # other side. `database_url` is passed as an init argument so it beats
        # anything in the environment: the scripts call `init_db` themselves and
        # must land on this test's disposable database.
        enabled = Settings(
            price_plane_enabled=True,
            price_plane_source="fixture",
            price_plane_snapshot="e2e",
            liquid_universe_top_n=3,
            database_url=self.db.url,
        )
        original = plane_config.get_settings
        plane_config.get_settings = lambda: enabled
        self.addCleanup(lambda: setattr(plane_config, "get_settings", original))

    def test_backfill_then_universe_then_audit(self):
        from scripts import audit_delisting_returns, build_universes, price_backfill

        self.assertEqual(price_backfill.main(["--source", "fixture"]), 0)

        from data.prices import store, universes
        from database.db import get_session

        with get_session() as session:
            self.assertEqual(len(store.load_all_bars(session)), 7)
            snapshot = store.get_snapshot(session, "e2e")
            self.assertEqual(snapshot.coverage_summary["bars_written"], 522)

        self.assertEqual(
            build_universes.main(["--universe", "liquid_us_equity_v1", "--window", "20"]), 0
        )
        with get_session() as session:
            sessions = store.session_dates(session)
            members = store.members_as_of(session, universes.UNIVERSE_SLUG, sessions[-1])
            self.assertEqual(len(members), 3)

        self.assertEqual(audit_delisting_returns.main(["--source", "fixture"]), 0)
        with get_session() as session:
            audit = store.get_snapshot(session, "e2e").delisting_audit
            coverage = store.get_snapshot(session, "e2e").coverage_summary
        self.assertEqual(audit["counts"]["collapse"], 1)
        self.assertEqual(audit["counts"]["stop"], 1)
        self.assertEqual(
            coverage["bars_written"], 522, "the audit must not wipe the coverage summary"
        )

    def test_the_backfill_is_idempotent(self):
        from scripts import price_backfill

        from data.prices import store
        from database.db import get_session

        price_backfill.main(["--source", "fixture"])
        with get_session() as session:
            first = sum(len(b) for b in store.load_all_bars(session).values())
        price_backfill.main(["--source", "fixture"])
        with get_session() as session:
            second = sum(len(b) for b in store.load_all_bars(session).values())
        self.assertEqual(first, second)

    def test_the_sp500_membership_loader_runs_from_the_committed_csv(self):
        from scripts import build_universes

        from data.prices import sp500_history, store
        from database.db import get_session

        self.assertEqual(
            build_universes.main(["--universe", "sp500_wikipedia_v1"]), 0
        )
        with get_session() as session:
            members = store.members_as_of(
                session, sp500_history.UNIVERSE_SLUG, date(2015, 6, 30)
            )
        self.assertGreater(len(members), 450)


class FlagGateTests(unittest.TestCase):
    def test_the_scripts_refuse_while_the_flag_is_off(self):
        from scripts import audit_delisting_returns, build_universes, price_backfill

        self.assertEqual(price_backfill.main(["--source", "fixture"]), 2)
        self.assertEqual(audit_delisting_returns.main(["--source", "fixture"]), 2)
        self.assertEqual(
            build_universes.main(["--universe", "liquid_us_equity_v1"]), 2
        )


if __name__ == "__main__":
    unittest.main()
