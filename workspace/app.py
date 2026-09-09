"""The workspace FastAPI application (Spec K §4).

    /health          liveness + dependency status, unauthenticated
    /v1/...          REST, bearer-authenticated
    /mcp             streamable-HTTP MCP, bearer-authenticated

One process, separate from the bot. Nothing here imports ``bot`` or
``execution``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
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
    """
    mcp = FastMCP(
        SERVICE_NAME,
        instructions=MCP_INSTRUCTIONS,
        stateless_http=True,
    )
    tool_module.register(mcp, settings)
    return mcp


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
        log.info(
            "workspace_starting",
            enabled=bool(settings.workspace_api_enabled),
            oauth_enabled=bool(settings.workspace_oauth_enabled),
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
                "research_workspace_enabled": bool(
                    getattr(settings, "research_workspace_enabled", False)
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
