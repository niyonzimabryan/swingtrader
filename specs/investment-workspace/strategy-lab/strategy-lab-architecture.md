# SwingTrader Strategy Lab Architecture

**Status:** Draft v0.1 for owner decisions
**Date:** 2026-08-21
**Target repository:** `niyonzimabryan/swingtrader`
**Supersedes:** The adaptive-intelligence direction in `swing-trader-prd.md` Section 2 for new strategy experimentation. Existing production behavior remains unchanged until individual phases ship and are enabled.

## 1. Outcome

Turn SwingTrader from one AI-assisted scoring pipeline into a controlled strategy laboratory that can:

1. Run one stable champion and many challengers over the same market opportunities.
2. Compare deterministic, versioned strategies without changing their rules mid-experiment.
3. Replay strategies historically where point-in-time data is available.
4. Run all credible strategies concurrently in shadow and paper modes.
5. Promote one strategy at a time into a tightly constrained, supervised real-money pilot.
6. Combine complementary strategies only after their individual behavior is measured.
7. Introduce regime-based routing only as its own separately tested strategy.

The operating principle is:

> Research intelligence proposes hypotheses. Versioned strategy code makes decisions. The experiment system measures the results. The owner controls promotion and live capital.

## 2. Plain-language product model

- **Champion:** the current baseline strategy. Initially this is the existing SwingTrader composite, not because it is proven, but because it is the system being replaced.
- **Challenger:** any fixed alternative strategy trying to beat or complement the champion.
- **Shadow:** records exactly what a strategy would do, but sends no broker order.
- **Paper:** submits the strategy through Alpaca paper trading so execution plumbing is exercised.
- **Live:** uses a dedicated Robinhood Agentic account with explicit owner promotion, hard risk limits, and verified position protection.
- **Promotion:** an auditable owner decision to move a specific, immutable strategy version to a higher tier. The system may recommend promotion but must never perform it automatically in V1.

Many challengers run at once. “Challenger” is a role, not a limit of one.

## 3. Owner decisions

The spec uses the recommended defaults below until Bryan changes them.

| Decision | Recommended default | Alternative | Why it matters |
|---|---|---|---|
| Live allocation in V1 | One live champion; all challengers shadow/paper | Up to three simultaneous live arms | One champion makes losses, fills, and operational incidents attributable. The data model supports more later. |
| Live approval | Human approval for every entry; exits automated once protection is verified | Fully automatic entries | The current Robinhood integration does not manage the complete exit lifecycle. Entry autonomy must remain disabled until that is fixed and proven. |
| Initial asset scope | Long-only, liquid US equities; no options, crypto, leverage, or shorting | Add short equity arms | This matches current data and broker capabilities and keeps the first experiment interpretable. |
| Strategy changes | Any rule or parameter change creates a new version | Mutable strategy configuration | Immutable versions prevent the experiment from being rewritten after seeing results. |
| Promotion authority | Owner only | Automated promotion | Small samples and multiple testing make automatic promotion unsafe and statistically misleading. |
| Existing LLM pipeline | Baseline strategy plus timestamped feature provider | Final universal decision-maker | This preserves its research value without allowing narrative judgment to contaminate every experiment. |

The dollar budget is deployment configuration, not part of the strategy definition. No implementation PR should set or change production capital limits.

## 4. Current system diagnosis

### Reuse

- `orchestrator/pipeline.py`: discovery, screening, research, memo production, scan scheduling.
- `agents/`: catalyst, fundamental, macro, pattern, and web-research outputs.
- `backtest/simulator.py`: T+1-open entry, adverse slippage, pessimistic same-bar resolution, gap-through stop behavior, target and time exits.
- `backtest/event_replay.py`: point-in-time event replay and parameter sensitivity.
- `tracking/shadow_ledger.py`: best-effort capture and forward returns.
- `execution/auto_approver.py`: structurally paper-only order path.
- `execution/order_manager.py`: broker review, explicit live gate, Robinhood notional caps, audit rows.
- Telegram commands, notifications, weekly reports, database session handling, and existing CI.

### Replace or extend

- `scoring/engine.py` is one static weighted ensemble, not a strategy framework.
- `scoring/weights.py:get_weights()` explicitly leaves contextual adaptation for future work and currently returns static weights.
- `ScoredCandidate` stores one final decision per ticker, not one decision per strategy and experiment arm.
- `tracking/attribution.py` reports closed trades but does not manage experiments, benchmarks, uncertainty, or multiple trials.
- `execution/order_monitor.py` and `execution/position_monitor.py` intentionally filter to Alpaca or legacy trades.
- Robinhood entry orders do not currently create and verify the complete stop/target/time-exit protection required for unattended operation.
- Database changes use startup `ALTER TABLE` logic. This project already calls for Alembic before more schema churn; Strategy Lab should establish it before adding experiment tables.

### Do not build again

- A second market scanner.
- A second broker order-placement implementation.
- Historical replay of LLM conclusions. Models and current web results know information that was unavailable at the historical decision time.
- A self-modifying live strategy.
- An automated “pick the recent winner” switcher.

## 5. Architecture

```text
                          EXISTING RESEARCH PLANE
                    (scan, agents, structured events)
                                     |
                                     v
                    Point-in-time Snapshot Bundle
             universe membership + facts + prices + provenance
                                     |
                    +----------------+----------------+
                    |                |                |
                    v                v                v
             Champion v1      Challenger A     Challenger B ... N
             fixed version      fixed version    fixed version
                    |                |                |
                    +----------------+----------------+
                                     |
                                     v
                           Strategy Decisions
                  act / abstain + reason + risk plan
                                     |
              +----------------------+----------------------+
              |                      |                      |
              v                      v                      v
        Shadow Executor        Paper Executor          Live Gate
        simulated fills        Alpaca paper        owner promotion only
              |                      |                      |
              +----------------------+                      v
                                     |             Broker capability check
                                     |              + risk reservation
                                     |                      |
                                     |                      v
                                     |             Robinhood order state
                                     |             + protection + reconcile
                                     +----------------------+
                                                            |
                                                            v
                           Canonical Experiment Ledger
                       decisions, fills, positions, costs
                                                            |
                                                            v
                              Evaluation + Scoreboard
                  benchmark, uncertainty, drawdown, correlation
                                                            |
                                                            v
                         Owner keeps / pauses / promotes
```

