"""``0014_securities_asset_class`` applies from the previous head and reverses.

``tests/test_schema_discipline.py`` already proves the graph has one base and
one head and that ``upgrade head`` reproduces the models. What this adds is the
step production performs: a database sitting at ``0013_merge_notify_owner``
moving forward, the column arriving with every existing row reading ``equity``,
and ``downgrade`` putting it back.

Two things here are specific to this revision rather than boilerplate:

* **the backfill value is asserted on a row written before the column existed.**
  ``equity`` is not a convenient default, it is the true value for every row
  that predates the revision — the only table the adapter could read was
  ``stocks``. A revision that left those rows NULL, or defaulted them to
  something else, would quietly reclassify real securities.
* **the server default must not survive the upgrade.** ``database/models.py``
  declares the column with a Python-side default like every other column on the
  table, and ``test_upgrade_head_reproduces_the_create_all_schema`` compares the
  two column by column, so a server default left behind is drift. The check is
  written out here as well so the failure names the cause rather than appearing
  as a generic metadata diff.

Like the other migration tests, it runs on the engine the suite targets and
additionally on Postgres whenever one is reachable, so one run covers both
engines rather than relying on the CI matrix to.
"""

from __future__ import annotations

import unittest

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from database.models import Base
from database.schema import alembic_config, ensure_schema
from tests.dbfixture import TestDatabase, postgres_url, target_backend

REVISION = "0014_securities_asset_class"
PREVIOUS_HEAD = "0013_merge_notify_owner"
COLUMN = "asset_class"
TABLE = "securities"


def _engines():
    targets = [(target_backend(), None)]
    pg = postgres_url()
    if pg is not None and target_backend() != "postgresql":
        targets.append(("postgresql", pg))
    return targets


def _columns(engine) -> dict:
    return {c["name"]: c for c in inspect(engine).get_columns(TABLE)}


class SecuritiesAssetClassMigrationTests(unittest.TestCase):
    def _database(self, base_url):
        db = TestDatabase("asset_class_migration", base_url=base_url)
        self.addCleanup(db.cleanup)
        engine = create_engine(db.url)
        self.addCleanup(engine.dispose)
        return engine

    def _assert_matches_models(self, engine):
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn, opts={"compare_type": True, "compare_server_default": True}
            )
            self.assertEqual(compare_metadata(context, Base.metadata), [])

    def test_the_revision_is_on_the_single_head_of_the_graph(self):
        script = ScriptDirectory.from_config(alembic_config())
        heads = list(script.get_heads())
        self.assertEqual(len(heads), 1, heads)
        ancestors = {rev.revision for rev in script.iterate_revisions(heads[0], "base")}
        self.assertIn(REVISION, ancestors)
        self.assertEqual(
            script.get_revision(REVISION).down_revision,
            PREVIOUS_HEAD,
            "0014 must branch from the head it was written against; never edit "
            "another phase's revision (migrations/README.md).",
        )

    def test_a_pre_existing_row_reads_equity_after_the_upgrade(self):
        for label, base_url in _engines():
            with self.subTest(engine=label):
                engine = self._database(base_url)

                with engine.begin() as conn:
                    command.upgrade(alembic_config(conn), PREVIOUS_HEAD)
                self.assertNotIn(COLUMN, _columns(engine))

                # A security written on the old schema, exactly as a backfill
                # before this revision would have left it.
                with engine.begin() as conn:
                    conn.execute(text(
                        "INSERT INTO securities (security_uid, ticker, venue, "
                        "delisting_reason, source) "
                        "VALUES ('sharadar:320193', 'AAPL', 'nasdaq', 'unknown', 'sharadar')"
                    ))

                with engine.begin() as conn:
                    command.upgrade(alembic_config(conn), "head")

                self.assertIn(COLUMN, _columns(engine))
                with engine.connect() as conn:
                    value = conn.execute(text(
                        "SELECT asset_class FROM securities WHERE ticker = 'AAPL'"
                    )).scalar()
                self.assertEqual(value, "equity")
                self._assert_matches_models(engine)

    def test_the_column_is_not_null_and_carries_no_server_default(self):
        for label, base_url in _engines():
            with self.subTest(engine=label):
                engine = self._database(base_url)
                ensure_schema(engine)
                column = _columns(engine)[COLUMN]
                self.assertFalse(column["nullable"])
                self.assertIsNone(
                    column.get("default"),
                    "the server default fills existing rows during the ALTER and "
                    "is dropped immediately after; one left behind is schema "
                    "drift (migrations/README.md)",
                )

    def test_a_rerun_is_a_no_op(self):
        for label, base_url in _engines():
            with self.subTest(engine=label):
                engine = self._database(base_url)
                self.assertEqual(ensure_schema(engine), "created")
                self.assertEqual(ensure_schema(engine), "upgraded")
                self._assert_matches_models(engine)

    def test_it_is_reversible_and_reappliable(self):
        for label, base_url in _engines():
            with self.subTest(engine=label):
                engine = self._database(base_url)
                ensure_schema(engine)

                with engine.begin() as conn:
                    command.downgrade(alembic_config(conn), PREVIOUS_HEAD)
                self.assertNotIn(COLUMN, _columns(engine))
                with engine.connect() as conn:
                    self.assertIn(
                        PREVIOUS_HEAD, MigrationContext.configure(conn).get_current_heads()
                    )

                # ...and forward again, because a downgrade that cannot be undone
                # is a one-way door dressed up as a rollback.
                with engine.begin() as conn:
                    command.upgrade(alembic_config(conn), "head")
                self.assertIn(COLUMN, _columns(engine))
                self._assert_matches_models(engine)

    def test_a_stored_fund_survives_an_upgrade_write_read_cycle(self):
        """The column is not just created — it holds what the store writes."""
        from data.prices import store
        from data.prices.base import ASSET_CLASS_FUND, SecurityMasterRow
        from database.db import get_session, init_db

        db = TestDatabase("asset_class_roundtrip")
        self.addCleanup(db.cleanup)
        init_db(db.url)

        with get_session() as session:
            store.upsert_securities(session, [SecurityMasterRow(
                security_uid="sharadar:118691", ticker="SPY", source="sharadar",
                venue="nyse_amex", asset_class=ASSET_CLASS_FUND,
            )])
        with get_session() as session:
            self.assertEqual(
                store.asset_class_by_uid(session), {"sharadar:118691": ASSET_CLASS_FUND}
            )


if __name__ == "__main__":
    unittest.main()
