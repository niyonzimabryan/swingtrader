"""Bearer authentication, scope checks, rate limiting, and the call log.

One authentication path serves both transports. ``/mcp`` is a mounted ASGI app,
so a FastAPI dependency cannot see it; authentication therefore happens in a
plain ASGI middleware that runs before either transport and leaves the
identity in ``scope["state"]``. REST handlers read it through a dependency and
MCP tools read it through ``ctx.request_context.request`` — same dict, one
decision.

Spec K §4.1 requires that **every request is logged with the token label, the
tool name, and an argument hash**. :func:`log_call` is that log line, and the
hash rather than the arguments is deliberate: an argument may carry a ticker, a
thesis, or a note, and the operations log is not where those belong.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse

from utils.logger import get_logger
from workspace import scopes as scope_module
from workspace.ratelimit import RateLimitExceeded, RateLimiter
from workspace.tokens import TokenIdentity, authenticate

log = get_logger("workspace")

#: Reachable without a token: liveness, and the OAuth discovery documents a
#: client must read *before* it has one.
PUBLIC_PATHS = frozenset({"/health", "/"})
PUBLIC_PREFIXES = ("/.well-known/",)

STATE_KEY = "workspace_identity"
AUTH_STATE_KEY = "workspace_auth"


class AuthError(Exception):
    """An authenticated call that must be refused, with an HTTP-shaped reason."""

    def __init__(self, status: int, code: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


@dataclass
class WorkspaceAuth:
    """Everything the middleware and the tools need, built once per app."""

    settings: object
    limiter: RateLimiter
    session_factory: object

    def resolve(self, presented: str) -> TokenIdentity:
        with self.session_factory() as session:
            identity = authenticate(session, presented)
        if identity is None:
            raise AuthError(
                401,
                "invalid_token",
                "Unknown, revoked, or malformed bearer token.",
                {"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
        return identity


def argument_hash(arguments) -> str:
    """A stable short digest of a call's arguments. Never the arguments."""
    payload = json.dumps(arguments or {}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def log_call(identity: TokenIdentity | None, tool: str, arguments, **extra) -> None:
    log.info(
        "workspace_call",
        token_label=identity.label if identity else None,
        tool=tool,
        argument_hash=argument_hash(arguments),
        **extra,
    )


def require_scope(identity: TokenIdentity, scope: str) -> None:
    """Refuse a token that does not carry ``scope``.

    This is ``test_token_scopes``: a ``read`` token reaching ``research_write``
    or ``propose_order`` is refused here, before the tool body exists.
    """
    if scope not in scope_module.SCOPES:
        raise AuthError(500, "server_error", f"unknown required scope {scope!r}")
    if not identity.has(scope):
        raise AuthError(
            403,
            "insufficient_scope",
            f"token {identity.label!r} has scopes {list(identity.scopes)}; "
            f"this call requires {scope!r}.",
            {"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{scope}"'},
        )


def authorize(auth: WorkspaceAuth, identity: TokenIdentity, tool: str, arguments=None):
    """Scope check, rate limit, and log for one named call. Raises AuthError."""
    scope = scope_module.TOOL_SCOPES.get(tool)
    if scope is None:
        raise AuthError(500, "server_error", f"tool {tool!r} has no declared scope")
    require_scope(identity, scope)
    kind = scope_module.kind_for(scope)
    try:
        auth.limiter.check(identity.token_id, kind)
    except RateLimitExceeded as exc:
        log_call(identity, tool, arguments, outcome="rate_limited")
        raise AuthError(
            429,
            "rate_limited",
            str(exc),
            {"Retry-After": str(exc.retry_after)},
        ) from exc
    log_call(identity, tool, arguments, outcome="ok")
    return identity


def bearer_from_headers(headers) -> str:
    raw = headers.get("authorization") or ""
    prefix = "bearer "
    if raw[: len(prefix)].lower() != prefix:
        return ""
    return raw[len(prefix):].strip()


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


class WorkspaceAuthMiddleware:
    """ASGI middleware: authenticate, then hand the identity downstream.

    Pure ASGI rather than ``BaseHTTPMiddleware`` because ``/mcp`` streams, and
    ``BaseHTTPMiddleware`` buffers.
    """

    def __init__(self, app, auth: WorkspaceAuth):
        self.app = app
        self.auth = auth

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or is_public(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        try:
            if not getattr(self.auth.settings, "workspace_api_enabled", False):
                raise AuthError(
                    503,
                    "workspace_api_disabled",
                    "WORKSPACE_API_ENABLED is false. /health is served so a "
                    "deploy is observable; nothing else is.",
                )
            presented = bearer_from_headers(request.headers)
            if not presented:
                raise AuthError(
                    401,
                    "missing_token",
                    "Authorization: Bearer <token> is required.",
                    {"WWW-Authenticate": "Bearer"},
                )
            identity = await anyio.to_thread.run_sync(self.auth.resolve, presented)
        except AuthError as exc:
            log.info(
                "workspace_call_refused",
                path=scope.get("path"),
                code=exc.code,
                status=exc.status,
            )
            response = JSONResponse(
                {"error": exc.code, "detail": exc.message},
                status_code=exc.status,
                headers=exc.headers,
            )
            await response(scope, receive, send)
            return

        # `scope["state"]` is the one dict every layer below shares — the
        # mounted MCP app builds its own Request from this same scope, so the
        # tool sees the identity the middleware resolved. `scope["app"]` does
        # *not* work for that: inside the mount it is the mounted Starlette app,
        # not the FastAPI one that holds `state.workspace_auth`.
        state = scope.setdefault("state", {})
        state[STATE_KEY] = identity
        state[AUTH_STATE_KEY] = self.auth
        await self.app(scope, receive, send)


def auth_from_scope(scope) -> WorkspaceAuth:
    auth = (scope.get("state") or {}).get(AUTH_STATE_KEY)
    if auth is None:  # pragma: no cover - middleware guarantees this
        raise AuthError(500, "server_error", "auth context missing from the request.")
    return auth


def identity_from_scope(scope) -> TokenIdentity:
    identity = (scope.get("state") or {}).get(STATE_KEY)
    if identity is None:  # pragma: no cover - middleware guarantees this
        raise AuthError(401, "missing_token", "No authenticated identity on this request.")
    return identity