No strategy module may import a broker, database session, Telegram client, or LLM client. It receives an immutable snapshot and returns a decision.

## 6. Core domain contracts

Create a new `strategy_lab/` package. Keep contracts explicit and boring.

```text
strategy_lab/
  domain.py              dataclasses/enums and validation
  registry.py            discovers enabled immutable strategy versions
  snapshots.py           point-in-time input builder and provenance hashes
  runner.py              runs every arm idempotently for one opportunity
  execution_policy.py    shared stop/target/hold policy definitions
  metrics.py             pure performance calculations
  promotion.py           owner-controlled tier transitions
  strategies/
    swingtrader_composite_v1.py
    earnings_drift_v1.py
    momentum_v1.py
    short_term_reversal_v1.py
```

### `MarketSnapshot`

Required fields:

- `snapshot_id`, `scope` (`ticker` or `universe`), `as_of_utc`, optional `ticker`, `universe_version`
- current and historical OHLCV needed by enabled strategies
- for universe scope: the complete constituent set, contemporaneous prices/returns, exclusions, and provenance under one shared cutoff
- structured event facts with event timestamps and source provenance
- macro regime inputs and their observation timestamps
- current research-agent outputs, labeled `replayable=False`
- for the compatibility arm only: the already-produced legacy final score, classification, direction, signal breakdown, trade parameters, model/trace provenance, output hash, and hash of the portfolio context used by the current scorer
- `data_cutoff_utc`, provider names, and a deterministic content hash
- references to immutable source observations carrying both `valid_at` (when a fact applies) and `known_at_utc` (when SwingTrader first received it)
- data-quality flags: missing, stale, revised, or not point-in-time safe

Raw provider payloads may remain in existing tables. The strategy snapshot stores normalized inputs plus identifiers/hashes needed to reproduce the decision. Cross-sectional strategies must use one immutable universe-scoped snapshot for every constituent rank; they may not assemble ranks from ticker snapshots created at different times.

Current cached OHLC and historical earnings rows are not automatically point-in-time evidence: prices may carry later corporate-action adjustments, universe membership may contain survivorship bias, and historical earnings values generally lack reliable availability timestamps. Replays using those rows are labeled `archival_reconstructed` and cannot support promotion. Promotion-eligible replay requires either a licensed archival source with availability/revision provenance or forward-collected source observations captured by SwingTrader.

### `StrategyVersion`

- stable slug and semantic version
- code hash
- human-readable hypothesis
- supported universe and direction
- required snapshot fields
- signal rules
- referenced execution-policy version
- expected holding horizon
- maximum data staleness
- `historically_replayable` flag and reason
- status: `draft`, `shadow`, `paper`, `live_eligible`, `retired`

### `StrategyDecision`

- experiment, arm, strategy version, snapshot, and ticker identifiers
- `action`: `long`, `flat`, or `abstain` in V1
- normalized signal strength and confidence, where confidence is optional and never used as evidence by itself
- reason codes, not only prose
- entry style, stop, targets, maximum hold, and position-risk request
- `blocked_reasons` here cover signal-generation problems only: stale data, missing dependency, or invalid point-in-time input
- deterministic decision hash

Calling the same strategy version with the same snapshot must produce the same decision hash. Portfolio cash, exposure, open positions, and broker state are deliberately excluded from the strategy decision. They are evaluated immediately before execution and recorded as a separate, versioned risk/execution intent on `strategy_trades`. This prevents a decision from becoming stale merely because portfolio state changed while preserving reproducible signals.

### Strategy interface

```python
class Strategy(Protocol):
    metadata: StrategyMetadata

    def evaluate(
        self,
        snapshot: MarketSnapshot,
    ) -> tuple[StrategyDecisionDraft, ...]:
        ...
```

The return value is ordered by ticker ascending before hashing. A ticker-scoped strategy returns exactly one draft for the snapshot ticker. A universe-scoped strategy returns exactly one draft for every constituent: selected names return `long`; eligible but unselected names return `flat`; names that cannot be evaluated return `abstain` with reason codes. This makes top-N/decile selection reproducible without re-running a strategy per ticker or mixing snapshot cutoffs. The runner validates cardinality, ticker membership, uniqueness, and sort order before persistence, then hashes both the ordered decision set and each constituent decision.

The strategy must return `abstain` when required inputs are stale, absent, or not point-in-time safe. It must never guess missing values. Each registered version has a canonical implementation manifest containing the strategy source, every imported project helper that can affect its output, the execution-policy source/config, indicator formulas, and pinned Python/dependency versions. On activation and before every run, the registry recomputes the manifest hash and compares it with `strategy_versions.implementation_manifest_hash`. A mismatch fails closed and requires registration of a new strategy version; an in-place code, helper, formula, policy, or runtime dependency change may never run under an existing version identity.

## 7. Initial strategy roster

These are reference implementations to test, not claims that the effects remain profitable after current costs.

### A. `swingtrader_composite_v1`, initial champion

- Adapter around the current `ScoringEngine` output. The existing pipeline runs first and its complete result/provenance is frozen into the snapshot. The adapter maps that stored payload; it does not call an LLM or recompute the score.
- Only runnable live from current-time snapshots.
- Historical replay explicitly prohibited because LLM and web-research outputs are not reconstructible at time T.
- Preserves current thresholds and trade-parameter logic without modification.

