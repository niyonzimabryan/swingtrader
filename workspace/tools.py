"""The MCP tool surface.

Spec K §4.2 fixes fifteen tools; Phases 1-4 build them. Shipping them early as
stubs would put tools in an agent's ``tools/list`` that return nothing useful,
which is worse than not having them, so a tool appears here only when the phase
that owns it has something to answer with.

Phase 0b shipped ``whoami``. Phase 3c adds ``compare_setups`` and
``cohort_detail`` (Spec N §8, §11) **behind ``COMPARABLE_SETUPS_ENABLED``,
which defaults false**. When the flag is off the tools are not registered at
all: an unregistered tool is a clearer refusal than a registered one that
answers "disabled", and an agent's ``tools/list`` should describe what the
server can actually do.

Every tool body starts with :func:`authorize_call`, which is where the scope
check, the rate limit, and the Spec K §4.1 call log live. Nothing enforces that
by construction, so ``tests/test_workspace_mcp.py`` enforces it by test: every
registered tool must refuse a token that lacks the scope its ``TOOL_SCOPES``
entry declares. A tool that forgets the call fails that test.
"""

from __future__ import annotations

from datetime import date

from mcp.server.fastmcp import Context, FastMCP

from utils.logger import get_logger
from workspace import scopes as scope_module
from workspace.auth import AuthError, auth_from_scope, authorize, identity_from_scope

log = get_logger("workspace")

#: Registered unconditionally. Kept as a constant so ``/health`` and the startup
#: log need not await ``tools/list``; the test asserts it matches what the
#: server actually advertises.
REGISTERED_TOOLS: tuple[str, ...] = ("whoami",)

#: Registered only when ``COMPARABLE_SETUPS_ENABLED`` is true (Spec N).
COMPARABLE_TOOLS: tuple[str, ...] = ("compare_setups", "cohort_detail")


def tools_for(settings=None) -> tuple[str, ...]:
    """The tool names this configuration actually advertises."""
    if settings is not None and getattr(settings, "comparable_setups_enabled", False):
        return REGISTERED_TOOLS + COMPARABLE_TOOLS
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
    """Attach the tool surface to ``mcp``; return what was registered."""

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
            "phase": "0b",
            "note": (
                "Order placement is not reachable by any token: there is no "
                "execute scope and no import path from this service to a broker."
            ),
        }

    registered = tools_for(settings)
    if getattr(settings, "comparable_setups_enabled", False):
        _register_comparables(mcp, settings)

    missing = [t for t in registered if t not in scope_module.TOOL_SCOPES]
    if missing:  # pragma: no cover - guarded by test_workspace_mcp too
        raise KeyError(
            f"tools {missing} have no entry in workspace.scopes.TOOL_SCOPES. "
            "Declare the scope before registering the tool."
        )
    return registered


# --------------------------------------------------------------------------- #
# Spec N: the comparable-setups tools
# --------------------------------------------------------------------------- #


def cohort_context(settings):
    """The stored world a cohort is built from, out of configuration.

    Every field is required and none is guessed: a cohort has to be able to
    name the universe it drew from, the price snapshot it ran on, and the
    benchmark its abnormal returns were measured against (Spec N §4.2, §5.2).
    A missing benchmark is a configuration error, not a default.
    """
    from comparables.cohort import CohortContext

    return CohortContext(
        universe_slug=settings.comparable_universe_slug,
        price_snapshot_slug=settings.comparable_price_snapshot,
        benchmark_security_uid=settings.comparable_benchmark_security_uid,
        cik_by_ticker=parse_cik_map(getattr(settings, "comparable_cik_map", "")),
    )


def parse_cik_map(raw: str) -> tuple[tuple[str, str], ...]:
    """`"AAPL:0000320193,MSFT:0000789019"` -> `(("AAPL", "0000320193"), ...)`.

    A malformed entry raises rather than being skipped: silently dropping a
    ticker means every event for that name is refused a market-cap decile, and
    the reader would see a composition change with no cause.
    """
    out: list[tuple[str, str]] = []
    for chunk in (raw or "").split(","):
        item = chunk.strip()
        if not item:
            continue
        ticker, _, cik = item.partition(":")
        if not ticker.strip() or not cik.strip():
            raise ValueError(
                f"COMPARABLE_CIK_MAP entry {item!r} is not 'TICKER:CIK'"
            )
        out.append((ticker.strip().upper(), cik.strip()))
    return tuple(out)


def _parse_as_of(raw: str | None) -> date:
    if not raw:
        return date.today()
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ToolRefused(
            f"invalid_argument: as_of must be an ISO date (YYYY-MM-DD), got {raw!r}"
        ) from exc


def bind_citation_seam() -> str:
    """Wire `comparables.citations.resolve` into Phase 2's citation registry.

    Spec M's `research_workspace/citations.py` is the seam `journal_append`
    resolves a cited answer through. It had not merged when Phase 3c landed, so
    the binding is attempted and its absence reported rather than assumed:
    `comparables/citations.py::resolve` is the seam until then, and
    `journal_append` can call it directly.
    """
    from comparables import citations as cohort_citations

    try:
        from research_workspace import citations as research_citations
    except ImportError:
        return "comparables.citations.resolve (research_workspace not present)"
    register = getattr(research_citations, "register", None)
    if register is None:  # pragma: no cover - depends on Phase 2's shape
        return "comparables.citations.resolve (research_workspace has no register())"
    register(cohort_citations.CITATION_PREFIX, cohort_citations.RESOLVER)
    return "research_workspace.citations"


