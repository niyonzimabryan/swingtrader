# Napkin Runbook

## Curation Rules
- Re-prioritize on every read.
- Keep recurring, high-value notes only.
- Max 10 items per category.
- Each item includes date + "Do instead".

## Execution & Validation (Highest Priority)
1. **[2026-07-05] Langfuse reads use split env files**
   Do instead: parse `~/.env` for Langfuse keys and repo `.env` for `LANGFUSE_BASE_URL` inside helper scripts before importing `evals.langfuse_api`.

## Shell & Command Reliability
1. **[2026-07-05] Avoid prod bot entrypoint locally**
   Do instead: do not run `python main.py`; use targeted scripts/tests to avoid Telegram polling conflicts with Railway.

## Domain Behavior Guardrails
1. **[2026-07-05] Existing audit owns baseline failures**
   Do instead: when writing audit addenda, only report new observation-level evidence beyond `docs/audits/2026-07-04-system-audit.md`.

## User Directives
1. **[2026-07-05] Langfuse is read-only**
   Do instead: use only GET endpoints and keep helper scripts outside the repo.
