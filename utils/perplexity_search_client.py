"""Perplexity Search API client.

Uses POST /search only. This intentionally does not use the Perplexity Agent API.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

import httpx

from database.models import PatternProviderCache
from utils.logger import get_logger
from utils.redaction import redact_payload

log = get_logger("perplexity_search")

PERPLEXITY_SEARCH_URL = "https://api.perplexity.ai/search"


class PerplexitySearchClient:
    def __init__(self, settings=None, session=None, api_key: str | None = None):
        self.settings = settings
        self.session = session
        self.api_key = api_key if api_key is not None else getattr(settings, "perplexity_api_key", "")
        self.max_requests = int(getattr(settings, "perplexity_search_max_requests_per_run", 20) if settings else 20)
        self.ttl_days = int(getattr(settings, "pattern_event_cache_ttl_days", 90) if settings else 90)
        self.requests_used = 0

    def search(
        self,
        query: str,
        max_results: int = 10,
        domains: list[str] | None = None,
        after: str | None = None,
        before: str | None = None,
        max_tokens_per_page: int = 512,
    ) -> dict:
        if self.requests_used >= self.max_requests:
            raise RuntimeError("Perplexity Search request budget exhausted")
        cache_key = self._cache_key(query, max_results, domains, after, before, max_tokens_per_page)
        cached = self._read_cache(cache_key)
        if cached is not None:
            return cached
        if not self.api_key:
            raise RuntimeError("Perplexity Search API key is not configured")

        payload: dict[str, Any] = {
            "query": query,
            "max_results": max_results,
            "max_tokens_per_page": max_tokens_per_page,
        }
        if domains:
            payload["search_domain_filter"] = domains
        if after:
            payload["search_after_date_filter"] = after
        if before:
            payload["search_before_date_filter"] = before

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        self.requests_used += 1
        response = httpx.post(PERPLEXITY_SEARCH_URL, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        self._write_cache(cache_key, query, payload, data)
        return data

    def search_many(self, queries: list[str], budget: int | None = None) -> list[dict]:
        limit = budget if budget is not None else self.max_requests
        results = []
        for query in queries:
            if len(results) >= limit or self.requests_used >= self.max_requests:
                break
            results.append(self.search(query))
        return results

    def _cache_key(self, query: str, max_results: int, domains, after, before, max_tokens_per_page: int) -> str:
        identity = json.dumps(
            {
                "provider": "perplexity_search",
                "query": query,
                "max_results": max_results,
                "domains": domains or [],
                "after": after,
                "before": before,
                "max_tokens_per_page": max_tokens_per_page,
            },
            sort_keys=True,
        )
        return "perplexity:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _read_cache(self, cache_key: str) -> dict | None:
        if self.session is None:
            return None
        row = (
            self.session.query(PatternProviderCache)
            .filter_by(cache_key=cache_key)
            .filter((PatternProviderCache.expires_at.is_(None)) | (PatternProviderCache.expires_at > datetime.utcnow()))
            .first()
        )
        if not row:
            return None
        try:
            return json.loads(row.result_json or "{}")
        except json.JSONDecodeError:
            return None

    def _write_cache(self, cache_key: str, query: str, payload: dict, data: dict) -> None:
        if self.session is None:
            return
        expires = datetime.utcnow() + timedelta(days=self.ttl_days)
        row = self.session.query(PatternProviderCache).filter_by(cache_key=cache_key).first()
        clean_data = json.dumps(redact_payload(data))
        clean_payload = json.dumps(redact_payload(payload))
        if row:
            row.result_json = clean_data
            row.filters_json = clean_payload
            row.updated_at = datetime.utcnow()
            row.expires_at = expires
        else:
            self.session.add(
                PatternProviderCache(
                    cache_key=cache_key,
                    provider="perplexity_search",
                    query=query,
                    filters_json=clean_payload,
                    result_json=clean_data,
                    expires_at=expires,
                )
            )
