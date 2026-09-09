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
    None of the migration-owned tables exist. Run ``upgrade head``, which
    creates the whole schema from the baseline. Non-migration leftovers (the
    retired ``reddit_sentiment`` table) do not make a database non-empty; the
    baseline drops them.
``legacy``
    The tables and columns present are exactly those of some revision in the
    migration graph, but there is no ``alembic_version``. This is a database
    built by the pre-Alembic ``create_all()`` + inline-``ALTER TABLE`` path (it
    matches ``0001_baseline``), or a test database built straight from the
    models (it matches ``head``). Stamp the revision it matches, then
    ``upgrade head``.
``unknown``
    Anything else — a partial schema, a missing column, an unexpected extra
    column on a migration-owned table. Fail closed with recovery instructions
    rather than guess; a wrong stamp silently skips migrations forever.

Why the comparison is against *every* revision and not just the models
----------------------------------------------------------------------

Phase 0a compared the database against ``Base.metadata`` alone, which was the
same thing while the baseline was the only revision. It stops being the same
thing the moment a phase adds a table: the production SQLite file has the
baseline's tables and not the new one, and a models-only comparison would
classify it ``unknown`` and refuse to adopt it — exactly the database the
``legacy`` path exists for. So the signature of each revision is computed once
per process by replaying the migration graph into a scratch in-memory SQLite
database, and the observed schema is matched against all of them.

The signature compares *table and column names only*, not full DDL. The
pre-Alembic path added columns with server defaults that ``create_all()`` never
emitted (``t1_hit BOOLEAN DEFAULT 0``, ``memo_data_json TEXT DEFAULT '{}'``),
so a byte-exact DDL comparison would reject the very databases this branch is
supposed to adopt. Names are the part that determines whether a migration can
run.

Concurrent startup
------------------

The bot service and the workspace service (Spec K §4) can boot at the same
moment against one Postgres database, and ``upgrade head`` is not safe to run
twice concurrently. :func:`ensure_schema` therefore takes a transaction-scoped
Postgres advisory lock before it classifies anything, so the second process
waits and then observes the first one's result. SQLite has no equivalent and
needs none: it is a single-host file with one writer.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import StaticPool

from database.models import Base

#: The revision every later phase branches its migrations from.
BASELINE_REVISION = "0001_baseline"

ALEMBIC_VERSION_TABLE = "alembic_version"

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "migrations"

#: Key for ``pg_advisory_xact_lock``. Any stable 64-bit constant works; this one
#: is the low 63 bits of a digest of the lock's purpose, so it is unlikely to
#: collide with an advisory lock taken by anything else in the same database.
SCHEMA_ADVISORY_LOCK_KEY = 6_927_442_183_055_311_871


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


def _signature_of(connection) -> dict[str, frozenset[str]]:
    """Table -> column names for every table in ``connection``'s database."""
    inspector = inspect(connection)
    return {
        name: frozenset(c["name"] for c in inspector.get_columns(name))
        for name in inspector.get_table_names()
        if name != ALEMBIC_VERSION_TABLE
    }


@lru_cache(maxsize=1)
def revision_signatures() -> dict[str, dict[str, frozenset[str]]]:
    """``revision -> {table: columns}``, oldest revision first.

    Built by replaying the migration graph one revision at a time into a
    throwaway in-memory SQLite database. Table and column *names* are what the
    adoption check needs, and those are engine-independent, so replaying on
    SQLite says the same thing as replaying on Postgres and costs nothing.

    Computed at most once per process, and only on the path that needs it: a
    database with tables but no ``alembic_version``.
    """
    script = ScriptDirectory.from_config(alembic_config())
    engine = create_engine("sqlite://", poolclass=StaticPool)
    signatures: dict[str, dict[str, frozenset[str]]] = {}
    try:
        for revision in reversed(list(script.walk_revisions())):
            with engine.begin() as connection:
                command.upgrade(alembic_config(connection), revision.revision)
            with engine.connect() as connection:
                signatures[revision.revision] = _signature_of(connection)
    finally:
        engine.dispose()
    return signatures


