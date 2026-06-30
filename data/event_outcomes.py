"""Historical event outcome and point-in-time context computation."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import numpy as np
import pandas as pd
import yfinance as yf

from database.models import EventContext, EventOutcome, HistoricalEvent, PatternProviderCache
from utils.logger import get_logger
from utils.rate_limiter import rate_limiter
from utils.redaction import redact_payload, redact_text

log = get_logger("event_outcomes")

FMP_BASE = "https://financialmodelingprep.com/stable"
HORIZONS = (1, 3, 5, 10, 20, 60)
SECTOR_ETFS = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Financials": "XLF",
    "Healthcare": "XLV",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Industrials": "XLI",
    "Energy": "XLE",
    "Communication Services": "XLC",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Basic Materials": "XLB",
    "Materials": "XLB",
}


class HistoricalMarketCapUnavailable(RuntimeError):
    """Raised when PIT market-cap data cannot be sourced from FMP."""


@dataclass
class PriceBar:
    date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None


class PriceHistoryCache:
    """FMP-default historical price cache backed by PatternProviderCache."""

    def __init__(self, settings=None):
        self.settings = settings
        self.fmp_key = getattr(settings, "fmp_api_key", "") if settings else ""
        self.source = getattr(settings, "pattern_price_source", "fmp") if settings else "fmp"

    def get_bars(self, ticker: str, start: date, end: date, session=None) -> list[PriceBar]:
        ticker = ticker.upper()
        cache_key = f"price:{self.source}:{ticker}:{start.isoformat()}:{end.isoformat()}"
        cached = self._read_cache(cache_key, session)
        if cached is not None:
            return self._parse_bars(cached)

        if self.source == "yfinance" or not self.fmp_key:
            payload = self._fetch_yfinance(ticker, start, end)
        else:
            payload = self._fetch_fmp(ticker, start, end)
            if not payload:
                payload = self._fetch_yfinance(ticker, start, end)

        self._write_cache(cache_key, "price_history", ticker, {"start": str(start), "end": str(end)}, payload, session)
        return self._parse_bars(payload)

    def _fetch_fmp(self, ticker: str, start: date, end: date) -> list[dict]:
        data = self._fmp_request(
            f"/historical-price-eod/full",
            {"symbol": ticker, "from": start.isoformat(), "to": end.isoformat()},
        )
        rows = []
        if isinstance(data, dict):
            rows = data.get("historical") or data.get("historicalStockList") or []
        elif isinstance(data, list):
            rows = data
        return rows

    def _fetch_yfinance(self, ticker: str, start: date, end: date) -> list[dict]:
        try:
            hist = yf.Ticker(ticker).history(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat())
            if hist is None or hist.empty:
                return []
            hist.index = hist.index.tz_localize(None)
            rows = []
            for idx, row in hist.iterrows():
                rows.append(
                    {
                        "date": idx.strftime("%Y-%m-%d"),
                        "open": _num(row.get("Open")),
                        "high": _num(row.get("High")),
                        "low": _num(row.get("Low")),
                        "close": _num(row.get("Close")),
                        "volume": _num(row.get("Volume")),
                    }
                )
            return rows
        except Exception as exc:
            log.warning("price_yfinance_failed", ticker=ticker, error=str(exc))
            return []

    def _parse_bars(self, payload: Any) -> list[PriceBar]:
        rows = payload if isinstance(payload, list) else []
        bars = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            parsed_date = _parse_date(row.get("date") or row.get("tradingDay"))
            close = _num(row.get("close") or row.get("adjClose") or row.get("price"))
            if not parsed_date or close is None:
                continue
            bars.append(
                PriceBar(
                    date=parsed_date,
                    open=_num(row.get("open")),
                    high=_num(row.get("high")) or close,
                    low=_num(row.get("low")) or close,
                    close=close,
                    volume=_num(row.get("volume")),
                )
            )
        return sorted(bars, key=lambda b: b.date)

    def _read_cache(self, cache_key: str, session=None) -> Any | None:
        if session is None:
            return None
        row = (
            session.query(PatternProviderCache)
            .filter_by(cache_key=cache_key)
            .filter((PatternProviderCache.expires_at.is_(None)) | (PatternProviderCache.expires_at > datetime.utcnow()))
            .first()
        )
        if not row:
            return None
        try:
            return json.loads(row.result_json or "[]")
        except json.JSONDecodeError:
            return None

    def _write_cache(self, cache_key: str, provider: str, query: str, filters: dict, payload: Any, session=None) -> None:
        if session is None:
            return
        expires = datetime.utcnow() + timedelta(days=30)
        clean = json.dumps(redact_payload(payload))
        row = session.query(PatternProviderCache).filter_by(cache_key=cache_key).first()
        if row:
            row.result_json = clean
            row.updated_at = datetime.utcnow()
            row.expires_at = expires
        else:
            session.add(
                PatternProviderCache(
                    cache_key=cache_key,
                    provider=provider,
                    query=query,
                    filters_json=json.dumps(filters),
                    result_json=clean,
                    expires_at=expires,
                )
            )

    def _fmp_request(self, endpoint: str, params: dict | None = None) -> list | dict | None:
        if not self.fmp_key:
            return None
        rate_limiter.acquire("fmp")
        try:
            query = {"apikey": self.fmp_key}
            if params:
                query.update(params)
            response = httpx.get(f"{FMP_BASE}{endpoint}", params=query, timeout=25)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            log.warning("price_fmp_request_failed", endpoint=endpoint, error=redact_text(str(exc)))
            return None


class EventOutcomeEngine:
    """Compute forward returns and PIT context for HistoricalEvent rows."""

    def __init__(self, settings=None, price_cache: PriceHistoryCache | None = None):
        self.settings = settings
        self.fmp_key = getattr(settings, "fmp_api_key", "") if settings else ""
        self.price_cache = price_cache or PriceHistoryCache(settings)

    def compute_outcome(self, event: HistoricalEvent, session=None) -> EventOutcome:
        event_date = _coerce_date(event.event_date)
        start = event_date - timedelta(days=10)
        end = event_date + timedelta(days=95)
        bars = self.price_cache.get_bars(event.ticker, start, end, session=session)
        anchor_idx = _first_bar_idx_on_or_after(bars, event_date)
        existing = session.query(EventOutcome).filter_by(event_id=event.id).first() if session else None

        if anchor_idx is None:
            outcome = existing or EventOutcome(event_id=event.id, ticker=event.ticker)
            outcome.status = "price_error"
            outcome.computed_at = datetime.utcnow()
            if session and not existing:
                session.add(outcome)
            return outcome

        anchor = bars[anchor_idx]
        anchor_price = anchor.close or 0
        outcome = existing or EventOutcome(event_id=event.id, ticker=event.ticker)
        outcome.anchor_price = anchor_price
        outcome.anchor_trade_date = anchor.date
        matured = []

        if anchor_price > 0:
            for horizon in HORIZONS:
                target_idx = anchor_idx + horizon
                value = None
                if target_idx < len(bars) and bars[target_idx].close is not None:
                    value = round(((bars[target_idx].close - anchor_price) / anchor_price) * 100, 2)
                    matured.append(f"t{horizon}")
                setattr(outcome, f"return_t{horizon}", value)

        outcome.gap_pct = _gap_pct(bars, anchor_idx)
        outcome.volume_ratio_t1 = _volume_ratio(bars, anchor_idx)
        outcome.max_drawdown_t20, outcome.max_drawdown_day = _max_drawdown(bars, anchor_idx, anchor_price, 20)
        outcome.matured_horizons_json = json.dumps(matured)
        outcome.status = (
            "complete"
            if "t20" in matured
            else "partial"
            if matured
            else "insufficient_forward_returns"
        )
        outcome.computed_at = datetime.utcnow()
        if session and not existing:
            session.add(outcome)
        return outcome

    def compute_context(self, event: HistoricalEvent, session=None, sector: str = "") -> EventContext:
        event_date = _coerce_date(event.event_date)
        existing = session.query(EventContext).filter_by(event_id=event.id).first() if session else None
        context = existing or EventContext(event_id=event.id)
        raw: dict[str, Any] = {}

        ticker_bars = self.price_cache.get_bars(event.ticker, event_date - timedelta(days=260), event_date + timedelta(days=5), session=session)
        spy_bars = self.price_cache.get_bars("SPY", event_date - timedelta(days=320), event_date + timedelta(days=5), session=session)
        vix_bars = self.price_cache.get_bars("^VIX", event_date - timedelta(days=10), event_date + timedelta(days=5), session=session)
        sector_symbol = SECTOR_ETFS.get(sector, "")
        sector_bars = self.price_cache.get_bars(sector_symbol, event_date - timedelta(days=45), event_date + timedelta(days=5), session=session) if sector_symbol else []

        context.ticker_momentum_20d = _momentum(ticker_bars, event_date, 20)
        context.ticker_volatility_20d = _volatility(ticker_bars, event_date, 20)
        context.sp500_distance_200ma = _distance_200ma(spy_bars, event_date)
        context.sector_momentum_20d = _momentum(sector_bars, event_date, 20) if sector_bars else None
        context.vix_level = _close_on_or_before(vix_bars, event_date)
        context.macro_regime = _macro_regime(context.vix_level, context.sp500_distance_200ma)

        valuation = self._pit_valuation(event.ticker, event_date)
        raw["valuation"] = valuation.get("raw", {})
        context.market_cap = valuation.get("market_cap")
        context.trailing_pe_ratio = valuation.get("trailing_pe_ratio")
        context.ev_sales = valuation.get("ev_sales")
        context.valuation_source_filing_date = valuation.get("valuation_source_filing_date")

        priced_fields = [
            context.vix_level,
            context.sp500_distance_200ma,
            context.ticker_momentum_20d,
            context.ticker_volatility_20d,
        ]
        valuation_fields = [context.market_cap, context.trailing_pe_ratio, context.ev_sales]
        if all(v is not None for v in priced_fields + valuation_fields):
            context.pit_quality = "full"
        elif any(v is not None for v in valuation_fields) and any(v is not None for v in priced_fields):
            context.pit_quality = "partial"
        elif any(v is not None for v in priced_fields):
            context.pit_quality = "price_only"
        else:
            context.pit_quality = "unavailable"

        context.raw_json = json.dumps(redact_payload(raw))
        context.computed_at = datetime.utcnow()
        if session and not existing:
            session.add(context)
        return context

    def _pit_valuation(self, ticker: str, event_date: date) -> dict:
        market_cap, raw_market_cap = self._historical_market_cap(ticker, event_date)
        if market_cap is None:
            raise HistoricalMarketCapUnavailable(
                f"FMP historical-market-capitalization unavailable for {ticker} at {event_date}; "
                "refusing to use current-as-of market cap."
            )

        income_rows = self._fmp_request(
            "/income-statement",
            {"symbol": ticker, "period": "quarter", "limit": 40},
        ) or []
        balance_rows = self._fmp_request(
            "/balance-sheet-statement",
            {"symbol": ticker, "period": "quarter", "limit": 40},
        ) or []

        filed_income = _filed_before(income_rows if isinstance(income_rows, list) else [], event_date)[:4]
        filed_balance = _filed_before(balance_rows if isinstance(balance_rows, list) else [], event_date)[:1]
        valuation_source = None
        trailing_pe = None
        ev_sales = None

        if len(filed_income) >= 4:
            ttm_eps = sum(_num(row.get("epsDiluted") or row.get("epsdiluted") or row.get("eps")) or 0 for row in filed_income)
            ttm_revenue = sum(_num(row.get("revenue")) or 0 for row in filed_income)
            valuation_source = max(_accepted_date(row) for row in filed_income if _accepted_date(row))
            if ttm_eps and ttm_eps > 0:
                trailing_pe = round(market_cap / ttm_eps, 3)
            if filed_balance and ttm_revenue and ttm_revenue > 0:
                balance = filed_balance[0]
                total_debt = _num(balance.get("totalDebt")) or 0
                cash = _num(balance.get("cashAndCashEquivalents")) or _num(balance.get("cashAndShortTermInvestments")) or 0
                ev_sales = round((market_cap + total_debt - cash) / ttm_revenue, 3)
                balance_date = _accepted_date(balance)
                if balance_date and (valuation_source is None or balance_date > valuation_source):
                    valuation_source = balance_date

        return {
            "market_cap": market_cap,
            "trailing_pe_ratio": trailing_pe,
            "ev_sales": ev_sales,
            "valuation_source_filing_date": valuation_source,
            "raw": {
                "historical_market_cap": raw_market_cap,
                "income_filing_dates": [str(_accepted_date(row)) for row in filed_income],
                "balance_filing_dates": [str(_accepted_date(row)) for row in filed_balance],
            },
        }

    def _historical_market_cap(self, ticker: str, event_date: date) -> tuple[float | None, Any]:
        data = self._fmp_request(
            "/historical-market-capitalization",
            {
                "symbol": ticker,
                "from": (event_date - timedelta(days=5)).isoformat(),
                "to": (event_date + timedelta(days=5)).isoformat(),
            },
        )
        rows = data if isinstance(data, list) else data.get("historical") if isinstance(data, dict) else []
        if not rows:
            return None, data
        best = None
        best_delta = 999
        for row in rows:
            row_date = _parse_date(row.get("date"))
            cap = _num(row.get("marketCap") or row.get("marketCapValue"))
            if row_date and cap:
                delta = abs((row_date - event_date).days)
                if delta < best_delta:
                    best = cap
                    best_delta = delta
        return best, rows[:5]

    def _fmp_request(self, endpoint: str, params: dict | None = None) -> list | dict | None:
        if not self.fmp_key:
            return None
        rate_limiter.acquire("fmp")
        try:
            query = {"apikey": self.fmp_key}
            if params:
                query.update(params)
            response = httpx.get(f"{FMP_BASE}{endpoint}", params=query, timeout=25)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            log.warning("event_context_fmp_request_failed", endpoint=endpoint, error=redact_text(str(exc)))
            return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")[:10]).date()
    except ValueError:
        return None


def _coerce_date(value: Any) -> date:
    parsed = _parse_date(value)
    if not parsed:
        raise ValueError(f"Invalid event date: {value!r}")
    return parsed


def _num(value: Any) -> float | None:
    try:
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    except (TypeError, ValueError):
        return None


def _first_bar_idx_on_or_after(bars: list[PriceBar], target: date) -> int | None:
    for idx, bar in enumerate(bars):
        if bar.date >= target:
            return idx
    return None


def _bar_idx_on_or_before(bars: list[PriceBar], target: date) -> int | None:
    indexes = [idx for idx, bar in enumerate(bars) if bar.date <= target]
    return indexes[-1] if indexes else None


def _close_on_or_before(bars: list[PriceBar], target: date) -> float | None:
    idx = _bar_idx_on_or_before(bars, target)
    return bars[idx].close if idx is not None else None


def _momentum(bars: list[PriceBar], target: date, days: int) -> float | None:
    idx = _bar_idx_on_or_before(bars, target)
    if idx is None or idx - days < 0:
        return None
    now = bars[idx].close
    then = bars[idx - days].close
    if not now or not then:
        return None
    return round(((now / then) - 1) * 100, 3)


def _volatility(bars: list[PriceBar], target: date, days: int) -> float | None:
    idx = _bar_idx_on_or_before(bars, target)
    if idx is None or idx - days < 0:
        return None
    closes = [bar.close for bar in bars[idx - days: idx + 1] if bar.close]
    if len(closes) < days:
        return None
    returns = pd.Series(closes).pct_change().dropna()
    return round(float(returns.std() * math.sqrt(252) * 100), 3) if not returns.empty else None


def _distance_200ma(bars: list[PriceBar], target: date) -> float | None:
    idx = _bar_idx_on_or_before(bars, target)
    if idx is None or idx < 199:
        return None
    close = bars[idx].close
    ma = np.mean([bar.close for bar in bars[idx - 199: idx + 1] if bar.close])
    if not close or not ma:
        return None
    return round(((close / ma) - 1) * 100, 3)


def _macro_regime(vix: float | None, spy_distance: float | None) -> str:
    if vix is None and spy_distance is None:
        return ""
    if (vix is not None and vix > 25) or (spy_distance is not None and spy_distance < -5):
        return "risk-off"
    if (vix is not None and vix < 16) and (spy_distance is None or spy_distance > 0):
        return "risk-on"
    return "neutral"


def _gap_pct(bars: list[PriceBar], anchor_idx: int) -> float | None:
    if anchor_idx <= 0:
        return None
    prior_close = bars[anchor_idx - 1].close
    anchor_open = bars[anchor_idx].open
    if not prior_close or not anchor_open:
        return None
    return round(((anchor_open / prior_close) - 1) * 100, 2)


def _volume_ratio(bars: list[PriceBar], anchor_idx: int) -> float | None:
    if anchor_idx < 20 or bars[anchor_idx].volume is None:
        return None
    prior = [bar.volume for bar in bars[anchor_idx - 20: anchor_idx] if bar.volume]
    if not prior:
        return None
    return round(float(bars[anchor_idx].volume / np.mean(prior)), 3)


def _max_drawdown(bars: list[PriceBar], anchor_idx: int, anchor_price: float, days: int) -> tuple[float | None, int | None]:
    if not anchor_price:
        return None, None
    end_idx = min(anchor_idx + days + 1, len(bars))
    lows = [(idx - anchor_idx, bars[idx].low) for idx in range(anchor_idx, end_idx) if bars[idx].low is not None]
    if not lows:
        return None, None
    day, low = min(lows, key=lambda item: item[1])
    return round(((low - anchor_price) / anchor_price) * 100, 2), int(day)


def _accepted_date(row: dict) -> date | None:
    return _parse_date(row.get("acceptedDate") or row.get("fillingDate") or row.get("filingDate"))


def _filed_before(rows: list[dict], event_date: date) -> list[dict]:
    eligible = []
    for row in rows:
        accepted = _accepted_date(row)
        if accepted and accepted <= event_date:
            eligible.append(row)
    return sorted(eligible, key=lambda row: _accepted_date(row) or date.min, reverse=True)
