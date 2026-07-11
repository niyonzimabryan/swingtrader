"""Shared ledger for Google GenAI (Gemini) calls (audit spec G2 / P2-LF-3).

Gemini generations are invisible in Langfuse (Claude-only OTEL capture), so the
screener, discovery, web-research, and pattern-event stages have zero
cost/latency/behavior telemetry. This wraps the raw `generate_content` call at
the one shared point every Gemini client routes through and emits a single
structlog `llm_call` event per call, making Railway logs the greppable source.

Scope: visibility only. No cost computation or persistence — that's BRY-106.
"""
from __future__ import annotations

import time

from utils.logger import get_logger

log = get_logger("gemini_ledger")


def generate_content(client, *, model: str, stage: str, ticker: str | None = None, **kwargs):
    """Call ``client.models.generate_content(model=model, **kwargs)`` and emit one
    ``llm_call`` ledger event (provider=gemini) with timing, token usage, and
    finish reason. Re-raises on failure after logging ``ok=False``.

    ``kwargs`` are forwarded verbatim (e.g. ``contents``, ``config``) so each call
    site keeps full control of its request shape.
    """
    start = time.monotonic()
    ok = True
    response = None
    try:
        response = client.models.generate_content(model=model, **kwargs)
        return response
    except Exception:
        ok = False
        raise
    finally:
        log.info(
            "llm_call",
            provider="gemini",
            model=model,
            stage=stage,
            ticker=ticker,
            duration_ms=round((time.monotonic() - start) * 1000, 1),
            ok=ok,
            **_usage_fields(response),
        )


def _usage_fields(response) -> dict:
    """Best-effort extraction of token usage + finish reason from a google-genai
    response. Tolerates missing/None fields and SDK enum drift (finish_reason may
    be an enum with ``.name`` or a bare string)."""
    fields = {"input_tokens": None, "output_tokens": None, "finish_reason": None}
    usage = getattr(response, "usage_metadata", None)
    if usage is not None:
        fields["input_tokens"] = getattr(usage, "prompt_token_count", None)
        fields["output_tokens"] = getattr(usage, "candidates_token_count", None)
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        fr = getattr(candidates[0], "finish_reason", None)
        if fr is not None:
            fields["finish_reason"] = getattr(fr, "name", None) or str(fr)
    return fields
