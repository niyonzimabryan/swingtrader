"""Engine-selectable database fixtures for the test suite.

The suite runs twice in CI: once on SQLite (the default, and what local dev and
the current Railway deploy use) and once on Postgres. Tests do not hardcode an
engine — they ask this module for a database and get whichever engine the run
targets.

Selection is by ``TEST_DATABASE_URL``:

* unset -> a throwaway SQLite file in a temporary directory;
* set to a Postgres URL -> a freshly created, uniquely named schema in that
  database, with ``search_path`` pinned to it. Every test therefore gets an
  empty schema of its own, which is what SQLite's per-test file gives for free.

Tests that are *about* SQLite (WAL pragmas, legacy ``.db`` file adoption) build
their own ``sqlite:///`` URL and keep running on both matrix entries; see
``sqlite_url``.

The name is deliberately not ``test_*``: ``unittest discover -p "test_*.py"``
would otherwise import it as a test module.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"


def target_url() -> str | None:
    """The Postgres URL this run targets, or ``None`` for the SQLite default."""
    return os.environ.get(TEST_DATABASE_URL_ENV) or None


def target_backend() -> str:
    url = target_url()
    return make_url(url).get_backend_name() if url else "sqlite"


class TestDatabase:
    """A disposable database for one test case.

    ``url`` is safe to pass to :func:`database.db.init_db`. ``path`` is the
    SQLite file, or ``None`` on Postgres. ``cleanup`` releases the temporary
    directory or drops the schema; call it from ``tearDown``.
    """

    def __init__(self, name: str = "test"):
        self.backend = target_backend()
        self._tmp: tempfile.TemporaryDirectory | None = None
        self._schema: str | None = None
        self._admin_url: str | None = None

        base = target_url()
        if base is None:
            self._tmp = tempfile.TemporaryDirectory()
            self.path: Path | None = Path(self._tmp.name) / f"{name}.db"
            self.url = f"sqlite:///{self.path}"
        else:
            self._admin_url = base
            self._schema = f"t_{uuid.uuid4().hex[:20]}"
            self._create_schema()
            self.path = None
            self.url = make_url(base).update_query_dict(
                {"options": f"-csearch_path={self._schema}"}
            ).render_as_string(hide_password=False)

    def _run_admin(self, *statements: str) -> None:
        engine = create_engine(self._admin_url)
        try:
            with engine.begin() as conn:
                for statement in statements:
                    conn.execute(text(statement))
        finally:
            engine.dispose()

    def _create_schema(self) -> None:
        self._run_admin(
            f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE',
            f'CREATE SCHEMA "{self._schema}"',
        )

    def cleanup(self) -> None:
        # Drop the engine the test bound to database.db first, or Postgres will
        # block on the open session's locks while dropping the schema.
        from database import db as db_module

        if db_module.engine is not None:
            db_module.engine.dispose()
        if self._schema is not None:
            self._run_admin(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE')
        if self._tmp is not None:
            self._tmp.cleanup()


def init_test_db(name: str = "test") -> TestDatabase:
    """Create a disposable database and point ``database.db`` at it."""
    from database.db import init_db

    database = TestDatabase(name)
    init_db(database.url)
    return database


def sqlite_url(directory: Path, name: str = "test") -> str:
    """A SQLite URL, regardless of which engine the run targets.

    For tests that assert SQLite-specific behaviour, so that coverage does not
    disappear from the Postgres matrix entry.
    """
    return f"sqlite:///{Path(directory) / f'{name}.db'}"
