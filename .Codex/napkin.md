# Napkin Runbook

## Curation Rules
- Re-prioritize on every read.
- Keep recurring, high-value notes only.
- Max 10 items per category.
- Each item includes date + "Do instead".

## Execution & Validation (Highest Priority)
1. **[2026-07-04] Never run the live bot entrypoint locally**
   Do instead: use targeted tests, `pytest`, `unittest`, and compile checks; do not run `python main.py`.

## Shell & Command Reliability
1. **[2026-07-04] Work audit packages in linked worktrees**
   Do instead: create the requested `claude/audit-...` branch in a clean worktree so unrelated dirty files in the main checkout do not enter the PR.

## Domain Behavior Guardrails
1. **[2026-07-04] Broker interactions must be mocked in tests**
   Do instead: use fake broker clients/order objects for order-manager tests and never place real orders from local validation.

## User Directives
1. **[2026-07-04] One PR per audit spec**
   Do instead: keep changes surgical, stage only spec-relevant files, and target `main`.
