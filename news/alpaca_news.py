"""The one module that talks to the Alpaca News API — Spec O section 5.

Benzinga-sourced, publisher timestamps, history to 2015, free with the Alpaca
account already configured (verification claim 11). It is the **primary**
timestamped news source; Finnhub is the cross-check.

The rate limit is inherited from the Market Data plan — "200 calls per minute
for Free plans" — and claim 11 rates that figure **strong secondary**, not
verified from the docs page directly. So it is a setting with the published
number as its default, one throttle for every caller, and a comment saying to
lower it rather than to trust it.

**The terms are the reason ``news/`` exists as its own package.** Alpaca bars
distributing the data "or any derived products", so article bodies stay in
Postgres (``news_articles``) and every ledger row derived from them carries
``mirror_allowed=False``. See ``news.mirror_guard``.

Timestamps: Alpaca returns ``created_at`` and ``updated_at`` in RFC 3339 UTC.
``created_at`` is the publication time and ``updated_at`` is the revision time;
an article whose ``updated_at`` differs is a **revision**, stored as a new row
(section 5.1), never an overwrite.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Sequence

import httpx

from news.articles import Article
from utils.logger import get_logger

log = get_logger("alpaca_news")

SOURCE_ALPACA = "alpaca_news"
NEWS_PATH = "/v1beta1/news"
DEFAULT_BASE_URL = "https://data.alpaca.markets"

#: Alpaca's documented free-plan limit. Strong secondary (claim 11).
DEFAULT_REQUESTS_PER_MINUTE = 200
MAX_PAGE_SIZE = 50

RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


class AlpacaNewsError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class AlpacaNewsSchemaError(AlpacaNewsError):
    """The payload did not have the shape this adapter was written for."""


@dataclass
class _Throttle:
    max_requests: int
    window_s: float = 60.0
    clock: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        self._times: list[float] = []

    def wait(self) -> None:
        while True:
            now = self.clock()
            self._times = [t for t in self._times if now - t < self.window_s]
            if len(self._times) < self.max_requests:
                self._times.append(now)
                return
            self.sleeper(self.window_s - (now - self._times[0]))


def parse_rfc3339(raw: Any) -> datetime | None:
    """``2024-03-15T13:45:02Z`` -> aware UTC datetime, or ``None``."""
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str):
        raise AlpacaNewsSchemaError(f"expected an RFC 3339 timestamp, got {raw!r}")
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise AlpacaNewsSchemaError(f"unparseable timestamp {raw!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


REQUIRED_ARTICLE_FIELDS = ("id", "headline", "created_at")


def article_from_payload(entry: Any, *, first_seen_at: datetime | None = None) -> Article:
    """One ``news`` entry -> an :class:`Article`, or raise.

    A missing ``created_at`` is **not** filled in with the fetch time. An
    article we cannot date is stored undated and quarantined; substituting our
    own clock would make an article look available the moment we noticed it.
    """
    if not isinstance(entry, dict):
        raise AlpacaNewsSchemaError(f"expected an object, got {type(entry).__name__}")
    missing = [key for key in REQUIRED_ARTICLE_FIELDS if key not in entry]
    if missing:
        raise AlpacaNewsSchemaError(
            f"news entry missing {missing}; keys were {sorted(entry)}"
        )

    symbols = entry.get("symbols") or []
    if not isinstance(symbols, list):
        raise AlpacaNewsSchemaError("news entry 'symbols' is not an array")

    created = parse_rfc3339(entry.get("created_at"))
    updated = parse_rfc3339(entry.get("updated_at"))
    extra: list[str] = []
    if updated is not None and created is not None and updated > created:
        extra.append("article_revised_after_publication")

    return Article(
        source=SOURCE_ALPACA,
        provider_id=str(entry["id"]),
        publisher=str(entry.get("source") or ""),
        headline=str(entry.get("headline") or ""),
        lead=str(entry.get("summary") or ""),
        body=str(entry.get("content") or entry.get("summary") or ""),
        url=str(entry.get("url") or ""),
        symbols=tuple(str(s).upper() for s in symbols),
        published_at=created,
        first_seen_at=first_seen_at or datetime.now(timezone.utc),
        extra_warnings=tuple(extra),
    )


class AlpacaNewsClient:
    """Throttled, paginated reader for the Alpaca News API."""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
        timeout_s: float = 30.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if not api_key or not secret_key:
            raise AlpacaNewsError(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY are required for the news "
                "plane. They are the same credentials the broker adapter uses; "
                "the News API is free on the account that already exists."
            )
        self.max_retries = max(0, int(max_retries))
        self._sleeper = sleeper
        self._throttle = _Throttle(max(1, int(requests_per_minute)), clock=clock, sleeper=sleeper)
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout_s,
            base_url=base_url,
            headers={
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": secret_key,
                "Accept": "application/json",
            },
        )
        self.request_count = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AlpacaNewsClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _get(self, params: dict) -> dict:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle.wait()
            try:
                response = self._client.get(NEWS_PATH, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self.max_retries:
                    self._sleeper(min(2.0**attempt, 30.0))
                continue

            self.request_count += 1
            if response.status_code in RETRYABLE_STATUS:
                last_error = AlpacaNewsError(
                    f"{response.status_code} from Alpaca news (attempt {attempt + 1})",
                    status_code=response.status_code,
                )
                if attempt < self.max_retries:
                    self._sleeper(min(2.0**attempt, 30.0))
                continue
            if response.status_code >= 400:
                raise AlpacaNewsError(
                    f"{response.status_code} from Alpaca news",
                    status_code=response.status_code,
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise AlpacaNewsError("non-JSON response from Alpaca news") from exc
            if not isinstance(payload, dict) or "news" not in payload:
                raise AlpacaNewsSchemaError(
                    f"Alpaca news response has no 'news' array; keys were "
                    f"{sorted(payload) if isinstance(payload, dict) else type(payload).__name__}"
                )
            return payload

        if isinstance(last_error, AlpacaNewsError):
            raise last_error
        raise AlpacaNewsError(f"giving up on Alpaca news: {last_error}") from last_error

    def fetch(
        self,
        symbols: Sequence[str],
        *,
        start: datetime | date | None = None,
        end: datetime | date | None = None,
        limit: int = MAX_PAGE_SIZE,
        max_pages: int = 20,
        include_content: bool = True,
    ) -> list[Article]:
        """Every article for ``symbols`` in the window, following page tokens."""
        params: dict[str, Any] = {
            "symbols": ",".join(sorted({s.upper() for s in symbols})),
            "limit": min(int(limit), MAX_PAGE_SIZE),
            "include_content": "true" if include_content else "false",
            "sort": "asc",
        }
        if start is not None:
            params["start"] = _iso(start)
        if end is not None:
            params["end"] = _iso(end)

        out: list[Article] = []
        seen_tokens: set[str] = set()
        for _page in range(max(1, int(max_pages))):
            payload = self._get(params)
            entries = payload.get("news") or []
            if not isinstance(entries, list):
                raise AlpacaNewsSchemaError("Alpaca news 'news' is not an array")
            out.extend(article_from_payload(entry) for entry in entries)

            token = payload.get("next_page_token")
            if not token or token in seen_tokens:
                break
            seen_tokens.add(str(token))
            params["page_token"] = token
        return out


def _iso(value: datetime | date) -> str:
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value.isoformat()


def client_from_settings(settings, *, transport: httpx.BaseTransport | None = None):
    return AlpacaNewsClient(
        getattr(settings, "alpaca_api_key", "") or "",
        getattr(settings, "alpaca_secret_key", "") or "",
        base_url=getattr(settings, "alpaca_news_base_url", DEFAULT_BASE_URL),
        requests_per_minute=getattr(
            settings, "alpaca_news_requests_per_minute", DEFAULT_REQUESTS_PER_MINUTE
        ),
        timeout_s=getattr(settings, "alpaca_news_timeout_s", 30.0),
        transport=transport,
    )
