"""Sidecar files land on the volume even when DATABASE_URL is Postgres.

``data_dir()`` derived its answer from a ``sqlite:///`` URL, so the moment
``DATABASE_URL`` became a Postgres URL it silently returned the working
directory — the ephemeral container filesystem. Two things live there: the
pattern-backfill queue, and the encrypted Robinhood OAuth token blob. Losing the
second on every deploy would force a manual re-authentication that nobody would
connect to the cutover.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from config.settings import Settings, data_dir, resolve_backfill_queue_path
from database.token_store import token_store_path

POSTGRES = "postgresql+psycopg://user:pw@host:5432/railway"


class DataDirTests(unittest.TestCase):
    def test_an_explicit_data_dir_wins(self):
        settings = SimpleNamespace(data_dir="/data", database_url="sqlite:///other/x.db")
        self.assertEqual(data_dir(settings), Path("/data"))

    def test_it_falls_back_to_the_sqlite_directory(self):
        settings = SimpleNamespace(data_dir="", database_url="sqlite:////data/swing_trader.db")
        self.assertEqual(data_dir(settings), Path("/data"))

    def test_a_bare_sqlite_filename_means_the_working_directory(self):
        settings = SimpleNamespace(data_dir="", database_url="sqlite:///swing_trader.db")
        self.assertEqual(data_dir(settings), Path.cwd())

    def test_postgres_without_data_dir_still_answers_but_off_the_volume(self):
        """Documented behaviour, not a good one: DATA_DIR is what to set."""
        settings = SimpleNamespace(data_dir="", database_url=POSTGRES)
        self.assertEqual(data_dir(settings), Path.cwd())

    def test_postgres_with_data_dir_lands_on_the_volume(self):
        settings = SimpleNamespace(data_dir="/data", database_url=POSTGRES)
        self.assertEqual(data_dir(settings), Path("/data"))

    def test_the_setting_exists_and_defaults_empty(self):
        self.assertEqual(Settings.model_fields["data_dir"].default, "")


class SidecarPathTests(unittest.TestCase):
    def test_the_robinhood_token_blob_follows_data_dir(self):
        settings = SimpleNamespace(data_dir="/data", database_url=POSTGRES)
        self.assertEqual(token_store_path(settings), Path("/data/robinhood_token.enc"))

    def test_the_backfill_queue_follows_data_dir(self):
        settings = SimpleNamespace(
            data_dir="/data",
            database_url=POSTGRES,
            pattern_backfill_queue_path=".pattern_backfill_queue.jsonl",
        )
        self.assertEqual(
            resolve_backfill_queue_path(settings),
            Path("/data/.pattern_backfill_queue.jsonl"),
        )

    def test_an_absolute_queue_path_is_left_alone(self):
        settings = SimpleNamespace(
            data_dir="/data",
            database_url=POSTGRES,
            pattern_backfill_queue_path="/tmp/queue.jsonl",
        )
        self.assertEqual(resolve_backfill_queue_path(settings), Path("/tmp/queue.jsonl"))


if __name__ == "__main__":
    unittest.main()