@lru_cache(maxsize=1)
def migration_owned_tables() -> frozenset[str]:
    """Every table any revision creates, plus every table the models declare."""
    tables = set(Base.metadata.tables)
    for signature in revision_signatures().values():
        tables.update(signature)
    return frozenset(tables)


def observed_signature(connection) -> dict[str, frozenset[str]]:
    """Table -> column names for the migration-owned tables that exist."""
    return {
        name: columns
        for name, columns in _signature_of(connection).items()
        if name in migration_owned_tables()
    }


def adoptable_revision(connection) -> str | None:
    """The revision whose schema an unversioned database already has, if any.

    Newest match wins, though the signatures are distinct in practice: two
    revisions with identical table and column names would be a migration that
    changed nothing a name reflects.
    """
    observed = observed_signature(connection)
    if not observed:
        return None
    for revision, signature in reversed(list(revision_signatures().items())):
        if observed == signature:
            return revision
    return None


def classify(connection) -> str:
    """One of ``versioned``, ``empty``, ``legacy``, ``unknown``."""
    inspector = inspect(connection)
    if ALEMBIC_VERSION_TABLE in set(inspector.get_table_names()):
        return "versioned"

    if not observed_signature(connection):
        return "empty"

    return "legacy" if adoptable_revision(connection) is not None else "unknown"


def recovery_message(connection) -> str:
    observed = observed_signature(connection)
    lines = [
        "Database schema does not match any Alembic revision and has no "
        f"{ALEMBIC_VERSION_TABLE} table, so it cannot be adopted automatically.",
    ]
    for revision, signature in reversed(list(revision_signatures().items())):
        missing_tables = sorted(set(signature) - set(observed))
        extra = sorted(set(observed) - set(signature))
        column_drift = {
            table: {
                "missing": sorted(signature[table] - observed[table]),
                "unexpected": sorted(observed[table] - signature[table]),
            }
            for table in sorted(set(signature) & set(observed))
            if observed[table] != signature[table]
        }
        lines += [
            f"  against {revision}:",
            f"    missing tables:    {missing_tables}",
            f"    unexpected tables: {extra}",
            f"    column drift:      {column_drift}",
        ]
    lines += [
        "Recovery: back the database up, then either",
        "  (a) reconcile it by hand and run "
        f"`alembic stamp {BASELINE_REVISION}` (or the revision it matches) "
        "`&& alembic upgrade head`, or",
        "  (b) point DATABASE_URL at an empty database and migrate the data in "
        "(scripts/migrate_sqlite_to_postgres.py).",
        "Never stamp a schema you have not inspected: a wrong stamp makes every "
        "later migration a silent no-op.",
    ]
    return "\n".join(lines)


def _take_advisory_lock(connection) -> None:
    """Serialise concurrent ``upgrade head`` runs, on Postgres.

    Held for the enclosing transaction and released by its commit or rollback,
    so a process that dies mid-migration does not leave the lock held. On any
    other backend this is a no-op.
    """
    if connection.dialect.name != "postgresql":
        return
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": SCHEMA_ADVISORY_LOCK_KEY},
    )


def ensure_schema(engine: Engine) -> str:
    """Bring ``engine``'s database to ``head``. Returns the action taken.

    ``created`` (empty database), ``upgraded`` (already versioned), or
    ``adopted`` (unversioned schema stamped at the revision it matches, then
    upgraded).
    """
    with engine.begin() as connection:
        _take_advisory_lock(connection)
        state = classify(connection)
        if state == "unknown":
            raise SchemaMismatch(recovery_message(connection))
        cfg = alembic_config(connection)
        if state == "legacy":
            command.stamp(cfg, adoptable_revision(connection))
        command.upgrade(cfg, "head")

    return {"empty": "created", "versioned": "upgraded", "legacy": "adopted"}[state]


def create_all_for_tests(engine: Engine) -> None:
    """Build the schema straight from the models, for ephemeral test databases.

    This is the one sanctioned ``create_all()`` (strategy-lab-architecture.md
    §14). It is not a production migration path, and it is not what
    ``init_db()`` uses — production always goes through Alembic.
    """
    Base.metadata.create_all(engine)
