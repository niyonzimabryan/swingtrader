"""The MCP tool surface. In Phase 0b that is exactly one read tool.

Spec K §4.2 fixes fifteen tools; Phases 1-4 build them. Shipping them early as
stubs would put tools in an agent's ``tools/list`` that return nothing useful,
which is worse than not having them, so the surface here is ``whoami`` and
nothing else — enough to prove that a client can attach, authenticate, and get
an answer end to end.

Every tool body starts with :func:`authorize_call`, which is where the scope
check, the rate limit, and the Spec K §4.1 call log live. Nothing enforces that
by construction, so ``tests/test_workspace_mcp.py`` enforces it by test: every
registered tool must refuse a token that lacks the scope its ``TOOL_SCOPES``
entry declares. A tool that forgets the call fails that test.
"""

from __future__ import annotations

from mcp.server.fastmcp import Context, FastMCP

from workspace import scopes as scope_module
from workspace.auth import AuthError, auth_from_scope, authorize, identity_from_scope

#: The tools this phase registers. Kept as a constant so ``/health`` and the
#: startup log need not await ``tools/list``; the test asserts it matches what
#: the server actually advertises.
REGISTERED_TOOLS: tuple[str, ...] = ("whoami",)


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
            "phase": "0b",
            "note": (
                "Order placement is not reachable by any token: there is no "
                "execute scope and no import path from this service to a broker."
            ),
        }

    missing = [t for t in REGISTERED_TOOLS if t not in scope_module.TOOL_SCOPES]
    if missing:  # pragma: no cover - guarded by test_workspace_mcp too
        raise KeyError(
            f"tools {missing} have no entry in workspace.scopes.TOOL_SCOPES. "
            "Declare the scope before registering the tool."
        )
