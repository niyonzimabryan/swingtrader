"""``0012_notify_email_cards`` applies from the previous head and reverses, on both engines.

``tests/test_schema_discipline.py`` already proves the graph has one base and
one head and that ``upgrade head`` reproduces the models. What this adds is the
step production actually performs: a database sitting at
``0011_comparable_subject_ticker`` moving forward, the two tables and their
indexes arriving, and ``downgrade`` putting it back exactly.

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
from sqlalchemy import create_engine, inspect

from database.models import Base
from database.schema import alembic_config, ensure_schema
from tests.dbfixture import TestDatabase, postgres_url, target_backend

REVISION = "0012_notify_email_cards"
PREVIOUS_HEAD = "0011_comparable_subject_ticker"

NOTIFY_TABLES = {"cards", "notifications_sent"}

EXPECTED_INDEXES = {
    "cards": {"ix_cards_card_uid", "ix_cards_kind_ref", "ix_cards_created_at"},
    "notifications_sent": {
        "ix_notifications_sent_kind_ref",
        "ix_notifications_sent_created_at",
    },
}


def _engines():
    """(label, base_url) for every engine this run can reach."""
    targets = [(target_backend(), None)]
    pg = postgres_url()
    if pg is not None and target_backend() != "postgresql":
        targets.append(("postgresql", pg))
    return targets


class NotifyMigrationTests(unittest.TestCase):
    def _database(self, base_url):
        db = TestDatabase("notify_migration", base_url=base_url)
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
            "0012 must branch from the head it was written against; never edit "
            "another phase's revision (migrations/README.md).",
        )

    def test_it_applies_from_the_previous_head(self):
        for label, base_url in _engines():
            with self.subTest(engine=label):
                engine = self._database(base_url)

                with engine.begin() as conn:
                    command.upgrade(alembic_config(conn), PREVIOUS_HEAD)
                self.assertEqual(
                    set(inspect(engine).get_table_names()) & NOTIFY_TABLES,
                    set(),
                    "the notify tables must not exist before 0012 runs",
                )

                with engine.begin() as conn:
                    command.upgrade(alembic_config(conn), "head")
                inspector = inspect(engine)
                self.assertTrue(NOTIFY_TABLES <= set(inspector.get_table_names()))
                for table, expected in EXPECTED_INDEXES.items():
                    names = {index["name"] for index in inspector.get_indexes(table)}
                    self.assertTrue(
                        expected <= names, f"{table}: missing {sorted(expected - names)}"
                    )
                self._assert_matches_models(engine)

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
                self.assertEqual(
                    set(inspect(engine).get_table_names()) & NOTIFY_TABLES, set()
                )
                with engine.connect() as conn:
                    self.assertIn(
                        PREVIOUS_HEAD, MigrationContext.configure(conn).get_current_heads()
                    )

                # ...and forward again, because a downgrade that cannot be undone
                # is a one-way door dressed up as a rollback.
                with engine.begin() as conn:
                    command.upgrade(alembic_config(conn), "head")
                self.assertTrue(NOTIFY_TABLES <= set(inspect(engine).get_table_names()))
                self._assert_matches_models(engine)

    def test_a_card_and_a_delivery_row_survive_a_round_trip(self):
        """The tables are not just created — they hold what the code writes."""
        import json

        from database.db import get_session, init_db
        from database.models import Card, NotificationSend
        from notify import store

        db = TestDatabase("notify_roundtrip")
        self.addCleanup(db.cleanup)
        init_db(db.url)

        payload = {
            "kind": "proposal",
            "subject": "[approval needed] AMD — proposal 42",
            "blocks": [],
            "chart": {"bars": [{"date": "2026-09-01", "close": 1.0}]},
        }
        self.assertTrue(
            store.store_card(
                uid="uid-1", kind="proposal", ref="ref-1", subject=payload["subject"], payload=payload
            )
        )
        self.assertTrue(
            store.record_send(
                kind="proposal", ref="ref-1", channel="email", status="sent",
                provider_id="re-1", card_uid="uid-1",
            )
        )

        self.assertEqual(store.load_card("uid-1"), payload)
        self.assertIsNone(store.load_card("uid-missing"))

        with get_session() as session:
            card = session.query(Card).one()
            self.assertEqual(json.loads(card.payload_json), payload)
            send = session.query(NotificationSend).one()
            self.assertEqual((send.channel, send.status, send.card_uid), ("email", "sent", "uid-1"))


if __name__ == "__main__":
    unittest.main()
