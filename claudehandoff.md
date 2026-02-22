# Claude Handoff - Fast-Path Parallelism + Handler Offloading

Date: 2026-02-22
Repository: `/Users/bryanniyonzima/Downloads/AppsinTesting/swingtrader`

## 1) Context and objectives from this conversation

The operator requested a stability-first implementation of two concrete architecture upgrades that improve perceived speed without risky rewrites:

1. Keep Telegram responsive by offloading blocking command work.
2. Speed up post-catalyst analysis by parallelizing independent stages.

Additional decision points from conversation that were explicitly resolved:

- Keep timeout budgets generous; timeout is a client-side execution limit and does not change model reasoning quality.
- Add automatic stability control (degrade/recover) so safeguards trigger without manual intervention.
- Keep cache-first reuse deferred (potential later optimization, not a concrete TODO now).
- Keep SQLite hardening, Redis, and Postgres migration deferred and trigger-based.
- Produce a detailed handoff file documenting exact implementation and deferred triggers.

## 2) What was implemented vs deferred

Implemented now:

1. Offloaded blocking Telegram handlers for `/ask`, `/regime`, `/score`, `/performance`.
2. Added shared blocking helper with timeout handling and safe error behavior.
3. Parallelized post-catalyst stages (`fundamental`, `pattern`, `web_research`) in both:
   - ad-hoc flow (`run_ad_hoc`, used by `/test`)
   - scan flow (`_process_scan_item`, used by `/scan`)
4. Added per-stage timeout budgets from settings and controlled fallback outputs on stage timeout/error.
5. Added auto stability controller:
   - bad run definition: 2+ failed/timed-out stages
   - degrade trigger: 3 bad runs in last 12
   - degraded cooldown: 20 runs
   - recovery: 8 consecutive healthy runs
6. Added state-change alerting via existing bot-loop notification pattern.
7. Added env-backed config for all new parallel/stability knobs.
8. Updated architecture trigger guide and scratchpad to reflect implemented/deferred decisions.

Deferred (not implemented now):

1. SQLite WAL/index hardening.
2. Redis introduction.
3. SQLite to Postgres migration.
4. Cache-first same-day ticker reuse.

## 3) Full per-file change list with rationale

### `/Users/bryanniyonzima/Downloads/AppsinTesting/swingtrader/bot/handlers/_blocking_utils.py` (new)

- Added `run_blocking(operation, fn, timeout_s)` to run sync work in executor with `asyncio.wait_for`.
- Added `BlockingCallTimeout` custom exception.
- Purpose: one shared offload/timeout pattern for command handlers.

### `/Users/bryanniyonzima/Downloads/AppsinTesting/swingtrader/bot/handlers/ask.py`

- Added executor offload path for AI call and context build.
- Added immediate progress response (`Thinking...`) then final response.
- Added command timeout handling (`ASK_TIMEOUT_S=210`) with user-safe retry message.
- Kept output format unchanged.

### `/Users/bryanniyonzima/Downloads/AppsinTesting/swingtrader/bot/handlers/commands.py`

- Updated `/regime` handler to:
  - send immediate ack (`Refreshing macro regime...`)
  - offload analysis in executor (`REGIME_TIMEOUT_S=120`)
  - return timeout-safe fallback message
- Extracted sync formatter helper `_build_regime_text(...)`.
- Kept existing MarkdownV2 output layout.

### `/Users/bryanniyonzima/Downloads/AppsinTesting/swingtrader/bot/handlers/test_idea.py`

- Updated `/score` handler to:
  - send immediate ack
  - offload fundamental analysis in executor (`SCORE_TIMEOUT_S=120`)
  - return timeout-safe fallback message
- Extracted sync formatter helper `_build_score_text(...)`.

### `/Users/bryanniyonzima/Downloads/AppsinTesting/swingtrader/bot/handlers/performance.py`

- Updated `/performance` handler to:
  - send immediate ack (`Generating performance dashboard...`)
  - offload dashboard aggregation in executor (`PERFORMANCE_TIMEOUT_S=180`)
  - return timeout-safe fallback message
- Extracted sync helper `_build_performance_text(...)`.
- Preserved existing dashboard content format.

### `/Users/bryanniyonzima/Downloads/AppsinTesting/swingtrader/config/settings.py`

- Added env-backed settings for:
  - parallel enable/scope
  - normal/degraded worker counts
  - per-stage timeout budgets
  - auto-degrade/recovery thresholds
  - alert toggle

### `/Users/bryanniyonzima/Downloads/AppsinTesting/swingtrader/.env.example`

- Added matching `PARALLEL_*` keys with comments for operator tuning.

### `/Users/bryanniyonzima/Downloads/AppsinTesting/swingtrader/orchestrator/pipeline.py`

- Added import of `ThreadPoolExecutor`, `FutureTimeoutError`, `deque`, `time`, and `AgentOutput`.
- Added runtime health state (`self._parallel_health`) in pipeline init.
- Replaced sequential post-catalyst stage execution with shared helper:
  - `_run_post_catalyst_agents(...)`
  - used by both `_process_scan_item(...)` and `run_ad_hoc(...)`
- Added `_fallback_stage_output(...)` to fail-open with controlled neutral-ish output.
- Added scope/worker helpers:
  - `_parallel_scope_enabled(...)`
  - `_get_parallel_workers(...)`