def _register_comparables(mcp: FastMCP, settings) -> None:
    """`compare_setups` and `cohort_detail` (Spec N §8, §11)."""
    from comparables import citations as cohort_citations
    from comparables import query as query_mod
    from comparables import setups as roster
    from comparables.cohort import CohortConstructionError
    from comparables.setup_spec import SetupSpecError

    seam = bind_citation_seam()
    log.info("comparable_setups_tools_registered", citation_seam=seam,
             roster=list(roster.slugs()))

    def _session():
        from database.db import get_session

        return get_session()

    @mcp.tool(
        name="compare_setups",
        description=(
            "How have setups genuinely like this one performed, and how sure "
            "are we? Builds a point-in-time cohort from stored facts and "
            "returns the Spec N §8 answer: status (ok | inconclusive | "
            "insufficient), evidence tier, n, distinct event dates, confidence "
            "intervals, balance diagnostics and the number of setup variants "
            "already tried against this fact pattern. "
            "`insufficient` is a valid, expected and frequently-correct answer "
            "and must be rendered as a refusal, never rounded into a hedge. "
            "Pass a roster slug (`setup`) with optional `parameters`, or an "
            "explicit `setup_spec`. `depth='quick'` is fast and NOT citable; "
            "`depth='full'` is mandatory for anything journal_append, "
            "research_write or a strategy promotion will reference. "
            "Read-only: it stores the query it was asked, and nothing else."
        ),
    )
    async def compare_setups(
        ctx: Context,
        setup: str | None = None,
        parameters: dict | None = None,
        setup_spec: dict | None = None,
        as_of: str | None = None,
        depth: str = "quick",
    ) -> dict:
        identity = authorize_call(ctx, "compare_setups", {
            "setup": setup, "as_of": as_of, "depth": depth,
        })
        day = _parse_as_of(as_of)
        try:
            with _session() as session:
                outcome = query_mod.answer_query(
                    session,
                    context=cohort_context(settings),
                    as_of=day,
                    depth=depth,
                    setup=setup,
                    parameters=parameters,
                    setup_spec=setup_spec,
                    requester_label=identity.label,
                    reps=(
                        settings.comparable_quick_bootstrap_reps
                        if depth == "quick"
                        else settings.comparable_full_bootstrap_reps
                    ),
                )
                payload = outcome.payload()
        except query_mod.QueryRefused as exc:
            return {
                "status": exc.status,
                "refusal_reason": exc.message,
                "roster": list(roster.slugs()),
                "note": (
                    "This is a refusal, not an `insufficient` answer: the "
                    "engine did not look, because there was nothing to look at."
                ),
            }
        except (SetupSpecError, roster.UnknownSetup) as exc:
            raise ToolRefused(f"invalid_argument: {exc}") from exc
        except CohortConstructionError as exc:
            raise ToolRefused(f"unavailable: {exc}") from exc

        payload["roster"] = list(roster.slugs())
        return payload

    @mcp.tool(
        name="cohort_detail",
        description=(
            "The constituent events behind a stored cohort answer: each "
            "event's ticker, the instant it became knowable, its provenance "
            "class (observed_live | vendor_pit | archival_reconstructed), its "
            "matched covariates, how its history ended, and its outcome at "
            "every horizon — matured or censored, with the reason. Also lists "
            "the candidates that were excluded and why. Takes the `query_id` "
            "or the `citation_id` that compare_setups returned. Read-only."
        ),
    )
    async def cohort_detail(
        ctx: Context,
        query_id: int | None = None,
        citation_id: str | None = None,
    ) -> dict:
        authorize_call(ctx, "cohort_detail", {
            "query_id": query_id, "citation_id": citation_id,
        })
        if query_id is None and not citation_id:
            raise ToolRefused(
                "invalid_argument: pass the query_id or the citation_id that "
                "compare_setups returned. Every answer this engine gives carries "
                "the id of the query that produced it."
            )
        try:
            with _session() as session:
                resolved_id = query_id
                if resolved_id is None:
                    resolved = cohort_citations.resolve(
                        session, citation_id, require_citable=False
                    )
                    resolved_id = resolved.query_id
                    if resolved_id is None:
                        raise ToolRefused(
                            f"unavailable: {citation_id} resolves to a stored "
                            f"answer that carries no query id"
                        )
                return query_mod.cohort_detail(
                    session,
                    context=cohort_context(settings),
                    query_id=int(resolved_id),
                )
        except cohort_citations.CitationError as exc:
            raise ToolRefused(f"invalid_argument: {exc}") from exc
        except query_mod.QueryRefused as exc:
            raise ToolRefused(f"{exc.status}: {exc.message}") from exc
        except CohortConstructionError as exc:
            raise ToolRefused(f"unavailable: {exc}") from exc
