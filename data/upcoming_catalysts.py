"""Upcoming catalyst lookup for watchlist + open positions.

MVP scope (t_82dcdb23): pulls the next earnings date per ticker via yfinance.
Other catalyst types (FDA, conferences) are out of scope until those data
sources land — the function shape leaves room to merge them in later.
"""

from datetime import date, datetime
from typing import Iterable, Optional

import pandas as pd
import yfinance as yf

from utils.logger import get_logger

log = get_logger("upcoming_catalysts")


def get_next_earnings_date(ticker: str, today: Optional[date] = None) -> Optional[date]:
    """Return the next future earnings date for a ticker, or None if unknown.

    Uses yfinance Ticker.earnings_dates which returns historical + scheduled
    earnings indexed by datetime. Filters to dates strictly after `today`.
    Catches all exceptions so a single bad ticker doesn't break the bulk path.
    """
    today = today or date.today()
    try:
        stock = yf.Ticker(ticker)
        ed = stock.earnings_dates
        if ed is None or ed.empty:
            return None

        index = ed.index
        if getattr(index, "tz", None) is not None:
            now = pd.Timestamp.now(tz=index.tz)
        else:
            now = pd.Timestamp.now()
        future = ed[index > now]
        if future.empty:
            return None
        # earnings_dates is sorted descending (most recent first); the soonest
        # future event is therefore the LAST row of the future slice.
        next_ts = future.index.max()
        return next_ts.date()
    except Exception as e:
        log.warning("upcoming_earnings_lookup_failed", ticker=ticker, error=str(e))
        return None


def collect_upcoming(
    tickers: Iterable[str],
    today: Optional[date] = None,
    earnings_fn=get_next_earnings_date,
) -> list[dict]:
    """Collect known upcoming catalysts for a set of tickers.

    Returns a list of dicts: {ticker, catalyst_type, event_date, days_until}.
    Sorted by event_date ascending. Tickers with no known catalyst are
    omitted. `earnings_fn` is injectable for tests.
    """
    today = today or date.today()
    seen = set()
    out: list[dict] = []
    for raw in tickers:
        ticker = (raw or "").strip().upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        next_earnings = earnings_fn(ticker, today=today)
        if next_earnings is None:
            continue
        out.append({
            "ticker": ticker,
            "catalyst_type": "earnings",
            "event_date": next_earnings,
            "days_until": (next_earnings - today).days,
        })
    out.sort(key=lambda r: r["event_date"])
    return out


def format_upcoming_message(catalysts: list[dict]) -> str:
    """Format upcoming catalysts as a plain-text Telegram message."""
    if not catalysts:
        return (
            "📅 No upcoming catalysts found for your watchlist or open positions.\n\n"
            "Add tickers via /watchlist add TICKER reason."
        )
    lines = ["📅 Upcoming catalysts:\n"]
    for c in catalysts:
        days = c["days_until"]
        when = "today" if days == 0 else f"in {days}d"
        lines.append(
            f"• {c['ticker']} — {c['catalyst_type']} — "
            f"{c['event_date'].isoformat()} ({when})"
        )
    return "\n".join(lines)
