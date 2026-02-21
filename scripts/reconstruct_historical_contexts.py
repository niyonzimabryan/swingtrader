"""
Reconstruct Historical Contexts — one-time backfill script.

For each existing HistoricalPattern in the DB, gathers contextual data:
- VIX level at the event date
- Forward P/E ratio at the event date (via yfinance)
- 20-day price momentum at the event date
- S&P 500 distance from 200-day MA at the event date
- Macro regime: INFERRED from VIX + S&P 500 (not from DB — macro agent
  only has data going forward, not for historical dates)

Uses yfinance (free, no API key needed) for all data. Works for any
publicly traded ticker, not just S&P 500 constituents.

Usage:
    python -m scripts.reconstruct_historical_contexts [--limit N] [--dry-run]

Rate limiting:
    yfinance has soft rate limits. The script batches requests and adds delays
    to stay within limits (~1-2 requests/second).
"""

import sys
import os
import time
import argparse
from datetime import datetime, timedelta

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.db import get_session, init_db
from database.models import HistoricalPattern, HistoricalContext
from utils.logger import get_logger

log = get_logger("reconstruct_contexts")


def get_vix_history(start_date: str, end_date: str) -> dict:
    """
    Fetch VIX closing prices for a date range.
    Returns {date_str: close_price} dict.
    """
    import yfinance as yf

    try:
        vix = yf.Ticker("^VIX")
        hist = vix.history(start=start_date, end=end_date)
        if hist.empty:
            return {}
        result = {}
        for idx, row in hist.iterrows():
            date_str = idx.strftime("%Y-%m-%d")
            result[date_str] = round(float(row["Close"]), 2)
        return result
    except Exception as e:
        log.warning("vix_fetch_failed", error=str(e))
        return {}


def get_sp500_200ma_distance(start_date: str, end_date: str) -> dict:
    """
    Compute S&P 500 distance from 200-day MA for each date in range.
    Returns {date_str: distance_pct} dict.
    """
    import yfinance as yf
    import numpy as np

    try:
        # Need extra lookback for 200-day MA
        start_dt = datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=300)
        spy = yf.Ticker("^GSPC")
        hist = spy.history(start=start_dt.strftime("%Y-%m-%d"), end=end_date)
        if hist.empty or len(hist) < 200:
            return {}

        hist["MA200"] = hist["Close"].rolling(window=200).mean()
        hist["distance_pct"] = ((hist["Close"] - hist["MA200"]) / hist["MA200"] * 100).round(2)

        result = {}
        for idx, row in hist.iterrows():
            date_str = idx.strftime("%Y-%m-%d")
            if date_str >= start_date and not np.isnan(row["distance_pct"]):
                result[date_str] = float(row["distance_pct"])
        return result
    except Exception as e:
        log.warning("sp500_200ma_failed", error=str(e))
        return {}


def get_momentum_20d(ticker: str, event_date: str) -> float | None:
    """
    Compute 20-day price momentum (%) for a ticker at a given date.
    Returns percentage return over prior 20 trading days, or None.
    """
    import yfinance as yf

    try:
        event_dt = datetime.strptime(event_date, "%Y-%m-%d")
        start_dt = event_dt - timedelta(days=40)  # Extra buffer for non-trading days
        stock = yf.Ticker(ticker)
        hist = stock.history(start=start_dt.strftime("%Y-%m-%d"), end=(event_dt + timedelta(days=1)).strftime("%Y-%m-%d"))

        if hist.empty or len(hist) < 20:
            return None

        # Find the row closest to event_date (or earlier)
        close_at_event = float(hist["Close"].iloc[-1])
        close_20d_before = float(hist["Close"].iloc[-20])

        if close_20d_before > 0:
            return round(((close_at_event / close_20d_before) - 1) * 100, 2)
        return None
    except Exception as e:
        return None


def get_fwd_pe_approx(ticker: str, event_date: str) -> float | None:
    """
    Approximate forward P/E at event date.
    Uses yfinance: price at date / trailing EPS (rough proxy).
    Returns None if unavailable.
    """
    import yfinance as yf

    try:
        stock = yf.Ticker(ticker)
        event_dt = datetime.strptime(event_date, "%Y-%m-%d")
        start_dt = event_dt - timedelta(days=5)

        hist = stock.history(start=start_dt.strftime("%Y-%m-%d"), end=(event_dt + timedelta(days=1)).strftime("%Y-%m-%d"))
        if hist.empty:
            return None

        price = float(hist["Close"].iloc[-1])

        # Try to get trailing EPS from financials
        info = stock.info
        trailing_eps = info.get("trailingEps")
        if trailing_eps and trailing_eps > 0:
            return round(price / trailing_eps, 1)

        return None
    except Exception as e:
        return None


def infer_macro_regime(vix: float | None, sp500_dist: float | None) -> str:
    """
    Infer macro regime from market data instead of relying on DB.
    Uses VIX level + S&P 500 distance from 200-day MA as signals.

    Logic:
    - risk-off: VIX > 25 OR S&P below 200MA (negative distance)
    - risk-on:  VIX < 16 AND S&P well above 200MA (> +5%)
    - neutral:  everything in between

    This mirrors what the macro agent would compute, but works for any historical date.
    """
    if vix is None and sp500_dist is None:
        return ""

    # Default to neutral, then override
    regime = "neutral"

    # Strong risk-off signals
    if vix is not None and vix > 25:
        regime = "risk-off"
    elif sp500_dist is not None and sp500_dist < -2:
        regime = "risk-off"
    # Strong risk-on signals (both must agree if both available)
    elif vix is not None and sp500_dist is not None:
        if vix < 16 and sp500_dist > 5:
            regime = "risk-on"
        elif vix < 18 and sp500_dist > 3:
            regime = "risk-on"
    elif vix is not None and vix < 16:
        regime = "risk-on"
    elif sp500_dist is not None and sp500_dist > 5:
        regime = "risk-on"

    return regime


