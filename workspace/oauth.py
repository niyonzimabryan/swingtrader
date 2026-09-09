"""OAuth 2.1 for the workspace endpoint — designed, not implemented.

**TODO (Phase 1 or when a client requires it): implement this.** Today the
service accepts a static bearer only, which is enough for Codex (whose
``bearer_token_env_var`` is verified), for ``curl``, and for cron. Claude Code's
documented remote-MCP path is OAuth 2.1 with dynamic client registration and a
loopback redirect, and claude.ai connectors are reported to accept a static
header but that is not confirmed on an Anthropic page (Spec K §4.1). So the
static path ships now and this one is written down so it can be built without
re-deciding it.

The design, so the work is mechanical when it happens:

* **Roles.** The workspace is the *resource server*. It is also the
  *authorization server*, because there is exactly one user and adding an IdP
  would be more moving parts than the thing it protects.
* **Discovery.** ``/.well-known/oauth-protected-resource`` (RFC 9728) names the
  authorization server; the 401 on an unauthenticated request carries
  ``WWW-Authenticate: Bearer resource_metadata="..."`` so a client can find it.
  Only that document is served today, and only when
  ``WORKSPACE_OAUTH_ENABLED`` is true — advertising endpoints that 404 is worse
  than advertising nothing.
* **Registration.** RFC 7591 dynamic client registration, open but rate
  limited; a registered client is a public client with PKCE (S256) and no
  secret.
* **Grant.** Authorization code + PKCE. The consent step is the owner pasting
  an ``admin``-scoped token into the approval form: the same secret that
  already governs this service, not a second identity system.
* **Tokens.** Short-lived access tokens (1 hour) with a refresh token, signed,
  carrying the same four scopes as the static tokens. **The scope model does
  not change and no ``execute`` scope appears here either** — that is the whole
  point of having one scope module.
* **Storage.** Registered clients and refresh tokens are two more Alembic
  tables, branching from whatever the head is at the time.

The MCP SDK ships the server-side pieces (``mcp.server.auth``), so this is
wiring and storage rather than protocol work; ``fastmcp`` is not needed for it.
"""

from __future__ import annotations

from workspace.scopes import SCOPES


def protected_resource_metadata(base_url: str) -> dict:
    """RFC 9728 metadata for the workspace as a protected resource.

    Served only when ``WORKSPACE_OAUTH_ENABLED`` is true. The authorization
    server it names is this same service, whose token and registration
    endpoints are **not implemented yet** — the flag exists so the document can
    be inspected and iterated on without pretending the flow works.
    """
    base = (base_url or "").rstrip("/")
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "scopes_supported": list(SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{base}/docs/WORKSPACE_ACCESS.md",
        "x_status": "not_implemented: static bearer only in Phase 0b",
    }