### B. `earnings_drift_v1`, challenger

- Uses structured earnings event timestamp, surprise magnitude, announcement timing, liquidity, and next tradable session.
- Long-only positive-surprise arm for V1.
- Uses only point-in-time structured data; no article generated after the event cutoff.
- T+1-open executable entry assumption in replay.
- Fixed execution policy version. No parameter sweep result may be applied to the running version.

### C. `momentum_v1`, challenger

- Long-only cross-sectional momentum over the configured liquid universe.
- Every rebalance uses one universe-scoped snapshot with a single cutoff, constituent list, prices, exclusions, and provenance. All ranks and per-ticker decisions reference that same snapshot ID.
- Formation and rebalance rules follow a written, fixed reference specification.
- Skips the most recent month in the reference implementation and uses a multi-month holding horizon, so it is the long-duration comparator.
- Must include delisting/survivorship and universe-version warnings when the available data cannot reproduce them.

### D. `short_term_reversal_v1`, shadow-only challenger

- Short-duration, long-only oversold/reversal signal over highly liquid equities.
- Shadow-only at launch because turnover and bid-ask effects can dominate the apparent anomaly.
- Uses conservative transaction-cost and fill assumptions.
- Cannot become paper/live eligible until replay shows stability under materially worse cost assumptions.

### Later, not V1

- Equal-weight ensemble of independently credible strategies.
- Risk-weighted ensemble.
- Regime router choosing among strategies.
- Short strategies, options, leverage, crypto, reinforcement learning, or autonomous parameter optimization.

Any ensemble or router is registered as another strategy and competes against its components. It does not get a free pass because it combines existing strategies.

### Normative V1 reference configuration

These values make the initial arms implementable and comparable. They are project experiment defaults, not claims of optimality. A worker must not invent replacements. Any change creates a new version.

#### Shared liquid-equity universe `liquid_us_equity_v1`

- Use the point-in-time active SwingTrader universe from the universe snapshot.
- Adjusted close at signal cutoff at least $5.
- At least 252 valid daily bars ending at or before the cutoff.
- Median daily dollar volume over the prior 20 complete sessions at least $20 million.
- Long-only and regular trading hours.
- Corporate-action-adjusted prices for signals; executable unadjusted OHLC for fills when available.
- If point-in-time membership, delisting, or adjustment status is unknown, preserve the decision in shadow with `data_quality_warning`; it is not paper/live eligible.

#### Execution policies

`event_swing_14cal_v1`:

- entry: first regular-session open after the timestamped signal becomes tradable
- ATR: Wilder ATR(14) using only bars completed before entry
- stop: entry minus 2.0 ATR
- target 1: entry plus 2R, exit 50%
- target 2: entry plus 3R, exit remainder
- maximum hold: 14 calendar days, using the existing simulator's first bar on or after the boundary
- replay slippage: 10 bps adverse on every fill; same-bar conflicts remain pessimistic

`reversal_5cal_v1`:

- entry: next regular-session open after signal close
- stop: entry minus 1.5 ATR(14)
- target 1: entry plus 1R, exit 50%; the remainder keeps the original stop and exits at the time boundary, matching the existing simulator
- no target 2
- maximum hold: 5 calendar days, using the existing simulator's first bar on or after the boundary
- baseline replay slippage: 10 bps adverse per fill
- mandatory stress reports at 25 and 50 bps adverse per fill

`momentum_quarterly_89cal_v1`:

- signal cutoff: final complete trading session of March, June, September, or December
- entry: next regular-session open
- hard risk overlay: a stop 10% below filled entry using the existing simulator's intrabar crossing and gap-through semantics
- otherwise maximum hold is 89 calendar days, exiting at the first bar on or after the boundary under the existing simulator; this deliberately clears the old basket before the next quarter's entry window
- no profit target
- replay slippage: 10 bps adverse per fill

#### `earnings_drift_v1` formula

- Source must be a structured earnings record with reported EPS, consensus EPS, event date, and source provenance available at cutoff.
- `surprise_pct = (reported_eps - consensus_eps) / max(abs(consensus_eps), 0.01) * 100`.
- Signal long when `surprise_pct >= 5.0` and the shared liquidity rules pass.
- Do not use post-announcement price or volume to qualify an entry.
- Conservatively enter at the first regular-session open strictly after the recorded event date. This remains executable when the source lacks reliable intraday announcement timing.
- Use `event_swing_14cal_v1`.
- Historical records without trustworthy `known_at_utc` provenance are exploratory only and cannot contribute to promotion evidence.

#### `momentum_v1` formula

- Use one universe snapshot for the entire rebalance.
- Score each eligible constituent by cumulative adjusted return from session T-252 through T-21 inclusive, excluding the most recent 20 complete sessions.
- Rank descending; ties break by ticker ascending.
- Select the top decile, capped at 20 names and requiring at least 5 selected names. If fewer than 50 eligible constituents exist, abstain for the entire arm.
- Equal weight selected names within the arm's virtual risk budget.
- Use `momentum_quarterly_89cal_v1`.
- This is a long-only adaptation of the cited cross-sectional momentum literature; the adaptation is part of the versioned hypothesis.

#### `short_term_reversal_v1` formula

- Evaluate after a complete regular session.
- Signal long when the three-session adjusted-close return is at or below -8%, RSI(2) is at or below 10 using Wilder smoothing, the current close is above SMA(200), and shared liquidity rules pass.
- Ties do not require ranking; cap the shadow arm at the 10 largest absolute three-session declines per signal date, ties by ticker ascending.
- Use `reversal_5cal_v1`.
- Remains structurally shadow-only in V1.

## 8. Persistence model

Use Alembic for all new tables and migrate the existing SQLite database without rewriting historical rows.

### `source_observations`

