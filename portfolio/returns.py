"""Daily returns for the snapshot's risk block, from the local price store.

Reads ``price_data`` — the table the scanner already fills — and turns it into
the return series :mod:`portfolio.metrics` needs. Deliberately separate from
``metrics.py``: the statistics there are pure functions over lists and are
testable without a database, and mixing a query into them would end that.

Two rules, both about not inventing data:

- **No forward fill.** A missing session is a missing session; the series is
  built from the closes that exist, in date order, and a name with a short
  history shortens the window rather than having its gaps filled in.
- **Adjusted close where there is one.** An unadjusted series turns a split
  into a -50% return and a dividend into a small one, which would then be fed
  straight into a correlation.
"""

from __future__ import annotations

from sqlalchemy import select

from database.models import PriceData, Ticker

#: What the beta is measured against. A broad-market ETF held in the local
#: price store; nothing here fetches it.
DEFAULT_BENCHMARK = "SPY"

#: Sessions to pull. 250 for the long beta window, plus slack for gaps.
DEFAULT_LOOKBACK_SESSIONS = 300


def daily_returns(closes) -> list[float]:
    """Simple returns from a close series. A zero or missing close breaks the chain."""
    returns: list[float] = []
    previous = None
    for close in closes:
        if close is None or close <= 0:
            previous = None
            continue
        if previous is not None:
            returns.append((close - previous) / previous)
        previous = close
    return returns


def closes_for(session, symbol: str, *, limit: int = DEFAULT_LOOKBACK_SESSIONS) -> list[float]:
    rows = list(
        session.execute(
            select(PriceData.adj_close, PriceData.close)
            .join(Ticker, PriceData.ticker_id == Ticker.id)
            .where(Ticker.symbol == symbol.upper())
            .order_by(PriceData.date.desc())
            .limit(limit)
        ).all()
    )
    rows.reverse()
    return [(adj if adj else close) for adj, close in rows]


def returns_for(session, symbols, *, limit: int = DEFAULT_LOOKBACK_SESSIONS) -> dict:
    """``symbol -> [daily return]``. Symbols with no price history are omitted.

    Omitted rather than present-and-empty: an empty series in the dict would
    make the shortest-overlap truncation in
    :func:`portfolio.metrics.portfolio_returns` collapse the whole book to zero
    sessions because of one name nobody has prices for.
    """
    series = {}
    for symbol in {str(s).upper() for s in symbols}:
        values = daily_returns(closes_for(session, symbol, limit=limit))
        if values:
            series[symbol] = values
    return series


def benchmark_returns(
    session, symbol: str = DEFAULT_BENCHMARK, *, limit: int = DEFAULT_LOOKBACK_SESSIONS
) -> list[float]:
    return daily_returns(closes_for(session, symbol, limit=limit))
