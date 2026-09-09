# Attaching a client to the workspace

The workspace is one HTTPS endpoint that every agent client attaches to, so the
same question gets the same answer from a laptop, a cloud session, or a phone
([spec K](../specs/investment-workspace/K-workspace-core-and-tool-surface.md)).
This is how to attach.

**Phase 0b state:** the service is a skeleton. It serves `/health`, a `/v1`
REST namespace, and MCP over streamable HTTP at `/mcp`, with exactly one tool —
`whoami`. The fifteen tools in spec K §4.2 arrive in Phases 1–4. Attaching now
is worth doing anyway: it proves the token, the transport, and the client
configuration work before there is anything at stake in the answer.

---

## 1. Get a token

Tokens are issued against the workspace database, so run this where
`DATABASE_URL` points at it:

```bash
python -m scripts.workspace_token --issue --label "claude-code"
python -m scripts.workspace_token --issue --label "codex-laptop" --scopes read
python -m scripts.workspace_token --list
python -m scripts.workspace_token --revoke --label "codex-laptop"
```

One token per client, named after the client. The label is what appears in every
log line, so two clients must not share one.

The secret is printed **once**. Only its SHA-256 digest is stored, so a lost
token is re-issued, never recovered.

Scopes are `read`, `research:write`, `propose`, and `admin`. **There is no
`execute` scope** — no token can place a broker order, and
`tests/test_no_execute_scope.py` asserts that structurally, by proving the
workspace's import graph never reaches `execution/`. Default is `read`.

An `admin` token is for you, not for an agent. Spec K §4.1: it is never placed
in an agent's environment.

## 2. Set two environment variables

```bash
export WORKSPACE_BASE_URL="https://<service>.up.railway.app"
export WORKSPACE_TOKEN="swt_..."
```

Every configuration file below reads them, so no secret is committed. Put them
in your shell profile, or in `.env` (which is gitignored) for local runs.

## 3. Attach

### Claude Code (local or cloud)

`.mcp.json` is committed at the repo root and is project-scoped, so opening the
repo is the whole setup. Claude Code expands `${WORKSPACE_BASE_URL}` and
`${WORKSPACE_TOKEN}` from your environment when it reads the file; approve the
server when prompted.

```jsonc
{
  "mcpServers": {
    "swingtrader-workspace": {
      "type": "http",
      "url": "${WORKSPACE_BASE_URL}/mcp",
      "headers": { "Authorization": "Bearer ${WORKSPACE_TOKEN}" }
    }
  }
}
```

Check it with `/mcp`, then ask the session to call `whoami`.

### Codex (local or cloud)

`.codex/config.toml` in the repo holds the entry. Codex reads
`~/.codex/config.toml`; if it does not pick the repo file up, copy the block
across.

```toml
[mcp_servers.swingtrader_workspace]
url = "${WORKSPACE_BASE_URL}/mcp"
bearer_token_env_var = "WORKSPACE_TOKEN"
```

`bearer_token_env_var` names the variable rather than holding the secret, which
is why the file is safe to commit. **Unverified:** whether Codex expands `${…}`
inside `url`. If the connection fails, paste the literal URL.

### Cursor (local or background agent)

`.cursor/mcp.json` is committed with the same server entry. **Unverified:**
Cursor's environment-variable expansion in that file. If it does not connect,
replace the two placeholders with literals in your local copy — and do not
commit that copy.

### claude.ai connector

Add the server URL `${WORKSPACE_BASE_URL}/mcp` as a custom connector.

**Unverified, and the known gap:** claude.ai connectors are reported to accept a
static `Authorization` header, but that is not confirmed on an Anthropic page
(spec K §4.1). Their documented path is OAuth 2.1 with dynamic client
registration, which this service **does not implement yet** —
`workspace/oauth.py` carries the design and a TODO, and
`WORKSPACE_OAUTH_ENABLED` (default false) only advertises protected-resource
metadata. So if the connector rejects a static bearer, that is expected, and the
answer is to build the OAuth path, not to work around it.

### curl, scripts, cron

```bash
curl -s "$WORKSPACE_BASE_URL/health" | jq .
curl -s -H "Authorization: Bearer $WORKSPACE_TOKEN" \
     "$WORKSPACE_BASE_URL/v1/whoami" | jq .
```

`/health` needs no token, on purpose: a healthcheck that needs a credential is a
healthcheck that fails for the wrong reason.

## What you get back

`whoami` — over MCP or as `GET /v1/whoami` — returns the token's label, its
scopes, and the fact that no `execute` scope exists. It is the "am I attached to
the right workspace with the access I expect" check, and it is deliberately
boring.

## When it does not work

| Symptom | Cause |
| --- | --- |
| `503 workspace_api_disabled` | `WORKSPACE_API_ENABLED` is false on the service. `/health` still answers; nothing else does. |
| `401 missing_token` | No `Authorization: Bearer` header arrived. Check the variable is exported in the shell the client actually runs in. |
| `401 invalid_token` | Unknown or revoked token. `--list` shows which. |
| `403 insufficient_scope` | The token lacks the scope the tool declares. Re-issue with the scope; do not widen the tool. |
| `429 rate_limited` | 60 read or 10 write calls in a minute on one token (spec K §4.1). `Retry-After` says how long. Usually an agent loop, not a limit that is too low. |
| `/health` reports `"reachable": false` | The service cannot reach Postgres. Check `DATABASE_URL` on the workspace service. |

## Verifying it once, by hand

Spec K §9 asks that the same three questions — holdings, thesis, cohort —
answer identically from Claude Code, a cloud session, and Codex, verified by
hand once and recorded here. Those three tools do not exist until Phases 1–3, so
that verification cannot be done yet and this table is the placeholder for it:

| Client | Date | `whoami` | `portfolio_overview` | `research_get` | `compare_setups` |
| --- | --- | --- | --- | --- | --- |
| Claude Code (local) | | | Phase 1 | Phase 2 | Phase 3 |
| Claude Code (cloud) | | | Phase 1 | Phase 2 | Phase 3 |
| Codex | | | Phase 1 | Phase 2 | Phase 3 |
| Cursor | | | Phase 1 | Phase 2 | Phase 3 |
| claude.ai connector | | | Phase 1 | Phase 2 | Phase 3 |

Fill the `whoami` column in at the cutover
([`POSTGRES_CUTOVER_RUNBOOK.md`](POSTGRES_CUTOVER_RUNBOOK.md) Phase D). An empty
cell is an honest "not tried"; do not fill one in from expectation.
