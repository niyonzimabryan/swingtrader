# Strategy Lab — operator runbook and rollout checklist

**Status: every Strategy Lab flag is false in production.** Nothing in this
document has been executed. It is the procedure for turning the tiers on, one at a
time, each one an explicit owner decision with its own observation window.

The reasoning behind each rule is in
`specs/investment-workspace/strategy-lab/strategy-lab-architecture.md`; the
implementation is described in `docs/STRATEGY_LAB.md`; the flags are listed in
`docs/ENV_SETUP.md` §10. This file is the *operations* view: what to set, what to
look at, what a failure looks like, and how to get back.

---

## 1. The flag ladder

Each tier requires every gate below it **as well as** its own. A flag that is
absent, empty, or unparseable reads as false — absence never means live (Spec Q
§12 invariant 1).

| Flag | Default | Turning it on enables |
|---|---|---|
| `STRATEGY_LAB_ENABLED` | false | the read surface: `/experiments`, `/strategies`, `/strategy`, `/promotions`, the weekly scoreboard section |
| `STRATEGY_LAB_SHADOW_ENABLED` | false | the post-scan shadow pass and the 04:15 ET maturation job |
| `STRATEGY_LAB_UNIVERSE_ENABLED` | false | the one cross-sectional snapshot per cutoff (needs `PRICE_PLANE_ENABLED` and `universe_membership`) |
| `PHASE6_EXECUTION_ENABLED` | false | the proposal → approval → execution path at all (Spec L §6) |
| `STRATEGY_LAB_PAPER_ENABLED` | false | the paper dispatcher and the three execution jobs (resume / expire / reconcile) |
| `STRATEGY_LAB_LIVE_ENABLED` | false | the Strategy Lab's own live gate — on top of `ALLOW_LIVE_TRADING`, `EXECUTION_MODE=live`, **and `STRATEGY_LAB_PAPER_ENABLED`**, which live requires because the resume/expire/reconcile jobs are gated on it |
| `STRATEGY_LAB_LIVE_RISK_BUDGET` | `0.0` | a live champion's sizing. Zero means the champion can exist and place nothing |

Variables are set with `railway variables set KEY=VALUE` and are never committed.
The kill switch is **not** a variable: it is a database row, set with
`/live_kill on`, and it survives a restart.

## 2. Daily operator loop, once shadow is on

1. `/experiments` — are the arms the ones you expect, and in the tiers you expect?
2. `/strategy <slug>` — decision counts, matured counts, and every warning code
   beside the numbers. A figure with no sample size next to it is a bug, not a
   result.
3. Read the funnel lines in the logs: `strategy_lab_shadow_funnel` (decisions,
   abstentions, refusals) and, once paper is on, `strategy_lab_paper_funnel`
   (candidates, proposed, blocked, skipped).
4. Once paper is on: `/orders`, and any `⚠️` page. A page carries recovery text,
   not only an error.

## 3. What a failure looks like, and what to do

| Symptom | What it means | Action |
|---|---|---|
| `strategy_snapshot_blocked` in the funnel | a snapshot could not be built for a name (missing or stale input) | nothing. The decision is not made rather than made badly. Check the price plane if it is every name. |
| every arm `abstain` with `missing_dependency` | the price plane is empty, so only the compatibility arm can decide | backfill prices (`docs/ENV_SETUP.md` §6) or leave the three cross-sectional arms abstaining — it is recorded, not hidden |
| `strategy_lab_paper_funnel` `blocked=N` | a gate refused N proposals. The reason code is in `skipped` | read the code. `kill_switch_engaged`, `strategy_lab_paper_enabled_false`, and `capability_refused` are the common three |
| `strategy_execution_blocked` page | one execution was refused before placement | nothing is reserved and nothing was placed. Clear the named condition; the arm proposes again on its next decision |
| `live_protection_failed` page | a fill could not be protected | **all new entries are already blocked.** Protect or close the position in the broker's own app, then resolve the execution |
| `broker_reconciliation_mismatch` page | the ledger and the broker disagree | new entries are blocked. Compare in the broker's app, correct the side that is wrong, re-run the reconciliation job. Do **not** place a compensating order from the bot |
| a `proposed` execution that never resolves | the approval card was never tapped | the hourly expiry job terminates it and frees the decision's slot |
| `placement_unknown` | the broker's response was ambiguous | the reservation is held and new entries are blocked until reconciliation proves whether an order exists. This is correct; do not clear it by hand |

