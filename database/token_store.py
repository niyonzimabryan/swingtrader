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
DEFAULT_CALLBACK_PORT = 8765
DEFAULT_SCOPE = "internal"


def generate_key() -> str:
    """Return a fresh Fernet key (urlsafe base64 str) for TOKEN_ENCRYPTION_KEY."""
    return Fernet.generate_key().decode("ascii")


def token_store_path(settings) -> Path:
    """Co-locate the encrypted token file with the SQLite DB (mirrors db.py:19-24
    so it lands on the persistent DB volume in prod and the project root locally)."""
    database_url = getattr(settings, "database_url", "sqlite:///swing_trader.db")
    if database_url.startswith("sqlite:///"):
        db_path = database_url.replace("sqlite:///", "")
        parent = Path(db_path).parent
    else:
        parent = Path(".")
    return parent / "robinhood_token.enc"


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
        data = self._read_blob().get("tokens")
        if not data:
            return None
        try:
            return OAuthToken.model_validate(data)
        except Exception as e:  # malformed persisted token -> force re-auth
            log.error("token_store_token_parse_failed", error=str(e))
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        blob = self._read_blob()
        blob["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        # Record an absolute expiry so status()/monitoring don't depend on the
        # relative expires_in after a restart.
        blob["tokens_obtained_at"] = datetime.now(timezone.utc).isoformat()
        if tokens.expires_in:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(tokens.expires_in))
            blob["tokens_expires_at"] = expires_at.isoformat()
        else:
            blob.pop("tokens_expires_at", None)
        self._write_blob(blob)
        log.info(
            "token_store_tokens_saved",
            has_refresh=bool(tokens.refresh_token),
            expires_in=tokens.expires_in,
            scope=tokens.scope,
        )

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
        }
