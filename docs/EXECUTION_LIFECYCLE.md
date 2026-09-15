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
  point `on_approval` is called from exactly two files — `bot/handlers/proposals.py`
  and `orchestrator/approval_poller.py` — both of them in packages the workspace's
  import closure cannot reach, which
  `tests/test_execution_lifecycle_isolation.py` now asserts for each of them by
  name. No MCP tool and no REST route can trigger it.

### 1a. The second approval channel (owner ruling 2026-09-13)

Bryan may also approve in his coding-agent chat rather than on the Telegram
button. The ruling is in Spec K §10 and Spec L §10; the picture it adds is one
box, and the box is the point:

```
 agent session                workspace service          owner_actions        bot process
       │                             │                    (a table)               │
       │  "approve proposal <uid>"   │                        │                   │
       │  ── after showing the card  │                        │                   │
       │     and hearing an explicit │                        │                   │
       │     yes from Bryan ───────► │                        │                   │
       │                             │ verify the signed,     │                   │
       │                             │ single-use, expiring,  │                   │
       │                             │ owner-bound reference  │                   │
       │                             │ RECORD the decision ──►│                   │
       │  ◄── "recorded; places      │                        │                   │
       │       nothing"              │                        │◄── claim (a       │
       │                             │                        │    conditional    │
       │                             │                        │    UPDATE)        │
       │                             │                        │                   │
       │                             │          execution.lifecycle ◄─────────────┤
       │                             │           (the SAME call, the same          │
       │                             │            arguments, the same guards)      │
```

Nothing in the second row of that diagram is new machinery: the poller calls
`on_approval` with exactly the arguments `bot/handlers/proposals.py` passes, and
every guard in §3 runs unchanged. What is new is only *where the owner's yes
comes from*.

Three independent things stop a double placement, and they fail differently:

1. the **claim** — `portfolio.owner_actions.claim` is a single conditional
   `UPDATE ... WHERE claimed_at IS NULL` whose row count decides the winner, on
   SQLite and Postgres alike — so two pollers cannot both take one decision;
2. **`proposals.approval_consumed_at`**, consumed inside `on_approval`'s own
   transaction, so a claim that somehow double-fired (or a poller racing the
   Telegram button) still places once;
3. `on_approval` refusing anything not still `proposed`.

Recording a decision writes **neither** of the first two columns on the
proposal: `execution/lifecycle.py` keeps exactly one writer on the approval
path. Rejecting a plain proposal is the one thing the tool applies itself,
because it writes a ledger row and nothing else, and a deployment with the
poller off must still be able to say no.

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
  `insufficient`, `inconclusive`, wrong ticker, a subject that did not qualify,
  stale, no policy interval, an interval at a level other than 90%, or
  `LB ≤ 0`. It draws from the separate, smaller `DISCRETIONARY_RISK_CAP` and its
  own `DISCRETIONARY_DAILY_NOTIONAL`, with the evidence printed on the card in
  full — **including a negative lower bound**.

`EVIDENCE_GATE_MODE=advisory` (default, owner decision 2026-09-08) never sizes to
zero on evidence alone: a weak citation is a relabel, not a refusal. `strict`
makes `LB ≤ 0` size to zero and refuses an uncited proposal, with no code change.

The card prints `risk_fraction`, `m`, `LB`, `PE`, the horizon used, and every cap
that bound the size.

### How to get an evidenced proposal, in full

The evidenced path is **reachable** as of the Spec L §6.6 / Spec N §8 closure.
Three things had to be true at once and now are:

1. **The interval exists.** `comparables.report.PolicySummary` carries `net_ci`,
   a lower 90% bound on the policy-simulated net return, from the same
   stationary block bootstrap the headline uses, run over the per-event policy
   net returns. The level is `comparables.config.POLICY_CONFIDENCE_LEVEL = 0.90`
   and not the engine-wide `CONFIDENCE_LEVEL` of 0.95, because §6.6 names the
   90% bound and this code refuses an interval at any other level rather than
   relabelling one.
2. **The answer knows which name it is about.** A `SetupSpec` is a *pattern*
   (Spec N §4.0), so `compare_setups` takes a `subject_ticker`, checks that the
   name met the setup's conditions at its most recent opportunity on or before
   `as_of`, and stores `subject_ticker` / `subject_qualifies` on both the query
   and the answer. "Same ticker" therefore means both: the subject **is** this
   proposal's ticker and it **qualified**.