def find_closest_date(date_str: str, date_dict: dict, max_days: int = 5) -> float | None:
    """Find the closest matching date in a dict within max_days tolerance."""
    target = datetime.strptime(date_str, "%Y-%m-%d")
    for offset in range(max_days + 1):
        for delta in [offset, -offset]:
            check = (target + timedelta(days=delta)).strftime("%Y-%m-%d")
            if check in date_dict:
                return date_dict[check]
    return None


def reconstruct_contexts(limit: int = 0, dry_run: bool = False):
    """
    Main reconstruction loop.
    Iterates through all HistoricalPattern rows missing a HistoricalContext,
    gathers context data, and inserts HistoricalContext rows.
    """
    init_db()

    with get_session() as session:
        # Find patterns that don't have context yet
        existing_context_ids = {
            ctx.pattern_id
            for ctx in session.query(HistoricalContext.pattern_id).all()
        }

        patterns = session.query(HistoricalPattern).all()
        patterns_to_process = [p for p in patterns if p.id not in existing_context_ids]

        if limit > 0:
            patterns_to_process = patterns_to_process[:limit]

        total = len(patterns_to_process)
        log.info("reconstruction_start", total_patterns=len(patterns),
                 missing_contexts=total, limit=limit)

        if total == 0:
            log.info("nothing_to_reconstruct")
            return

        # Pre-fetch bulk data: VIX and S&P 500 for the full date range
        dates = sorted(set(p.event_date for p in patterns_to_process if p.event_date))
        if not dates:
            log.info("no_valid_dates")
            return

        earliest = dates[0]
        latest = dates[-1]

        # Add buffer for lookback
        earliest_dt = datetime.strptime(earliest, "%Y-%m-%d") - timedelta(days=5)
        latest_dt = datetime.strptime(latest, "%Y-%m-%d") + timedelta(days=5)

        log.info("fetching_bulk_data", date_range=f"{earliest} to {latest}")

        vix_data = get_vix_history(earliest_dt.strftime("%Y-%m-%d"), latest_dt.strftime("%Y-%m-%d"))
        log.info("vix_data_fetched", data_points=len(vix_data))

        sp500_data = get_sp500_200ma_distance(earliest_dt.strftime("%Y-%m-%d"), latest_dt.strftime("%Y-%m-%d"))
        log.info("sp500_data_fetched", data_points=len(sp500_data))

        time.sleep(1)  # Rate limit courtesy

        # Process each pattern
        created = 0
        errors = 0
        ticker_cache = {}  # Cache per-ticker requests to avoid duplicates

        for i, pattern in enumerate(patterns_to_process):
            event_date = pattern.event_date
            source_ticker = pattern.source_ticker

            if not event_date:
                errors += 1
                continue

            try:
                # VIX — from pre-fetched data
                vix_level = find_closest_date(event_date, vix_data)

                # S&P 500 distance from 200 MA — from pre-fetched data
                sp500_dist = find_closest_date(event_date, sp500_data)

                # Macro regime — inferred from VIX + S&P 500 distance
                macro_regime = infer_macro_regime(vix_level, sp500_dist)

                # Per-ticker data (momentum, P/E) — rate limited
                cache_key = (source_ticker, event_date)
                if cache_key in ticker_cache:
                    momentum = ticker_cache[cache_key].get("momentum")
                    fwd_pe = ticker_cache[cache_key].get("fwd_pe")
                else:
                    momentum = get_momentum_20d(source_ticker, event_date)
                    time.sleep(0.3)  # Rate limit yfinance

                    fwd_pe = get_fwd_pe_approx(source_ticker, event_date)
                    time.sleep(0.3)

                    ticker_cache[cache_key] = {"momentum": momentum, "fwd_pe": fwd_pe}

                if dry_run:
                    log.info("dry_run_context",
                             pattern_id=pattern.id,
                             ticker=source_ticker,
                             event_date=event_date,
                             vix=vix_level,
                             sp500_dist=sp500_dist,
                             macro=macro_regime,
                             momentum=momentum,
                             fwd_pe=fwd_pe)
                else:
                    ctx = HistoricalContext(
                        pattern_id=pattern.id,
                        macro_regime=macro_regime or "",
                        vix_level=vix_level,
                        fwd_pe_ratio=fwd_pe,
                        momentum_20d=momentum,
                        sp500_distance_200ma=sp500_dist,
                    )
                    session.add(ctx)
                    session.flush()

                created += 1

                if (i + 1) % 10 == 0:
                    if not dry_run:
                        session.commit()
                    log.info("reconstruction_progress",
                             processed=i + 1,
                             total=total,
                             created=created,
                             errors=errors)

            except Exception as e:
                log.error("context_reconstruction_failed",
                          pattern_id=pattern.id,
                          ticker=source_ticker,
                          event_date=event_date,
                          error=str(e))
                errors += 1
                continue

        # Final commit
        if not dry_run:
            session.commit()

        log.info("reconstruction_complete",
                 total_processed=total,
                 contexts_created=created,
                 errors=errors,
                 dry_run=dry_run)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reconstruct historical contexts for pattern similarity scoring")
    parser.add_argument("--limit", type=int, default=0, help="Max patterns to process (0 = all)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without writing to DB")
    args = parser.parse_args()

    reconstruct_contexts(limit=args.limit, dry_run=args.dry_run)
