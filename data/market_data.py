"""
Market data adapter — Yahoo Finance (yfinance).
Provides: OHLCV, current price, VIX, sector ETF momentum, ATR, moving averages.
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, date
from database.db import get_session
from database.models import PriceData, Ticker
from utils.logger import get_logger

log = get_logger("market_data")


class MarketDataAdapter:

    def get_daily_bars(self, ticker: str, days: int = 252) -> pd.DataFrame:
        """Fetch daily OHLCV bars. Caches to DB."""
        try:
            end = datetime.now()
            start = end - timedelta(days=int(days * 1.5))  # extra buffer for weekends
            stock = yf.Ticker(ticker)
            df = stock.history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
            if df.empty:
                log.warning("no_price_data", ticker=ticker)
                return pd.DataFrame()
            df = df.tail(days)
            self._cache_prices(ticker, df)
            return df
        except Exception as e:
            log.error("price_fetch_failed", ticker=ticker, error=str(e))
            return pd.DataFrame()

    def get_current_price(self, ticker: str) -> dict:
        """Get current/latest price data."""
        try:
            stock = yf.Ticker(ticker)
            info = stock.fast_info
            hist = stock.history(period="2d")
            if hist.empty:
                return {}
            latest = hist.iloc[-1]
            prev_close = hist.iloc[-2]["Close"] if len(hist) > 1 else latest["Close"]
            return {
                "price": round(float(latest["Close"]), 2),
                "open": round(float(latest["Open"]), 2),
                "high": round(float(latest["High"]), 2),
                "low": round(float(latest["Low"]), 2),
                "volume": int(latest["Volume"]),
                "prev_close": round(float(prev_close), 2),
                "change_pct": round((float(latest["Close"]) - float(prev_close)) / float(prev_close) * 100, 2),
                "market_cap": getattr(info, "market_cap", 0),
            }
        except Exception as e:
            log.error("current_price_failed", ticker=ticker, error=str(e))
            return {}

    def get_vix(self) -> dict:
        """Get VIX level and short-term term structure."""
        try:
            vix = yf.Ticker("^VIX")
            hist = vix.history(period="3mo")
            if hist.empty:
                return {"level": 20, "trend": "stable"}
            current = float(hist.iloc[-1]["Close"])
            ma_20 = float(hist["Close"].tail(20).mean())
            month_ago = float(hist["Close"].iloc[-22]) if len(hist) >= 22 else current
            return {
                "level": round(current, 2),
                "ma_20": round(ma_20, 2),
                "month_ago": round(month_ago, 2),
                "trend": "rising" if current > ma_20 else "declining",
                "elevated": current > 25,
            }
        except Exception as e:
            log.error("vix_fetch_failed", error=str(e))
            return {"level": 20, "trend": "stable", "elevated": False}

    def get_sector_etf_momentum(self) -> dict:
        """Get 30-day momentum for sector ETFs."""
        from config.tickers import SECTOR_ETFS
        results = {}
        for etf, sector in SECTOR_ETFS.items():
            try:
                stock = yf.Ticker(etf)
                hist = stock.history(period="2mo")
                if len(hist) < 22:
                    continue
                current = float(hist.iloc[-1]["Close"])
                month_ago = float(hist.iloc[-22]["Close"])
                momentum = (current - month_ago) / month_ago * 100
                results[etf] = {
                    "sector": sector,
                    "momentum_30d": round(momentum, 2),
                    "price": round(current, 2),
                }
            except Exception:
                continue
        return results

    def get_moving_averages(self, ticker: str) -> dict:
        """Get 50-day and 200-day moving averages."""
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period="1y")
            if len(hist) < 200:
                return {}
            current = float(hist.iloc[-1]["Close"])
            ma_50 = float(hist["Close"].tail(50).mean())
            ma_200 = float(hist["Close"].tail(200).mean())
            return {
                "price": round(current, 2),
                "ma_50": round(ma_50, 2),
                "ma_200": round(ma_200, 2),
                "above_50": current > ma_50,
                "above_200": current > ma_200,
                "golden_cross": ma_50 > ma_200,
            }
        except Exception as e:
            log.error("ma_fetch_failed", ticker=ticker, error=str(e))
            return {}

    def get_atr(self, ticker: str, period: int = 14) -> float:
        """Calculate Average True Range for position sizing."""
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period="3mo")
            if len(hist) < period + 1:
                return 0.0
            high = hist["High"]
            low = hist["Low"]
            close = hist["Close"].shift(1)
            tr1 = high - low
            tr2 = abs(high - close)
            tr3 = abs(low - close)
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = float(tr.tail(period).mean())
            return round(atr, 2)
        except Exception as e:
            log.error("atr_failed", ticker=ticker, error=str(e))
            return 0.0

    def get_sp500_data(self) -> dict:
        """Get S&P 500 index data for macro regime."""
        try:
            spy = yf.Ticker("^GSPC")
            hist = spy.history(period="1y")
            if len(hist) < 200:
                return {}
            current = float(hist.iloc[-1]["Close"])
            ma_50 = float(hist["Close"].tail(50).mean())
            ma_200 = float(hist["Close"].tail(200).mean())
            return {
                "price": round(current, 2),
                "ma_50": round(ma_50, 2),
                "ma_200": round(ma_200, 2),
                "above_200": current > ma_200,
            }
        except Exception:
            return {}

    def get_russell2000_data(self) -> dict:
        """Get Russell 2000 data for macro regime."""
        try:
            rut = yf.Ticker("^RUT")
            hist = rut.history(period="1y")
            if len(hist) < 200:
                return {}
            current = float(hist.iloc[-1]["Close"])
            ma_200 = float(hist["Close"].tail(200).mean())
            return {
                "price": round(current, 2),
                "ma_200": round(ma_200, 2),
                "above_200": current > ma_200,
            }
        except Exception:
            return {}

    def _cache_prices(self, ticker_symbol: str, df: pd.DataFrame):
        """Cache price data to the database."""
        try:
            with get_session() as session:
                ticker_obj = session.query(Ticker).filter_by(symbol=ticker_symbol).first()
                if not ticker_obj:
                    return
                for idx, row in df.iterrows():
                    row_date = idx.date() if hasattr(idx, "date") else idx
                    existing = (
                        session.query(PriceData)
                        .filter_by(ticker_id=ticker_obj.id, date=row_date)
                        .first()
                    )
                    if existing:
                        continue
                    price = PriceData(
                        ticker_id=ticker_obj.id,
                        date=row_date,
                        open=round(float(row.get("Open", 0)), 4),
                        high=round(float(row.get("High", 0)), 4),
                        low=round(float(row.get("Low", 0)), 4),
                        close=round(float(row.get("Close", 0)), 4),
                        volume=float(row.get("Volume", 0)),
                        adj_close=round(float(row.get("Close", 0)), 4),
                    )
                    session.add(price)
        except Exception as e:
            log.error("cache_prices_failed", ticker=ticker_symbol, error=str(e))
