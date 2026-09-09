"""`0007_strategy_lab` applies from empty and from the previous head, on both engines.

`tests/test_schema_discipline.py` already proves the graph has one base and one
head and that `upgrade head` reproduces the models. What it does not prove is
the step this revision actually performs in production: an existing database
sitting at `0006_merge_research_workspace` moving forward, and the partial
unique indexes surviving that path rather than only the create-from-empty one.

Like `test_strategy_lab_registry.SchemaConstraintTests`, this runs on the engine
the suite targets and additionally on Postgres whenever one is reachable, so a
single run covers both engines instead of relying on the CI matrix to.
"""

from __future__ import annotations

import unittest

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from database.models import Base
from database.schema import alembic_config, current_revision, ensure_schema
from tests.dbfixture import TestDatabase, postgres_url, target_backend

REVISION = "0007_strategy_lab"
PREVIOUS_HEAD = "0006_merge_research_workspace"

STRATEGY_LAB_TABLES = {
    "strategy_versions",
    "experiments",
    "experiment_arms",
    "market_snapshots",
    "strategy_decisions",
    "strategy_trades",
    "experiment_metric_snapshots",
    "promotion_events",
}

PARTIAL_UNIQUE_INDEXES = {
    "experiment_arms": {"uq_experiment_arms_active", "uq_experiment_arms_single_live_champion"},
    "strategy_trades": {"uq_strategy_trades_open_execution"},
}


def _engines():
    """(label, base_url) for every engine this run can reach."""
    targets = [(target_backend(), None)]
    pg = postgres_url()
    if pg is not None and target_backend() != "postgresql":
        targets.append(("postgresql", pg))
    return targets


class StrategyLabMigrationTests(unittest.TestCase):
    def _database(self, base_url):
        db = TestDatabase("sl_migration", base_url=base_url)
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
        self.assertEqual(list(script.get_heads()), [REVISION])
        self.assertEqual(
            script.get_revision(REVISION).down_revision,
            PREVIOUS_HEAD,
            "0007 must branch from the head it was written against; never edit "
            "another phase's revision (migrations/README.md).",
        )

    def test_it_applies_from_empty_and_from_the_previous_head(self):
        for label, base_url in _engines():
            with self.subTest(engine=label):
                engine = self._database(base_url)

                with engine.begin() as conn:
                    command.upgrade(alembic_config(conn), PREVIOUS_HEAD)
                present = set(inspect(engine).get_table_names())
                self.assertEqual(
                    present & STRATEGY_LAB_TABLES,
                    set(),
                    "the Strategy Lab tables must not exist before 0007 runs",
                )

                with engine.begin() as conn:
                    command.upgrade(alembic_config(conn), "head")
                after = set(inspect(engine).get_table_names())
                self.assertTrue(STRATEGY_LAB_TABLES <= after)
                with engine.connect() as conn:
                    self.assertEqual(current_revision(conn), REVISION)
                self._assert_matches_models(engine)

    def test_a_rerun_is_a_no_op(self):
        for label, base_url in _engines():
            with self.subTest(engine=label):
                engine = self._database(base_url)
                self.assertEqual(ensure_schema(engine), "created")
                self.assertEqual(ensure_schema(engine), "upgraded")
                self._assert_matches_models(engine)

    def test_it_is_reversible(self):
        for label, base_url in _engines():
            with self.subTest(engine=label):
                engine = self._database(base_url)
                ensure_schema(engine)

                with engine.begin() as conn:
                    command.downgrade(alembic_config(conn), PREVIOUS_HEAD)
                self.assertEqual(
                    set(inspect(engine).get_table_names()) & STRATEGY_LAB_TABLES, set()
                )
                with engine.connect() as conn:
                    self.assertEqual(current_revision(conn), PREVIOUS_HEAD)

                self.assertEqual(ensure_schema(engine), "upgraded")
                self._assert_matches_models(engine)

    def test_the_partial_unique_indexes_exist_on_both_engines(self):
        """The invariants are indexes; an index that only lands on one engine
        is not an invariant (Spec Q §8, §12 invariant 6)."""
        for label, base_url in _engines():
            with self.subTest(engine=label):
                engine = self._database(base_url)
                ensure_schema(engine)
                inspector = inspect(engine)
                for table, expected in PARTIAL_UNIQUE_INDEXES.items():
                    found = {
                        index["name"]
                        for index in inspector.get_indexes(table)
                        if index.get("unique")
                    }
                    self.assertTrue(
                        expected <= found,
                        f"{label}/{table}: missing {sorted(expected - found)}",
                    )

    def test_both_engines_were_actually_reached_when_available(self):
        labels = {label for label, _ in _engines()}
        self.assertIn(target_backend(), labels)
        if postgres_url() is not None:
            self.assertIn("postgresql", labels)


if __name__ == "__main__":
    unittest.main()
