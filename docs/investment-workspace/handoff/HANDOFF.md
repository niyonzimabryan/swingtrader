# Investment Workspace build — handoff

**Purpose.** Everything a person (or a fresh agent session in Cursor, Codex, or
Claude Code) needs to finish the build if the orchestrating session stops. It is
updated in every integration commit; the "Last updated" line says how fresh it
is. If it is more than a few hours old, trust `git log origin/main` and the
open-PR list over this file.

**Last updated:** 2026-09-10 08:22 UTC, by the orchestrating session
(`session_01F6Ca8hXxdGkaYhPQ6id9Q4`).

## 1. Where main is

| | |
|---|---|
| `main` head | `ee5bb46` (#68) |
| Alembic head | `0011_comparable_subject_ticker` (single) |
| Tests | ~1,700 on SQLite (3 Postgres-only skips); CI: 4 shards per engine, ~7 min |
| CI | `.github/workflows/ci.yml` — sqlite + postgres matrix, Python **3.12** |

Merged, in order: Phase 0a/0b, 3a, 3b-core, 3p, 1, 2, 4, P, Strategy Lab 1 (#54),
ENV_SETUP (#52), rulings (#55), **Phase 3c (#56), Phase 6 (#57), Strategy Lab 2
(#58), rulings (#59), handoff (#62), Strategy Lab 3 (#60), Sharadar reference (#63), evidenced-budget closure (#61), Strategy Lab 4 (#64), orchestrated-build skill (#67), OWNER_SETUP (#69), mirror-test fix (#71), CI sharding (#66), Sharadar direct-API port (#70), Strategy Lab 5 (#65), service-role guard (#68)**.

## 2. What is in flight

| PR / branch | What | Session | State |
|---|---|---|---|
| `claude/strategy-lab-6-tournament` | Strategy Lab 6: paper tournament through PR 5's entry point, audited promotion, `STRATEGY_LAB_PAPER_ENABLED`/`_LIVE_ENABLED`, E2E release proof, rollout checklist | `session_01KjN1qSEpxSgJxpHmLSgUqV` (Opus) | building since 08:20Z |

## 3. What is left, in order

1. Merge SL6 when it opens (integrate per §4; it may add a migration off
   `0011_comparable_subject_ticker`).
2. Docs PR: Spec Q rulings log (a "Rulings log" section at the end of
   `specs/investment-workspace/strategy-lab/strategy-lab-architecture.md`,
   same shape as Spec N §12) covering SL3–SL6; refresh `docs/ENV_SETUP.md`
   §10 and the order-of-operations list; note in Spec N §12 the Sharadar port's
   mapping decisions if #70 did not already.
3. The owner summary (§7 below is the skeleton).
4. Owner-side, in `docs/OWNER_SETUP.md` order: link the repo to the Railway
   `workspace` service (safe now that #68 is on `main`), the Postgres cutover,
   workspace variables + token, Sharadar backfill, paper trade submission.

Known follow-ups not blocking a usable system: rename `Settings.nasdaq_data_link_api_key`
(the adapter reads `SHARADAR_API_KEY` first); `memos` has no run id (PR 4 pairs
memo to ledger row by time); CSCV/PBO in `comparables/inference.py`; the
`analog_ranker` post-event feature; `docs/robinhood/tool_schemas.json` still
uncommitted (owner).

## 4. How to integrate a PR (the recipe that has worked ten times)

```
git fetch origin main                                  # ALWAYS first
git worktree add /tmp/wt-<name> origin/<branch>
cd /tmp/wt-<name> && git checkout -B <branch> origin/<branch>
git merge origin/main --no-edit
```

Resolve conflicts by these rules, never by blind textual union:

- `database/models.py`: rebuild as **main's file plus the branch's appended
  block** (every phase appends classes at the end; `git diff <base> <branch> --
  database/models.py` gives the block). Patch any new imports at the top.
- `workspace/tools.py`: keep main's `register()` / `registered_tools()` shape
  (`names = REGISTERED_TOOLS; if <flag>: names += ...`) and splice the branch's
  flag-gated registration in as one more `if`. Append the branch's helper
  functions at the end.
- `workspace/app.py`: keep main's shape; add the branch's health-field lines.
- `database/schema.py`: keep the replay-based adoption on main.
- `config/settings.py`, `.env.example`, `todoscratchpad.md`, docs: union.
- Two Alembic heads → `alembic merge -m "..." --rev-id 00NN_merge_<phase>
  <head1> <head2>`, rename the generated file to `00NN_merge_<phase>.py`, and
  list it in `migrations/README.md`.
- Tests that hard-code a migration head: make them graph-aware (see
  `tests/test_strategy_lab_migration.py`).

Validate on **Python 3.12** (CI's version; `sum()` of floats differs from 3.11
and has already flipped one test):

```
python3.12 -m venv /tmp/venv312 && /tmp/venv312/bin/pip install -r requirements.txt
/tmp/venv312/bin/python -m compileall -q .
/tmp/venv312/bin/python -m unittest discover -s tests -p "test_*.py"   # ~14 min; expect 3 skips
```

Push the merge commit to the PR branch, wait for CI (sqlite + postgres) to be
green, merge with a merge commit (not squash — the merge revisions matter).
Never weaken a test to get green.

## 5. Resuming a child session that hit the usage limit

Cloud sessions cannot message the parent. A child that shows
`post_turn_summary: "You've hit your session limit · resets HH:MM"` is idle,
not dead. From any session with the `Claude_Code_Remote` MCP: create a Routine
bound to it (`create_trigger` with `persistent_session_id`, either poke-only
and then `fire_trigger`, or `run_once_at` the reset time) whose prompt says:
run `git status` and `git log --oneline -5` first; if the container was
reclaimed, `git fetch` + checkout the branch and rebuild only what is not
pushed, from the transcript; commit and push after each coherent unit; then
finish the brief and open the PR. Without that MCP: open a fresh session with
the brief and tell it the branch already carries N commits.

## 6. Gotchas learned tonight

- Pushes to a PR branch trigger the `pull_request` workflow whoever pushes
  them (earlier note to the contrary was wrong); `workflow_dispatch` is also
  enabled. Local validation on 3.12 stays the first gate.
- The Postgres CI job was cancelled at exactly the 20-minute cap on #60; the
  cap is 40 now. The real fix is the CI-speed PR.
- `pkill -f` with the test command string kills your own shell.
- The PR bodies say "do not merge / for review" — that is the children's
  default courtesy; the owner's standing instruction is to merge everything
  that validates.

## 7. Owner actions to reach a testing state (unchanged from ENV_SETUP)

`docs/ENV_SETUP.md` "Order of operations" is the runbook. Open owner items:
commit `docs/robinhood/tool_schemas.json`; record real SEC / Robinhood / macro
fixtures from the laptop; confirm the Rule 10b5-1 element; name tracked
investors; buy Sharadar Prices, run the delisting audit and backfill; Postgres
cutover per `docs/POSTGRES_CUTOVER_RUNBOOK.md`; workspace service + token;
`COMPARABLE_BENCHMARK_SECURITY_UID`; `scripts/cohort_smoke.py` against prod;
the live `gtc stop_market` probe (`docs/EXECUTION_LIFECYCLE.md` §6);
`schema_status` against the prod DB.

## 8. Change log of this file

- 2026-09-10 05:20Z — created, after #56–#59 merged; #60/#61 open; SL5 and
  CI-speed building.
- 2026-09-10 05:35Z — #62 (this file) and #60 merged; SL4 spawned; #61 waiting
  on a Postgres CI run under the 40-minute cap.
- 2026-09-10 07:10Z — `data/prices/sharadar.py` ported from Nasdaq Data Link to
  Sharadar's direct API (`https://api.sharadar.com/v1.0`), against payloads
  recorded live with the public `test-api-key` under
  `tests/fixtures/sharadar_direct/`. Added `SharadarPricePlane.bulk_download`
  and `--bulk years=5|10|full` on `scripts/price_backfill.py` for the 10-year
  Prices tier. `PRICE_PLANE_SOURCE=sharadar` no longer needs the port; it
  needs `SHARADAR_API_KEY` (`docs/ENV_SETUP.md` §6).
