"""
Tier 1 Structured Scanner — rules-based screening of ~2,000 US equities.

Uses free APIs only (yfinance batch download, Finnhub earnings calendar).
No AI cost. Runs before each scheduled scan to identify tickers with catalysts.

Catalyst checks:
- Price move >3% on >1.5x average volume
- 52-week high/low proximity (within 3%)
- Earnings within 5 trading days (Finnhub)
- Volume spike >2x 20-day average (no price move required)
- Gap up/down >2% from previous close
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import yfinance as yf
import pandas as pd

from utils.logger import get_logger

log = get_logger("structured_scanner")

# ── Thresholds ──
PRICE_MOVE_PCT = 3.0          # Minimum % move to flag
VOLUME_MULTIPLE = 1.5         # Volume vs 20-day average for price move flag
VOLUME_SPIKE_MULTIPLE = 2.0   # Pure volume spike (no price move needed)
HIGH_LOW_PROXIMITY_PCT = 3.0  # Within 3% of 52-week high/low
GAP_PCT = 2.0                 # Gap up/down from previous close
EARNINGS_DAYS_AHEAD = 5       # Flag tickers with earnings within N days
BATCH_SIZE = 100              # yfinance download batch size


@dataclass
class FlaggedTicker:
    """A ticker flagged by the structured scanner with catalyst metadata."""
    symbol: str
    catalysts: list[str]          # e.g. ["price_move", "volume_spike"]
    price: float = 0.0
    change_pct: float = 0.0
    volume_ratio: float = 0.0     # Today's volume / 20-day avg
    distance_to_52w_high: float = 0.0  # % below 52-week high
    distance_to_52w_low: float = 0.0   # % above 52-week low
    earnings_date: str = ""
    gap_pct: float = 0.0
    sector: str = ""


@dataclass
class ScanResult:
    """Result of a structured scan."""
    flagged: list[FlaggedTicker] = field(default_factory=list)
    total_scanned: int = 0
    scan_duration_s: float = 0.0
    errors: int = 0
    earnings_tickers: set = field(default_factory=set)


class StructuredScanner:
    """
    Rules-based scanner that checks ~2,000 tickers using free APIs.
    Designed to run in <60 seconds using yfinance batch downloads.
    """

    def __init__(self, settings=None, news_data=None):
        self.settings = settings
        self.news_data = news_data  # NewsDataAdapter for Finnhub earnings

    def scan(self, universe: dict[str, str]) -> ScanResult:
        """
        Scan the full universe. Returns flagged tickers with catalyst metadata.

        Args:
            universe: dict of {symbol: sector}

        Returns:
            ScanResult with flagged tickers
        """
        start = time.time()
        symbols = list(universe.keys())
        log.info("structured_scan_start", total_tickers=len(symbols))

        result = ScanResult(total_scanned=len(symbols))

        # 1. Get earnings calendar (Finnhub) — one API call
        earnings_map = self._get_earnings_map()
        result.earnings_tickers = set(earnings_map.keys())

        # 2. Batch download price data via yfinance
        price_data = self._batch_download(symbols)

        # 3. Apply rules to each ticker
        for symbol in symbols:
            try:
                sector = universe.get(symbol, "Unknown")
                flagged = self._check_rules(symbol, price_data, earnings_map, sector)
                if flagged:
                    result.flagged.append(flagged)
            except Exception as e:
                result.errors += 1
                # Don't log per-ticker — too noisy for 2,000 tickers

        result.scan_duration_s = round(time.time() - start, 1)
        log.info(
            "structured_scan_complete",
            flagged=len(result.flagged),
            total=result.total_scanned,
            duration_s=result.scan_duration_s,
            errors=result.errors,
        )
        return result

    def _batch_download(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        """
        Download price data for all symbols using yfinance batch download.
        Uses 3-month history for 20-day volume averages and 52-week proximity.
        """
        all_data = {}

        # Process in batches to avoid timeouts
        for i in range(0, len(symbols), BATCH_SIZE):
            batch = symbols[i:i + BATCH_SIZE]
            try:
                # Download 3 months of daily data for the batch
                data = yf.download(
                    batch,
                    period="3mo",
                    group_by="ticker",
                    auto_adjust=True,
                    threads=True,
                    progress=False,
                )
                if data.empty:
                    continue

                # For single ticker, yfinance doesn't nest by ticker
                if len(batch) == 1:
                    all_data[batch[0]] = data
                else:
                    for symbol in batch:
                        try:
                            ticker_data = data[symbol] if symbol in data.columns.get_level_values(0) else pd.DataFrame()
                            if not ticker_data.empty:
                                all_data[symbol] = ticker_data
                        except (KeyError, TypeError):
                            continue
            except Exception as e:
                log.warning("batch_download_failed", batch_start=i, error=str(e))
                continue

        log.info("batch_download_complete", tickers_with_data=len(all_data))
        return all_data

    def _check_rules(
        self, symbol: str, price_data: dict, earnings_map: dict, sector: str,
    ) -> FlaggedTicker | None:
        """
        Apply all rules to a single ticker. Returns FlaggedTicker if any catalyst found.
        """
        df = price_data.get(symbol)
        if df is None or len(df) < 22:  # Need at least 22 days for 20-day avg
            return None

        catalysts = []
        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else latest

        close = float(latest["Close"])
        prev_close = float(prev["Close"])
        volume = float(latest["Volume"])
        open_price = float(latest["Open"])

        if close <= 0 or prev_close <= 0 or volume <= 0:
            return None

        # 20-day average volume
        recent_volumes = df["Volume"].tail(21).iloc[:-1]  # Last 20 days excluding today
        avg_volume = float(recent_volumes.mean()) if len(recent_volumes) >= 10 else 0
        volume_ratio = volume / avg_volume if avg_volume > 0 else 0

        # Daily change
        change_pct = (close - prev_close) / prev_close * 100

        # 52-week high/low (from available data — 3 months minimum)
        high_52w = float(df["High"].max())
        low_52w = float(df["Low"].min())
        dist_to_high = (high_52w - close) / high_52w * 100 if high_52w > 0 else 999
        dist_to_low = (close - low_52w) / low_52w * 100 if low_52w > 0 else 999

        # Gap from previous close
        gap_pct = (open_price - prev_close) / prev_close * 100

        # ── Rule 1: Price move >3% on >1.5x volume ──
        if abs(change_pct) >= PRICE_MOVE_PCT and volume_ratio >= VOLUME_MULTIPLE:
            catalysts.append("price_move_volume")

        # ── Rule 2: Pure volume spike >2x (any price move) ──
        if volume_ratio >= VOLUME_SPIKE_MULTIPLE and "price_move_volume" not in catalysts:
            catalysts.append("volume_spike")

        # ── Rule 3: Near 52-week high (within 3%) ──
        if dist_to_high <= HIGH_LOW_PROXIMITY_PCT:
            catalysts.append("near_52w_high")

        # ── Rule 4: Near 52-week low (within 3%) ──
        if dist_to_low <= HIGH_LOW_PROXIMITY_PCT:
            catalysts.append("near_52w_low")

        # ── Rule 5: Gap >2% ──
        if abs(gap_pct) >= GAP_PCT:
            catalysts.append("gap_up" if gap_pct > 0 else "gap_down")

        # ── Rule 6: Earnings within 5 days ──
        earnings_date = earnings_map.get(symbol, "")
        if earnings_date:
            catalysts.append("earnings_soon")

        if not catalysts:
            return None

        return FlaggedTicker(
            symbol=symbol,
            catalysts=catalysts,
            price=round(close, 2),
            change_pct=round(change_pct, 2),
            volume_ratio=round(volume_ratio, 2),
            distance_to_52w_high=round(dist_to_high, 2),
            distance_to_52w_low=round(dist_to_low, 2),
            earnings_date=earnings_date,
            gap_pct=round(gap_pct, 2),
            sector=sector,
        )

    def _get_earnings_map(self) -> dict[str, str]:
        """
        Get tickers with earnings in the next N days.
        Returns {symbol: date_string}.
        """
        if not self.news_data:
            return {}

        try:
            earnings = self.news_data.get_earnings_calendar(days_ahead=EARNINGS_DAYS_AHEAD)
            return {
                e["symbol"]: e["date"]
                for e in earnings
                if e.get("symbol") and e.get("date")
            }
        except Exception as e:
            log.warning("earnings_calendar_failed", error=str(e))
            return {}