Bitemporal source ledger: entity/ticker, observation type, `valid_at`, `known_at_utc`, provider, provider revision/as-of identifier when supplied, normalized payload JSON, payload hash, optional raw-source reference, optional superseded-observation ID, replay-eligibility flag, and quality warnings. Backfilled rows with unknown historical availability are explicitly `archival_reconstructed` and `replay_eligible=false`.

### `strategy_versions`

Identity, slug, semantic version, implementation-manifest JSON/hash, configuration JSON, hypothesis, dependency declaration, replayability, status, created timestamp. Unique on `(slug, version)`. Registration stores the complete transitive executable manifest described in Section 6; activation recomputes it and refuses drift.

### `experiments`

Name, hypothesis, status, start/end criteria, universe specification, data cutoff, benchmark set, primary metric, guardrail metrics, pre-registration JSON, total planned variants, owner decision fields, and timestamps.

Statuses:

```text
draft -> registered -> running -> evaluating -> completed
                   \-> paused ----^          \-> cancelled
```

No arm may run until the experiment is `registered`. Registration freezes the hypothesis and analysis plan.

### `experiment_arms`

Experiment, strategy version, immutable mode (`shadow`, `paper`, `live`), risk budget, status, start/end timestamps, and promotion source. Unique active arm per experiment/strategy/mode. A database-level partial unique index over a constant for active `live` rows enforces at most one active live arm globally. Replacing the champion atomically deactivates the old live arm and activates the already-created target live arm in one transaction; failure rolls the transaction back and leaves the prior champion unchanged.

### `market_snapshots`

Scope (`ticker` or `universe`), optional ticker, universe version, `as_of_utc`, normalized inputs JSON, referenced source-observation IDs, provenance JSON, data-quality JSON, content hash, and creation timestamp. Universe-scoped rows contain an immutable constituent/price matrix under one cutoff. Large raw documents remain in existing research tables.

### `strategy_decisions`

Arm, snapshot, non-null ticker/constituent identity, action, reason codes, decision JSON, decision hash, blocked reason, and creation timestamp. Unique on `(arm_id, snapshot_id, ticker)` for idempotency. Universe strategies persist one row for every constituent—`long`, `flat`, or `abstain`—while sharing the same universe snapshot ID.

### `strategy_trades`

Canonical experiment execution intent/trade identity across all modes: persisted `execution_id`, arm, decision, immutable arm mode, portfolio/risk-context hash, linked legacy `trades.id` when broker-executed, lifecycle status (including risk- or capability-blocked), intended/filled entry, stop, targets, quantity/notional, costs, exit, realized P&L, exit reason, timestamps, and reconciliation state. A partial unique index permits at most one nonterminal execution for a decision; the execution row is inserted and committed before risk reservation or any external broker call. Retries resolve the same `execution_id`. Risk eligibility is recalculated from a fresh portfolio context for each execution attempt and never reused from the strategy decision.

### `experiment_metric_snapshots`

Immutable evaluation output for an arm and evaluation cutoff, including sample counts, exposure, return after costs, benchmark-relative return, drawdown, turnover, profit factor, R metrics, uncertainty intervals, cost assumptions, and warning codes.

### `promotion_events`

Source-evidence arm ID, target arm ID, shared strategy version, from/to tier, immutable experiment metric/evidence snapshot ID, owner identity, reason, previous/new risk budget, and timestamp. This is append-only. Promotion validation requires that the evidence snapshot belongs to the source arm, the source met its preregistered gate, the target is an inactive arm with the same immutable strategy version and requested higher mode, and the transition is allowed (`shadow -> paper`, `paper -> live`, or a recorded demotion). A confirmed promotion activates the target without mutating the source arm's mode. Live execution must match the exact promoted target arm ID, strategy version, mode, and budget; source evidence or a promotion for another target can never authorize it. Activating a live target also uses the global single-champion transaction described above.

## 9. Experiment lifecycle

```text
Idea
  |
  v
Draft strategy + tests
  |
  v
Register experiment (rules and metrics freeze)
  |
  +--> Historical replay, when valid
  |       |
  |       +--> fail: revise as NEW version
  |       \--> pass operational gates
  |
  +--> Live shadow, all challengers concurrently
          |
          +--> insufficient data: continue
          +--> fail: retire/pause
          \--> owner promotes to paper
                  |
                  +--> execution/reconciliation failures: pause
                  +--> insufficient evidence: continue
                  \--> owner marks live eligible
                          |
                          v
                  Micro-live champion, approved entries
                          |
                          v
                  keep / pause / replace by owner
```

Every edit to a rule, threshold, feature, cost model, universe filter, exit policy, or parameter creates a new strategy version and normally a new arm.

### Short and long experiments

- **Operational experiment:** days or a small number of orders. Tests idempotency, fills, alerts, protection, and reconciliation. It does not claim strategy edge.
- **Strategy experiment:** enough matured, reasonably independent opportunities across more than one market condition. Duration is based on information count, not a convenient calendar deadline.
- **Long-horizon strategy:** remains in shadow until its decisions mature. It must not be ranked against a five-day strategy using incomplete open positions.

## 10. Evaluation rules

### Required comparisons

- Champion under the same opportunity set.
- Cash/no-trade baseline.
- Broad-market benchmark appropriate to the long-only equity scope, reported with exposure.
- Each challenger’s previous immutable version, if one exists.

### Primary metrics

- Net return after modeled and actual costs.
- Mean and median R per closed trade.
- Maximum drawdown and time under water.
- Exposure and turnover.
- Profit factor and win rate, never shown without sample size.
- Benchmark-relative performance.
- Strategy correlation and overlapping ticker/time exposure.
- Execution slippage: intended price versus filled price.

### Evidence controls

