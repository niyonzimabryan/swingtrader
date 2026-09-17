# Prompt for a local agent with the Railway CLI

Paste into a Claude Code / Codex session on the laptop that has `railway`
logged in and this repo checked out. Read-only except step 2.

```
You are doing three owner-side steps for the simplification in
docs/simplify/PLAN.md. Do them in order, print what you find, and stop.
Do not change any variable, and do not delete anything.

1. Link and inspect. `railway link` to project e556a6d9-2023-4c81-a031-e32e160a33be
   if not linked. `railway service` to list services. For each service print
   its name, start command, and status.

2. Stop the bot service (the one whose start command is `python main.py`).
   Use the Railway dashboard if the CLI cannot stop a service in this version;
   say which you did. Leave the workspace service (`python -m workspace.server`)
   running for now; it is needed for step 3 only if it holds DATABASE_URL.

3. Read, don't print in full: `railway variables` on each service. Report
   (a) whether SEC_USER_AGENT is set and its first 8 characters, (b) whether
   any variable name contains EDGAR, SEC_API, or ALPHASENSE, with the same
   8-char preview, (c) whether DATABASE_URL is set (yes/no only).

4. Final research export: with DATABASE_URL exported from step 3 into the
   shell (never written to a file), run
   `python scripts/sync_research_mirror.py` from the repo root, then
   `git status research/` and report how many files changed. Do not commit.

Report the four results as a short list. Nothing else.
```

The orchestrator uses the report to (a) confirm the runtime is stopped,
(b) know which EDGAR credential exists, (c) know the `research/` snapshot is
current before brief 5 copies it.