**Stopping everything, in order of bluntness:**

1. `/live_kill on` — blocks every approval-to-placement immediately, survives a
   restart, cancels nothing already at the broker.
2. `/pause_experiment` — the arms stop producing decisions and stop dispatching.
3. `STRATEGY_LAB_PAPER_ENABLED=false` / `STRATEGY_LAB_LIVE_ENABLED=false` — the
   tier stops existing on the next restart; in-flight rows stay where they are
   for the resume pass to resolve.
4. `STRATEGY_LAB_ENABLED=false` — the whole lab goes quiet. Memos and the
   existing scan are unaffected at every step above.

Rollback at every phase is a flag change plus pausing arms. No migration needs
reversing: PR 6 added none.

## 4. Promotion procedure

1. `/strategy <slug>` and the weekly scorecard. Read the **warnings**, not the
   headline return. `insufficient_evidence` means the sample is too small, and it
   is printed instead of a ranking on purpose.
2. Acknowledge the evidence snapshot's warnings. A promotion whose evidence
   warnings have not been acknowledged is refused.
3. `/promote_arm <source_arm_id> <tier> <reason>`. Read the card: the source arm's
   immutable version, the evidence snapshot and its floor, the **proposed inactive
   target arm**, and the budget. Every refusal is listed — all of them, not the
   first.
4. Tap Confirm within the TTL (`STRATEGY_LAB_PROMOTION_TTL_SECONDS`, 300s). The
   confirmation is single-use, owner-bound, expiring and signed; a restart drops
   it and you re-run the command.
5. `/promotions` to see the appended event.

A promotion is never an approval of an entry. Each proposed execution gets its own
signed, expiring, single-use Approve/Reject card.

To reverse one: `/demote_arm <arm> <tier> <reason>`, which activates the lower
arm and stands the demoted one down. The original event is **not** deleted —
`promotion_events` is append-only, and a correction is another event.

---

# 5. The production rollout checklist (DRAFT — not executed)

Each step is an owner decision. Do not batch them; the observation window between
them is the entire point, and every step after the first is cheap to defer.

### Step 0 — preconditions (no flags change)

- [ ] `main` carries PR 6 and CI is green on both engines.
- [ ] Railway has deployed; the bot restarted cleanly (no `schema_mismatch` line).
- [ ] `alembic current` on the production database is `0011_comparable_subject_ticker`.
      **PR 6 adds no migration**, so this should be unchanged from before it.
- [ ] `python -m scripts.schema_status` against the production database agrees.
- [ ] A database backup exists (`docs/POSTGRES_CUTOVER_RUNBOOK.md`).

### Step 1 — shadow only

- [ ] `STRATEGY_LAB_ENABLED=true`
- [ ] `STRATEGY_LAB_SHADOW_ENABLED=true`
- [ ] Next scan: `/experiments` shows one experiment, four arms, all `shadow`.
- [ ] `strategy_lab_shadow_funnel` shows decisions and, where the price plane is
      empty, `missing_dependency` abstentions. Both are expected.
- [ ] Memo generation, the ledger row and the notifications are unchanged. If any
      of them moved, turn the flag back off first and investigate second.

### Step 2 — the observation window (no flags change)

