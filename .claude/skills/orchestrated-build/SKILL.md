---
name: orchestrated-build
description: Run a multi-PR build as one orchestrator session that briefs worker sessions, reviews and merges their PRs, keeps a living handoff, and compounds context. Use when a piece of work is more than one PR, spans days or usage windows, or should keep going while the owner is away.
---

# Orchestrated build

One long-lived **orchestrator** session owns a build. It writes briefs, spawns
**workers** (one per PR), reviews and merges their PRs, records rulings, keeps a
handoff file current, and is the only thing that talks to the owner. Workers
build one PR each and stop.

The pattern is harness-agnostic. This file says what to do; the harness notes at
the end say how to do it here. `.codex/prompts/orchestrated-build.md` and
`.cursor/rules/orchestrated-build.mdc` are the same pattern for those clients.

## Roles

**Orchestrator** — the strongest model you can afford for judgment work: reading
PR bodies, resolving conflicts, deciding what to ratify. It holds the compounding
context of the build. It does not write feature code except integration glue.

**Worker** — one session per PR, model chosen for the task with cost in mind:
the best model where the change touches money, safety, statistics, or a
migration; a cheaper capable model for ports, CI, fixtures, docs, mechanical
refactors. Not a Claude model necessarily — whatever the harness offers.

**Owner** — hears about decisions that change the design, anything needing
credentials or production access, and milestone summaries. Nothing else.

## The loop

1. **Plan.** Turn the spec into a dependency graph of PRs with disjoint file
   ownership where possible. Write the briefs up front and commit them
   (`docs/<build>/handoff/briefs/`), so any client can spawn a worker by pasting.
2. **Spawn** every worker whose prerequisites are on `main`. Parallel where files
   don't overlap; tell each worker what else is in flight and which files it may
   not touch.
3. **Wait for the signal.** A worker's finished state is an open PR whose body is
   the review packet. If the harness lets a worker message the orchestrator, it
   does that on opening the PR; otherwise the orchestrator polls open PRs and
   worker status on a timer. Either way the PR body is the message.
4. **Integrate** (§ Integration recipe). Validate locally the way CI does, push
   the merge to the PR branch, merge on green.
5. **Record.** Update the handoff file in the integration commit. Ratify the
   worker's design rulings into the spec's rulings log with a docs PR.
6. **Close out.** Archive or end the worker session once its PR is merged. Delete
   any timer it created. A finished worker holds no running tasks.
7. Repeat until the graph is empty, then write the owner summary: what is on
   `main`, what is behind flags, what needs the owner, what was not verified.

## The brief (what every worker gets)

- Branch name, base (`main`), and the rule to **commit and push after each
  coherent unit** so an interruption loses nothing.
- Read list, in order, starting with the repo's agent instructions.
- Goal, the contract items numbered, and acceptance criteria.
- File ownership: what it owns, what is concurrently in flight and off-limits.
- Standing rules: flags default off, never weaken a test, no secrets, the
  validation command, the interpreter version CI uses.
- The PR: title, and a body with contract items ticked, what was exercised and
  what was not, migrations, deferred items with reasons, anything touched outside
  scope. **Open the PR as soon as the work is pushed and locally validated —
  never wait for CI or a background suite to finish first.** An idle session
  cannot observe a background job, so a worker that ends its turn "waiting for
  the suite" has stopped for good; three of six workers did exactly that and had
  to be poked. **Then stop.** No self-scheduled check-ins, no PR babysitting;
  the orchestrator merges and drives any fix cycle.

## Integration recipe

```
git fetch origin main                     # always first
git worktree add /tmp/wt-<name> origin/<branch>
cd /tmp/wt-<name> && git checkout -B <branch> origin/<branch>
git merge origin/main --no-edit
```

Resolve by rule, never by blind textual union:
- Append-only files (ORM models): rebuild as main's file plus the branch's
  appended block, patch imports.
- Registration lists (tool surfaces, routers): keep main's shape, splice the
  branch's flag-gated entry in as one more case.
- Config, examples, docs, scratchpads: union.
- Two migration heads: `alembic merge` (or the equivalent) and list it.
- Tests that hard-code a head: make them graph-aware, not wrong.
- **The identical-trailing-block trap.** When two branches each append a
  block that *ends* with byte-identical lines (two classes with the same
  `payload` property, two functions with the same epilogue), git treats the
  shared tail as common context and keeps one copy — the second block
  silently loses it and the file still compiles. A textual union hides this,
  so after resolving any append-append conflict, diff each side's symbols
  (`grep -n "def \|class "`) against the result and re-run both branches'
  tests before trusting it.

When two open PRs conflict with *each other*, stack them: integrate the second
on top of the first's branch before the first merges, run the full suite once
on the combined tree, and let the later `main` merge be a no-op. Merge the one
that adds a migration first.

Validate on CI's interpreter version (float `sum()` differs between Python
3.11 and 3.12 and has flipped a test); run the full suite in the background
with a generous timeout; push the merge commit to the PR branch; wait for CI;
merge with a merge commit so the join revisions survive.

## Resilience

- Usage-limit hits are expected. A worker's transcript survives; resume it
  rather than respawn (bind a timer to the session with a prompt: `git status`
  first, rebuild only what isn't pushed, keep pushing incrementally).
- The orchestrator keeps one timer as a floor (hourly), armed with the full
  procedure and current state in the prompt, so a cold resume knows what to do.
- Everything that matters is in the repo: handoff file, briefs, rulings. The
  handoff file says where `main` is, what is in flight (branch, session), what
  is left in order, the recipe, and the owner list. Update it in every
  integration commit.
- CI time grows with the suite; treat a job cancelled at its timeout as no
  signal, raise the cap once, and fix suite speed as its own PR.
- A worker that is idle with no PR is stuck, not thinking: poke it (a timer
  bound to its session, fired now) with "push everything, open the PR, stop".
- When a milestone completes, delete the orchestrator's own pending check-ins
  before writing the summary; a late one fires into a finished build and
  re-summarizes.

## Harness notes — Claude Code

- Spawn: `create_session` (Claude Code Remote MCP) with `source_revision: main`
  and `outcome_branch`; choose `model` per task. Workers cannot message the
  parent, so poll: `list_pull_requests` + `get_session` on each worker.
- Timer: `send_later` (one-shot) or `create_trigger` (cron); bind to a session
  with `persistent_session_id` to resume a worker; `fire_trigger` to poke now.
- Close out: `delete_trigger` for anything a worker armed; `archive_session`
  once its PR is merged.
- CI: poll the public check-runs endpoint from a background shell loop
  (`curl .../commits/<sha>/check-runs`, exit when none are pending or one is
  not `success`) — the loop's completion is a notification, an MCP call is not.
  Guard the loop on the run count too, so a re-triggered workflow (13 runs vs
  11) does not read as "done" early.
- Subagents (`Agent` tool) are fine for in-session research; they are not
  workers, because they die with the turn.
