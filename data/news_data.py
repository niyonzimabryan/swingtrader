"""
News data adapter — Finnhub.
Provides: company news, earnings calendar, analyst recommendations.
"""

import finnhub
from datetime import datetime, timedelta
from utils.logger import get_logger
from utils.rate_limiter import rate_limiter

log = get_logger("news_data")


class NewsDataAdapter:
    def __init__(self, api_key: str):
        self.client = finnhub.Client(api_key=api_key) if api_key else None

    def get_company_news(self, ticker: str, days: int = 2) -> list[dict]:
        """Fetch recent company news."""
        if not self.client:
            return []
        rate_limiter.acquire("finnhub")
        try:
            end = datetime.now()
            start = end - timedelta(days=days)
            news = self.client.company_news(
                ticker,
                _from=start.strftime("%Y-%m-%d"),
                to=end.strftime("%Y-%m-%d"),
            )
            return [
                {
                    "headline": item.get("headline", ""),
                    "summary": item.get("summary", ""),
                    "source": item.get("source", ""),
                    "url": item.get("url", ""),
                    "datetime": datetime.fromtimestamp(item.get("datetime", 0)).isoformat(),
                    "category": item.get("category", ""),
                    "related": item.get("related", ""),
                }
                for item in (news or [])
            ]
        except Exception as e:
            log.error("company_news_failed", ticker=ticker, error=str(e))
            return []

    def get_earnings_calendar(self, days_ahead: int = 7) -> list[dict]:
        """Fetch upcoming earnings dates."""
        if not self.client:
            return []
        rate_limiter.acquire("finnhub")
        try:
            start = datetime.now()
            end = start + timedelta(days=days_ahead)
            data = self.client.earnings_calendar(
                _from=start.strftime("%Y-%m-%d"),
                to=end.strftime("%Y-%m-%d"),
                symbol="",
            )
            earnings = data.get("earningsCalendar", []) if isinstance(data, dict) else []
            return [
                {
                    "symbol": item.get("symbol", ""),
                    "date": item.get("date", ""),
                    "hour": item.get("hour", ""),  # bmo (before market), amc (after market)
                    "eps_estimate": item.get("epsEstimate"),
                    "eps_actual": item.get("epsActual"),
                    "revenue_estimate": item.get("revenueEstimate"),
                    "revenue_actual": item.get("revenueActual"),
                }
                for item in earnings
            ]
        except Exception as e:
            log.error("earnings_calendar_failed", error=str(e))
            return []

    def get_analyst_recommendations(self, ticker: str) -> list[dict]:
        """Fetch recent analyst recommendations."""
        if not self.client:
            return []
        rate_limiter.acquire("finnhub")
        try:
            recs = self.client.recommendation_trends(ticker)
            return [
                {
                    "period": item.get("period", ""),
                    "strong_buy": item.get("strongBuy", 0),
                    "buy": item.get("buy", 0),
                    "hold": item.get("hold", 0),
                    "sell": item.get("sell", 0),
                    "strong_sell": item.get("strongSell", 0),
                }
                for item in (recs or [])[:4]
            ]
        except Exception as e:
            log.error("analyst_recs_failed", ticker=ticker, error=str(e))
            return []

    def get_price_target(self, ticker: str) -> dict:
        """Fetch analyst price targets."""
        if not self.client:
            return {}
        rate_limiter.acquire("finnhub")
        try:
            pt = self.client.price_target(ticker)
            return {
                "target_high": pt.get("targetHigh", 0),
                "target_low": pt.get("targetLow", 0),
                "target_mean": pt.get("targetMean", 0),
                "target_median": pt.get("targetMedian", 0),
                "last_updated": pt.get("lastUpdated", ""),
            }
        except Exception as e:
            log.error("price_target_failed", ticker=ticker, error=str(e))
            return {}
