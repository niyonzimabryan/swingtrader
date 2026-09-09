@AGENTS.md

# Claude-specific notes

`AGENTS.md` above is the shared entry point and holds everything that is true in
every client: the four non-negotiables, the session loop, the tool surface and
its scopes, how to attach, the untrusted-content rule, the sizing budgets, and
the repo workflow. Only material that is specific to Claude Code belongs below.

## Subagents

The six briefs in `.claude/agents/` are Claude Code subagent definitions and run
natively here — `thesis-critic` on `opus`, the rest on `sonnet`, each with an
explicit `tools` allowlist and a `maxTurns` bound. Invoke them by name; do not
paste their bodies into the main turn, because the front-matter is what enforces
the tool scope and the turn bound, and a paste keeps neither.

`.claude/` is gitignored except `.claude/agents/`, which is committed on purpose:
the briefs are repo files so Codex and Cursor can mirror them. Local settings
under `.claude/` stay untracked.

## MCP

`.mcp.json` is project-scoped and committed, so opening the repo is the whole
setup once `WORKSPACE_BASE_URL` and `WORKSPACE_TOKEN` are exported. Claude Code
expands `${VAR}` in that file — verified by observation, 2026-09-09. Check the
connection with `/mcp`, then ask for `whoami`.

Workspace tools appear as `mcp__swingtrader-workspace__<tool>`.

## Deployment

- **Never run the bot locally while Railway is active.** Telegram allows one
  polling connection; stop the Railway service from the dashboard first.
- Railway project `e556a6d9-2023-4c81-a031-e32e160a33be`, auto-deploying from
  `main` on `niyonzimabryan/swingtrader`. The workspace API/MCP is its own web
  service, so a workspace deploy never restarts the trading monitor.
- State of record is Postgres on Railway (`DATABASE_URL`), with the narrative
  research mirror as Markdown under `research/`.
- Environment variables are set with `railway variables set KEY=VALUE`, never
  committed. Logs with `railway logs`.

## Repo map

- `workspace/` — the FastAPI service: `/health`, `/v1`, `/mcp`, `/admin`. Its
  import closure never reaches `execution/`, `bot/`, or `orchestrator/`, and
  `tests/test_no_execute_scope.py` asserts that statically.
- `research_workspace/` — dossiers, theses, invalidators, journal, git mirror.
- `portfolio/` — the ledger, sync, reconciliation.
- `comparables/` — the cohort engine. Every number in an answer comes from here.
- `filings/`, `data/` — the evidence planes and vendor adapters.
- `execution/`, `bot/`, `orchestrator/` — the trading path. Off-limits to the
  workspace and to anything an agent session can reach.
- `specs/investment-workspace/` — the K–Q specs behind all of the above.