- Track every parameter/version attempted.
- Use chronological walk-forward splits, never random train/test shuffling.
- Promotion-eligible historical replay may consume only observations whose `known_at_utc <= decision_cutoff` and whose source is marked replay eligible.
- `archival_reconstructed` results are displayed separately and never combined with clean replay or forward-shadow evidence.
- Reserve a final untouched holdout for each historical experiment family.
- Prevent overlapping label windows from leaking between train and validation sets.
- Report uncertainty intervals using a time-aware or block bootstrap.
- Compute probability-of-backtest-overfitting/deflated-Sharpe style diagnostics when enough variants and observations exist.
- Never select a winner from raw return alone.
- Never use an LLM’s qualitative assessment as the primary promotion metric.
- Print `insufficient_evidence` instead of manufacturing a ranking when samples are too small.

Operational minimums may gate tier movement, but they are not proof of profitability. Recommended initial defaults:

- clean replay: at least 100 matured events when bitemporal data exists; reconstructed archival results do not satisfy this gate
- shadow: at least 60 calendar days and 100 matured decisions
- paper: at least 30 closed executions with zero unresolved reconciliation events
- micro-live: explicit owner approval, not a statistical auto-gate

These defaults must be configurable and displayed beside every result.

## 11. Portfolio allocation

V1 does not allocate capital by “pick the strategy with the highest recent return.”

- Shadow and paper arms receive independent virtual budgets.
- Only the live champion receives live capital by default.
- Every execution call carries the immutable arm mode. A paper arm must explicitly select the Alpaca paper adapter even when the application-wide primary broker is Robinhood or global execution mode is live. Any arm-mode/broker-mode mismatch fails before broker review or placement.
- A ticker-level exposure reservation prevents two arms from accidentally creating duplicate live positions.
- Portfolio risk is checked after combining all existing broker positions, not arm by arm.
- Strategy results retain their hypothetical independent performance even when a portfolio-level conflict blocks the live order.

Later ensemble allocation considers expected return after costs, uncertainty, drawdown, correlation, turnover, and current exposure. Equal weighting is the first ensemble baseline. Optimization and contextual bandits are later challengers, not infrastructure defaults.

## 12. Live order safety

Live execution remains disabled until this state machine is implemented for Robinhood and tested against a fake broker plus a supervised canary:

```text
proposed
   +--> owner_rejected / cancelled / expired / risk_rejected  [terminal]
   |
   v
owner_approved -> risk_reserved -> submitted -> accepted
                       |             |          |
                       |             |          +--> order_rejected [terminal]
                       |             +--> placement_unknown -> reconciliation_required
                       +--> review_rejected / cancelled / expired / failed_no_order [terminal]
                                         |          |
                                         |          v
                                         |     partially_filled
                                         |          |
                                         +----------v
                                               filled
                                                  |
                                                  v
                                      protection_pending
                                         |             |
                                         v             v
                                     protected    protection_failed
                                         |             |
                                         v             +--> PAUSE ALL NEW LIVE ENTRIES
                                      closing
                                         |
                                         v
                                       closed

Any state can move to reconciliation_required when broker and local state disagree.
Every terminal path releases its notional/risk reservation exactly once. An unknown placement outcome is not terminal and cannot release its reservation until reconciliation proves no order exists or resolves the order into the normal lifecycle.
```

### Mandatory invariants

1. Default mode is shadow. Absence or invalidity of a flag must never mean live.
2. Live requires all current gates plus the single globally active live target arm and an owner `promotion_event` whose target arm, shared strategy version, source evidence snapshot, mode, and approved budget all match.
3. A new live entry is refused when any existing live trade is `protection_pending`, `protection_failed`, or `reconciliation_required`.
4. The broker adapter declares capabilities. If the requested exit policy cannot be enforced, the order is refused before placement.
5. Entry placement, fill detection, protective order placement, protection verification, partial-fill adjustment, exit, and reconciliation are idempotent.
6. The daily notional counter reserves pending orders so concurrent approvals cannot overspend the cap. The unique active-execution constraint is acquired before reservation; retries and concurrent workers reuse or reject the existing `execution_id` and can never create a second broker placement for one decision.
7. Stale quote or stale snapshot means abstain.
8. Unknown broker responses are stored redacted and transition to manual review, never guessed successful.
9. A persistent kill switch blocks new orders even after process restart.
10. Telegram alerts include recovery instructions, not only error text.
11. Execution mode is an explicit immutable input, not inferred from global mutable settings. `shadow` cannot reach an order API; `paper` can reach only the Alpaca paper adapter; `live` can reach only the promoted live broker path.
12. Rejected, cancelled, expired, and verified-no-order failures are terminal and release reservations exactly once. Unknown outcomes remain reserved and block conflicting new entries until reconciled.

If Robinhood cannot provide a verifiable attached/protective exit primitive through its current interface, all Strategy Lab live entries remain impossible. Review-only analysis may continue, but no live position may be opened. The system must state that limitation directly and must not simulate safety with an in-process watcher that disappears during an outage.

## 13. Operator experience

Add owner-only Telegram commands after the underlying APIs exist:

- `/experiments`: running experiments and tier distribution.
- `/strategies`: champion, challengers, versions, and status.
- `/strategy <slug>`: decision count, matured count, results, drawdown, warnings, and recent decisions.
- `/pause_experiment <id>` and `/resume_experiment <id>`.
- `/promote_arm <source_arm_id> <tier>`: renders the source arm's immutable strategy version, evidence snapshot, warnings, proposed inactive target arm, and budget, then requires a confirmation callback. Live confirmation atomically replaces the global champion or fails without changing either arm.
- Every proposed live `strategy_trade` receives its own owner-only Approve/Reject callback containing a signed, expiring reference to `execution_id`. Approval atomically transitions only that row from `proposed` to `owner_approved`; duplicate, stale, mismatched-owner, non-proposed, or already-used callbacks are rejected. A strategy promotion never counts as approval of an individual entry.
- `/live_kill on|off`: persistent kill switch with confirmation and audit event.

Weekly report order:

