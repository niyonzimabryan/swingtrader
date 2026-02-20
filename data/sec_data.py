"""
SEC EDGAR data adapter.
Provides: recent filings (8-K, Form 4), insider transactions.
Free API — requires only a User-Agent header.
"""

import httpx
from datetime import datetime, timedelta
from utils.logger import get_logger

log = get_logger("sec_data")

EDGAR_BASE = "https://efts.sec.gov/LATEST"
EDGAR_COMPANY = "https://data.sec.gov/submissions"
HEADERS = {
    "User-Agent": "SwingTrader research@swingtrader.dev",
    "Accept-Encoding": "gzip, deflate",
}


class SECDataAdapter:

    def get_recent_filings(self, ticker: str, cik: str = "", filing_types: list = None) -> list[dict]:
        """
        Get recent SEC filings for a company.
        Supports: 8-K, 4 (insider), SC 13D (activist).
        """
        if filing_types is None:
            filing_types = ["8-K", "4"]
        try:
            # Full-text search for the ticker
            results = self._search_filings(ticker, filing_types)
            return results
        except Exception as e:
            log.error("sec_filings_failed", ticker=ticker, error=str(e))
            return []

    def get_insider_transactions(self, ticker: str, days: int = 30) -> list[dict]:
        """Get Form 4 insider transactions."""
        try:
            filings = self._search_filings(ticker, ["4"])
            # Filter to recent
            cutoff = datetime.now() - timedelta(days=days)
            recent = []
            for f in filings:
                try:
                    filed = datetime.strptime(f.get("date_filed", ""), "%Y-%m-%d")
                    if filed >= cutoff:
                        recent.append(f)
                except (ValueError, TypeError):
                    continue
            return recent
        except Exception as e:
            log.error("insider_transactions_failed", ticker=ticker, error=str(e))
            return []

    def _search_filings(self, query: str, form_types: list, limit: int = 10) -> list[dict]:
        """Search EDGAR full-text search API."""
        try:
            params = {
                "q": query,
                "dateRange": "custom",
                "startdt": (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"),
                "enddt": datetime.now().strftime("%Y-%m-%d"),
                "forms": ",".join(form_types),
            }
            resp = httpx.get(
                f"{EDGAR_BASE}/search-index",
                params=params,
                headers=HEADERS,
                timeout=15,
            )
            if resp.status_code != 200:
                # Fallback: try the simpler full-text search
                return self._fulltext_search(query, form_types, limit)

            data = resp.json()
            hits = data.get("hits", {}).get("hits", [])
            return [
                {
                    "form_type": hit.get("_source", {}).get("form_type", ""),
                    "date_filed": hit.get("_source", {}).get("file_date", ""),
                    "company_name": hit.get("_source", {}).get("display_names", [""])[0] if hit.get("_source", {}).get("display_names") else "",
                    "description": hit.get("_source", {}).get("display_date_filed", ""),
                    "filing_url": f"https://www.sec.gov/Archives/edgar/data/{hit.get('_source', {}).get('file_num', '')}" if hit.get("_source", {}).get("file_num") else "",
                }
                for hit in hits[:limit]
            ]
        except Exception as e:
            log.error("edgar_search_failed", query=query, error=str(e))
            return self._fulltext_search(query, form_types, limit)

    def _fulltext_search(self, query: str, form_types: list, limit: int = 10) -> list[dict]:
        """Fallback full-text search via EDGAR."""
        try:
            resp = httpx.get(
                "https://efts.sec.gov/LATEST/search-index",
                params={
                    "q": f'"{query}"',
                    "forms": ",".join(form_types),
                },
                headers=HEADERS,
                timeout=15,
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            hits = data.get("hits", {}).get("hits", [])
            return [
                {
                    "form_type": hit.get("_source", {}).get("form_type", ""),
                    "date_filed": hit.get("_source", {}).get("file_date", ""),
                    "description": str(hit.get("_source", {}).get("display_date_filed", "")),
                }
                for hit in hits[:limit]
            ]
        except Exception:
            return []