- Added auto stability controller:
  - `_update_parallel_health(...)`
  - `_announce_parallel_mode_change(...)` with log + Telegram system message via `run_coroutine_threadsafe`.
- Added structured per-stage logging (`post_catalyst_stage_result` with status, timeout budget, latency, worker mode, and timeout/error detail).

### `/Users/bryanniyonzima/Downloads/AppsinTesting/swingtrader/ARCHITECTURE_EVOLUTION_TRIGGERS.md`

- Updated trigger guide to reflect implemented state and current guardrails:
  - normal workers=3, degraded workers=2
  - timeout budgets
  - no aggressive orchestration retries
  - auto degrade/recover thresholds

### `/Users/bryanniyonzima/Downloads/AppsinTesting/swingtrader/todoscratchpad.md`

- Marked completed architecture items:
  - blocking handler offload
  - post-catalyst parallelization
  - automatic stability controller
- Left deferred items (cache-first, SQLite hardening, Postgres trigger) explicitly open.

## 4) New env vars and defaults

Added in settings and `.env.example`:

- `PARALLEL_AGENTS_ENABLED=true`
- `PARALLEL_AGENTS_SCOPE=both` (`ad_hoc` | `scan` | `both`)
- `PARALLEL_WORKERS_DEFAULT=3`
- `PARALLEL_WORKERS_DEGRADED=2`
- `PARALLEL_TIMEOUT_FUNDAMENTAL_S=180`
- `PARALLEL_TIMEOUT_PATTERN_S=300`
- `PARALLEL_TIMEOUT_WEB_RESEARCH_S=300`
- `PARALLEL_AUTO_DEGRADE_ENABLED=true`
- `PARALLEL_BAD_RUN_WINDOW=12`
- `PARALLEL_BAD_RUN_COUNT_TRIGGER=3`
- `PARALLEL_COOLDOWN_RUNS=20`
- `PARALLEL_RECOVERY_GOOD_RUNS=8`
- `PARALLEL_ALERT_ON_STATE_CHANGE=true`

## 5) Failure-mode and fallback behavior

Handler offloading:

- `/ask`, `/regime`, `/score`, `/performance` send immediate ack first.
- If blocking work exceeds command timeout:
  - handler returns explicit timeout message
  - bot loop remains responsive
  - no handler crash
- Non-timeout exceptions are logged and surfaced with bounded error text.

Post-catalyst parallel stages:

- Each stage has its own timeout budget.
- On stage timeout/error:
  - stage gets fallback `AgentOutput` (`fallback=true`, low confidence, neutral direction)
  - pipeline continues to scoring/memo path (no full-run abort)
- No aggressive orchestration retry loop for timed-out stages.

Auto stability controller:

- A run is bad when 2+ stage outcomes are `timeout`/`error`.
- Mode transitions happen automatically in memory:
  - normal -> degraded on threshold breach
  - degraded -> normal after cooldown + healthy streak
- On transition:
  - warning log emitted
  - Telegram system message sent when notification manager and bot loop are available

## 6) Test steps executed and observed results

Executed:

1. Syntax validation (pass):

   ```bash
   python -m py_compile \
     bot/handlers/_blocking_utils.py \
     bot/handlers/ask.py \
     bot/handlers/commands.py \
     bot/handlers/test_idea.py \
     bot/handlers/performance.py \
     config/settings.py \
     orchestrator/pipeline.py \
     main.py \
     bot/telegram_bot.py
   ```

   Observed result: no syntax errors.

2. Runtime smoke script for internal pipeline behavior (blocked by local env dependency):

   - Attempted to run targeted no-network smoke checks for degrade/recover + fallback behavior.
   - Observed failure: `ModuleNotFoundError: No module named 'sqlalchemy'` in this execution environment.

What this means:

- Static correctness is verified.
- Full runtime validation of behavior should be executed in the project runtime environment where dependencies are installed.

## 7) Known limitations and rollback instructions

Known limitations:

1. Stability health state is in-memory only; restarts reset rolling history/mode.
2. Timed-out thread work cannot be force-killed at Python thread level; timeout stops waiting and pipeline continues, but provider call may still finish in background.
3. Fallback stage outputs still flow into scoring (intended fail-open behavior).
4. Runtime integration tests were not executed locally due missing `sqlalchemy` dependency in this shell.

Rollback instructions:

Low-risk config rollback (no code revert):

1. Disable parallel stages: set `PARALLEL_AGENTS_ENABLED=false`.
2. Or limit scope: set `PARALLEL_AGENTS_SCOPE=scan` or `ad_hoc`.
3. Disable auto mode changes: set `PARALLEL_AUTO_DEGRADE_ENABLED=false`.
4. Disable alerts only: set `PARALLEL_ALERT_ON_STATE_CHANGE=false`.

Code rollback:

1. Revert changed files listed in Section 3 and remove `bot/handlers/_blocking_utils.py`.
2. Remove added `PARALLEL_*` env keys from deployment config.

## 8) Follow-up triggers

Operational triggers and deferred upgrade criteria are documented in:

- `/Users/bryanniyonzima/Downloads/AppsinTesting/swingtrader/ARCHITECTURE_EVOLUTION_TRIGGERS.md`

Implementation decisions to revisit using that trigger matrix:

1. SQLite hardening (WAL + busy timeout + indexes).
2. Postgres migration when multi-writer/replica pressure appears.
3. Redis only for concrete shared-state needs.
4. Cache-first repeated-analysis path only if same-day re-query frequency becomes meaningful.