3. **The gate reads a stored answer.** A citation resolves through
   `research_workspace.citations` to a `comparables.citations.ResolvedCitation`
   — the stored row, with the answer as JSON. `portfolio/evidence.py` reads that
   and an in-memory `CohortAnswer` through one adapter, including the
   repr-string floats `report.to_json` writes for byte-determinism.

So, end to end:

```
compare_setups(setup=..., as_of=..., depth="full", subject_ticker="AMD")
  -> {"citation_id": "cohort:41", "subject": {"ticker": "AMD", "qualifies": true, ...}}
journal_append(decision=..., tickers=["AMD"], cohort_answer_id="cohort:41",
               budget="evidenced")          # the citer re-checks the label
propose_order(ticker="AMD", entry=..., stop=..., risk_fraction=0.005,
              cohort_answer_id="cohort:41")
  -> budget=evidenced, m=clip(LB/PE,0,1), risk_fraction_effective=0.005 x m
```

`tests/test_evidenced_budget_end_to_end.py` runs exactly that against stored
rows, through the real citation seam, and runs every way it can fail beside it.

**A `quick` answer can carry a subject and still not be evidence.** The subject
is recorded on any depth, because it is a fact about the question; citability is
a separate axis and `quick` fails it structurally (Spec N §8).

**Recency of the *event* is not the recency of the *answer*.** The 5-session
`CITATION_MAX_AGE_SESSIONS` budget bounds how old the cohort answer may be. The
subject's qualifying event carries its own `subject_event_date`, which is
printed rather than bounded: "SY05 qualified" and "SY05 qualified nine months
ago" are different statements, and the second is the reader's call, not the
gate's. Tracked as a candidate follow-up.

---

## 5. Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `PHASE6_EXECUTION_ENABLED` | `false` | Off ⇒ `propose_order` is not registered and every approval callback is refused. |
| `ALLOW_LIVE_TRADING` | `false` | Required, on top of the flag, for any **live** placement. |
| `EXECUTION_MODE` | `paper` | `live` reaches Robinhood Agentic; `paper` reaches Alpaca paper (same lifecycle). |
| `ROBINHOOD_STOP_PROBE_REQUIRED` | `false` | Off ⇒ a **live Robinhood** entry with no recorded protective-exit probe (§6) proceeds, and logs a `robinhood_stop_probe_not_enforced` WARNING naming what is unverified. On ⇒ the same entry is refused. The *evidence* is a `broker_stop_probes` row either way; this variable only decides what its absence costs. Owner ruling 2026-09-15. |
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
| `WORKSPACE_OWNER_TOOLS_ENABLED` | `false` | **Workspace service.** Off ⇒ the ten owner tools are not registered at all. |
| `OWNER_ACTION_POLLER_ENABLED` | `false` | **Bot service.** Off ⇒ nothing acts on a recorded decision. Gated on `PHASE6_EXECUTION_ENABLED` as well. |
| `OWNER_ID` | `TELEGRAM_CHAT_ID` | Who an approval binds to. **Must match on both services.** |
| `OWNER_ACTION_POLL_SECONDS` | `20` | Poller cadence, clamped to 5–120. |
| `OWNER_ACTION_TTL_SECONDS` | `900` | How long a prepared tier-change confirmation stays valid. |

The kill switch is **not** an env var: it is a database row
(`execution_kill_switch`), set with `/live_kill on|off` — or with the
`kill_switch` MCP tool, which needs only a `read` token to **engage** and an
`admin` one to release — so it survives a restart (Spec L §6.5).
`ROBINHOOD_ACCOUNT_BUDGET` (existing) still caps the Agentic account's loaded
budget in code.

---

## 6. The owner's live probe — an owner action, NOT run here

Everything above is verified against a fake broker and recorded-shape Robinhood
fixtures. Robinhood is unreachable from the build environment (it needs the
owner's token and a desktop re-auth), so **one empirical fact remains**, and it
is the fact Spec L §5.1 flags as the Phase 6 live probe: that a `gtc`
`stop_market` placed through the MCP is visible in `get_equity_orders` the next
session and triggers when touched.

The probe is an owner action, placed **by hand in the Robinhood app**. Do not
run it from a build session, and do not run it through `propose_order`: the
system's first live placement must not be the one that tests whether the
system's protection works. (Until 2026-09-15 this runbook did say to propose and
approve it. It says by hand now for two reasons, and only the first depends on
enforcement being on: under `ROBINHOOD_STOP_PROBE_REQUIRED=true`, a runbook that
asks the system to place the order that authorises the system is a circle; and
in either mode, asking the untested protection path to protect the very order
that tests it is backwards. Placing it by hand was always the safer half of that
choice.)