1. System and data health.
2. Open broker/reconciliation warnings.
3. Champion scorecard.
4. Challenger league table with sample and evidence warnings.
5. Correlation/overlap view.
6. Recommended owner actions. Recommendations never execute themselves.

## 14. Migration and compatibility

### Phase 0: migration foundation

- Introduce Alembic and baseline the current schema.
- Prove upgrade from a copy of the existing SQLite schema and from an empty database.
- Change startup ordering so schema classification and Alembic migration run before any ORM session is created. After the baseline, production startup must not call `Base.metadata.create_all()` for Alembic-managed tables.
- Empty database: run `alembic upgrade head` to create the full schema.
- Existing unversioned database: verify an exact legacy schema signature, back it up, stamp the matching baseline revision, then run `alembic upgrade head`. Unknown or partially matching schemas fail closed with recovery instructions.
- `Base.metadata.create_all()` may remain only in an explicit test helper for isolated ephemeral databases; it is not a production migration fallback.
- Forbid new inline `ALTER TABLE` migrations after the Alembic baseline.
- Backup and restore test the SQLite file before production migration.

### Compatibility adapter

- Existing scans and memos continue unchanged behind `STRATEGY_LAB_ENABLED=false`.
- When enabled in shadow mode, build one `MarketSnapshot` after existing analysis and run registered arms.
- Adapt the existing final score into `swingtrader_composite_v1` without changing its outputs.
- Continue writing `ScoredCandidate` during the migration period. Strategy Lab writes are best-effort in shadow mode and must not break memo generation.
- Paper/live execution must reuse the existing `OrderManager` placement internals through a new explicit-mode entry point. The entry point receives the immutable arm mode and target broker adapter; it must not infer either from global settings. Strategy code never calls a broker directly.

### Feature flags

- `STRATEGY_LAB_ENABLED=false`
- `STRATEGY_LAB_SHADOW_ENABLED=false`
- `STRATEGY_LAB_PAPER_ENABLED=false`
- `STRATEGY_LAB_LIVE_ENABLED=false`
- `STRATEGY_LAB_LIVE_KILL=true`

Each higher tier requires every lower-tier gate plus its current broker safety gates. No deployment step changes these variables without explicit owner authorization.

## 15. Delivery plan and file ownership

The complete feature is larger than eight files. It should ship as small, independently reviewable PRs with no structural and live-behavior changes mixed together.

### PR 1: Domain, Alembic, and experiment persistence

Owns:

- `alembic.ini`, `alembic/`
- `database/models.py`, `database/db.py`
- `strategy_lab/domain.py`, `strategy_lab/registry.py`
- migration and model tests

No pipeline, broker, or production behavior change.

### PR 2: Snapshot builder, strategy SDK, and reference strategies

Owns:

- `strategy_lab/snapshots.py`
- `strategy_lab/execution_policy.py`
- `strategy_lab/strategies/`
- source-observation capture/normalization adapters required by enabled strategies
- pure unit and golden-vector tests

No broker calls. No scheduler integration.

### PR 3: Experiment runner, shadow executor, and evaluator

Owns:

- `strategy_lab/runner.py`
- `strategy_lab/shadow.py`
- `strategy_lab/metrics.py`
- replay/scoreboard CLI
- experiment tests

Must reuse `backtest/simulator.py` for the specified calendar-day, intrabar-stop, target, and slippage semantics. Do not fork or reinterpret them. A session-aware or close-triggered exit would be a new execution-policy version and requires an explicit backward-compatible simulator extension with golden tests.

### PR 4: Pipeline integration and operator reporting

Owns:

- narrow hook in `orchestrator/pipeline.py`
- scheduler jobs
- Telegram handlers and weekly report section
- feature flags and `.env.example`
- shadow-only end-to-end tests

Production flag stays off after merge.

### PR 5: Broker-independent live lifecycle and Robinhood capability closure

Owns:

- broker capability contract
- persistent order state and risk reservation
- Robinhood fill/protection/reconciliation implementation
- kill switch and incident alerts
- contract, integration, and safety regression tests

Must not enable production live mode. A real-money canary is a separate owner-authorized release step.

### PR 6: Paper tournament, promotion workflow, docs, and rollout

Owns:

- paper-arm dispatch through existing `OrderManager`
- promotion events and owner-only commands
- docs/PRD/capabilities/runbook updates
- full E2E and deployment canary checklist

## 16. Test plan

```text
Snapshot input
  +-- complete/fresh ----------------> deterministic strategy decision
  +-- missing required field --------> abstain + reason
  +-- stale field -------------------> abstain + reason
  +-- non-PIT historical field ------> replay refused
  +-- reconstructed archival field --> exploratory report only, never promotion

Decision
  +-- duplicate snapshot/arm --------> existing decision, no duplicate
  +-- shadow ------------------------> simulated trade, no broker call
  +-- paper -------------------------> existing paper order path
  +-- paper + global live mode ------> Alpaca paper only, or refuse mismatch
  +-- live without promotion --------> refused
  +-- live kill on ------------------> refused
  +-- live unsupported exit ---------> refused before order
  +-- live valid --------------------> state machine + protection verify

Broker lifecycle
  +-- rejected ----------------------> terminal rejected, budget released
  +-- partial fill ------------------> protection resized idempotently
  +-- timeout/unknown ---------------> reconciliation_required
  +-- protection failure ------------> alert + global new-entry pause
  +-- restart -----------------------> resumes from persistent state

Evaluation
  +-- no matured trades -------------> insufficient_evidence
  +-- open long-duration trades ------> excluded/labeled pending
  +-- costs missing -----------------> result blocked, not zero-cost
  +-- many variants -----------------> multiple-testing warning/diagnostic
  +-- correlated arms ---------------> overlap and correlation shown
```

Required test categories:

