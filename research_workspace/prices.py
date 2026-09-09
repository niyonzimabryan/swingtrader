"""The price seam the ``price_level`` invalidator reads through.

The price plane is a parallel phase (Spec O). Until it lands, the incumbent
``data/market_data.py`` is what this system has, and the daily invalidator job
has to work today. So the job never calls a price source directly: it calls
:class:`PriceSource`, and the adapter behind it is swapped when the plane
arrives — one import site, not a grep across the package.

Two implementations ship:

:class:`TablePriceSource`
    The cached ``price_data`` rows the incumbent adapter already writes. No
    network, which is what makes the job runnable in CI and what tests use.
:class:`MarketDataPriceSource`
    ``data.market_data.MarketDataAdapter``, imported lazily so this package
    does not drag ``yfinance`` into every process that reads a dossier.

**Missing data never triggers an invalidator.** A source that cannot supply the
sessions a check needs returns what it has, the check reports
``insufficient_data``, and the thesis is left alone. A page fired because a
price feed was down is worse than a page not fired: it teaches the owner to
ignore pages.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol, Sequence

from utils.logger import get_logger

log = get_logger("research_prices")


@dataclass(frozen=True)
class SessionClose:
    """One session's closing price. Adjusted where the source supplies it."""

    session_date: date
    close: float


class PriceSource(Protocol):
    """The whole contract: the last ``sessions`` closes, oldest first."""

    def recent_closes(self, ticker: str, sessions: int) -> Sequence[SessionClose]:
        ...


class TablePriceSource:
    """Cached ``price_data`` rows. Offline, and therefore CI-runnable."""

    def __init__(self, session):
        self._session = session

    def recent_closes(self, ticker: str, sessions: int) -> list[SessionClose]:
        from database.models import PriceData, Ticker

        rows = (
            self._session.query(PriceData)
            .join(Ticker, PriceData.ticker_id == Ticker.id)
            .filter(Ticker.symbol == ticker.upper())
            .filter(PriceData.close.isnot(None))
            .order_by(PriceData.date.desc())
            .limit(int(sessions))
            .all()
        )
        return [SessionClose(r.date, float(r.close)) for r in reversed(rows)]


class MarketDataPriceSource:
    """The incumbent ``MarketDataAdapter``, behind the seam.

    ``get_daily_bars`` returns an empty frame rather than raising when a fetch
    fails, so an outage arrives here as "no sessions" and the check reports
    ``insufficient_data`` — the designed behaviour, not a swallowed error.
    """

    def __init__(self, adapter=None):
        self._adapter = adapter

    def _get(self):
        if self._adapter is None:
            from data.market_data import MarketDataAdapter

            self._adapter = MarketDataAdapter()
        return self._adapter

    def recent_closes(self, ticker: str, sessions: int) -> list[SessionClose]:
        # A generous lookback: `sessions` calendar days would not cover
        # `sessions` *trading* days across a holiday week.
        frame = self._get().get_daily_bars(ticker, days=max(int(sessions) * 3, 10))
        if frame is None or getattr(frame, "empty", True):
            log.warning("price_source_empty", ticker=ticker)
            return []
        tail = frame.tail(int(sessions))
        out: list[SessionClose] = []
        for stamp, row in tail.iterrows():
            close = row.get("Close")
            if close is None:
                continue
            day = stamp.date() if hasattr(stamp, "date") else stamp
            out.append(SessionClose(day, float(close)))
        return out


def default_price_source(session=None) -> PriceSource:
    """The cached table when a database session is to hand, else the vendor."""
    return TablePriceSource(session) if session is not None else MarketDataPriceSource()
