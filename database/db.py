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

    # v3: Add position monitoring columns to trades table
    if "trades" in inspector.get_table_names():
        columns = [c["name"] for c in inspector.get_columns("trades")]
        new_cols = {
            "peak_price": "FLOAT",
            "t1_hit": "BOOLEAN DEFAULT 0",
            "t2_hit": "BOOLEAN DEFAULT 0",
            "t1_approaching_sent": "BOOLEAN DEFAULT 0",
            "time_warning_sent": "BOOLEAN DEFAULT 0",
            "drawdown_alert_sent": "BOOLEAN DEFAULT 0",
            "broker": "VARCHAR(30) DEFAULT 'alpaca'",
            "broker_account_id": "VARCHAR(100)",
            "broker_order_id": "VARCHAR(100)",
            "broker_stop_order_id": "VARCHAR(100)",
            "broker_order_strategy": "VARCHAR(50)",
            "order_review_json": "TEXT DEFAULT '{}'",
            "execution_mode": "VARCHAR(20) DEFAULT 'paper'",
            "requested_notional": "FLOAT",
            "filled_notional": "FLOAT",
        }
        with eng.connect() as conn:
            for col_name, col_type in new_cols.items():
                if col_name not in columns:
                    conn.execute(text(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}"))
                    log.info("migration_applied", migration=f"add_{col_name}_to_trades")
            conn.commit()

    if "web_research_cache" not in inspector.get_table_names():
        with eng.connect() as conn:
            conn.execute(text("""
                CREATE TABLE web_research_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cache_key VARCHAR(120) NOT NULL UNIQUE,
                    ticker VARCHAR(10) NOT NULL,
                    research_date VARCHAR(10) NOT NULL,
                    catalyst_hash VARCHAR(64) NOT NULL,
                    provider VARCHAR(30) DEFAULT '',
                    model_used VARCHAR(80) DEFAULT '',
                    result_json TEXT DEFAULT '{}',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    expires_at DATETIME
                )
            """))
            conn.execute(text(
                "CREATE INDEX ix_web_research_cache_lookup "
                "ON web_research_cache (ticker, research_date, catalyst_hash)"
            ))
            conn.commit()
        log.info("migration_applied", migration="create_web_research_cache")


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