- Unit tests for every strategy rule, boundary, abstention, and execution policy.
- Golden tests with hand-computed OHLCV and event examples.
- Property tests for idempotency, bounded sizing, and deterministic hashes.
- Identity tests proving market decisions are stable while fresh portfolio/risk contexts can independently allow or block execution.
- Migration tests: empty database, current production-shaped schema, upgrade rerun, downgrade/restore rehearsal.
- Integration test: mocked scan -> snapshot -> all arms -> decisions -> shadow fills -> matured metrics -> Telegram report.
- Broker contract tests shared by Alpaca and Robinhood fakes.
- Safety regressions proving every missing/false/inconsistent live gate produces zero order calls.
- Restart/reconciliation tests from every nonterminal order state.
- Evaluation tests proving incomplete samples cannot be labeled winners.
- Provenance tests proving observations known after the decision cutoff are rejected and reconstructed archival results cannot satisfy promotion gates.
- Existing full suite, compileall, model lint, secret scan, and independent code review on every PR.

No live canary may submit an order until all safety tests pass, Robinhood exit capabilities are verified against current documentation/interface, and Bryan explicitly authorizes the canary budget.

## 17. Observability

Structured events:

- `strategy_snapshot_built|blocked`
- `strategy_decision_recorded|deduplicated|abstained`
- `experiment_arm_started|paused|completed`
- `strategy_trade_intended|filled|closed`
- `live_order_state_changed`
- `live_protection_verified|failed`
- `broker_reconciliation_mismatch|resolved`
- `strategy_metric_snapshot_created`
- `strategy_promotion_requested|confirmed|rejected`

Never log raw brokerage payloads, account numbers, tokens, or sensitive research-provider responses. Persist only the allowlisted fields required for audit and reconciliation.

## 18. Rollout

1. Ship migrations and domain objects with all flags off.
2. Enable local shadow mode against synthetic and copied data.
3. Enable production shadow mode only; compare existing SwingTrader output to its compatibility arm.
4. Register challengers and collect shadow decisions.
5. Run replay CLI and publish clean versus reconstructed-archival artifacts separately, including data limitations.
6. Enable Alpaca paper tournament after shadow health is stable.
7. Finish and verify Robinhood position protection/reconciliation.
8. Conduct an owner-authorized no-money/review-only canary.
9. Conduct one owner-authorized micro-live order with manual observation from entry through closure.
10. Keep human approval for every entry until a later, separately approved autonomy spec.

Rollback at every phase is a feature-flag change plus pausing experiment arms. Existing memo generation remains available throughout.

## 19. Definition of done

- Multiple strategy versions process the same snapshot deterministically; cross-sectional arms share a universe-scoped snapshot.
- At least the four initial strategy arms exist with documented data limitations.
- Historical replay refuses contaminated strategies and enforces time ordering.
- Shadow and paper tournaments run concurrently without duplicate broker orders.
- Scoreboard reports results after costs, sample size, uncertainty, drawdown, benchmark, and correlation.
- Promotion is owner-only and fully audited.
- Live mode cannot place an order without every gate, owner promotion, risk reservation, supported exit policy, and kill-switch clearance.
- Robinhood positions are tracked, protected, reconciled, and recoverable after restart, or live automation remains disabled with that limitation documented.
- All tests and CI pass; independent review finds no unresolved P0/P1 issue.
- Production remains unchanged until each release flag is explicitly approved.

## 20. Research references

- Bailey et al., backtest overfitting: https://escholarship.org/uc/item/4hn4t174
- Harvey, Liu, and Zhu, multiple testing in expected-return research: https://www.nber.org/papers/w20592
- Jegadeesh and Titman, cross-sectional momentum: https://doi.org/10.1111/j.1540-6261.1993.tb04702.x
- Bernard and Thomas, post-earnings-announcement drift: https://doi.org/10.2307/2491062
- Robinhood Agentic Trading risks and account model: https://robinhood.com/us/en/support/articles/agentic-trading-overview/
- SEC auto-trading caution: https://www.sec.gov/about/reports-publications/investorpubsautotradinghtm

## 21. Rulings log (post-build)

