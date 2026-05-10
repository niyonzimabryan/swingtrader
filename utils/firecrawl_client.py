"""
Firecrawl client wrapper.

Thin abstraction over `firecrawl-py` that:
- Reads FIRECRAWL_API_KEY from settings (or env)
- Gracefully degrades when the key or library is missing (returns None)
- Caps calls per instance so a runaway scan can't blow the budget

Usage:
    fc = FirecrawlClient(settings)
    if fc.is_available:
        markdown = fc.scrape(url)
        results = fc.search("AAPL earnings transcript", limit=5)

Both `scrape` and `search` return None on any failure; callers should treat
None as "no narrative context" and fall back accordingly.
"""

import os

from utils.logger import get_logger

log = get_logger("firecrawl_client")


class FirecrawlClient:
    def __init__(self, settings=None, api_key: str = None, max_calls: int = None):
        if api_key is None and settings is not None:
            api_key = getattr(settings, "firecrawl_api_key", "") or ""
        if api_key is None:
            api_key = os.environ.get("FIRECRAWL_API_KEY", "")

        if max_calls is None and settings is not None:
            max_calls = getattr(settings, "firecrawl_max_calls_per_scan", 50)
        if max_calls is None:
            max_calls = 50

        self.api_key = api_key or ""
        self.max_calls = int(max_calls)
        self.calls_used = 0
        self._app = None

        if not self.api_key:
            log.info("firecrawl_disabled_no_key")
            return

        try:
            from firecrawl import FirecrawlApp
        except ImportError:
            log.warning("firecrawl_disabled_lib_missing")
            self.api_key = ""
            return

        try:
            self._app = FirecrawlApp(api_key=self.api_key)
        except Exception as e:
            log.error("firecrawl_init_failed", error=str(e))
            self._app = None
            self.api_key = ""

    @property
    def is_available(self) -> bool:
        return self._app is not None and self.api_key != ""

    def _consume_call(self) -> bool:
        if self.calls_used >= self.max_calls:
            log.warning("firecrawl_call_cap_reached", cap=self.max_calls)
            return False
        self.calls_used += 1
        return True

    def scrape(self, url: str) -> str | None:
        """Scrape a single URL and return the markdown body, or None on failure."""
        if not self.is_available or not url:
            return None
        if not self._consume_call():
            return None
        try:
            page = self._app.scrape(url, formats=["markdown"])
            markdown = self._extract_markdown(page)
            if markdown:
                log.info("firecrawl_scrape_ok", url=url, chars=len(markdown))
                return markdown
            log.warning("firecrawl_scrape_empty", url=url)
            return None
        except Exception as e:
            log.warning("firecrawl_scrape_failed", url=url, error=str(e)[:200])
            return None

    def search(self, query: str, limit: int = 5) -> list | None:
        """Search the web and return up to `limit` result dicts, or None on failure."""
        if not self.is_available or not query:
            return None
        if not self._consume_call():
            return None
        try:
            results = self._app.search(query, limit=limit)
            normalized = self._normalize_search_results(results)
            log.info("firecrawl_search_ok", query=query[:80], hits=len(normalized))
            return normalized
        except Exception as e:
            log.warning("firecrawl_search_failed", query=query[:80], error=str(e)[:200])
            return None

    @staticmethod
    def _extract_markdown(page) -> str:
        if page is None:
            return ""
        if isinstance(page, dict):
            md = page.get("markdown") or page.get("data", {}).get("markdown", "")
            return md or ""
        md = getattr(page, "markdown", None)
        if md:
            return md
        data = getattr(page, "data", None)
        if data is not None:
            return getattr(data, "markdown", "") or ""
        return ""

    @staticmethod
    def _normalize_search_results(results) -> list:
        if results is None:
            return []
        items = results
        if isinstance(results, dict):
            items = results.get("data") or results.get("results") or []
        elif hasattr(results, "data"):
            items = getattr(results, "data") or []
        out = []
        for item in items or []:
            if isinstance(item, dict):
                out.append({
                    "url": item.get("url", ""),
                    "title": item.get("title", ""),
                    "description": item.get("description", ""),
                    "markdown": item.get("markdown", ""),
                })
            else:
                out.append({
                    "url": getattr(item, "url", ""),
                    "title": getattr(item, "title", ""),
                    "description": getattr(item, "description", ""),
                    "markdown": getattr(item, "markdown", ""),
                })
        return out
