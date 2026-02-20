from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
from database.models import Base

engine = None
SessionLocal = None


def init_db(database_url: str = "sqlite:///swing_trader.db"):
    global engine, SessionLocal
    engine = create_engine(
        database_url,
        echo=False,
        connect_args={"check_same_thread": False} if "sqlite" in database_url else {},
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False)
    Base.metadata.create_all(engine)
    return engine


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