Ratified 2026-09-10 from the Strategy Lab builds (PR 1 #54, PR 2 #58, PR 3 #60,
PR 4 #64, PR 5 #65). Each is a decision a build session made where this spec
was silent or where the code as merged differs from the text above; the spec
text stands where the two agree, and this log is the record where they do not.

**PR 2 — snapshots and the SDK**

- **A future fact fails closed.** An input whose `known_at_utc` is after the
  cutoff raises `NonPointInTimeInput`; nothing is filtered quietly. Membership is
  filtered on both times: the `member_from <= day < member_to` interval *and*
  `known_at_utc <= cutoff`.
- **`REPLAY_ELIGIBLE_PRICE_SOURCES` is empty.** Every snapshot built from stored
  bars is `archival_reconstructed`, `replay_eligible=False`, exploratory only.
  No promotion-eligible replay exists until a licensed archival source carries
  availability/revision provenance; the way to change that is the source, not
  the flag.
- **One execution-policy contract.** `ResolvedExecutionPlan.policy_spec_fields()`
  produces exactly the constructor keywords of `comparables/outcomes.py::PolicySpec`,
  so Spec N's simulator is the one simulator; the fraction tuple crosses the
  package boundary instead of an import.
- **`short_term_reversal_v1` is universe-scoped and structurally shadow-only.**
  Its cap is a cross-sectional rule and cannot be evaluated per ticker without
  mixing cutoffs.
- **Abstain is not flat.** `abstain` means the strategy could not evaluate the
  name; `flat` means it evaluated and did not select. Indicators return `None`
  rather than padding a short window.
- Project assumptions labelled as such in each version's immutable config: the
  earnings evaluation cadence (first evaluation at which the record was
  knowable), signal-strength normalisation, top decile = `ceil(eligible/10)`,
  equal weight ⇒ `position_risk_pct = 1/selected`, and `cohort == "memo"` as the
  composite's actionability gate.

**PR 3 — runner, replay, measurement**

- **Reconstructed evidence is scored in its own section and can never rank a
  winner** or satisfy a promotion gate; the scorecard prints the evidence class
  in its section header.
- **An unmatured position is open, not flat at zero.** `ReplayOutcome.matured`
  is false when bars run out before the horizon with no exit; metrics count it,
  name it, and exclude it from every statistic.
- **Uncertainty and multiplicity are called, not reimplemented**: the bootstrap,
  effective sample size, Šidák and Romano–Wolf step-M come from
  `comparables/inference.py`. A run without the backends prints
  `uncertainty_unavailable` and refuses to name a winner.
- **The ranking gate is a project decision, printed on every card**: a leader is
  named only if the evidence is clean, no arm is blocked, every arm clears its
  floors, the multiplicity-adjusted lower bound is above zero, above the
  benchmark and above the runner-up's point estimate, and step-M rejects.
  Otherwise `insufficient_evidence`. Worth revisiting once a real shadow sample
  exists.
- **The variant denominator is the larger of pre-registered and tried**, retired
  and paused arms included.
- **Fills replay on the split-adjusted series**, matching Spec N's `policy_bars`;
  every outcome records `price_basis`. §7's "unadjusted OHLC for fills" is not
  implemented because a hold spanning a split cannot mix conventions without
  inventing a return.
- **A shadow fill walks the §12 state machine** with the lab standing in for
  owner, risk desk and broker, every row `mode='shadow'`, every hop checked
  against `EXECUTION_TRANSITIONS`.
- Deferred: CSCV / probability of backtest overfitting belongs in Spec N's
  inference layer as its own PR.
- A knife-edge fixture in the ranking-gate test flipped between Python 3.11's
  naive float `sum` and 3.12's compensated one; CI runs 3.12 and fixtures now
  keep real margins.

**PR 4 — shadow integration and operator surface**

- **Two real gates, not five.** `STRATEGY_LAB_ENABLED` (the read surface) and
  `STRATEGY_LAB_SHADOW_ENABLED` (the one path that writes). §14's paper, live
  and live-kill flags were withheld because their services did not exist; a
  switch whose "on" does nothing is worse than an absent one. PR 6 adds the
  paper and live flags with the services; Phase 6's persistent kill switch
  (`/live_kill`) is the one kill switch.
- **One ticker snapshot per scored name plus at most one universe snapshot per
  cutoff**, shared by every arm; never one per (ticker, arm), never a
  cross-section assembled per ticker.
- **A Strategy Lab failure never blocks memo delivery**: the hook runs last in
  the scan, isolated, logged, and continues.
- **Two PR 2 adapter defects fixed out of scope** because either made the
  compatibility arm produce nothing on every real scan: the pipeline writes a
  *view* (`bullish`/`bearish`/`neutral`) where the adapter tested a *side*
  (`long`); and the pipeline writes the memo before the ledger row, so the
  adapter's ordering filter never matched. Rows are now paired within a named
  two-hour window; a run id on `memos` should replace the window.
- **Settlement lags a full horizon plus grace** (30 days for the champion), so
  the scorecard reads empty for about a month after the flags flip; settling
  earlier would record `bars_exhausted` exits as matured outcomes.
- **Cross-sectional arms stay behind `STRATEGY_LAB_UNIVERSE_ENABLED`** until the
  price plane and `universe_membership` are backfilled.

**PR 5 — the execution machine**

- **Built on Phase 6's `ExecutionService`, not beside it.** Approval, fresh risk
  re-evaluation, the kill switch, placement, fill polling, the `gtc`
  `stop_market` with read-back, `unprotected` paging and daily stop replacement
  are Phase 6's and are not reimplemented. PR 5 adds the persistent §12 machine
  those steps land on, via an advisory observer seam.
- **The §12 rules are pure.** `strategy_lab/execution.py` (mode→venue binding,
  reserving/blocking state sets, redaction allowlist) is in `PURE_SDK_MODULES`
  and cannot import `config`, `database`, `execution` or `portfolio` — the
  enforcement point for invariant 11.
- **`registry.py` stays the one writer of `strategy_trades`**; PR 5's
  `open_execution` (reuse-on-race inside a SAVEPOINT), `advance_execution`,
  `reserved_notional`, `blocking_executions`, `resumable_executions` compose on
  PR 3's writer.
- **The reservation is a predicate on `status`, not a counter**, so "released
  exactly once" is structural: terminal states have no outgoing edge.
- **An attempt is `(decision, portfolio_context_hash)`** (inherited from PR 3):
  re-proposing a decision the owner just cancelled against an unmoved book
  resolves to the cancelled row and places nothing; a moved book is a new
  attempt.
- **Capability inspection is review-only.** `inspect_order_capabilities` reads
  the Robinhood server's own `tools/list` schema and reports disagreements with
  `ROBINHOOD_CAPABILITIES`; a `bracket`/`stop_loss` parameter appearing is a
  finding for a human, never an automatic capability. `AlpacaBroker` declares
  `can_place_attached_stop=False` on purpose so a gate that passes in paper
  cannot fail in live.
- **Five Phase 6 defects closed**: an ambiguous placement whose order existed was
  marked `failed` (now `reconciliation_required` either way); a partial fill's
  remainder left the stop undersized (resume cancels first, re-places at true
  size under a quantity-keyed `ref_id`); shadow rows leaked into `resume()` and
  `blocking_executions` (both exclude `shadow`); two resume hops jumped edges the
  spec does not draw; `AlpacaBroker` declared no capabilities.
- **Deferred to PR 6**: scheduling `resume()` / `expire_stale()` / `reconcile()`,
  and the owner approve/reject card for a live `strategy_trade`; no per-tranche
  history for partial fills (needs a migration no invariant asks for).
