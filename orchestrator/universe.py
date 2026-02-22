"""
Ticker universe management.
Phase 1: loads from static config.
V2: adds watchlist management (operator/Opus-driven lower-threshold re-scanning).
"""

from datetime import datetime

from config.tickers import UNIVERSE
from database.db import get_session
from database.models import Ticker, WatchlistTicker
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


# --- V2: Watchlist Management ---

def add_to_watchlist(
    ticker: str, reason: str = "", source: str = "operator", sector: str = ""
) -> bool:
    """
    Add a ticker to the watchlist. Returns True if added, False if already active.
    Enforces max size — deactivates oldest if at capacity.
    """
    from config.settings import Settings
    settings = Settings()

    with get_session() as session:
        # Check if already active
        existing = session.query(WatchlistTicker).filter_by(
            ticker=ticker, active=True
        ).first()
        if existing:
            log.info("watchlist_already_active", ticker=ticker)
            return False

        # Check capacity
        active_count = session.query(WatchlistTicker).filter_by(active=True).count()
        if active_count >= settings.watchlist_max_size:
            # Deactivate oldest
            oldest = (
                session.query(WatchlistTicker)
                .filter_by(active=True)
                .order_by(WatchlistTicker.added_at.asc())
                .first()
            )
            if oldest:
                oldest.active = False
                oldest.deactivated_at = datetime.utcnow()
                log.info("watchlist_evicted", ticker=oldest.ticker)

        # If no sector provided, look up from UNIVERSE
        if not sector:
            sector = UNIVERSE.get(ticker, "Unknown")

        session.add(WatchlistTicker(
            ticker=ticker, sector=sector, reason=reason, source=source
        ))
        log.info("watchlist_added", ticker=ticker, source=source)
        return True


def get_watchlist() -> list[dict]:
    """Get all active watchlist tickers."""
    with get_session() as session:
        items = session.query(WatchlistTicker).filter_by(active=True).all()
        return [
            {
                "ticker": w.ticker,
                "sector": w.sector,
                "reason": w.reason,
                "source": w.source,
            }
            for w in items
        ]


def remove_from_watchlist(ticker: str) -> bool:
    """Soft-delete a ticker from watchlist."""
    with get_session() as session:
        item = session.query(WatchlistTicker).filter_by(
            ticker=ticker, active=True
        ).first()
        if item:
            item.active = False
            item.deactivated_at = datetime.utcnow()
            log.info("watchlist_removed", ticker=ticker)
            return True
        return False


