# Brief: the system overview document

Branch: `claude/system-overview-doc`. Model: sonnet. Docs only — no code.

Write `docs/SYSTEM_OVERVIEW.md`: the one document a technical reader (the
owner, six months from now, or a collaborator) reads to understand what this
system is, how it is put together, what it does on its own, what needs a
human, and what is and is not verified in production. It will also be
published to Google Drive, so it must stand alone without the repo.

## Sources, in priority order

1. `specs/investment-workspace/` K–Q and `specs/investment-workspace/strategy-lab/`,
   including every "Rulings log" section (they record where the build
   diverged from the spec).
2. The merged PR bodies on GitHub for #54–#78 — they are unusually careful
   records of what was built, verified, and deferred. Use `gh pr view <n>`
   or the GitHub MCP tools if `gh` is unavailable.
3. `AGENTS.md`, `CLAUDE.md`, `docs/*.md` (especially `STRATEGY_LAB.md`,
   `EXECUTION_LIFECYCLE.md`, `ENV_SETUP.md`, `OWNER_SETUP.md`,
   `investment-workspace/handoff/HANDOFF.md` and
   `OWNER_SETUP_EXECUTION_2026-09-12.md`).
4. The code, to confirm anything the docs claim.

## Shape (≈ 4–6k words; diagrams as Mermaid fenced blocks)

1. What it is, in one page: a paper-first swing-trading bot plus an
   investment research workspace that coding agents attach to over MCP.
2. Architecture: the three processes (bot runtime, workspace API/MCP,
   Postgres on Railway), the packages and what each owns, the import
   boundary that keeps agents away from brokers, and one diagram.
3. Data: the ledger, snapshots, price plane (Sharadar), evidence planes
   (filings, macro vintages, news), what is point-in-time and why.
4. The research loop (Spec P): recall, frame, gather, attack, situate,
   record, propose — with the tool surface table and the subagents.
5. Comparable setups (Spec N): what a SetupSpec is, what a cohort answer is,
   what "insufficient" means, evidenced vs discretionary budgets.
6. Execution (Spec L §6, Phase 6): propose → approve → place → protect, the
   risk re-check, the kill switch, paper vs live gates.
7. Strategy Lab (Spec Q): arms, tiers, snapshots, replay, scorecard,
   promotion; what is on and what the shadow clock means.
8. Operations: Railway services, flags, scheduled jobs, what runs when,
   notifications, how to attach a client, how to roll back.
9. **Honesty section**: what has never run against a real broker, what is
   fake-broker-only, what is blocked (bulk backfill OOM, SPY benchmark,
   delisting audit symbols, macro fixtures), known follow-ups.
10. Glossary.

## Rules

- Every non-obvious claim carries a pointer: a spec section, a file path, or
  a PR number. No number that is not in a source. Where sources disagree,
  the spec's rulings log wins, then the PR body, then the code; say so.
- Do not paraphrase the four non-negotiables loosely; quote AGENTS.md.
- Describe the system as it is on `main` today, not as planned; planned work
  goes in one clearly labelled short section.
- Run `python -m unittest tests.test_agent_layer` before pushing (it asserts
  documentation invariants) and `python -m compileall -q .`.
- Open the PR and stop.

## Worker rules (every brief)

- Read `AGENTS.md`, then `docs/investment-workspace/handoff/HANDOFF.md`, before
  writing code. The four non-negotiables are asserted by tests; never weaken a
  test to pass. `mcp` stays pinned `<2`. No secrets anywhere in the diff.
- Work on the branch named in this brief, from `origin/main` (`git fetch origin
  main` first). Commit after each coherent unit; push often.
- Validate on Python 3.12 (CI's version): `python -m compileall -q .` and
  `python -m unittest discover -s tests -p "test_*.py"` (~14 min; 3 Postgres-only
  skips are expected). CI is sharded; `scripts/test_shard_weights.json` and the
  `test-count-check` job exist — if you add test modules, run
  `python scripts/ci_shard.py --help` and follow it so the count check passes.
- Every new capability ships behind a flag defaulting **off**. Production must
  not change behaviour when this PR merges with no variable set.
- When done: open the PR against `main` with a body that states what was built,
  every design decision you made where the spec was silent, what was verified
  and how, what was NOT verified, and what you deferred and why. Then **stop**.
  Do not schedule check-ins, do not subscribe to the PR, do not merge. The
  orchestrating session reviews and merges.
- If you hit a usage limit, the orchestrator will resume you; keep the branch
  pushed so nothing is lost.
