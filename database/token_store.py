"""
Encrypted on-disk token store for the Robinhood Agentic Trading MCP OAuth flow.

Implements the MCP SDK's `mcp.client.auth.TokenStorage` interface so the SDK's
`OAuthClientProvider` can persist (and, if the server issues a refresh token,
auto-refresh) credentials for an unattended service.

Design notes (see docs/ROBINHOOD_TOKEN_STORE.md):
- The rotating secret lives on the SAME persistent volume as the SQLite DB, NOT
  in an environment variable. Only the Fernet KEY belongs in deployment secrets.
- Ciphertext only on disk (Fernet / AES-128-CBC + HMAC). Atomic writes
  (temp file -> fsync -> os.replace), mode 0o600.
- The SDK drives registration/exchange/refresh; this class is just persistence.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from mcp.client.auth import TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from utils.logger import get_logger

log = get_logger("token_store")

# Refresh-skew buffer: treat the access token as expired this many seconds early.
EXPIRY_SKEW_SECONDS = 300
# How many token writes the on-disk refresh log keeps. 200 covers a year of
# ~6.7-day access tokens several times over, and the file must stay small:
# it is read and rewritten on every refresh.
REFRESH_LOG_LIMIT = 200
DEFAULT_CALLBACK_PORT = 8765
DEFAULT_SCOPE = "internal"


def _seconds_until(iso_timestamp: str | None) -> int | None:
    """Seconds from now until an ISO-8601 instant, or ``None`` if unparseable."""
    if not iso_timestamp:
        return None
    try:
        target = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return int((target - datetime.now(timezone.utc)).total_seconds())


def generate_key() -> str:
    """Return a fresh Fernet key (urlsafe base64 str) for TOKEN_ENCRYPTION_KEY."""
    return Fernet.generate_key().decode("ascii")


def token_store_path(settings) -> Path:
    """Put the encrypted token file on the persistent volume.

    Delegates to :func:`config.settings.data_dir`, which resolves ``DATA_DIR``
    first and only then falls back to the SQLite file's directory. Deriving the
    location from a ``sqlite:///`` URL was correct until the Postgres cutover
    and silently wrong after it: the token blob would land on the ephemeral
    container filesystem and be lost on every deploy, forcing a manual
    re-authentication that nobody would expect.
    """
    from config.settings import data_dir

    return data_dir(settings) / "robinhood_token.enc"


def is_configured(settings) -> bool:
    """True when a usable encryption key is set (i.e. the token store can run)."""
    return bool(getattr(settings, "token_encryption_key", "") or "")


def build_client_metadata(
    port: int = DEFAULT_CALLBACK_PORT,
    scope: str = DEFAULT_SCOPE,
) -> OAuthClientMetadata:
    """OAuth metadata shared by the bootstrap script and runtime broker."""
    return OAuthClientMetadata(
        client_name="SwingTrader",
        redirect_uris=[f"http://localhost:{port}/callback"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=scope,
        token_endpoint_auth_method="none",
    )


def build_oauth_provider(
    settings,
    *,
    port: int = DEFAULT_CALLBACK_PORT,
    scope: str = DEFAULT_SCOPE,
    redirect_handler=None,
    callback_handler=None,
    timeout: float = 300.0,
):
    """Return an MCP SDK OAuth provider backed by the encrypted token store."""
    from mcp.client.auth import OAuthClientProvider

    storage = EncryptedFileTokenStorage(settings)
    provider = OAuthClientProvider(
        server_url=getattr(settings, "robinhood_mcp_url", "https://agent.robinhood.com/mcp/trading"),
        client_metadata=build_client_metadata(port=port, scope=scope),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        timeout=timeout,
    )
    return provider, storage


class EncryptedFileTokenStorage(TokenStorage):
    """Fernet-encrypted JSON blob holding the OAuth tokens + dynamic client info."""

    def __init__(self, settings, path: Path | None = None):
        key = getattr(settings, "token_encryption_key", "") or ""
        if not key:
            raise ValueError(
                "TOKEN_ENCRYPTION_KEY is not set. Generate one with "
                "`python -m scripts.robinhood_auth --gen-key` and set it as a "
                "deployment secret."
            )
        try:
            self._fernet = Fernet(key.encode("ascii") if isinstance(key, str) else key)
        except (ValueError, TypeError) as e:
            raise ValueError(f"TOKEN_ENCRYPTION_KEY is not a valid Fernet key: {e}") from e
        self._path = path or token_store_path(settings)

    # --- blob persistence -------------------------------------------------
    def _read_blob(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            plaintext = self._fernet.decrypt(self._path.read_bytes())
            return json.loads(plaintext.decode("utf-8"))
        except (InvalidToken, ValueError, json.JSONDecodeError) as e:
            # A wrong key or corrupted file must not crash the caller; surface
            # as "no tokens" so the flow falls back to (re-)authentication.
            log.error("token_store_read_failed", error=str(e), path=str(self._path))
            return {}

    def _write_blob(self, blob: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        ciphertext = self._fernet.encrypt(json.dumps(blob).encode("utf-8"))
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, ciphertext)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self._path)  # atomic
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    # --- TokenStorage interface (async per the SDK) -----------------------
    async def get_tokens(self) -> OAuthToken | None:
        blob = self._read_blob()
        data = blob.get("tokens")
        if not data:
            return None
        # The refresh-attempt half of the unattended-survival log (Spec L §5.1).
        # The SDK only calls `set_tokens` when a refresh *succeeds*, so a run
        # that fails to refresh would otherwise leave no trace at all — which is
        # exactly the 30-day question the log exists to answer. Reading an
        # expired access token is the moment a refresh becomes necessary, so it
        # is logged here, with timestamps, before the SDK attempts one.
        expires_at = blob.get("tokens_expires_at")
        seconds_left = _seconds_until(expires_at)
        if seconds_left is not None and seconds_left <= EXPIRY_SKEW_SECONDS:
            log.info(
                "robinhood_token_refresh_due",
                checked_at=datetime.now(timezone.utc).isoformat(),
                expires_at=expires_at,
                seconds_until_expiry=seconds_left,
                has_refresh_token=bool(data.get("refresh_token")),
            )
        try:
            return OAuthToken.model_validate(data)
        except Exception as e:  # malformed persisted token -> force re-auth
            log.error("token_store_token_parse_failed", error=str(e))
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        blob = self._read_blob()
        previous = blob.get("tokens") or {}
        previous_expires_at = blob.get("tokens_expires_at")
        previous_obtained_at = blob.get("tokens_obtained_at")
        # A write with a token already stored is a refresh; the first write of
        # a bootstrap is not. The distinction is what makes the log answer
        # "did a continuously refreshing service survive 30 days unattended?"
        is_refresh = bool(previous.get("access_token"))

        blob["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        # Record an absolute expiry so status()/monitoring don't depend on the
        # relative expires_in after a restart.
        now_iso = datetime.now(timezone.utc).isoformat()
        blob["tokens_obtained_at"] = now_iso
        if tokens.expires_in:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(tokens.expires_in))
            blob["tokens_expires_at"] = expires_at.isoformat()
        else:
            blob.pop("tokens_expires_at", None)

        entry = {
            "at": now_iso,
            "kind": "refresh" if is_refresh else "bootstrap",
            "previous_obtained_at": previous_obtained_at,
            "previous_expires_at": previous_expires_at,
            "new_expires_at": blob.get("tokens_expires_at"),
            "expires_in": tokens.expires_in,
            "has_refresh_token": bool(tokens.refresh_token),
        }
        history = [e for e in (blob.get("refresh_log") or []) if isinstance(e, dict)]
        history.append(entry)
        blob["refresh_log"] = history[-REFRESH_LOG_LIMIT:]

        self._write_blob(blob)
        log.info(
            "token_store_tokens_saved",
            has_refresh=bool(tokens.refresh_token),
            expires_in=tokens.expires_in,
            scope=tokens.scope,
        )
        # One line per refresh, with both timestamps. Railway's log retention is
        # what the 30-day unattended test reads; the on-disk `refresh_log` is
        # the copy that survives a log rotation.
        log.info("robinhood_token_refresh", **entry)

    def refresh_log(self) -> list:
        """Every recorded token write, oldest first. Never contains a token."""
        entries = self._read_blob().get("refresh_log") or []
        return [e for e in entries if isinstance(e, dict)]

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        data = self._read_blob().get("client_info")
        if not data:
            return None
        try:
            return OAuthClientInformationFull.model_validate(data)
        except Exception as e:
            log.error("token_store_client_info_parse_failed", error=str(e))
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        blob = self._read_blob()
        blob["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write_blob(blob)
        log.info("token_store_client_info_saved")

    # --- introspection (never returns the raw token) ----------------------
    def status(self) -> dict:
        """Masked summary for /status and the bootstrap report."""
        blob = self._read_blob()
        tokens = blob.get("tokens") or {}
        expires_at = blob.get("tokens_expires_at")
        seconds_left = None
        if expires_at:
            try:
                delta = datetime.fromisoformat(expires_at) - datetime.now(timezone.utc)
                seconds_left = int(delta.total_seconds())
            except ValueError:
                seconds_left = None
        return {
            "path": str(self._path),
            "exists": self._path.exists(),
            "has_access_token": bool(tokens.get("access_token")),
            "has_refresh_token": bool(tokens.get("refresh_token")),
            "token_type": tokens.get("token_type"),
            "scope": tokens.get("scope"),
            "expires_at": expires_at,
            "seconds_until_expiry": seconds_left,
            "needs_reauth": bool(tokens.get("access_token"))
            and not tokens.get("refresh_token")
            and (seconds_left is not None and seconds_left <= EXPIRY_SKEW_SECONDS),
            "has_client_registration": bool(blob.get("client_info")),
            # Spec L §5.1: the 30-day unattended-refresh probe reads these.
            "refresh_count": len([e for e in (blob.get("refresh_log") or []) if isinstance(e, dict) and e.get("kind") == "refresh"]),
            "last_refresh_at": next(
                (e.get("at") for e in reversed(blob.get("refresh_log") or []) if isinstance(e, dict) and e.get("kind") == "refresh"),
                None,
            ),
        }
