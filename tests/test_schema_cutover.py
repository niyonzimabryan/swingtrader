"""Cutover-specific schema behaviour: adopting the production file, and the lock.

Two things Phase 0b changes about ``database/schema.py``:

1. A pre-Alembic database is adopted at **the revision it matches**, not only at
   ``head``. Without that, the first phase to add a table (this one, with
   ``workspace_tokens``) would make the production SQLite file classify as
   ``unknown`` and startup would fail closed on the very database the legacy
   path exists to adopt.
2. ``ensure_schema`` takes a Postgres advisory lock, so the bot and the
   workspace service booting together cannot both run ``upgrade head``.
"""

from __future__ import annotations

import threading
import time
import unittest

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from database.schema import (
    ALEMBIC_VERSION_TABLE,
    BASELINE_REVISION,
    SCHEMA_ADVISORY_LOCK_KEY,
    adoptable_revision,
    alembic_config,
    classify,
    current_revision,
    ensure_schema,
    revision_signatures,
)
from tests.dbfixture import TestDatabase, postgres_url


class RevisionSignatureTests(unittest.TestCase):
    def test_the_graph_has_more_than_one_revision_and_they_differ(self):
        signatures = revision_signatures()
        self.assertIn(BASELINE_REVISION, signatures)
        script = ScriptDirectory.from_config(alembic_config())
        head = script.get_current_head()
        self.assertIn(head, signatures)
        self.assertNotEqual(
            head,
            BASELINE_REVISION,
            "this phase adds a revision; if head is still the baseline the "
            "migration did not land",
        )
        self.assertNotEqual(signatures[BASELINE_REVISION], signatures[head])

    def test_the_workspace_table_arrives_after_the_baseline(self):
        signatures = revision_signatures()
        script = ScriptDirectory.from_config(alembic_config())
        self.assertNotIn("workspace_tokens", signatures[BASELINE_REVISION])
        self.assertIn("workspace_tokens", signatures[script.get_current_head()])


class BaselineShapedDatabaseTests(unittest.TestCase):
    """A database shaped like the production file: baseline tables, no version row."""

    def setUp(self):
        self.db = TestDatabase("baseline_shaped")
        self.addCleanup(self.db.cleanup)
        self.engine = create_engine(self.db.url)
        self.addCleanup(self.engine.dispose)

        with self.engine.begin() as conn:
            command.upgrade(alembic_config(conn), BASELINE_REVISION)
        # Strip the version table: this is what the pre-Alembic Railway file
        # looks like, and it is the case Phase 0a's models-only comparison would
        # now get wrong.
        with self.engine.begin() as conn:
            conn.execute(text(f"DROP TABLE {ALEMBIC_VERSION_TABLE}"))

    def test_it_is_adoptable_at_the_baseline_not_at_head(self):
        with self.engine.connect() as conn:
            self.assertEqual(classify(conn), "legacy")
            self.assertEqual(adoptable_revision(conn), BASELINE_REVISION)

    def test_adoption_stamps_the_baseline_and_then_migrates_forward(self):
        tables_before = set(inspect(self.engine).get_table_names())
        self.assertNotIn("workspace_tokens", tables_before)

        self.assertEqual(ensure_schema(self.engine), "adopted")

        script = ScriptDirectory.from_config(alembic_config())
        with self.engine.connect() as conn:
            self.assertEqual(current_revision(conn), script.get_current_head())
        tables_after = set(inspect(self.engine).get_table_names())
        self.assertIn("workspace_tokens", tables_after)
        # Adoption stamps and migrates; it must not rebuild the data tables.
        self.assertTrue(tables_before <= tables_after)


class AdvisoryLockTests(unittest.TestCase):
    """Two processes booting at once must not both run ``upgrade head``."""

    def setUp(self):
        self.postgres = postgres_url()
        if self.postgres is None:
            self.skipTest(
                "No Postgres available. Set TEST_POSTGRES_URL to exercise the "
                "concurrent-startup lock."
            )
        self.db = TestDatabase("advisory", base_url=self.postgres)
        self.addCleanup(self.db.cleanup)

    def test_ensure_schema_waits_for_a_held_advisory_lock(self):
        holder = create_engine(self.postgres)
        self.addCleanup(holder.dispose)
        migrator = create_engine(self.db.url)
        self.addCleanup(migrator.dispose)

        finished = threading.Event()
        failure = []

        def migrate():
            try:
                ensure_schema(migrator)
            except Exception as exc:  # pragma: no cover - surfaced by the assert
                failure.append(exc)
            finally:
                finished.set()

        connection = holder.connect()
        try:
            connection.execute(
                text("SELECT pg_advisory_lock(:key)"),
                {"key": SCHEMA_ADVISORY_LOCK_KEY},
            )
            connection.commit()

            worker = threading.Thread(target=migrate, daemon=True)
            worker.start()
            self.assertFalse(
                finished.wait(timeout=1.5),
                "ensure_schema ran while another session held the schema lock",
            )
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": SCHEMA_ADVISORY_LOCK_KEY},
            )
            connection.commit()
            connection.close()

        self.assertTrue(finished.wait(timeout=30), "ensure_schema never completed")
        self.assertEqual(failure, [])
        with migrator.connect() as conn:
            self.assertEqual(classify(conn), "versioned")

    def test_the_lock_is_released_when_the_transaction_ends(self):
        engine = create_engine(self.db.url)
        self.addCleanup(engine.dispose)
        ensure_schema(engine)

        observer = create_engine(self.postgres)
        self.addCleanup(observer.dispose)
        with observer.connect() as conn:
            acquired = conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": SCHEMA_ADVISORY_LOCK_KEY},
            ).scalar()
            conn.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": SCHEMA_ADVISORY_LOCK_KEY},
            )
            conn.commit()
        self.assertTrue(acquired, "ensure_schema left the advisory lock held")


class SqliteNeedsNoLockTests(unittest.TestCase):
    def test_ensure_schema_runs_on_sqlite_without_a_lock_call(self):
        """`pg_advisory_xact_lock` is a syntax error on SQLite; it must not run."""
        import tempfile
        from pathlib import Path

        from tests.dbfixture import sqlite_url

        with tempfile.TemporaryDirectory() as tmp:
            engine = create_engine(sqlite_url(Path(tmp), "nolock"))
            try:
                self.assertEqual(ensure_schema(engine), "created")
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
