"""Alembic environment.

Two entry points share this file:

* the ``alembic`` CLI, which supplies no connection and falls back to
  ``DATABASE_URL`` (or ``sqlalchemy.url`` in ``alembic.ini``);
* :func:`database.schema.ensure_schema`, which puts a live ``Connection`` in
  ``config.attributes["connection"]`` so startup migrations run on the same
  engine the application is about to use.

``logging.fileConfig`` is deliberately *not* called: the application configures
structlog before the database is touched, and re-reading ``alembic.ini``'s
logging section would tear that down.
"""

import logging
import os
import sys
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection

# Repo root on sys.path so `database.models` imports when run via the CLI.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.models import Base  # noqa: E402

config = context.config

target_metadata = Base.metadata


def _resolve_url() -> str:
    url = os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "No database URL. Set DATABASE_URL or alembic.ini's sqlalchemy.url."
        )
    return url


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        # SQLite cannot ALTER most things in place; batch mode rewrites the
        # table instead. Harmless on Postgres, so it is switched per dialect.
        render_as_batch=connection.dialect.name == "sqlite",
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
        return

    # CLI mode. Nothing has configured logging here (the app configures
    # structlog before it ever reaches this file), so `alembic upgrade` would
    # otherwise run silently.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("alembic").setLevel(logging.INFO)

    connectable = create_engine(_resolve_url(), poolclass=pool.NullPool)
    with connectable.connect() as conn:
        _configure(conn)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
