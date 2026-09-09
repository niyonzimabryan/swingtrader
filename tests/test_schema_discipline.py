"""Schema discipline: Alembic owns the schema, on both engines.

Four things are enforced here.

1. ``alembic upgrade head`` on an empty database reproduces the schema the ORM
   models declare, on whichever engine the run targets. This is what fails when
   someone edits ``database/models.py`` without writing a migration.
2. That schema is identical to what ``Base.metadata.create_all()`` produces, so
   the baseline is a faithful snapshot of the pre-Alembic schema rather than an
   approximation of it.
3. No production module performs inline DDL or calls ``create_all()``.
4. ``0001_baseline`` is the single root revision and there is exactly one head,
   so parallel phases cannot quietly fork the migration graph.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from database.models import Base
from database.schema import (
    BASELINE_REVISION,
    SchemaMismatch,
    alembic_config,
    classify,
    create_all_for_tests,
    current_revision,
    ensure_schema,
)
from database.types import UtcDateTime
from tests.dbfixture import TestDatabase

REPO_ROOT = Path(__file__).resolve().parents[1]

# tests/ and evals/ build throwaway fixture databases with hand-written DDL and
# are not part of the production schema path; migrations/ is where DDL belongs.
GUARD_EXCLUDED_DIRS = {"tests", "evals", "migrations"}

DDL_PATTERN = re.compile(
    r"\b(ALTER\s+TABLE|CREATE\s+TABLE|DROP\s+TABLE|CREATE\s+INDEX|DROP\s+INDEX|ADD\s+COLUMN)\b",
    re.IGNORECASE,
)

# Only database/schema.py may call create_all, and only for ephemeral test DBs.
CREATE_ALL_ALLOWED = {Path("database/schema.py")}


def _production_sources() -> list[Path]:
    out = []
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0].startswith(".") or rel.parts[0] in GUARD_EXCLUDED_DIRS:
            continue
        if "__pycache__" in rel.parts or ".venv" in rel.parts or "venv" in rel.parts:
            continue
        out.append(rel)
    return sorted(out)


def _code_lines(rel: Path) -> list[tuple[int, str]]:
    """Source lines with docstring/comment prose stripped out.

    The guard is about executed DDL, not about the words appearing in a comment
    that explains why the DDL is gone.
    """
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    # Drop triple-quoted blocks (docstrings) and trailing comments.
    text = re.sub(r'"""(?:.|\n)*?"""', '""', text)
    text = re.sub(r"'''(?:.|\n)*?'''", "''", text)
    lines = []
    for i, line in enumerate(text.splitlines(), start=1):
        lines.append((i, re.sub(r"#.*$", "", line)))
    return lines


class InlineDdlGuardTests(unittest.TestCase):
    def test_no_inline_ddl_in_production_modules(self):
        offenders = [
            f"{rel}:{n}: {line.strip()}"
            for rel in _production_sources()
            for n, line in _code_lines(rel)
            if DDL_PATTERN.search(line)
        ]
        self.assertEqual(
            offenders,
            [],
            "Schema changes belong in migrations/versions, not inline DDL. "
            "Write a revision that branches from the current head.",
        )

    def test_create_all_is_confined_to_the_test_helper(self):
        offenders = [
            f"{rel}:{n}: {line.strip()}"
            for rel in _production_sources()
            if rel not in CREATE_ALL_ALLOWED
            for n, line in _code_lines(rel)
            if "create_all" in line
        ]
        self.assertEqual(
            offenders,
            [],
            "create_all() is not a migration path. Use database.schema.ensure_schema(); "
            "create_all_for_tests() exists only for ephemeral test databases.",
        )


class MigrationGraphTests(unittest.TestCase):
    def setUp(self):
        self.script = ScriptDirectory.from_config(alembic_config())

    def test_baseline_is_the_single_root_revision(self):
        self.assertEqual(list(self.script.get_bases()), [BASELINE_REVISION])

    def test_migration_graph_has_exactly_one_head(self):
        heads = list(self.script.get_heads())
        self.assertEqual(
            len(heads),
            1,
            f"Expected one head, found {heads}. Two phases branched without a merge "
            "revision; see migrations/README.md.",
        )

    def test_every_revision_descends_from_the_baseline(self):
        head = self.script.get_current_head()
        lineage = {rev.revision for rev in self.script.walk_revisions(BASELINE_REVISION, head)}
        allrevs = {rev.revision for rev in self.script.walk_revisions()}
        self.assertEqual(allrevs, lineage)


class ModelTypeDisciplineTests(unittest.TestCase):
    def test_every_timestamp_column_is_utc_normalised(self):
        """A plain sa.DateTime would let an aware datetime through unnormalised."""
        offenders = [
            f"{table}.{column.name}"
            for table, tbl in Base.metadata.tables.items()
            for column in tbl.columns
            if column.type.__class__.__name__ == "DateTime"
        ]
        self.assertEqual(
            offenders,
            [],
            "Use database.types.UtcDateTime so aware datetimes are normalised to "
            "naive UTC identically on SQLite and Postgres.",
        )

    def test_utc_datetime_round_trips_aware_and_naive_alike(self):
        from datetime import datetime, timedelta, timezone

        from database.db import get_session
        from database.models import ScoredCandidate

        db = TestDatabase("utcdt")
        self.addCleanup(db.cleanup)
        from database.db import init_db

        init_db(db.url)

        aware = datetime(2026, 3, 1, 12, 0, tzinfo=timezone(timedelta(hours=-5)))
        with get_session() as session:
            session.add(ScoredCandidate(ticker="AWAR", scored_at=aware))
        with get_session() as session:
            stored = session.query(ScoredCandidate).filter_by(ticker="AWAR").one().scored_at

        self.assertIsNone(stored.tzinfo)
        self.assertEqual(stored, datetime(2026, 3, 1, 17, 0))


class SchemaParityTests(unittest.TestCase):
    """The parts of the stop condition that must hold on both engines."""

    def setUp(self):
        self.db = TestDatabase("alembic_head")
        self.addCleanup(self.db.cleanup)

    def test_upgrade_head_on_an_empty_database_matches_the_models(self):
        engine = create_engine(self.db.url)
        self.addCleanup(engine.dispose)
        self.assertEqual(ensure_schema(engine), "created")

        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn,
                opts={"compare_type": True, "compare_server_default": True},
            )
            diff = compare_metadata(context, Base.metadata)

        self.assertEqual(
            diff,
            [],
            "alembic upgrade head no longer produces the schema database/models.py "
            "declares. Add a migration for the model change.",
        )

    def test_upgrade_head_reproduces_the_create_all_schema(self):
        other = TestDatabase("create_all")
        self.addCleanup(other.cleanup)

        alembic_engine = create_engine(self.db.url)
        create_all_engine = create_engine(other.url)
        self.addCleanup(alembic_engine.dispose)
        self.addCleanup(create_all_engine.dispose)

        ensure_schema(alembic_engine)
        create_all_for_tests(create_all_engine)

        self.assertEqual(
            _reflect(alembic_engine),
            _reflect(create_all_engine),
            "The baseline has drifted from Base.metadata.create_all().",
        )

    def test_baseline_round_trips_down_to_base_and_back(self):
        """migrations/README.md claims the baseline is reversible; prove it."""
        engine = create_engine(self.db.url)
        self.addCleanup(engine.dispose)
        ensure_schema(engine)

        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "base")
        remaining = set(inspect(engine).get_table_names()) & set(Base.metadata.tables)
        self.assertEqual(remaining, set())

        self.assertEqual(ensure_schema(engine), "upgraded")
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn,
                opts={"compare_type": True, "compare_server_default": True},
            )
            self.assertEqual(compare_metadata(context, Base.metadata), [])

    def test_head_revision_is_recorded(self):
        engine = create_engine(self.db.url)
        self.addCleanup(engine.dispose)
        ensure_schema(engine)
        script = ScriptDirectory.from_config(alembic_config())
        with engine.connect() as conn:
            self.assertEqual(current_revision(conn), script.get_current_head())


class LegacyAdoptionTests(unittest.TestCase):
    """A pre-Alembic database is stamped, not rebuilt; anything else fails closed."""

    def setUp(self):
        self.db = TestDatabase("legacy")
        self.addCleanup(self.db.cleanup)
        self.engine = create_engine(self.db.url)
        self.addCleanup(self.engine.dispose)

    def test_empty_database_is_created(self):
        with self.engine.connect() as conn:
            self.assertEqual(classify(conn), "empty")
        self.assertEqual(ensure_schema(self.engine), "created")

    def test_unversioned_pre_alembic_schema_is_adopted(self):
        create_all_for_tests(self.engine)
        with self.engine.connect() as conn:
            self.assertEqual(classify(conn), "legacy")

        self.assertEqual(ensure_schema(self.engine), "adopted")

        script = ScriptDirectory.from_config(alembic_config())
        with self.engine.connect() as conn:
            self.assertEqual(current_revision(conn), script.get_current_head())
        # Adoption stamps; it must not have dropped or rebuilt the data tables.
        self.assertIn("trades", inspect(self.engine).get_table_names())

    def test_a_baseline_era_database_is_still_adopted_after_a_new_table_lands(self):
        """A pre-Alembic database predates every post-baseline migration.

        ``create_all()`` builds today's schema, which includes tables no
        baseline-era database can have. Adoption has to tolerate exactly those
        absences and nothing else, or the first phase that adds a table turns
        every un-adopted production database into ``SchemaMismatch`` at startup.
        """
        # Post-baseline tables are derived from the migration graph (Phase 0b
        # replaced Phase 3a's hardcoded list with a replay of every revision),
        # so a phase that adds a table changes nothing here.
        from database.schema import migration_owned_tables, revision_signatures

        baseline_tables = set(revision_signatures()[BASELINE_REVISION])
        post_baseline_names = set(migration_owned_tables()) - baseline_tables
        post_baseline = {
            name: table
            for name, table in Base.metadata.tables.items()
            if name in post_baseline_names
        }
        self.assertTrue(
            post_baseline,
            "No post-baseline table is declared; this test needs at least one.",
        )

        Base.metadata.create_all(
            self.engine,
            tables=[
                table
                for name, table in Base.metadata.tables.items()
                if name not in post_baseline_names
            ],
        )
        present = set(inspect(self.engine).get_table_names())
        self.assertFalse(present & post_baseline_names)

        with self.engine.connect() as conn:
            self.assertEqual(classify(conn), "legacy")

        self.assertEqual(ensure_schema(self.engine), "adopted")

        # The stamp is followed by an upgrade, which is what creates them.
        after = set(inspect(self.engine).get_table_names())
        self.assertTrue(post_baseline_names <= after)
        self.assertIn("trades", after)

    def test_already_versioned_database_is_upgraded_not_restamped(self):
        ensure_schema(self.engine)
        self.assertEqual(ensure_schema(self.engine), "upgraded")

    def test_partial_schema_fails_closed_with_recovery_instructions(self):
        # One ORM table present, the rest missing: not empty, not the legacy
        # schema. Guessing a stamp here would skip every migration forever.
        Base.metadata.tables["tickers"].create(self.engine)
        with self.engine.connect() as conn:
            self.assertEqual(classify(conn), "unknown")

        with self.assertRaises(SchemaMismatch) as caught:
            ensure_schema(self.engine)
        message = str(caught.exception)
        self.assertIn("missing tables", message)
        self.assertIn(f"alembic stamp {BASELINE_REVISION}", message)


def _reflect(engine) -> dict:
    inspector = inspect(engine)
    return {
        table: {
            "columns": sorted(
                (c["name"], str(c["type"]), bool(c["nullable"]), str(c.get("default")))
                for c in inspector.get_columns(table)
            ),
            "pk": inspector.get_pk_constraint(table).get("constrained_columns"),
            "indexes": sorted(
                (i["name"], tuple(i["column_names"]), bool(i.get("unique")))
                for i in inspector.get_indexes(table)
            ),
            "unique": sorted(
                (u["name"], tuple(u["column_names"]))
                for u in inspector.get_unique_constraints(table)
            ),
            "foreign_keys": sorted(
                (
                    tuple(f["constrained_columns"]),
                    f["referred_table"],
                    tuple(f["referred_columns"]),
                )
                for f in inspector.get_foreign_keys(table)
            ),
        }
        for table in sorted(inspector.get_table_names())
        if table != "alembic_version"
    }


if __name__ == "__main__":
    unittest.main()
