"""
Pattern data adapter — FMP historical events + yfinance forward returns.
Provides: earnings surprises, insider trading, analyst upgrades/downgrades.
Caches aggressively to SQLite since historical data doesn't change.
"""

import json
import httpx
import yfinance as yf
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from database.db import get_session
from database.models import HistoricalPattern, Ticker
from utils.rate_limiter import rate_limiter
from utils.logger import get_logger

log = get_logger("pattern_data")

FMP_BASE = "https://financialmodelingprep.com/stable"


class PatternDataAdapter:
    def __init__(self, fmp_key: str):
        self.fmp_key = fmp_key

    # ── Earnings data (yfinance — unlimited, no API key needed) ─────

    def get_earnings_surprises(self, ticker: str) -> list[dict]:
        """
        Fetch historical earnings surprises via yfinance earnings_dates.
        Falls back to FMP if yfinance fails.
        Returns list of dicts with event_date, actual_eps, estimated_eps, surprise_pct.
        """
        results = self._yfinance_earnings(ticker)
        if results:
            return results
        # Fallback to FMP (may not work on free plan)
        return self._fmp_earnings(ticker)

    def _yfinance_earnings(self, ticker: str) -> list[dict]:
        """Get earnings surprise data from yfinance."""
        try:
            stock = yf.Ticker(ticker)
            ed = stock.earnings_dates
            if ed is None or ed.empty:
                return []

            # Filter to past earnings only
            now = pd.Timestamp.now(tz=ed.index.tz) if ed.index.tz else pd.Timestamp.now()
            past = ed[ed.index <= now]
            if past.empty:
                return []

            results = []
            for idx, row in past.iterrows():
                reported = row.get("Reported EPS")
                estimated = row.get("EPS Estimate")
                surprise = row.get("Surprise(%)")

                if pd.isna(reported) or pd.isna(estimated):
                    continue

                # Compute surprise if not provided
                if pd.isna(surprise) and estimated != 0:
                    surprise = ((reported - estimated) / abs(estimated)) * 100

                event_date = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]

                results.append({
                    "event_date": event_date,
                    "actual_eps": float(reported),
                    "estimated_eps": float(estimated),
                    "surprise_pct": round(float(surprise), 2) if not pd.isna(surprise) else 0.0,
                })

            log.info("yfinance_earnings_loaded", ticker=ticker, count=len(results))
            return results

        except Exception as e:
            log.warning("yfinance_earnings_failed", ticker=ticker, error=str(e))
            return []

    def _fmp_earnings(self, ticker: str) -> list[dict]:
        """Fallback: fetch earnings surprises from FMP (may require paid plan)."""
        data = self._fmp_request("/earnings-surprises", {"symbol": ticker})
        if not data:
            return []
        results = []
        for item in data:
            actual = item.get("actualEarningResult", 0) or 0
            estimated = item.get("estimatedEarning", 0) or 0
            if estimated != 0:
                surprise_pct = ((actual - estimated) / abs(estimated)) * 100
            else:
                surprise_pct = 0
            results.append({
                "event_date": item.get("date", ""),
                "actual_eps": actual,
                "estimated_eps": estimated,
                "surprise_pct": round(surprise_pct, 2),
            })
        return results

    def get_insider_trading(self, ticker: str) -> list[dict]:
        """Fetch insider trading history from FMP."""
        data = self._fmp_request("/insider-trading", {"symbol": ticker, "limit": 100})
        if not data:
            return []
        results = []
        for item in data:
            if item.get("transactionType") in ("P-Purchase", "P - Purchase"):
                results.append({
                    "event_date": (item.get("transactionDate") or item.get("filingDate", ""))[:10],
                    "insider_name": item.get("reportingName", ""),
                    "shares": item.get("securitiesTransacted", 0),
                    "price": item.get("price", 0),
                    "transaction_type": "purchase",
                })
        return results

    def get_upgrades_downgrades(self, ticker: str) -> list[dict]:
        """Fetch analyst upgrades/downgrades from FMP."""
        data = self._fmp_request("/upgrades-downgrades", {"symbol": ticker})
        if not data:
            return []
        results = []
        for item in data:
            results.append({
                "event_date": (item.get("publishedDate") or item.get("date", ""))[:10],
                "analyst": item.get("gradingCompany", ""),
                "action": item.get("action", ""),
                "new_grade": item.get("newGrade", ""),
                "previous_grade": item.get("previousGrade", ""),
            })
        return results

    # ── Forward return computation ───────────────────────────────────

    def compute_forward_returns(self, ticker: str, event_date: str) -> dict | None:
        """
        Compute forward returns at T+5, T+10, T+15, T+20 from an event date.
        Uses yfinance for price data.
        Returns dict with returns and drawdown info, or None if data unavailable.
        """
        try:
            event_dt = pd.Timestamp(event_date)
            # Fetch enough data: event_date minus 5 days to event_date plus 30 calendar days
            start = (event_dt - timedelta(days=5)).strftime("%Y-%m-%d")
            end = (event_dt + timedelta(days=45)).strftime("%Y-%m-%d")

            stock = yf.Ticker(ticker)
            hist = stock.history(start=start, end=end)

            if hist.empty or len(hist) < 2:
                return None

            # Find the closest trading day to or after the event date
            hist.index = hist.index.tz_localize(None)
            valid_dates = hist.index[hist.index >= event_dt]
            if valid_dates.empty:
                # Event date is after all available data
                return None
            event_idx = hist.index.get_loc(valid_dates[0])
            event_close = float(hist.iloc[event_idx]["Close"])

            if event_close <= 0:
                return None

            result = {"event_close": event_close}

            # Compute returns at T+5, T+10, T+15, T+20
            for horizon, label in [(5, "return_t5"), (10, "return_t10"), (15, "return_t15"), (20, "return_t20")]:
                target_idx = event_idx + horizon
                if target_idx < len(hist):
                    target_close = float(hist.iloc[target_idx]["Close"])
                    result[label] = round(((target_close - event_close) / event_close) * 100, 2)
                else:
                    result[label] = None

            # Compute max drawdown from event close over the T+20 window
            end_idx = min(event_idx + 21, len(hist))
            window = hist.iloc[event_idx:end_idx]
            if len(window) > 1:
                lows = window["Low"].values
                max_dd = float(((min(lows) - event_close) / event_close) * 100)
                dd_day = int(np.argmin(lows))
                result["max_drawdown"] = round(max_dd, 2)
                result["max_drawdown_day"] = dd_day
            else:
                result["max_drawdown"] = 0.0
                result["max_drawdown_day"] = 0

            return result

        except Exception as e:
            log.warning("forward_returns_failed", ticker=ticker, event_date=event_date, error=str(e))
            return None

    # ── Cache layer (SQLite) ─────────────────────────────────────────

    def get_cached_patterns(self, setup_type: str, tickers: list[str]) -> list[dict]:
        """Retrieve cached historical patterns from the database."""
        try:
            with get_session() as session:
                patterns = (
                    session.query(HistoricalPattern)
                    .filter(
                        HistoricalPattern.setup_type == setup_type,
                        HistoricalPattern.source_ticker.in_(tickers),
                    )
                    .all()
                )
                return [
                    {
                        "setup_type": p.setup_type,
                        "event_date": p.event_date,
                        "source_ticker": p.source_ticker,
                        "is_peer": p.is_peer,
                        "beat_magnitude": p.beat_magnitude,
                        "return_t5": p.return_t5,
                        "return_t10": p.return_t10,
                        "return_t15": p.return_t15,
                        "return_t20": p.return_t20,
                        "max_drawdown": p.max_drawdown,
                        "max_drawdown_day": p.max_drawdown_day,
                    }
                    for p in patterns
                ]
        except Exception as e:
            log.error("cache_read_failed", error=str(e))
            return []

    def cache_pattern(self, setup_type: str, source_ticker: str, event_date: str,
                      is_peer: bool, beat_magnitude: float | None,
                      returns: dict, target_ticker: str | None = None):
        """Cache a computed pattern to the database. Upserts on unique constraint."""
        try:
            with get_session() as session:
                # Check for existing
                existing = (
                    session.query(HistoricalPattern)
                    .filter_by(
                        setup_type=setup_type,
                        source_ticker=source_ticker,
                        event_date=event_date,
                    )
                    .first()
                )
                if existing:
                    return  # Already cached

                # Find ticker_id if target_ticker is provided
                ticker_id = None
                if target_ticker:
                    ticker_obj = session.query(Ticker).filter_by(symbol=target_ticker).first()
                    if ticker_obj:
                        ticker_id = ticker_obj.id

                pattern = HistoricalPattern(
                    ticker_id=ticker_id,
                    setup_type=setup_type,
                    event_date=event_date,
                    source_ticker=source_ticker,
                    is_peer=is_peer,
                    beat_magnitude=beat_magnitude,
                    return_t5=returns.get("return_t5"),
                    return_t10=returns.get("return_t10"),
                    return_t15=returns.get("return_t15"),
                    return_t20=returns.get("return_t20"),
                    max_drawdown=returns.get("max_drawdown"),
                    max_drawdown_day=returns.get("max_drawdown_day"),
                    raw_data=json.dumps(returns),
                )
                session.add(pattern)
        except Exception as e:
            log.warning("cache_write_failed", source_ticker=source_ticker, error=str(e))

    # ── Summary statistics ───────────────────────────────────────────

    @staticmethod
    def compute_summary_stats(instances: list[dict]) -> dict:
        """Compute summary statistics across all historical instances."""
        if not instances:
            return {}

        same_ticker = [i for i in instances if not i.get("is_peer")]
        peers = [i for i in instances if i.get("is_peer")]

        def _stats_for(items, horizon_key):
            vals = [i[horizon_key] for i in items if i.get(horizon_key) is not None]
            if not vals:
                return {}
            winners = [v for v in vals if v > 0]
            losers = [v for v in vals if v <= 0]
            return {
                "median_return": round(float(np.median(vals)), 2),
                "mean_return": round(float(np.mean(vals)), 2),
                "win_rate": round(len(winners) / len(vals), 3),
                "avg_winner": round(float(np.mean(winners)), 2) if winners else 0.0,
                "avg_loser": round(float(np.mean(losers)), 2) if losers else 0.0,
                "std_dev": round(float(np.std(vals)), 2) if len(vals) > 1 else 0.0,
                "count": len(vals),
            }

        # Compute stats for each horizon
        combined_stats = {}
        for horizon in ("return_t5", "return_t10", "return_t15", "return_t20"):
            label = horizon.replace("return_", "")  # t5, t10, etc.
            combined_stats[label] = _stats_for(instances, horizon)

        # Drawdown stats
        drawdowns = [i["max_drawdown"] for i in instances if i.get("max_drawdown") is not None]

        return {
            "same_ticker_count": len(same_ticker),
            "peer_count": len(peers),
            "total_instances": len(instances),
            **{f"{k}_{horizon}": v for horizon, stats in combined_stats.items() for k, v in stats.items()},
            "max_drawdown_median": round(float(np.median(drawdowns)), 2) if drawdowns else 0.0,
            "max_drawdown_worst": round(float(min(drawdowns)), 2) if drawdowns else 0.0,
            # Primary T+10 stats at top level for easy access
            "win_rate_t10": combined_stats.get("t10", {}).get("win_rate", 0.5),
            "median_return_t10": combined_stats.get("t10", {}).get("median_return", 0.0),
            "mean_return_t10": combined_stats.get("t10", {}).get("mean_return", 0.0),
            "avg_winner_t10": combined_stats.get("t10", {}).get("avg_winner", 0.0),
            "avg_loser_t10": combined_stats.get("t10", {}).get("avg_loser", 0.0),
            "std_dev_t10": combined_stats.get("t10", {}).get("std_dev", 0.0),
        }

    # ── Private helpers ──────────────────────────────────────────────

    def _fmp_request(self, endpoint: str, params: dict = None) -> list | dict | None:
        """Make a rate-limited request to FMP API."""
        if not self.fmp_key:
            return None
        rate_limiter.acquire("fmp")
        try:
            url = f"{FMP_BASE}{endpoint}"
            p = {"apikey": self.fmp_key}
            if params:
                p.update(params)
            resp = httpx.get(url, params=p, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.error("fmp_request_failed", endpoint=endpoint, error=str(e))
            return None
