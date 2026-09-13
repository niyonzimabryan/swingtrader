"""The workspace FastAPI application (Spec K §4).

    /health          liveness + dependency status, unauthenticated
    /v1/...          REST, bearer-authenticated
    /mcp             streamable-HTTP MCP, bearer-authenticated

One process, separate from the bot. Nothing here imports ``bot`` or
``execution``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

from database.schema import current_revision
from utils.logger import get_logger
from workspace import scopes as scope_module
from workspace import tools as tool_module
from workspace.auth import (
    AuthError,
    WorkspaceAuth,
    WorkspaceAuthMiddleware,
    authorize,
    identity_from_scope,
)
from workspace.oauth import protected_resource_metadata
from workspace.ratelimit import RateLimiter

log = get_logger("workspace")

SERVICE_NAME = "swingtrader-workspace"

MCP_INSTRUCTIONS = (
    "SwingTrader investment workspace. Read tools are free of side effects. "
    "No tool in this server can place, modify, or cancel a broker order, and "
    "no token scope grants it."
)


def build_mcp(settings=None) -> FastMCP:
    """A stateless streamable-HTTP MCP server carrying the Phase 0b tools.

    ``stateless_http`` because nothing here holds per-session state and a
    stateless server survives a proxy dropping an idle connection, which is the
    failure Railway's reference deployment adds Redis to fix. When a tool needs
    resumability (Spec K §4 names ``compare_setups``), that is when the event
    store goes in — not before.

    ``streamable_http_path`` is left at its default because the transport is
    attached as an explicit route rather than a mount; see
    :func:`mount_mcp_endpoint`.

    ``transport_security`` is passed **explicitly** and must stay that way.
    ``FastMCP``'s ``host`` defaults to ``127.0.0.1``, and on that default the
    SDK auto-enables DNS-rebinding protection with
    ``allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"]``
    (``mcp/server/fastmcp/server.py``). This process never binds that host —
    uvicorn serves it behind Railway's edge — but the guard fired anyway and
    answered **every** ``/mcp`` request on the public domain with
    ``421 Invalid Host header``, while ``/health`` and ``/v1/...`` kept working
    because they are ordinary FastAPI routes. The whole agent tool surface was
    unreachable in production and nothing pointed at the cause.

    The protection guards a *browser* against a malicious page rebinding DNS at
    a server bound to loopback. This server is bearer-authenticated, is not
    bound to loopback, and is not reachable from a browser origin without a
    token, so the control it provides here is redundant — but it is only
    disabled where we can name the host we do serve. When ``WORKSPACE_BASE_URL``
    is set, that host is the allowlist and the protection stays on.
    """
    mcp = FastMCP(
        SERVICE_NAME,
        instructions=MCP_INSTRUCTIONS,
        stateless_http=True,
        transport_security=_transport_security(settings),
    )
    tool_module.register(mcp, settings)
    return mcp


def _transport_security(settings=None) -> TransportSecuritySettings:
    """Allow the host this workspace is actually served on.

    See :func:`build_mcp` for why this is never left to the SDK default. With a
    configured ``WORKSPACE_BASE_URL`` the protection stays on and names that
    host (plus loopback, for local development). Without one there is no host to
    name, and refusing every request would be the same outage in a new costume,
    so it is switched off explicitly rather than inherited by accident.
    """
    base_url = getattr(settings, "workspace_base_url", "") if settings else ""
    if not base_url:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    host = urlsplit(base_url).netloc
    if not host:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    hostname = host.split("@")[-1].split(":")[0]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host, hostname, f"{hostname}:*",
                       "127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=[f"https://{hostname}", f"https://{hostname}:*",
                         "http://127.0.0.1:*", "http://localhost:*",
                         "http://[::1]:*"],
    )


def mount_mcp_endpoint(app: FastAPI, mcp: FastMCP) -> None:
    """Serve the streamable-HTTP transport at ``/mcp`` and ``/mcp/``.

    ``app.mount("/mcp", mcp.streamable_http_app())`` also works, but a client
    asking for ``/mcp`` gets a 307 to ``/mcp/`` first. The official SDK follows
    that redirect; not every client will, and a redirect on the one URL the
    owner pastes into four different configuration files is a bad trade for one
    saved line. Two exact routes over the same ASGI app cost nothing.
    """
    # Creates the session manager as a side effect; `mcp.session_manager`
    # raises until something has.
    mcp.streamable_http_app()
    asgi = StreamableHTTPASGIApp(mcp.session_manager)
    for path in ("/mcp", "/mcp/"):
        app.router.routes.append(Route(path, endpoint=asgi))


def _portfolio_sync_health(settings) -> dict:
    """Last-sync age for ``/health`` (Spec K §6).

    Reports the age of the **stalest** enabled account, because an overview is
    only as fresh as its worst account and a maximum would let one healthy
    account mask one that has been failing all day. Reads the ledger tables
    only; it cannot reach a broker.
    """
    from portfolio.freshness import budget_from_settings

    budget = budget_from_settings(settings)
    payload = {"enabled": bool(getattr(settings, "portfolio_sync_enabled", False)),
               "freshness_budget_minutes": budget}
    try:
        from database.db import get_session
        from portfolio import ledger

        with get_session() as session:
            age = ledger.last_sync_age_seconds(session)
    except Exception as exc:  # pragma: no cover - a broken database is already reported
        payload["detail"] = f"{type(exc).__name__}: {exc}"
        return payload
    payload["last_sync_age_seconds"] = None if age is None else round(age, 1)
    payload["stale"] = True if age is None else age > budget * 60
    if age is None:
        payload["detail"] = "no portfolio sync has ever completed."
    return payload


def _database_health() -> dict:
    from sqlalchemy import text

    from database import db as db_module

    engine = db_module.engine
    if engine is None:
        return {"reachable": False, "detail": "database not initialised"}
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            return {
                "reachable": True,
                "backend": engine.dialect.name,
                "migration_revision": current_revision(connection),
            }
    except Exception as exc:  # pragma: no cover - exercised by hand, not in CI
        return {"reachable": False, "detail": f"{type(exc).__name__}: {exc}"}


def build_cards_router(settings) -> APIRouter:
    """The signed, unauthenticated, read-only card page (Spec K §7's successor).

    Two routes and one control. ``/cards/{uid}`` renders the page from the
    stored payload; ``/cards/{uid}/chart.png`` renders the chart from the bars
    in that same payload. Both verify an HMAC over the uid with
    ``CARD_LINK_SECRET`` — falling back to ``EXECUTION_APPROVAL_SECRET`` — in
    constant time, and both answer **404** for a bad signature, an unknown uid,
    an unreachable database, and a card with no chart alike.

    One 404 for all of those on purpose: a route that distinguished "no such
    card" from "wrong signature" would be an oracle for enumerating uids, and
    the uid is half the credential. The cost is a slightly less helpful error
    for a link somebody mangled, which is the right side of that trade.

    Nothing here writes, and nothing here reaches an approval: approval is
    Telegram's signed, single-use, expiring callback, handled in the bot process
    that this service cannot import (Spec L §6.1). The page is a record.
    """
    router = APIRouter(tags=["cards"])

    def _verify(uid: str, signature: str) -> bool:
        from notify.links import CardLinkUnavailable, card_secret, verify_uid

        try:
            secret = card_secret(settings)
        except CardLinkUnavailable as exc:
            log.warning("card_link_secret_missing", detail=str(exc))
            return False
        return verify_uid(uid, signature, secret)

    def _not_found() -> Response:
        return Response(status_code=404)

    @router.get("/cards/{uid}")
    def card_page(uid: str, s: str = ""):
        from notify.cards import render
        from notify.store import load_card

        if not _verify(uid, s):
            return _not_found()
        payload = load_card(uid)
        if payload is None:
            return _not_found()
        from notify.links import card_links

        card_url, chart_url = card_links(uid, settings=settings)
        if not (payload.get("chart") or {}).get("bars"):
            chart_url = ""
        rendered = render(payload, chart_url=chart_url, card_url=card_url)
        return HTMLResponse(
            rendered.html_page,
            headers={"Cache-Control": "private, max-age=300", "X-Robots-Tag": "noindex"},
        )

    @router.get("/cards/{uid}/chart.png")
    def card_chart(uid: str, s: str = ""):
        from notify.cards.chart import ChartUnavailable, render_png
        from notify.store import load_card

        if not _verify(uid, s):
            return _not_found()
        payload = load_card(uid)
        if payload is None:
            return _not_found()
        try:
            png = render_png(payload.get("chart") or {})
        except ChartUnavailable as exc:
            log.info("card_chart_unavailable", card_uid=uid, detail=str(exc))
            return _not_found()
        return Response(
            content=png,
            media_type="image/png",
            headers={
                # The payload behind a card never changes, so the image is
                # immutable for the life of the uid. Gmail proxies and caches it
                # anyway; saying so keeps the proxy from re-fetching.
                "Cache-Control": "private, max-age=86400, immutable",
                "X-Robots-Tag": "noindex",
            },
        )

    return router


def build_v1_router(auth: WorkspaceAuth) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["v1"])

    @router.get("/whoami")
    def whoami(request: Request):
        """The REST twin of the ``whoami`` MCP tool, for scripts and cron."""
        identity = identity_from_scope(request.scope)
        authorize(auth, identity, "whoami", None)
        return {
            "token_label": identity.label,
            "scopes": list(identity.scopes),
            "all_scopes": list(scope_module.SCOPES),
            "execute_scope_exists": False,
            "service": SERVICE_NAME,
        }

    return router


def create_app(settings=None, *, limiter: RateLimiter | None = None) -> FastAPI:
    if settings is None:
        from config.settings import Settings

        settings = Settings()

    from database.db import get_session, init_db
    from database import db as db_module

    auth = WorkspaceAuth(
        settings=settings,
        limiter=limiter
        or RateLimiter(
            read_per_minute=settings.workspace_read_rate_limit_per_minute,
            write_per_minute=settings.workspace_write_rate_limit_per_minute,
        ),
        session_factory=get_session,
    )

    mcp = build_mcp(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The bot may already have brought the schema to head; ensure_schema is
        # idempotent and takes a Postgres advisory lock, so both services
        # starting at once is safe (database/schema.py).
        if db_module.SessionLocal is None:
            init_db(settings.database_url)
        # Phase 6: with the flag on, wire the approval card to Telegram over
        # HTTPS. This posts through the same bot the owner already uses; the
        # callback still lands in the bot process, which is unreachable from
        # here. With the flag off, or the bot unconfigured, the log-only
        # default stays and nothing can be approved (workspace/proposal_card.py).
        from workspace.proposal_card import register_if_configured

        card_channels = register_if_configured(settings)
        log.info(
            "workspace_starting",
            enabled=bool(settings.workspace_api_enabled),
            oauth_enabled=bool(settings.workspace_oauth_enabled),
            phase6_enabled=bool(getattr(settings, "phase6_execution_enabled", False)),
            approval_card_channel=(",".join(card_channels) if card_channels else "log_only"),
            card_page=bool(getattr(settings, "notify_email_enabled", False)),
            tools=list(tool_module.registered_tools(settings)),
        )
        async with mcp.session_manager.run():
            yield

    app = FastAPI(
        title="SwingTrader workspace",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.workspace_auth = auth
    app.state.mcp = mcp
    app.state.registered_tools = tool_module.registered_tools(settings)

    @app.get("/health")
    def health():
        """Liveness plus what the workspace depends on.

        Spec K §6 wants the portfolio-sync and cohort-maturation ages here.
        The portfolio sync arrives with Phase 1 and is reported; cohort
        maturation arrives with Phase 3 and is still listed as pending, because
        reporting a field whose value would be invented is worse than reporting
        the ones that exist.
        """
        database = _database_health()
        ok = database["reachable"]
        portfolio_sync = _portfolio_sync_health(settings) if ok else {"detail": "database unreachable"}
        return JSONResponse(
            {
                "status": "ok" if ok else "degraded",
                "service": SERVICE_NAME,
                "workspace_api_enabled": bool(settings.workspace_api_enabled),
                "database": database,
                "mcp": {
                    "path": "/mcp",
                    "transport": "streamable-http",
                    "tools": list(tool_module.registered_tools(settings)),
                },
                "notifications": {
                    "email_enabled": bool(getattr(settings, "notify_email_enabled", False)),
                    "card_page": bool(getattr(settings, "notify_email_enabled", False)),
                },
                "research_workspace_enabled": bool(
                    getattr(settings, "research_workspace_enabled", False)
                ),
                "comparable_setups_enabled": bool(
                    getattr(settings, "comparable_setups_enabled", False)
                ),
                "portfolio_sync": portfolio_sync,
                "pending_checks": [
                    "last_cohort_maturation_age (Spec N, Phase 3)",
                ],
            },
            status_code=200 if ok else 503,
        )

    if settings.workspace_oauth_enabled:
        @app.get("/.well-known/oauth-protected-resource")
        def oauth_metadata():
            return protected_resource_metadata(settings.workspace_base_url)

    app.include_router(build_v1_router(auth))
    # The card page ships with the email channel and only with it: with
    # NOTIFY_EMAIL_ENABLED off nothing mints a card, so a route that could only
    # ever 404 is not registered at all.
    if bool(getattr(settings, "notify_email_enabled", False)):
        app.include_router(build_cards_router(settings))
    mount_mcp_endpoint(app, mcp)
    app.add_middleware(WorkspaceAuthMiddleware, auth=auth)

    @app.exception_handler(AuthError)
    def _auth_error(request: Request, exc: AuthError):
        return JSONResponse(
            {"error": exc.code, "detail": exc.message},
            status_code=exc.status,
            headers=exc.headers,
        )

    return app