Runbook:

1. Fund the Agentic account with a minimal budget (`ROBINHOOD_ACCOUNT_BUDGET`).
2. Confirm `/live_kill off`. No execution flag needs to be on for the probe
   itself — nothing in this repository places it.
3. **In the Robinhood app**, buy one share of a liquid name at or near the
   touch, then place a standalone **`stop_market`, `gtc`, regular-hours** sell
   order for that one share, well below the market. One share, so the position
   is the price of a coffee and the stop is the only thing being tested.
4. Confirm the app shows the stop as working, and that
   `python -m scripts.robinhood_stop_probe --list` sees the same order through
   `get_equity_orders` — the read path the protection check actually uses.
5. **Confirm it survives the next session** — the unstated-GTC-horizon question,
   and the whole reason for the probe. Leave it overnight and re-run `--list`.
6. **Record the observation**, so the code knows the probe passed:

   ```
   python -m scripts.robinhood_stop_probe --record --order-id <id> \
       --note "hand-placed <date>, survived overnight, re-checked <date>"
   python -m scripts.robinhood_stop_probe --status
   ```

   That script **places nothing**. It reads the order back out of
   `get_equity_orders`, refuses anything that is not a `gtc` `stop_market` in
   the configured account, and writes one `broker_stop_probes` row.
7. Cancel the stop and close the probe position by hand, and record the result
   in `docs/ROBINHOOD_INTEGRATION_PLAN.md`.

**What this version of the probe does not establish, stated plainly.** A
hand-placed stop proves that a `gtc` `stop_market` survives at Robinhood and is
readable through `get_equity_orders` — the fact Spec L §5.1 names. It does *not*
exercise `place_equity_order` through the MCP with the stop payload. That
residual is covered as far as it can be without spending a live order on it: the
tool schema has been captured twice (`docs/robinhood/tool_schemas.json`,
2026-09-08 and 2026-09-14, `place_equity_order` byte-identical), the adapter's
request shape is asserted against its own builder, and the first real live entry
still reads its stop back inside `PROTECTION_WINDOW_SECONDS` and pages,
marks `unprotected` and blocks every further entry if it cannot. What changes
with the probe recorded is that the *survival* of that stop is no longer a guess.

### Strongly recommended, checked in code, and advisory by default

**Run this probe.** It is still the only way to establish that a `gtc`
`stop_market` survives at Robinhood and is readable back through
`get_equity_orders` — the fact the whole protective-exit contract rests on, and
the one thing standing between a live fill and a position with no automated
exit. Nothing below makes it optional as a piece of engineering; it makes it
optional as a *blocker*.

**Live entries do not stay closed until it passes.** Spec L §5.1 and this
section used to say they did, and for a while nothing in the code agreed with
them. Since `0015_broker_stop_probes` the check exists — `live_gate_refusal`'s
third condition, reading a `broker_stop_probes` row — but Bryan ruled on
2026-09-15, having been shown what it protects against, that it must not block
him. So:

* **Default (`ROBINHOOD_STOP_PROBE_REQUIRED` unset or `false`)** — a live
  Robinhood entry with no probe on record **proceeds**, and every time it does,
  `portfolio/stop_probe.py` logs a WARNING (`robinhood_stop_probe_not_enforced`)
  carrying the refusal code it would have used, the masked account, what a
  record would have contained, and the variable that turns enforcement on. The
  warning is not suppressible: advisory-and-silent would be the original defect
  — a safety claim nobody enforces and nobody prints — in a new costume.
* **`ROBINHOOD_STOP_PROBE_REQUIRED=true`** — the same finding refuses instead,
  with the same code and the same message. Set it once the probe is recorded if
  you want it to stay that way, or before it is, if you would rather be blocked
  than warned.

The finding itself does not move with the flag. Only the consequence does:

