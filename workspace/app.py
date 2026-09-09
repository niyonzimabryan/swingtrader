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


def build_mcp(settings=None) -> tuple[FastMCP, tuple[str, ...]]:
    """A stateless streamable-HTTP MCP server carrying this phase's tools.

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
    registered = tool_module.register(mcp, settings)
    return mcp, registered


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

    mcp, registered_tools = build_mcp(settings)

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
            tools=list(registered_tools),
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
    app.state.registered_tools = registered_tools

    @app.get("/health")
    def health():
        """Liveness plus what the workspace depends on.

        Spec K §6 also wants the portfolio-sync and cohort-maturation ages here.
        Those jobs arrive in Phases 1 and 3; reporting a field whose value would
        be invented is worse than reporting the ones that exist, so they are
        listed as pending rather than faked.
        """
        database = _database_health()
        ok = database["reachable"]
        return JSONResponse(
            {
                "status": "ok" if ok else "degraded",
                "service": SERVICE_NAME,
                "workspace_api_enabled": bool(settings.workspace_api_enabled),
                "database": database,
                "mcp": {
                    "path": "/mcp",
                    "transport": "streamable-http",
                    "tools": list(registered_tools),
                },
                "comparable_setups_enabled": bool(
                    getattr(settings, "comparable_setups_enabled", False)
                ),
                "pending_checks": [
                    "last_portfolio_sync_age (Spec L, Phase 1)",
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
