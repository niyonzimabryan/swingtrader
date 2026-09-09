from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
from database.schema import backend_name, ensure_schema
from utils.logger import get_logger

log = get_logger("database")

engine = None
SessionLocal = None

DEFAULT_DATABASE_URL = "sqlite:///swing_trader.db"


def init_db(database_url: str = DEFAULT_DATABASE_URL):
    """Create the engine and bring the schema to the Alembic head.

    ``DATABASE_URL`` selects the engine: ``sqlite:///...`` for local dev and the
    current Railway deploy, ``postgresql+psycopg://...`` for Postgres. Nothing
    else in the call path differs — engine-specific tuning is gated on the URL's
    parsed backend name, never on a substring match.
    """
    global engine, SessionLocal

    backend = backend_name(database_url)

    if backend == "sqlite":
        _ensure_sqlite_parent_dir(database_url)

    engine = create_engine(
        database_url,
        echo=False,
        # SQLite's DBAPI binds a connection to its creating thread unless told
        # otherwise; every other driver is already thread-safe here.
        connect_args={"check_same_thread": False} if backend == "sqlite" else {},
    )
    if backend == "sqlite":
        _configure_sqlite_pragmas(engine)

    SessionLocal = sessionmaker(bind=engine, autoflush=False)

    # Schema first, sessions after (strategy-lab-architecture.md §14): no ORM
    # session is created before migrations have run.
    action = ensure_schema(engine)
    log.info("schema_ready", backend=backend, action=action)

    return engine


def _ensure_sqlite_parent_dir(database_url: str) -> None:
    """Create the directory holding the SQLite file (Railway volume mounts)."""
    db_path = database_url.replace("sqlite:///", "")
    parent = Path(db_path).parent
    if parent != Path("."):
        parent.mkdir(parents=True, exist_ok=True)


def _configure_sqlite_pragmas(eng):
    """WAL + busy_timeout, for SQLite only.

    WAL lets the scheduler read while the bot writes; busy_timeout absorbs the
    short lock contention that follows. Both are SQLite storage-engine settings
    with no Postgres equivalent (Postgres has MVCC and lock timeouts already),
    so the listener is only ever attached to a SQLite engine.
    """
    @event.listens_for(eng, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()


@contextmanager
def get_session() -> Session:
    if SessionLocal is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session_factory():
    if SessionLocal is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return SessionLocal
