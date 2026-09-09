"""The MCP tool surface: Phase 0b's ``whoami``, Phase 1's three, Phase 4's three.

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

Phase 4 (Spec O) adds ``filings_recent``, ``macro_state`` and ``news_timeline``,
also at ``read``. They answer from ``source_observations`` and the plane tables
through ``filings.api`` / ``macro.api`` / ``news.api``, which reach a database
and nothing else — in particular no model client, which is what keeps "a model
may never produce a statistic" (Spec N §9) a structural property of this
surface too.

``news_timeline`` is the one tool here whose output carries a licence
constraint: every row it returns is marked ``mirror_allowed=false``, and its
provenance notes say so, because Alpaca's terms bar redistributing the data or
any derived products (Spec O §5). An agent may read it and cite a story by URL
and date; nothing may copy it into the research mirror.

Every read tool returns a ``provenance`` block carrying ``as_of_utc``, a
per-field source, ``stale``, and a ``data_quality`` tier. A tool whose ledger is
past the freshness budget returns the data **with the flag set** rather than
raising (Spec K §4.2). The opposite rule applies on the proposal side, where
``portfolio.freshness.require_fresh`` refuses instead — see
``portfolio/freshness.py`` for why the two directions are not a contradiction.

Phase 2 (Spec M) adds the five research tools when ``RESEARCH_WORKSPACE_ENABLED``
is on, and *only* then: a tool that answers "disabled" still tells a client this
workspace does research, and it does not.

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
    "filings_recent",
    "macro_state",
    "news_timeline",
)


def registered_tools(settings=None) -> tuple[str, ...]:
    """What a workspace built with ``settings`` advertises.

    ``/health`` and the startup log call this rather than reading
    :data:`REGISTERED_TOOLS`, because the surface is now flag-dependent and a
    health check that reports a fixed list would be reporting a guess.
    """
    from workspace.research_tools import RESEARCH_TOOLS

    if settings is not None and getattr(settings, "research_workspace_enabled", False):
        return REGISTERED_TOOLS + RESEARCH_TOOLS
    return REGISTERED_TOOLS


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


def register(mcp: FastMCP, settings=None) -> tuple[str, ...]:
    """Attach the tool surface to ``mcp``; returns the names registered."""

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

    @mcp.tool(
        name="filings_recent",
        description=(
            "Recent SEC filings facts for a ticker or CIK, as they were "
            "knowable at `as_of`: Form 4 insider transactions, Schedules "
            "13D/G, and 8-K items. Read-only and side-effect free. Insider "
            "transaction codes are never pooled — an open-market purchase (P) "
            "and a vesting award (A) are different fact types, and a 10b5-1 "
            "sale is flagged where the filing says so and reported as unknown "
            "where it does not. Amendments carry "
            "`superseded_observation_id`; the original stays in the answer "
            "because it is what was known before the amendment landed. 13F is "
            "not covered. Every timestamp is the filing's acceptance time, "
            "never its filing date."
        ),
    )
    async def filings_recent(
        ctx: Context, ticker: str = "", entity_cik: str = "", limit: int = 50
    ) -> dict:
        authorize_call(
            ctx,
            "filings_recent",
            {"ticker": ticker, "entity_cik": entity_cik, "limit": limit},
        )
        if not (ticker or "").strip() and not (entity_cik or "").strip():
            raise ToolRefused("invalid_argument: ticker or entity_cik is required.")
        return await anyio.to_thread.run_sync(
            _filings_recent, ticker, entity_cik, limit
        )

    @mcp.tool(
        name="macro_state",
        description=(
            "The macro picture as it stood on `as_of` (YYYY-MM-DD; omit for "
            "today), plus the deterministic `regime_v1` label. Read-only. "
            "Values are ALFRED vintages, so a past date returns what a person "
            "could have seen then, including the release lag: a series not yet "
            "published at `as_of` appears in `missing` and is absent, never "
            "back-filled or zero. The regime label is assigned by a versioned "
            "threshold rule over never-revised inputs — a model may describe "
            "it, never assign it."
        ),
    )
    async def macro_state(ctx: Context, as_of: str = "") -> dict:
        authorize_call(ctx, "macro_state", {"as_of": as_of})
        return await anyio.to_thread.run_sync(_macro_state, as_of)

    @mcp.tool(
        name="news_timeline",
        description=(
            "Timestamped, deduplicated news for a ticker as it was knowable at "
            "`as_of`. Read-only. One row per STORY, not per republication: "
            "twenty outlets running one wire story are one row, and its "
            "timestamp is the earliest publisher timestamp in the cluster. A "
            "fact extracted from an article carries that article's own "
            "timestamp instead. Articles whose publication time could not be "
            "established are quarantined and excluded unless "
            "`include_quarantined` is set. LICENCE: every row is "
            "`mirror_allowed=false` — cite a story by URL and date, and copy "
            "nothing else out."
        ),
    )
    async def news_timeline(
        ctx: Context,
        ticker: str = "",
        as_of: str = "",
        limit: int = 50,
        include_quarantined: bool = False,
    ) -> dict:
        authorize_call(
            ctx,
            "news_timeline",
            {"ticker": ticker, "as_of": as_of, "limit": limit},
        )
        if not (ticker or "").strip():
            raise ToolRefused("invalid_argument: ticker is required.")
        return await anyio.to_thread.run_sync(
            _news_timeline, ticker, as_of, limit, include_quarantined
        )

    names = REGISTERED_TOOLS
    if settings is not None and getattr(settings, "research_workspace_enabled", False):
        # Imported here rather than at module scope: `workspace.research_tools`
        # imports `authorize_call` and `ToolRefused` from this module, and a
        # top-level import either way would be a cycle.
        from workspace import research_tools

        names = names + research_tools.register(
            mcp, settings, authorize_call=authorize_call, ToolRefused=ToolRefused
        )

    missing = [t for t in names if t not in scope_module.TOOL_SCOPES]
    if missing:  # pragma: no cover - guarded by test_workspace_mcp too
        raise KeyError(
            f"tools {missing} have no entry in workspace.scopes.TOOL_SCOPES. "
            "Declare the scope before registering the tool."
        )
    return names


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


# --- evidence-plane reads (Spec O) -----------------------------------------
# Same shape as the ledger reads above, and on a worker thread for the same
# reason. Each delegates to the plane's own stable API, so the tool layer holds
# no query logic and the point-in-time rules stay in one place per plane.


def _parse_as_of(raw: str):
    """``YYYY-MM-DD`` or an ISO timestamp -> a cutoff, or ``None`` for now.

    A date means the **close** of that date, which is what the planes' own
    helpers do — so a tool call and a direct call agree about whether a print
    released that morning is visible.
    """
    from datetime import date, datetime, timezone

    value = (raw or "").strip()
    if not value:
        return None
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolRefused(
            f"invalid_argument: as_of={raw!r} is not a date (YYYY-MM-DD) or an "
            "ISO-8601 timestamp."
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _filings_recent(ticker: str, entity_cik: str, limit: int) -> dict:
    from database.db import get_session
    from filings.api import filings_recent as read

    with get_session() as session:
        return read(
            session,
            ticker=(ticker or "").strip().upper() or None,
            entity_cik=(entity_cik or "").strip() or None,
            limit=max(1, min(int(limit), 200)),
        )


def _macro_state(as_of: str) -> dict:
    from database.db import get_session
    from macro.api import macro_state as read

    cutoff = _parse_as_of(as_of)
    with get_session() as session:
        return read(session, as_of=cutoff)


def _news_timeline(
    ticker: str, as_of: str, limit: int, include_quarantined: bool
) -> dict:
    from datetime import date, datetime, timezone

    from database.db import get_session
    from news.api import news_timeline as read

    cutoff = _parse_as_of(as_of)
    if isinstance(cutoff, date) and not isinstance(cutoff, datetime):
        cutoff = datetime(
            cutoff.year, cutoff.month, cutoff.day, 23, 59, 59, 999999, tzinfo=timezone.utc
        )
    with get_session() as session:
        return read(
            session,
            ticker=ticker.strip().upper(),
            as_of=cutoff,
            limit=max(1, min(int(limit), 200)),
            include_quarantined=bool(include_quarantined),
        )
