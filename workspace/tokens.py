"""Issue, authenticate, and revoke workspace owner tokens (Spec K §4.1).

One long-lived token per client, issued by ``scripts/workspace_token.py``,
stored as a SHA-256 digest, revocable, and scoped. The plaintext is shown once
at issue time and never persisted, so a database dump is not a set of keys.

Comparison is by digest lookup on an indexed unique column, and the digest of
an attacker-supplied string is compared with ``secrets.compare_digest`` after
the row is found, so neither the lookup nor the check leaks by timing.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from database.models import WorkspaceToken
from utils.timeutils import utcnow_naive
from workspace import scopes as scope_module

#: Distinguishes a workspace token from every other secret in an .env file.
TOKEN_PREFIX = "swt_"

#: 32 bytes of entropy, urlsafe-base64 encoded.
TOKEN_ENTROPY_BYTES = 32

#: How much of the secret is stored in the clear, for logs and `--list`.
DISPLAY_PREFIX_LENGTH = 12


@dataclass(frozen=True)
class TokenIdentity:
    """The authenticated caller. Never carries the secret."""

    token_id: int
    label: str
    scopes: tuple[str, ...]

    def has(self, scope: str) -> bool:
        return scope in self.scopes


class TokenError(RuntimeError):
    """The token could not be issued or revoked."""


def generate_secret() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def issue(session, label: str, requested_scopes, note: str = "") -> str:
    """Create a token row and return the plaintext secret, once.

    Raises :class:`TokenError` if the label is already taken — labels identify a
    client in every log line, so two clients sharing one is not allowed.
    """
    label = (label or "").strip()
    if not label:
        raise TokenError("a token needs a --label, e.g. 'codex-laptop'")
    if session.query(WorkspaceToken).filter_by(label=label).first():
        raise TokenError(
            f"a token labelled {label!r} already exists. Revoke it first, or "
            "choose another label."
        )

    stored_scopes = scope_module.render(requested_scopes)
    if not stored_scopes:
        raise TokenError("a token needs at least one scope")

    secret = generate_secret()
    session.add(
        WorkspaceToken(
            label=label,
            token_hash=hash_secret(secret),
            token_prefix=secret[:DISPLAY_PREFIX_LENGTH],
            scopes=stored_scopes,
            note=note or "",
            created_at=utcnow_naive(),
        )
    )
    return secret


def revoke(session, label: str) -> bool:
    """Mark a token revoked. Returns False if there was nothing to revoke."""
    row = session.query(WorkspaceToken).filter_by(label=label).first()
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = utcnow_naive()
    return True


def listing(session) -> list[dict]:
    rows = session.query(WorkspaceToken).order_by(WorkspaceToken.id).all()
    return [
        {
            "label": r.label,
            "prefix": r.token_prefix,
            "scopes": r.scopes,
            "created_at": r.created_at,
            "last_used_at": r.last_used_at,
            "revoked": r.revoked_at is not None,
            "note": r.note,
        }
        for r in rows
    ]


def authenticate(session, secret: str) -> TokenIdentity | None:
    """Resolve a presented bearer secret to an identity, or ``None``.

    ``None`` covers every failure mode on purpose — unknown token, revoked
    token, malformed scope string — because the caller must not learn which.
    """
    if not secret:
        return None
    digest = hash_secret(secret)
    row = session.query(WorkspaceToken).filter_by(token_hash=digest).first()
    if row is None or not secrets.compare_digest(row.token_hash, digest):
        return None
    if row.revoked_at is not None:
        return None
    try:
        granted = scope_module.parse(row.scopes)
    except scope_module.UnknownScope:
        # A scope that no longer exists (or never did) fails closed rather than
        # silently downgrading the token to whatever still parses.
        return None
    if not granted:
        return None

    row.last_used_at = utcnow_naive()
    return TokenIdentity(token_id=row.id, label=row.label, scopes=granted)
