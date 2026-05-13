"""
Article fetcher with paywall fallback chain.

Order of attempts: Firecrawl scrape → archive.is mirror → give up. Returns
`(text, status)` so callers can record the narrative provenance and let
downstream models weight evidence accordingly.

`status` values:
  - "firecrawl"   — full body via Firecrawl
  - "archive"     — body recovered from archive.is
  - "paywalled"   — both attempts failed; body unavailable
  - "unavailable" — fetcher disabled (no Firecrawl, archive.is off)
  - "error"       — unexpected exception during fetch
"""

import time

import httpx

from utils.logger import get_logger

log = get_logger("article_fetcher")

ARCHIVE_IS_NEWEST = "https://archive.is/newest/{url}"
ARCHIVE_MIN_INTERVAL_S = 5.0
ARCHIVE_TIMEOUT_S = 15.0
ARCHIVE_MIN_BODY_CHARS = 400


class ArticleFetcher:
    def __init__(
        self,
        firecrawl_client=None,
        archive_enabled: bool = True,
        min_archive_interval_s: float = ARCHIVE_MIN_INTERVAL_S,
    ):
        self.firecrawl = firecrawl_client
        self.archive_enabled = archive_enabled
        self.min_archive_interval_s = min_archive_interval_s
        self._last_archive_call = 0.0

    def fetch(self, url: str) -> tuple[str | None, str]:
        if not url:
            return None, "unavailable"

        if self.firecrawl is not None and getattr(self.firecrawl, "is_available", False):
            text = self.firecrawl.scrape(url)
            if text:
                return text, "firecrawl"

        if self.archive_enabled:
            text = self._fetch_archive_is(url)
            if text:
                return text, "archive"

        if self.firecrawl is None and not self.archive_enabled:
            return None, "unavailable"

        return None, "paywalled"

    def _fetch_archive_is(self, url: str) -> str | None:
        elapsed = time.monotonic() - self._last_archive_call
        if elapsed < self.min_archive_interval_s:
            time.sleep(self.min_archive_interval_s - elapsed)
        self._last_archive_call = time.monotonic()

        archive_url = ARCHIVE_IS_NEWEST.format(url=url)
        try:
            with httpx.Client(timeout=ARCHIVE_TIMEOUT_S, follow_redirects=True) as client:
                resp = client.get(archive_url, headers={"User-Agent": "SwingTrader/1.0"})
                if resp.status_code != 200:
                    log.info("archive_is_no_capture", url=url, status=resp.status_code)
                    return None
                text = self._strip_html(resp.text)
                if len(text) < ARCHIVE_MIN_BODY_CHARS:
                    log.info("archive_is_thin_body", url=url, chars=len(text))
                    return None
                log.info("archive_is_ok", url=url, chars=len(text))
                return text
        except Exception as e:
            log.warning("archive_is_failed", url=url, error=str(e)[:200])
            return None

    @staticmethod
    def _strip_html(html: str) -> str:
        import re

        no_script = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
        no_style = re.sub(r"<style[\s\S]*?</style>", " ", no_script, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", no_style)
        text = re.sub(r"\s+", " ", text).strip()
        return text
