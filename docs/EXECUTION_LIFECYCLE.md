# Execution lifecycle — proposal → approval → placement → protection

**Spec:** L §5.1 (Robinhood order semantics), §6 (the execution path and its six
invariants), §6.6 (sizing and the two budgets); K §4.1–§4.2 (`propose_order` at
scope `propose`; no execute scope); Q §12 (the mandatory live-order invariants).
**Status:** Phase 6. Ships behind `PHASE6_EXECUTION_ENABLED`, default off. No
live order can be placed without `ALLOW_LIVE_TRADING=true`, `EXECUTION_MODE=live`,
a recorded owner approval, and the kill switch off — and even then only in the
Robinhood Agentic account.

This is the one path in the repository that can reach live capital. It is split
across a boundary on purpose: an **agent proposes**, a **human approves out of
band**, and **code — not an agent — executes**.

---

## 1. The boundary, in one picture

```
 agent session                     workspace service                 bot process
 (Claude/Codex/…)                  (FastAPI + MCP)                    (Telegram)
       │                                  │                                │
       │  propose_order (scope propose)   │                                │
       ├─────────────────────────────────►│                                │
       │                                  │ portfolio.proposals             │
       │                                  │  · freshness, account, caps     │
       │                                  │  · citation rule + §6.6 sizing   │
       │                                  │  · writes a `proposals` row      │
       │                                  │  · signs a single-use reference  │
       │                                  │  · sends the approval card ──────┼──► owner's phone
       │  ◄── proposal row (places nothing)│      (Telegram HTTPS)           │
       │                                  │                                 │  owner taps ✅
       │                                  │                                 │
       │                                  │        execution.lifecycle ◄─────┤  callback (bot only)
       │                                  │         · verify signature/       │
       │                                  │           single-use/expiry/owner │
       │                                  │         · re-run risk from FRESH   │
       │                                  │           state (never the stored  │
       │                                  │           numbers)                 │
       │                                  │         · kill switch + flags      │
       │                                  │         · review → place entry     │
       │                                  │         · poll fill                 │
       │                                  │         · place gtc stop_market     │
       │                                  │         · READ THE STOP BACK        │
       │                                  │         · protected / unprotected   │
       │                                  │              │                      │
       │                                  │              └──► Robinhood Agentic  │
       │                                  │                   (live) or Alpaca   │
       │                                  │                   paper              │
```

Two import-graph facts hold this together, and both are asserted statically
(`tests/test_no_execute_scope.py`, `tests/test_portfolio_import_graph.py`,
`tests/test_execution_lifecycle_isolation.py`):

* The **workspace** — everything reachable from the MCP surface, `propose_order`
  included — imports no `execution/`, `bot/`, `orchestrator/`, or `agents/`
  module, and names no placement call. `propose_order` calls
  `portfolio.proposals`, which lives on the ledger side of the boundary so that
  it can be reached from the workspace without dragging a broker in.
* `execution/lifecycle.py` is **not** reachable from the workspace, and its entry
  point `on_approval` is called from exactly one file — `bot/handlers/proposals.py`,
  the out-of-band channel. No MCP tool and no REST route can trigger it.

---

## 2. The state machine

```
proposed ──► risk_rejected                                   [terminal, returned with a reason]
   │
   │  owner approval (signed, single-use, expiring, owner-bound)
   ▼
approved ──► risk_rejected (re-evaluated at approval; the book moved)   [terminal]
   │
   ▼
submitted ──► failed (broker review or placement refused; no order)     [terminal]
   │      └─► reconciliation_required (unknown placement outcome)        [blocks entries]
   ▼
filled
   │
   ├─► protected      (the gtc stop_market was READ BACK from the broker)
   │
   └─► unprotected    (stop not read back within PROTECTION_WINDOW_SECONDS)
                       → pages immediately, BLOCKS all further entries
```

`expired` and `cancelled` are the other two terminals: an approval reference that
was never used, and a proposal withdrawn before approval.

`protected` is the **only** state in which a live position is considered safe.
It is reached only after the protective stop is read back from
`get_equity_orders` — never because a placement call returned. The window
between the entry filling and that read-back is where a position is uninsured,
and it is the whole reason the two-method adapter contract (`place_stop` **and**
`read_open_orders`) exists.

---

## 3. What each guard refuses, and why

Guards run in this order in `portfolio.proposals.evaluate`, most-structural
first. A refusal is written as a **returned** `risk_rejected` row with a reason
(Spec L §6.4) — never a silently dropped idea — except the two malformed-input
cases (a `quantity` argument, a short side), which refuse before a row exists.

