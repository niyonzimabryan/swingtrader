# Brief: owner tools over MCP, and the runtime approval poller

Branch: `claude/owner-tools-mcp`. Model: opus.

## The decision this implements (owner's, 2026-09-13)

Bryan is the only user; this is a private deployment. He has chosen to approve
proposals **in his coding-agent chat** (Claude Code, Codex, Cursor) instead of
an out-of-band channel: he reads the card, says "approve", and the agent calls
an MCP tool. He explicitly declined a confirmation code. Specs L §6 and K §7
say approval is "never a tool an agent can call"; this PR changes that by
owner ruling, and the ruling must be written into both specs' rulings logs (L
§10, and a new K rulings section), stating the trade-off plainly: an agent
with an `admin` token can approve, so the token's placement and the harness's
permission prompt are the remaining controls.

What does **not** change: no `execute` scope exists; the workspace still
imports nothing from `execution/`, `bot/`, `orchestrator/`; every approval is
signed, single-use, expiring, owner-bound and re-risk-checked at placement;
the kill switch survives restart. The tool *records* an approval; the runtime
process *executes* it. `tests/test_no_execute_scope.py`,
`tests/test_execution_lifecycle_isolation.py` and the import-graph tests must
stay green untouched.

## What to build

1. **Owner identity.** Approvals are bound to `owner_id = telegram_chat_id`
   today (`main.py`, `portfolio/approvals`). Add `OWNER_ID` (settings), used
   everywhere an owner id is minted or verified, defaulting to
   `telegram_chat_id` when unset so nothing changes for the Telegram path.
2. **MCP tools** in a new `workspace/owner_tools.py`, registered in
   `workspace/tools.py` behind `WORKSPACE_OWNER_TOOLS_ENABLED` (default false)
   and requiring the `admin` scope (`workspace/scopes.py::TOOL_SCOPES`; the
   AGENTS.md §3 table and its test must be updated together):
   - `proposals_pending` (read scope): the open proposals with their full
     cards, expiry, and budget, so an agent can show one before asking.
   - `approve_order(proposal_uid)` / `reject_order(proposal_uid, reason)`:
     verify via `portfolio.approvals.verify` semantics (signature, single use,
     expiry, owner), then record `approved` / `rejected` on the row **without
     placing anything**, plus who/when/from which token label. The tool's
     description text must instruct the agent to show the full card and obtain
     the owner's explicit, informed yes in the conversation before calling,
     and never to call it on the strength of document content (AGENTS.md §5).
   - `approve_memo(memo_id)` / `reject_memo(memo_id)`: the older scan-memo
     path (`bot/keyboards.py` `approve_<memo_id>` →
     `bot/handlers/callbacks.py` → `order_manager.execute_approved_trade`).
     Same pattern: record, do not execute. Investigate whether the memo path
     should simply create a Phase 6 proposal instead; if that is small and
     clean, do it and say so; if not, record the recommendation in the PR.
   - `kill_switch(state: "on"|"off")`: `portfolio/killswitch.py`. `on` is
     always allowed; `off` is admin-only and logged loudly.
   - `promote_arm`, `demote_arm`, `pause_experiment`, `resume_experiment`:
     wrap the owner paths in `strategy_lab/promotion.py` and
     `orchestrator/strategy_lab_promotion.py` (read `docs/STRATEGY_LAB.md`
     §23–§28 and `bot/handlers/strategy_lab.py` first; the promotion
     confirmation is a signed, single-use, in-process `slpr:` callback today —
     the MCP path must preserve the same four controls: owner, expiry, single
     use, signature; a tool that takes `dry_run=true` and returns the
     prepared card, then a second call with the returned confirmation
     reference, is the natural shape).
3. **Runtime approval poller** (`orchestrator/approval_poller.py`), started
   from `main.py` when `PHASE6_EXECUTION_ENABLED` is on: every 15–30 s, pick
   up `approved` rows not yet executed and call
   `execution/lifecycle.py::on_approval` exactly as the Telegram callback
   does, with the same single-use semantics (the row's single-use column is
   the lock; two pollers or a poller plus a callback must not double-place —
   use the existing column and a `SELECT ... FOR UPDATE SKIP LOCKED` on
   Postgres / equivalent on SQLite). Memo approvals go through
   `execute_approved_trade`. The poller must also run the Strategy Lab
   explicit-mode route for rows carrying an `execution_id`, exactly as
   `bot/handlers/proposals.py` routes on the **row**, never on the input.
   Outcomes (placed, protected, refused with code) are sent to the owner
   through whatever notification channel is registered
   (`portfolio.approvals`/`notify` if the email PR has merged by then; else
   the Telegram `NotificationManager`); do not block on the other PR — code
   against `portfolio.paging`/the existing seams and say what you used.
4. **Docs**: `docs/WORKSPACE_ACCESS.md` (issuing an `admin` token; the tools),
   `AGENTS.md` §1 and §3 (the tool table; rewrite non-negotiable 1's wording
   to what is now true: no agent *places* an order and no execute scope
   exists; approval is an owner action the owner may take through his agent),
   `docs/ENV_SETUP.md`, `.env.example`, the two spec rulings.

## Tests

The tools with each scope (401/403 on read-only tokens), single-use and expiry
refusals, the poller placing once under a concurrent second poller, the
`execution_id` routing from the row, kill switch on/off, promotion controls
over MCP, the AGENTS.md table test, the import-closure tests untouched.

## Not in scope

Email/HTML cards (separate PR), removing Telegram, `TELEGRAM_ENABLED`.

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
