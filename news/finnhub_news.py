"""Finnhub as the **cross-check**, not a primary — Spec O section 5.

Its one job here is the earliest-timestamp rule: a story that Alpaca carries at
09:15 and Finnhub carries at 09:02 was available at 09:02, and the cluster's
``known_at_utc`` is the earlier of the two. Using two sources for that and only
that is why this module is small.

It goes through ``data/news_data.py``'s existing adapter rather than opening a
second Finnhub connection: that adapter already owns the ``finnhub`` rate-limit
bucket, and Spec O's "respect each source's rate limits in one client module
per source" is a rule about the *source*, not about the file.

Finnhub's ``datetime`` field is a Unix timestamp in **seconds, UTC**. An entry
with ``datetime`` of 0 — which the free tier does emit — has no publication
time, and is passed through undated rather than dated to the epoch. An article
apparently published on 1970-01-01 is not a colourful bug; it sorts first and
becomes the cluster's ``known_at_utc``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from news.articles import Article
from utils.logger import get_logger

log = get_logger("finnhub_news")

SOURCE_FINNHUB = "finnhub_news"


class FinnhubNewsSchemaError(RuntimeError):
    """The payload did not have the shape this adapter was written for."""


def _timestamp(raw: Any) -> datetime | None:
    if raw in (None, "", 0, 0.0):
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError) as exc:
        raise FinnhubNewsSchemaError(f"unparseable datetime {raw!r}") from exc
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def article_from_payload(entry: Any, *, symbol: str = "") -> Article:
    if not isinstance(entry, dict):
        raise FinnhubNewsSchemaError(f"expected an object, got {type(entry).__name__}")
    if "headline" not in entry:
        raise FinnhubNewsSchemaError(
            f"news entry has no 'headline'; keys were {sorted(entry)}"
        )
    related = str(entry.get("related") or symbol or "")
    symbols = tuple(s.strip().upper() for s in related.split(",") if s.strip())
    return Article(
        source=SOURCE_FINNHUB,
        provider_id=str(entry.get("id") or "") or None,
        publisher=str(entry.get("source") or ""),
        headline=str(entry["headline"]),
        lead=str(entry.get("summary") or ""),
        body=str(entry.get("summary") or ""),
        url=str(entry.get("url") or ""),
        symbols=symbols,
        published_at=_timestamp(entry.get("datetime")),
        first_seen_at=datetime.now(timezone.utc),
    )


def articles_from_adapter(adapter, ticker: str, *, days: int = 7) -> list[Article]:
    """Cross-check articles for one ticker, via the existing Finnhub adapter.

    The adapter returns already-shaped dicts with an ISO ``datetime`` string;
    this converts them back to the article record without a second HTTP client.
    """
    out: list[Article] = []
    for item in adapter.get_company_news(ticker, days=days) or []:
        stamp = None
        raw = item.get("datetime")
        if raw:
            try:
                parsed = datetime.fromisoformat(str(raw))
                stamp = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                log.warning("finnhub_unparseable_datetime", ticker=ticker, raw=raw)
        if stamp is not None and stamp.timestamp() <= 0:
            stamp = None
        out.append(
            Article(
                source=SOURCE_FINNHUB,
                publisher=str(item.get("source") or ""),
                headline=str(item.get("headline") or ""),
                lead=str(item.get("summary") or ""),
                body=str(item.get("summary") or ""),
                url=str(item.get("url") or ""),
                symbols=(ticker.upper(),),
                published_at=stamp,
                first_seen_at=datetime.now(timezone.utc),
            )
        )
    return out
