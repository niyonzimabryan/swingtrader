"""Shared payload redaction helpers for persisted provider/broker blobs."""

from __future__ import annotations

import re
from typing import Any


SENSITIVE_PAYLOAD_KEYS = {
    "access_token",
    "apikey",
    "api_key",
    "authorization",
    "bearer",
    "cookie",
    "headers",
    "mfa_code",
    "password",
    "refresh_token",
    "secret",
    "token",
}

ACCOUNT_PAYLOAD_KEYS = {
    "account",
    "account_id",
    "account_number",
    "broker_account_id",
    "number",
}

KEY_SHAPED_RE = re.compile(
    r"(?i)\b(?:pplx|sk|rk|ghp|gho|xox[baprs])-?[a-z0-9_\-]{16,}\b|"
    r"\b[A-Za-z0-9_\-]{32,}\b"
)
BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.=:+/]{8,}")
QUERY_SECRET_RE = re.compile(r"(?i)(apikey|api_key|token|secret|key)=([^&\s]+)")


def redact_payload(value: Any) -> Any:
    """Recursively redact sensitive keys and key-shaped string values."""
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if any(sensitive in normalized for sensitive in SENSITIVE_PAYLOAD_KEYS):
                clean[key] = "[REDACTED]"
            elif normalized in ACCOUNT_PAYLOAD_KEYS:
                clean[key] = mask_identifier(item)
            else:
                clean[key] = redact_payload(item)
        return clean
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_payload(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(value: str) -> str:
    """Redact bearer strings, URL query secrets, and long key-shaped tokens."""
    if not value:
        return value
    redacted = BEARER_RE.sub("Bearer [REDACTED]", value)
    redacted = QUERY_SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    redacted = KEY_SHAPED_RE.sub("[REDACTED]", redacted)
    return redacted


def mask_identifier(value: Any) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    if len(raw) <= 4:
        return "****"
    return f"****{raw[-4:]}"