| Guard | Refuses when | Code | Why |
|---|---|---|---|
| freshness | ledger older than `PORTFOLIO_FRESHNESS_BUDGET_MINUTES` | `stale_ledger` | a stale position size is a real trade against a book that no longer exists |
| account | target account has `agent_placeable=False`, or none exists | `account_not_agent_placeable` / `no_agent_placeable_account` | placement is confined to the Agentic account (Spec L §5.1) |
| capability | account can place neither an attached nor a standalone gtc stop | `capability_refused` | an unattended entry with no protective exit may not be opened |
| kill switch | `/live_kill on`, or an existing `unprotected`/`reconciliation_required` position | `kill_switch_engaged` / `unprotected_position_blocks_entries` | a courtesy at propose time; the real check is at approval |
| evidence (strict only) | citation not full/ok/same-ticker/recent | `evidence_gate_refused` | in advisory mode this is a relabel, not a refusal (see §4) |
| risk_fraction | `≥ 0.05` (a percentage typed as a fraction) | `percentage_input` | converting it is the guess that must not be made on a live order |
| risk_fraction | `> RISK_FRACTION_HARD_CAP` | `risk_fraction_above_hard_cap` | refused, not clamped: a clamped size approves as though it were what was asked |
| sizing | rounds down to zero whole shares | `zero_shares` | a protective stop is whole-share only, so it cannot be rounded up |
| evidence (strict only) | cited `LB ≤ 0` | `evidence_sized_to_zero` | strict mode's answer to a bound that does not exclude zero |
| settled cash | notional would need T+1 proceeds | `unsettled_cash` | the cash Agentic account settles T+1; the refusal carries the settlement date |

At **approval**, `execution.lifecycle` re-runs every one of these from fresh
state (`risk_recomputed_rejected` when the book has moved), then adds:
`phase6_disabled`, and for a live proposal `live_trading_disabled` /
`execution_mode_not_live`.

---

## 4. Sizing and the two budgets (§6.6)

An agent never chooses a quantity. Code computes it from `entry`, `stop` and
`risk_fraction`, rounded **down** to whole shares, under every cap:

```
per_share_risk        = entry − stop
effective_fraction    = risk_fraction × m         (m = clip(LB/PE, 0, 1) for evidenced; absent otherwise)
effective_fraction    = min(effective_fraction, budget_per_trade_cap)
risk_dollars          = effective_fraction × equity          (equity = combined book, all accounts)
shares                = floor(risk_dollars / per_share_risk)
shares                = min(shares, concentration_headroom, sector_headroom, daily_notional_headroom)
```

Concentration and sector caps count the **combined** book across every account,
including the read-only primary one — a name held there is counted when sizing an
Agentic-account proposal in the same name (`test_risk_caps_span_all_accounts`).

**Two budgets, and they never share a number:**

* **Evidenced.** The proposal cites one `cohort_answer_id` that resolves to a
  `depth="full"`, `status="ok"` answer for the **same ticker**, computed within
  `CITATION_MAX_AGE_SESSIONS`, with a positive lower 90% bound (`LB`) and point
  estimate (`PE`) on the **policy-simulated net return** (Spec N §5.3). Then
  `m = clip(LB/PE, 0, 1)` and it draws from `EVIDENCED_RISK_CAP`.
* **Discretionary.** Everything else — uncited, unresolvable, `quick`,
  `insufficient`, `inconclusive`, wrong ticker, stale, no policy interval, or
  `LB ≤ 0`. It draws from the separate, smaller `DISCRETIONARY_RISK_CAP` and its
  own `DISCRETIONARY_DAILY_NOTIONAL`, with the evidence printed on the card in
  full — **including a negative lower bound**.

`EVIDENCE_GATE_MODE=advisory` (default, owner decision 2026-09-08) never sizes to
zero on evidence alone: a weak citation is a relabel, not a refusal. `strict`
makes `LB ≤ 0` size to zero and refuses an uncited proposal, with no code change.

The card prints `risk_fraction`, `m`, `LB`, `PE`, the horizon used, and every cap
that bound the size.

### Needs owner / known gap

`comparables.report.PolicySummary` currently carries the policy net **point
estimate** but **no interval**, and a `SetupSpec` carries no ticker. So a *real*
Spec N answer today reaches the evidence gate with no lower bound and no ticker,
and is correctly labelled **discretionary** with the reason
`citation_no_policy_lower_bound`. Evidenced sizing is fully implemented and
tested against answer objects of the right shape; it begins to fire on real
answers the moment Phase 3c adds a lower-90% interval on the policy net (under
any of `net_ci` / `net_interval` / `net_bootstrap_ci`) and a ticker on the
answer — no change to this code. This is the one place the phase depends on a
sibling phase's field, and it fails safe (toward the smaller budget) until then.

---