* **A row, not a flag, is what counts as evidence.**
  `ROBINHOOD_STOP_PROBE_PASSED=true` would record an assertion — a human writing
  down that something is so, which is what `ALLOW_LIVE_TRADING` and
  `EXECUTION_MODE` already are. The row records a verification: the order id
  that was read back and the moment it was seen.
  `ROBINHOOD_STOP_PROBE_REQUIRED` is not that assertion in disguise — it cannot
  say the probe passed, only whether an absent one is fatal.
* **Keyed to `(broker, account)`.** A probe is evidence about the account it ran
  in. A record for another Robinhood account does not authorise this one; the
  key is a SHA-256 of the account number, because a masked `****1234` can
  collide and a collision in a gate reads as permission.
* **Absence is never read as "probed".** Neither is a row with no order id or no
  observation timestamp, nor a gate with no database session to read from (Spec
  Q §12 invariant 1). In the default mode all four warn; under
  `ROBINHOOD_STOP_PROBE_REQUIRED=true` all four refuse. What none of them ever
  does is pass silently.
* **Only Robinhood live entries.** `BROKER_PRIMARY` is what routes a live
  placement (`execution/brokers/factory.py::_build_primary`), so it is what the
  check reads. Paper routes to Alpaca paper and needs no Robinhood stop, and a
  Strategy Lab paper arm may reach only Alpaca paper (Spec Q §11). Neither is
  gated and neither warns, in either state and either mode.
* **One place.** The check is inside `live_gate_refusal`, not beside it, for the
  reason that function's docstring gives: two copies of "what makes live legal"
  is the drift this path cannot afford. All three of its callers — the approval
  path, the Phase 5 pre-placement gate, and the promotion deployment check —
  get it by passing their session through.

The codes are `robinhood_stop_probe_not_recorded`,
`robinhood_stop_probe_account_unknown` and `robinhood_stop_probe_unverifiable`,
and each message names the remedy whether it is refused or logged.
`tests/test_robinhood_stop_probe_gate.py` holds the rows for both modes —
including that the default really is `false`, and that it passes loudly rather
than quietly.

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

---

## 8. Strategy Lab arms on this service (Phase 5, Spec Q §12)

A Strategy Lab arm does not get its own execution path. It gets
`execution/strategy_lifecycle.py`, which runs the pre-placement gates an arm
needs and then calls the service documented above, with an observer attached.

Three seams were added here for it, and all three are additive — with no
observer injected, this service behaves exactly as Phase 6 shipped it:

* **`observer`** — an optional `(event, proposal, detail) -> None`, notified
  after the transaction that made each state change and before the next broker
  call. Its exceptions are swallowed and logged, for the same reason
  `_journal_fill` swallows its own: the position is real either way, and an
  exception there would abandon a fill mid-protection to report a bookkeeping
  problem.
* **`partially_filled` vs `filled`** — this service already protected
  `filled_quantity` rather than the requested size, so nothing about what is
  placed changed. The distinction is reported so the §12 machine can resize
  protection when the remainder fills later.
* **`live_gate_refusal(settings, session)`** — the live conjunction, extracted
  from `_require_live_gates` so Phase 5 can run the same one a step earlier,
  before a live card is minted. Two copies of "what makes live legal" is the
  kind of drift this path cannot afford, which is also why §6's protective-exit
  probe was added to this function rather than checked beside it. `session` is
  how the probe record is read; a caller with none is never told the probe is on
  record — it is refused under `ROBINHOOD_STOP_PROBE_REQUIRED=true` and warned
  about by default.

One defect was fixed rather than added to. `_reconcile_unknown` previously
returned `None` when an ambiguous placement's `ref_id` **was** found at the
broker, and the caller then marked the proposal `failed` — releasing its
reservation and telling the owner nothing was placed, while a real order sat at
the broker. It now records the found order id and lands in
`reconciliation_required` either way, with a recovery line that says which case
it is. No test covered that branch before, because no fake could produce an
ambiguous placement; `FakeExecutionBroker.placement_unknown` and
`phantom_ref_ids` now can.

What is exercised against fakes only is unchanged and now larger: every Phase 5
path in `tests/test_strategy_lab_execution.py` and
`tests/test_strategy_lab_safety.py` runs against `FakeExecutionBroker`. Several
of those tests turn `ALLOW_LIVE_TRADING` and `EXECUTION_MODE=live` on; every one
of them is pointed at a fake, and §6's owner probe is still the only thing that
will ever touch the live server.
