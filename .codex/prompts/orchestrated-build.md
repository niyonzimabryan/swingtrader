# Orchestrated build — Codex

Paste this as the first message of a Codex session that will act as the
**orchestrator** for a multi-PR build. The pattern is the one in
`.claude/skills/orchestrated-build/SKILL.md`; read that file first, it is the
source of truth for the loop, the brief, the integration recipe and resilience.
This file is only what differs in Codex.

## What differs

- **Workers.** Codex has no session-spawn primitive from inside a session. Start
  each worker as a separate Codex thread (cloud task or local), pasting the
  brief from `docs/<build>/handoff/briefs/<pr>.md`. Choose the model per task
  with cost in mind: the strongest model where the change touches money, safety,
  statistics or a migration; a cheaper capable one for ports, CI, fixtures,
  docs.
- **Signal.** If the worker thread can post back (a comment on the PR, a message
  to the orchestrator thread, a note in the handoff file), tell it to do that on
  opening the PR. If it cannot, the orchestrator polls: `gh pr list` and the
  worker's PR body. The PR body is the review packet either way.
- **Timers.** Codex has no scheduler inside a session; run the orchestrator
  loop on demand, or drive it from cron on your machine
  (`codex exec "<the check-in prompt>"`). Put the full procedure and the
  current state in that prompt so a cold start knows what to do.
- **Close out.** A worker stops when its PR is open. Nothing it started should
  keep running; close the thread when the PR merges.

## The check-in prompt (template)

> Check-in for the <build> build. Read `docs/<build>/handoff/HANDOFF.md`.
> List open PRs; for each, read the body and CI; integrate per HANDOFF §4
> (fetch main first, worktree, merge, resolve by rule, validate on CI's
> interpreter, update HANDOFF in the same commit, push, merge on green).
> Spawn the next worker for any brief whose prerequisites are now on main.
> Record rulings into the spec logs via a docs PR. Stay silent unless
> something changed or the owner is needed.