## 5. Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `PHASE6_EXECUTION_ENABLED` | `false` | Off ⇒ `propose_order` is not registered and every approval callback is refused. |
| `ALLOW_LIVE_TRADING` | `false` | Required, on top of the flag, for any **live** placement. |
| `EXECUTION_MODE` | `paper` | `live` reaches Robinhood Agentic; `paper` reaches Alpaca paper (same lifecycle). |
| `EXECUTION_APPROVAL_SECRET` | *(unset)* | HMAC key for the signed approval reference. Unset ⇒ no card can be minted or verified — nothing can be approved. |
| `RISK_FRACTION_HARD_CAP` | `0.01` | A `risk_fraction` above this is refused, not clamped. |
| `RISK_FRACTION_PERCENTAGE_FLOOR` | `0.05` | A `risk_fraction` at/above this is refused as a percentage typed as a fraction. |
| `EVIDENCED_RISK_CAP` | `0.01` | Evidenced budget, per-trade risk fraction cap. |
| `EVIDENCED_DAILY_NOTIONAL` | `0.0` | Evidenced budget daily notional; `0` ⇒ unconfigured, does not bind. |
| `DISCRETIONARY_RISK_CAP` | `0.0025` | Discretionary budget, per-trade risk fraction cap. |
| `DISCRETIONARY_DAILY_NOTIONAL` | `0.0` | Discretionary budget daily notional; `0` ⇒ unconfigured. |
| `EVIDENCE_GATE_MODE` | `advisory` | `advisory` never sizes to zero on evidence; `strict` restores zero-sizing and uncited refusal. |
| `CITATION_MAX_AGE_SESSIONS` | `5` | A cited answer older than this (trading sessions) is not a citation. |
| `PROTECTION_WINDOW_SECONDS` | `120` | Fill → stop read-back budget. Past it: `unprotected`, page, block entries. |
| `PROTECTION_POLL_INTERVAL_SECONDS` | `2.0` | Poll cadence within the window. |
| `APPROVAL_TTL_SECONDS` | `1800` | How long an approval card stays valid. |
| `PROPOSAL_MAX_POSITION_PCT` | `0.10` | Concentration cap over the combined book (separate from the scan bot's caps). |
| `PROPOSAL_MAX_SECTOR_PCT` | `0.30` | Sector cap over the combined book. |

The kill switch is **not** an env var: it is a database row
(`execution_kill_switch`), set with `/live_kill on|off`, so it survives a restart
(Spec L §6.5). `ROBINHOOD_ACCOUNT_BUDGET` (existing) still caps the Agentic
account's loaded budget in code.

---

## 6. The owner's live probe — an owner action, NOT run here

Everything above is verified against a fake broker and recorded-shape Robinhood
fixtures. Robinhood is unreachable from the build environment (it needs the
owner's token and a desktop re-auth), so **one empirical fact remains**, and it
is the fact Spec L §5.1 flags as the Phase 6 live probe: that a `gtc`
`stop_market` placed through the MCP is visible in `get_equity_orders` the next
session and triggers when touched.

The probe is an owner action. Do not run it from a build session. Runbook:

1. Fund the Agentic account with a minimal budget (`ROBINHOOD_ACCOUNT_BUDGET`).
2. Set `PHASE6_EXECUTION_ENABLED=true`, `ALLOW_LIVE_TRADING=true`,
   `EXECUTION_MODE=live`, `EXECUTION_APPROVAL_SECRET=<a strong secret>`; confirm
   `/live_kill off`.
3. From an attached agent session, `propose_order` a **one-share** limit entry of
   minimal size in a liquid name, at a limit near the touch. Read the card: it
   should show `budget=discretionary` (no evidenced citation exists yet), one
   whole share, and the caps.
4. Approve on Telegram. Watch the logs: entry submitted → filled → stop placed →
   **stop read back** → `protected`.
5. In the Robinhood app (or `get_equity_orders`), confirm the `stop_market`,
   `gtc`, `regular_hours` order is present and shows the right stop price.
6. **Confirm it survives the next session** — the unstated-GTC-horizon question.
   Leave it overnight and re-check `get_equity_orders`. The daily
   `replace_missing_stops` job re-places a stop that has vanished; note whether
   it had to.
7. Close the probe position by hand, record the result in
   `docs/ROBINHOOD_INTEGRATION_PLAN.md`, and turn the flags back off until real
   use.

Until this probe passes, live entries stay closed (Spec L §5.1: "Until the probe
passes, live entries stay closed").

---

## 7. What is exercised against the fake broker only

Faithfully: **everything touching Robinhood placement.** The full approval →
place → fill → stop → read-back → protected/unprotected path, the single-use and
signature checks, the risk re-evaluation at approval, the daily missing-stop
replacement, and the two budgets all run against
`execution.brokers.fake.FakeExecutionBroker` and recorded-shape fixtures. The
Robinhood adapter's request **shape** (`stop_market`, `gtc`, `regular_hours`,
whole shares, `ref_id`) is asserted against the real adapter's own builder, and
`read_open_orders` normalization against the recorded fixtures — but no assertion
here has ever touched the live Robinhood server. That is §6's job.
