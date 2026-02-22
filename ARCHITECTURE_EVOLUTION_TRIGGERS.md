# Architecture Evolution Triggers

Purpose: keep the system simple now, but define exact conditions for when infrastructure upgrades should happen.

## Current Baseline (Feb 22, 2026)

- Single user / single operator workflow
- Single Railway replica
- SQLite database
- Scheduled scans plus occasional ad-hoc `/test` and admin commands
- Implemented now: blocking-handler offloading + post-catalyst parallel stages with auto stability controller

## What "Trigger" Means

A trigger is a measurable condition that, once hit, moves an upgrade from "nice to have" to "implement now."

Default rule:
- If a trigger is met 3 times in 14 days, or is sustained for 7 consecutive days, schedule the upgrade.

## Metrics To Track

Track these in logs first (no heavy observability stack needed):

- Command latency per command (`/ask`, `/regime`, `/score`, `/performance`, `/test`, `/scan`)
- Full scan duration
- API rate-limit errors (429) and timeout errors by provider
- SQLite lock/busy errors
- Background backlog signals (missed scan window, long-running tasks)

## Trigger Matrix

### 1) Offload blocking Telegram handlers

Trigger:
- `/ask`, `/regime`, `/score`, or `/performance` takes longer than 3 seconds to first response more than 10% of the time
- Or bot feels unresponsive while one heavy command is running

Action:
- Move handler work to executor/background wrapper with immediate "started" response.

### 2) Parallelize post-catalyst agent stages

Trigger:
- `/test` median latency > 45s for 7 days
- Or `/test` p95 latency > 90s
- Or scheduled scan runtime consistently exceeds planned window

Action:
- Run fundamental + pattern + web-research in parallel after catalyst gate.
- Use bounded concurrency, timeout budgets, and fail-open behavior.

### 3) SQLite hardening (WAL, busy timeout, hot indexes)

Trigger:
- Any observed SQLite lock/busy errors in normal operation
- Or command/query latency starts drifting as trades/memos grow

Action:
- Enable WAL mode and busy timeout.
- Add indexes for known hot filters (`trades.status`, `trades.exit_date`, `memos.status`, `memos.created_at`).

### 4) SQLite to Postgres migration

Trigger:
- Need for >1 app replica or >1 write-heavy worker process
- Or persistent lock contention after SQLite hardening
- Or operational need for stronger multi-writer guarantees

Action:
- Migrate DB engine to Postgres with migration tooling and staged cutover.

### 5) Redis introduction

Trigger:
- Need shared cross-instance queue/caching/rate-limit state
- Or multiple worker services require shared fast coordination

Action:
- Introduce Redis only for specific jobs (queue, distributed rate-limit, or shared cache), not as a blanket dependency.

## Parallel Execution Guardrails (Implemented)

- Normal mode concurrency cap = 3 workers, degraded mode cap = 2 workers
- Per-agent timeout budgets:
  - fundamental = 180s
  - pattern = 300s
  - web_research = 300s
- No aggressive orchestration retries for timed-out stages (avoid duplicate load storms)
- If one agent times out/fails, continue pipeline with controlled fallback output instead of aborting run
- Auto-degrade trigger: 3 bad runs inside last 12 runs (bad run = 2+ failed/timed-out stages)
- Degraded cooldown = 20 runs; recover after 8 consecutive healthy runs

Why cap is not "number of agents":
- Agent count is a code fact; cap is a traffic-control choice.
- Bounded concurrency protects API quotas and lowers instability risk while still improving latency.

## Decision Owner and Review Cadence

- Owner: operator (you) + implementing coding agent
- Review cadence: monthly, or immediately after any incident with missed scans, lock errors, or large latency jump

## Implementation Priority Once Triggered

1. SQLite hardening
2. Postgres migration (only when multi-writer pressure appears)
3. Redis (only for concrete cross-instance needs)
