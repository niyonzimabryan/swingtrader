"""Alembic-owned schema management.

Every schema change lives in ``migrations/versions``. Application startup calls
:func:`ensure_schema`, which decides what a given database needs and hands the
work to Alembic. ``Base.metadata.create_all()`` is no longer a production code
path — see ``migrations/README.md``.

Startup classification
----------------------

``ensure_schema`` looks at the tables that already exist:

``versioned``
    An ``alembic_version`` table is present. Run ``upgrade head``.
``empty``
    None of the ORM tables exist. Run ``upgrade head``, which creates the whole
    schema from the baseline. Non-ORM leftovers (the retired
    ``reddit_sentiment`` table) do not make a database non-empty; the baseline
    drops them.
``legacy``
    Every ORM table exists with the expected column names but there is no
    ``alembic_version``. This is a database built by the pre-Alembic
    ``create_all()`` + inline-``ALTER TABLE`` path. Stamp the baseline, then
    ``upgrade head``.
``unknown``
    Anything else — a partial schema, a missing column, an unexpected extra
    column on an ORM table. Fail closed with recovery instructions rather than
    guess; a wrong stamp silently skips migrations forever.

The legacy signature compares *table and column names only*, not full DDL. The
pre-Alembic path added columns with server defaults that ``create_all()`` never
emitted (``t1_hit BOOLEAN DEFAULT 0``, ``memo_data_json TEXT DEFAULT '{}'``),
so a byte-exact DDL comparison would reject the very databases this branch is
supposed to adopt. Names are the part that determines whether a migration can
run.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine import Engine, make_url

from database.models import Base

#: The revision every later phase branches its migrations from.
BASELINE_REVISION = "0001_baseline"

ALEMBIC_VERSION_TABLE = "alembic_version"

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "migrations"


class SchemaMismatch(RuntimeError):
    """The database does not match any schema Alembic knows how to adopt."""


def backend_name(database_url: str) -> str:
    """``sqlite``/``postgresql``/... for a URL, without substring guessing.

    ``"sqlite" in database_url`` was the old test and it is wrong for a
    Postgres URL whose host, database, or password happens to contain the
    string.
    """
    return make_url(database_url).get_backend_name()


def alembic_config(connection=None) -> Config:
    """Config pointing at ``migrations/``, optionally bound to a connection."""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    # The URL is never written into the Config: it can contain '%' (which
    # configparser would interpolate) and credentials. env.py takes the
    # connection from attributes, or DATABASE_URL when run from the CLI.
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def current_revision(connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def expected_signature() -> dict[str, set[str]]:
    """Table -> column names as the ORM models declare them."""
    return {
        name: {c.name for c in table.columns}
        for name, table in Base.metadata.tables.items()
    }


def observed_signature(connection) -> dict[str, set[str]]:
    """Table -> column names for the ORM tables that exist in the database."""
    inspector = inspect(connection)
    present = set(inspector.get_table_names())
    return {
        name: {c["name"] for c in inspector.get_columns(name)}
        for name in Base.metadata.tables
        if name in present
    }


def classify(connection) -> str:
    """One of ``versioned``, ``empty``, ``legacy``, ``unknown``."""
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())

    if ALEMBIC_VERSION_TABLE in tables:
        return "versioned"

    observed = observed_signature(connection)
    if not observed:
        return "empty"

    expected = expected_signature()
    if set(observed) != set(expected):
        return "unknown"
    for table, columns in expected.items():
        if observed[table] != columns:
            return "unknown"
    return "legacy"


def _recovery_message(connection) -> str:
    expected = expected_signature()
    observed = observed_signature(connection)
    missing_tables = sorted(set(expected) - set(observed))
    extra = sorted(set(observed) - set(expected))
    column_drift = {
        table: {
            "missing": sorted(expected[table] - observed[table]),
            "unexpected": sorted(observed[table] - expected[table]),
        }
        for table in sorted(set(expected) & set(observed))
        if observed[table] != expected[table]
    }
    return (
        "Database schema does not match the Alembic baseline and has no "
        f"{ALEMBIC_VERSION_TABLE} table, so it cannot be adopted automatically.\n"
        f"  missing tables:   {missing_tables}\n"
        f"  unexpected tables:{extra}\n"
        f"  column drift:     {column_drift}\n"
        "Recovery: back the database up, then either\n"
        "  (a) reconcile it by hand and run "
        f"`alembic stamp {BASELINE_REVISION} && alembic upgrade head`, or\n"
        "  (b) point DATABASE_URL at an empty database and migrate the data in.\n"
        "Never stamp a schema you have not inspected: a wrong stamp makes every "
        "later migration a silent no-op."
    )


def ensure_schema(engine: Engine) -> str:
    """Bring ``engine``'s database to ``head``. Returns the action taken.

    ``created`` (empty database), ``upgraded`` (already versioned), or
    ``adopted`` (unversioned pre-Alembic schema stamped, then upgraded).
    """
    with engine.begin() as connection:
        state = classify(connection)
        if state == "unknown":
            raise SchemaMismatch(_recovery_message(connection))
        cfg = alembic_config(connection)
        if state == "legacy":
            command.stamp(cfg, BASELINE_REVISION)
        command.upgrade(cfg, "head")

    return {"empty": "created", "versioned": "upgraded", "legacy": "adopted"}[state]


def create_all_for_tests(engine: Engine) -> None:
    """Build the schema straight from the models, for ephemeral test databases.

    This is the one sanctioned ``create_all()`` (strategy-lab-architecture.md
    §14). It is not a production migration path, and it is not what
    ``init_db()`` uses — production always goes through Alembic.
    """
    Base.metadata.create_all(engine)