- [ ] **At least 60 calendar days and 100 matured decisions** before considering
      paper (Spec Q §10's shadow minimum). This is a *minimum*, not a target.
- [ ] The 04:15 ET maturation job settles decisions; `strategy_trades` grows with
      `mode=shadow` rows reaching `closed`.
- [ ] Run `scripts/strategy_lab_scoreboard.py` weekly. Clean and
      `archival_reconstructed` results are printed separately; never combine them.
- [ ] Watch for: an arm that never decides, an arm that always decides, correlated
      arms (the overlap view), and any warning code that appears every week.

### Step 3 — the cross-sectional arms (optional, anytime after step 1)

- [ ] `PRICE_PLANE_ENABLED=true`, prices backfilled, `universe_membership`
      populated, the delisting audit run.
- [ ] `STRATEGY_LAB_UNIVERSE_ENABLED=true`.
- [ ] One universe snapshot per cutoff, shared by both cross-sectional arms.

### Step 4 — paper execution

Preconditions: step 2's minimums met, and Phase 6 paper already exercised by hand
(`docs/ENV_SETUP.md` §9 step 7).

- [ ] `EXECUTION_APPROVAL_SECRET` set (a card cannot be minted without it).
- [ ] `PHASE6_EXECUTION_ENABLED=true`, `EXECUTION_MODE=paper`.
- [ ] Review the paper book settings *before* the flag: `STRATEGY_LAB_PAPER_EQUITY`,
      `_RISK_BUDGET`, `_MAX_OPEN_POSITIONS`, `_MAX_POSITION_FRACTION`,
      `_DAILY_NOTIONAL`, `_MAX_PROPOSALS_PER_RUN`.
- [ ] `/promote_arm <shadow_arm> paper <reason>` for **one** arm, and confirm.
      One arm first: a tournament of four that all misbehave at once is four
      incidents.
- [ ] `STRATEGY_LAB_PAPER_ENABLED=true`.
- [ ] Next scan: `strategy_lab_paper_funnel` shows `proposed=N`, and an approval
      card arrives in Telegram. **Nothing is placed until you tap Approve.**
- [ ] Approve one. Watch it reach `protected` — entry filled, stop placed, stop
      read back from Alpaca. Then `/orders`.
- [ ] Confirm the three jobs registered: the log line at boot names
      `resume */30m, expire hourly, reconcile 16:45 ET`.
- [ ] Let the expiry job terminate one unapproved card, on purpose, and confirm
      the decision's slot frees.
- [ ] Promote the remaining arms only after the first has closed several
      executions cleanly.

### Step 5 — the paper observation window

- [ ] **At least 30 closed executions with zero unresolved reconciliation
      events** (Spec Q §10's paper minimum).
- [ ] Execution slippage: intended versus filled price, from the scorecard.
- [ ] No `reconciliation_required` row left standing; no `protection_failed`.
- [ ] A deliberate restart during market hours, to watch the resume pass resolve
      an in-flight execution without placing an entry.

### Step 6 — Robinhood review-only canary

This is the step that decides whether live is possible at all.

- [ ] `BROKER_PRIMARY=robinhood`, a dedicated Agentic account,
      `ROBINHOOD_ACCOUNT_NUMBER` set, OAuth bootstrapped.
- [ ] `EXECUTION_MODE=review_only`. **`ALLOW_LIVE_TRADING` stays false.**
- [ ] Run the real `gtc stop_market` probe — `docs/EXECUTION_LIFECYCLE.md` §6 —
      against the live account and record the result. Until this passes, the
      adapter's `can_place_standalone_gtc_stop` declaration is unverified, and
      Spec Q §12's closing paragraph means **no Strategy Lab live entry may be
      opened**: the capability gate refuses before an order is formed, by design,
      with no flag to flip and no manual-exit fallback.
- [ ] Confirm capabilities are recorded for the account and that the promotion
      card's live section stops reporting `exit capability cannot be verified`.
- [ ] `STRATEGY_LAB_LIVE_ENABLED` stays **false** through this entire step.

### Step 7 — the micro-live canary (SEPARATELY AUTHORIZED)

Do not start this step as a continuation of step 6. It is a distinct decision
with money on it, and Spec Q requires explicit owner authorization of the budget.

- [ ] Written authorization of the canary budget, recorded in the journal.
- [ ] `ROBINHOOD_MAX_ORDER_NOTIONAL` and `ROBINHOOD_MAX_DAILY_NOTIONAL` at their
      micro values (defaults 5 and 10).
- [ ] `STRATEGY_LAB_LIVE_RISK_BUDGET` set to a deliberate non-zero number.
- [ ] `ALLOW_LIVE_TRADING=true`, `EXECUTION_MODE=live`.
- [ ] `/live_kill off` — and know that `/live_kill on` is one message away.
- [ ] `/promote_arm <paper_arm> live <reason>` and confirm. This replaces the one
      global champion atomically.
- [ ] `STRATEGY_LAB_LIVE_ENABLED=true`.
- [ ] **One** order, watched by a human from entry through protection to closure.
      Approve exactly one card and then stop.
- [ ] Verify: the entry filled, the protective stop was placed **and read back**,
      the reservation released on closure, the ledger and the broker agree at the
      16:45 reconciliation.
- [ ] Keep human approval for every entry. Entry autonomy is a separate spec and
      a separate approval, and nothing in this build provides it.

### At every step

- Rollback is a flag change plus `/pause_experiment`. Existing memo generation
  remains available throughout.
- Never weaken a gate to get a step to pass. A refusal is the gate working.
- A step that produces a page is not complete until the page's recovery
  instruction has been followed and the condition is gone.
