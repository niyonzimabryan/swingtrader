"""
Ticker universe management.
Phase 1: loads from static config. Phase 2: refreshes from screener.
"""

from config.tickers import UNIVERSE
from database.db import get_session
from database.models import Ticker
from utils.logger import get_logger

log = get_logger("universe")


def seed_universe():
    """Populate the tickers table from config."""
    with get_session() as session:
        existing = {t.symbol for t in session.query(Ticker).all()}
        added = 0
        for symbol, sector in UNIVERSE.items():
            if symbol not in existing:
                session.add(Ticker(symbol=symbol, sector=sector, in_universe=True))
                added += 1
        log.info("universe_seeded", total=len(UNIVERSE), added=added, existing=len(existing))


def get_active_universe() -> list[dict]:
    """Get all tickers currently in the universe."""
    with get_session() as session:
        tickers = session.query(Ticker).filter_by(in_universe=True).all()
        return [{"symbol": t.symbol, "sector": t.sector, "id": t.id} for t in tickers]
