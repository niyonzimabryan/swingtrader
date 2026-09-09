"""The MCP tool surface. Phase 0b's ``whoami``, plus Phase 1's three read tools.

Spec K §4.2 fixes fifteen tools; Phases 1-4 build them. Shipping them early as
stubs would put tools in an agent's ``tools/list`` that return nothing useful,
which is worse than not having them, so a tool appears here only when it can
answer.

Phase 1 (Spec L) adds ``portfolio_overview``, ``position_detail`` and
``orders_open``, all at scope ``read``. Each answers from the ledger tables
alone: this module imports ``portfolio.ledger``, which reaches a database and
nothing else. **No tool here can place, review, modify, or cancel an order**,
and no import path from this package reaches one — asserted statically in
``tests/test_no_execute_scope.py`` and ``tests/test_portfolio_import_graph.py``,
not promised in this docstring.

Every read tool returns a ``provenance`` block carrying ``as_of_utc``, a
per-field source, ``stale``, and a ``data_quality`` tier. A tool whose ledger is
past the freshness budget returns the data **with the flag set** rather than
raising (Spec K §4.2). The opposite rule applies on the proposal side, where
``portfolio.freshness.require_fresh`` refuses instead — see
``portfolio/freshness.py`` for why the two directions are not a contradiction.

Every tool body starts with :func:`authorize_call`, which is where the scope
check, the rate limit, and the Spec K §4.1 call log live. Nothing enforces that
by construction, so ``tests/test_workspace_mcp.py`` enforces it by test: every
registered tool must refuse a token that lacks the scope its ``TOOL_SCOPES``
entry declares. A tool that forgets the call fails that test.
"""

from __future__ import annotations

import anyio
from mcp.server.fastmcp import Context, FastMCP

from workspace import scopes as scope_module
from workspace.auth import AuthError, auth_from_scope, authorize, identity_from_scope

#: The tools this phase registers. Kept as a constant so ``/health`` and the
#: startup log need not await ``tools/list``; the test asserts it matches what
#: the server actually advertises.
REGISTERED_TOOLS: tuple[str, ...] = (
    "whoami",
    "portfolio_overview",
    "position_detail",
    "orders_open",
)


class ToolRefused(ValueError):
    """A refusal the agent can read, rather than a transport failure it cannot."""


def authorize_call(ctx: Context, tool: str, arguments: dict | None = None):
    """Scope check + rate limit + call log for one tool invocation.

    The MCP transport hands the tool the same ASGI scope the auth middleware
    already wrote the identity into, so there is one authentication decision per
    request no matter which transport made it.
    """
    request = ctx.request_context.request
    if request is None:  # pragma: no cover - only reachable over stdio
        raise ToolRefused("missing_token: this server is reachable over HTTP only.")
    try:
        auth = auth_from_scope(request.scope)
        identity = identity_from_scope(request.scope)
        return authorize(auth, identity, tool, arguments)
    except AuthError as exc:
        raise ToolRefused(f"{exc.code}: {exc.message}") from exc


def register(mcp: FastMCP) -> None:
    """Attach the Phase 0b tool surface to ``mcp``."""

    @mcp.tool(
        name="whoami",
        description=(
            "Identify the token this session is using: its label, its scopes, "
            "and the workspace it is attached to. Read-only and side-effect "
            "free. Call it to confirm an agent client is attached to the right "
            "workspace with the access it expects."
        ),
    )
    async def whoami(ctx: Context) -> dict:
        identity = authorize_call(ctx, "whoami", {})
        return {
            "token_label": identity.label,
            "scopes": list(identity.scopes),
            "all_scopes": list(scope_module.SCOPES),
            "execute_scope_exists": False,
            "service": "swingtrader-workspace",
            "phase": "1",
            "note": (
                "Order placement is not reachable by any token: there is no "
                "execute scope and no import path from this service to a broker."
            ),
        }

    @mcp.tool(
        name="portfolio_overview",
        description=(
            "Holdings, cash, and exposure across every connected brokerage "
            "account, with sync freshness. Read-only and side-effect free. "
            "Exposure spans all accounts, including read-only ones, so a name "
            "held outside the agent-placeable account is still counted. "
            "Non-equity positions are reported as "
            "'unsupported_instrument_present' with their notional and are never "
            "omitted. A null cost basis means unknown, never zero. Check the "
            "'provenance' block: when 'stale' is true the ledger is older than "
            "its freshness budget and the numbers are the last good ones."
        ),
    )
    async def portfolio_overview(ctx: Context) -> dict:
        authorize_call(ctx, "portfolio_overview", {})
        return await anyio.to_thread.run_sync(_portfolio_overview)

    @mcp.tool(
        name="position_detail",
        description=(
            "One position across every account: quantity, tax lots where the "
            "broker exposes them, cost basis, and unrealized P&L. Read-only. "
            "Unrealized is null rather than zero wherever the basis is unknown. "
            "The linked thesis arrives with the research workspace and is null "
            "until then. Requires a `symbol` argument."
        ),
    )
    async def position_detail(ctx: Context, symbol: str = "") -> dict:
        # `symbol` carries a default so that the scope check runs *before*
        # argument validation. Declared required, the SDK's pydantic model
        # would reject an unauthorised call with a schema error, which tells an
        # anonymous caller about the tool's shape and skips the Spec K §4.1
        # call log entirely. The refusal below is the required-ness.
        authorize_call(ctx, "position_detail", {"symbol": symbol})
        if not (symbol or "").strip():
            raise ToolRefused("invalid_argument: symbol is required.")
        return await anyio.to_thread.run_sync(_position_detail, symbol)

    @mcp.tool(
        name="orders_open",
        description=(
            "Pending and recently filled orders across every connected broker. "
            "Read-only: this tool cannot place, modify, or cancel anything. An "
            "order placed by the owner in the broker's own app appears with "
            "origin='external' and no execution link."
        ),
    )
    async def orders_open(ctx: Context) -> dict:
        authorize_call(ctx, "orders_open", {})
        return await anyio.to_thread.run_sync(_orders_open)

    missing = [t for t in REGISTERED_TOOLS if t not in scope_module.TOOL_SCOPES]
    if missing:  # pragma: no cover - guarded by test_workspace_mcp too
        raise KeyError(
            f"tools {missing} have no entry in workspace.scopes.TOOL_SCOPES. "
            "Declare the scope before registering the tool."
        )


# --- ledger reads ----------------------------------------------------------
# Synchronous, and run on a worker thread by the tools above: the database
# session is blocking and holding the event loop through it would stall every
# other MCP call on the same connection.


def _ledger_settings():
    from config.settings import Settings

    return Settings()


def _budget_minutes() -> int:
    from portfolio.freshness import budget_from_settings

    return budget_from_settings(_ledger_settings())


def _portfolio_overview() -> dict:
    from database.db import get_session
    from portfolio import ledger

    with get_session() as session:
        return ledger.portfolio_overview(session, budget_minutes=_budget_minutes())


def _position_detail(symbol: str) -> dict:
    from database.db import get_session
    from portfolio import ledger

    with get_session() as session:
        return ledger.position_detail(session, symbol, budget_minutes=_budget_minutes())


def _orders_open() -> dict:
    from database.db import get_session
    from portfolio import ledger

    with get_session() as session:
        return ledger.orders_open(session, budget_minutes=_budget_minutes())
