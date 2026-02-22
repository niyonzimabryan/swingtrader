import os
from pathlib import Path

from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
from database.models import Base
from utils.logger import get_logger

log = get_logger("database")

engine = None
SessionLocal = None


def init_db(database_url: str = "sqlite:///swing_trader.db"):
    global engine, SessionLocal

    # Ensure parent directory exists for SQLite (needed for Railway volume mounts)
    if database_url.startswith("sqlite:///"):
        db_path = database_url.replace("sqlite:///", "")
        parent = Path(db_path).parent
        if parent != Path("."):
            parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        database_url,
        echo=False,
        connect_args={"check_same_thread": False} if "sqlite" in database_url else {},
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False)
    Base.metadata.create_all(engine)
    _run_migrations(engine)
    return engine


def _run_migrations(eng):
    """Run lightweight schema migrations for existing SQLite DBs."""
    inspector = inspect(eng)

    # v2.1: Add memo_data_json column to memos table
    if "memos" in inspector.get_table_names():
        columns = [c["name"] for c in inspector.get_columns("memos")]
        if "memo_data_json" not in columns:
            with eng.connect() as conn:
                conn.execute(text("ALTER TABLE memos ADD COLUMN memo_data_json TEXT DEFAULT '{}'"))
                conn.commit()
            log.info("migration_applied", migration="add_memo_data_json_to_memos")


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
