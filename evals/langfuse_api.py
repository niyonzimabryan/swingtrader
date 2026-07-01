"""Minimal Langfuse REST client (stdlib urllib — no SDK dependency).

The langfuse SDK is only in the prod container; this reader works anywhere the
LANGFUSE_* env vars are set, so the eval corpus can be pulled locally/CI without
installing or initialising the SDK. Reads only (never writes).

Why REST not SDK: the eval "pull" path must not depend on prod's OTEL setup, and
the legacy list endpoints are flaky under wide date ranges — this client narrows
by tag + time window and paginates defensively.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.parse
import urllib.request


def _cfg():
    pub = os.environ["LANGFUSE_PUBLIC_KEY"]
    sec = os.environ["LANGFUSE_SECRET_KEY"]
    base = os.environ.get("LANGFUSE_BASE_URL", "https://us.cloud.langfuse.com").rstrip("/")
    auth = base64.b64encode(f"{pub}:{sec}".encode()).decode()
    return base, auth


def _get(path: str, params: dict, timeout: int = 45, retries: int = 3) -> dict:
    base, auth = _cfg()
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    last = None
    for _ in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:      # flaky endpoint: timeouts/5xx under wide ranges
            last = e
    raise last


def traces_by_tag(tag: str, from_ts: str, limit: int = 50, max_pages: int = 40):
    """Yield trace summaries carrying `tag`, newest first, from `from_ts` (ISO8601).
    Tolerant of the flaky list endpoint: a page that fails ends pagination cleanly
    (return what we have) rather than aborting the whole pull."""
    page = 1
    while page <= max_pages:
        try:
            d = _get("/api/public/traces", {"tags": tag, "fromTimestamp": from_ts,
                                            "limit": limit, "page": page})
        except Exception:
            return
        data = d.get("data", [])
        if not data:
            return
        yield from data
        if len(data) < limit:
            return
        page += 1


def trace(trace_id: str) -> dict:
    return _get(f"/api/public/traces/{trace_id}", {})
