"""Normalize, validate, and dedupe historical event candidates."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlparse
from typing import Any

from database.models import HistoricalEvent
from utils.logger import get_logger
from utils.redaction import redact_payload

log = get_logger("event_extractor")

SOURCE_PRIORITY = {
    "company_ir": 90,
    "sec_filing": 85,
    "earnings_transcript": 80,
    "press_release": 75,
    "regulator": 70,
    "news": 55,
    "analyst_report": 50,
    "recap": 30,
    "other": 20,
}
VALID_TIMINGS = {"pre_market", "regular_hours", "after_hours", "unknown"}
VALID_POLARITIES = {"bullish", "bearish", "mixed", "neutral"}
VALID_SOURCE_TYPES = set(SOURCE_PRIORITY)
MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


class EventValidationError(ValueError):
    pass


class EventExtractor:
    """Persist provider event candidates into canonical HistoricalEvent rows."""

    def __init__(self, min_confidence: float = 0.55):
        self.min_confidence = min_confidence

    def upsert_candidate(
        self,
        session,
        candidate: dict,
        provider: str,
        provider_query: str = "",
        provider_result: dict | None = None,
    ) -> tuple[HistoricalEvent | None, str]:
        try:
            normalized = self.normalize_candidate(candidate, provider, provider_query, provider_result)
        except EventValidationError as exc:
            log.info("event_extraction_rejected", reason=str(exc), provider=provider)
            return None, str(exc)

        existing = self._find_existing(session, normalized["ticker"], normalized["event_type"], normalized["event_date"])
        if existing:
            self._merge_existing(existing, normalized)
            return existing, "merged"

        event = HistoricalEvent(**normalized)
        session.add(event)
        session.flush()
        log.info(
            "historical_event_stored",
            ticker=event.ticker,
            event_type=event.event_type,
            event_date=str(event.event_date),
            provider=provider,
        )
        return event, "created"

    def normalize_candidate(
        self,
        candidate: dict,
        provider: str,
        provider_query: str = "",
        provider_result: dict | None = None,
    ) -> dict:
        ticker = (candidate.get("ticker") or "").upper().strip()
        event_type = (candidate.get("event_type") or "").strip()
        if not ticker:
            raise EventValidationError("missing ticker")
        if not event_type:
            raise EventValidationError("missing event_type")

        headline = (candidate.get("headline") or "").strip()
        summary = (candidate.get("summary") or "").strip()
        if not headline and not summary:
            raise EventValidationError("missing headline_or_summary")

        source_url = (candidate.get("source_url") or candidate.get("url") or "").strip()
        if not source_url:
            raise EventValidationError("missing source_url")

        event_date = self._validated_event_date(candidate, provider_result)
        confidence = _float(candidate.get("confidence"), 0)
        if confidence < self.min_confidence:
            raise EventValidationError("low_confidence")

        source_type = (candidate.get("source_type") or "other").strip()
        if source_type not in VALID_SOURCE_TYPES:
            source_type = "other"

        event_timing = (candidate.get("event_timing") or "unknown").strip()
        if event_timing not in VALID_TIMINGS:
            event_timing = "unknown"
        polarity = (candidate.get("polarity") or "neutral").strip()
        if polarity not in VALID_POLARITIES:
            polarity = "neutral"

        event_timestamp = _parse_datetime(candidate.get("event_timestamp"))
        source_domain = _domain(source_url)
        dedupe_key = make_dedupe_key(ticker, event_type, event_date)
        raw_json = {
            "sources": [source_url],
            "provider_result": redact_payload(provider_result or {}),
            "candidate": redact_payload(candidate),
        }

        return {
            "ticker": ticker,
            "company_name": candidate.get("company_name") or "",
            "event_type": event_type,
            "event_subtype": candidate.get("event_subtype") or "",
            "event_date": event_date,
            "event_timestamp": event_timestamp,
            "event_timing": event_timing,
            "polarity": polarity,
            "magnitude": _float(candidate.get("magnitude"), None),
            "headline": headline,
            "summary": summary,
            "evidence": candidate.get("evidence") or "",
            "source_url": source_url,
            "source_domain": source_domain,
            "source_type": source_type,
            "provider": provider,
            "provider_query": provider_query,
            "confidence": confidence,
            "dedupe_key": dedupe_key,
            "embedding_json": json.dumps(candidate.get("embedding")) if candidate.get("embedding") else None,
            "raw_json": json.dumps(raw_json),
        }

    def _validated_event_date(self, candidate: dict, provider_result: dict | None) -> date:
        source = (candidate.get("event_date_source") or "").lower()
        if source in {"provider_date", "provider_result", "last_updated", "crawl_date"}:
            raise EventValidationError("event_date_from_provider_metadata")

        event_date = _parse_date(candidate.get("event_date"))
        content = " ".join(
            str(candidate.get(key, "") or "")
            for key in ("source_content", "evidence", "headline", "summary")
        )
        content_date = derive_event_date_from_content(content)
        if content_date:
            return content_date
        if event_date and source in {"content", "press_release", "filing", "transcript", "dateline", "accepted_date"}:
            return event_date
        if event_date:
            provider_dates = {
                _parse_date((provider_result or {}).get("date")),
                _parse_date((provider_result or {}).get("last_updated")),
                _parse_date((provider_result or {}).get("publishedDate")),
            }
            if event_date in provider_dates:
                raise EventValidationError("event_date_matches_provider_metadata_without_content_date")
        raise EventValidationError("missing_content_derived_event_date")

    def _find_existing(self, session, ticker: str, event_type: str, event_date: date) -> HistoricalEvent | None:
        lo = event_date - timedelta(days=1)
        hi = event_date + timedelta(days=1)
        return (
            session.query(HistoricalEvent)
            .filter(
                HistoricalEvent.ticker == ticker,
                HistoricalEvent.event_type == event_type,
                HistoricalEvent.event_date >= lo,
                HistoricalEvent.event_date <= hi,
            )
            .order_by(HistoricalEvent.confidence.desc())
            .first()
        )

    def _merge_existing(self, existing: HistoricalEvent, normalized: dict) -> None:
        try:
            raw = json.loads(existing.raw_json or "{}")
        except json.JSONDecodeError:
            raw = {}
        sources = list(dict.fromkeys([*(raw.get("sources") or []), normalized["source_url"]]))
        raw["sources"] = sources
        provider_payloads = raw.get("provider_payloads") or []
        provider_payloads.append(
            {
                "provider": normalized["provider"],
                "provider_query": normalized["provider_query"],
                "source_url": normalized["source_url"],
            }
        )
        raw["provider_payloads"] = redact_payload(provider_payloads)

        if SOURCE_PRIORITY.get(normalized["source_type"], 0) > SOURCE_PRIORITY.get(existing.source_type or "other", 0):
            existing.source_type = normalized["source_type"]
            existing.source_url = normalized["source_url"]
            existing.source_domain = normalized["source_domain"]
            existing.headline = normalized["headline"] or existing.headline
            existing.summary = normalized["summary"] or existing.summary
            existing.evidence = normalized["evidence"] or existing.evidence

        existing.confidence = max(existing.confidence or 0, normalized["confidence"])
        existing.provider = "+".join(sorted(set((existing.provider or "").split("+")) | {normalized["provider"]}))
        existing.raw_json = json.dumps(redact_payload(raw))
        existing.updated_at = datetime.utcnow()


def make_dedupe_key(ticker: str, event_type: str, event_date: date) -> str:
    bucket = event_date.isoformat()
    identity = f"{ticker.upper()}|{event_type}|{bucket}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def derive_event_date_from_content(text: str) -> date | None:
    text = text or ""
    iso = re.search(r"\b(20\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])\b", text)
    if iso:
        return _safe_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))

    month_names = "|".join(MONTHS)
    month_match = re.search(
        rf"\b({month_names})\s+([0-3]?\d)(?:st|nd|rd|th)?[,]?\s+(20\d{{2}})\b",
        text,
        re.IGNORECASE,
    )
    if month_match:
        return _safe_date(
            int(month_match.group(3)),
            MONTHS[month_match.group(1).lower()],
            int(month_match.group(2)),
        )
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


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def _float(value: Any, default: float | None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
