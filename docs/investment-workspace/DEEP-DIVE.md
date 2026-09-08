# Investment Workspace — Deep Dive

*For whoever is about to implement or review a phase of specs K–Q.*

Read order for an implementer: K → L → M → N → O → P, with Q running in parallel
(README §2).

## 1. Architecture

```text
   Claude (web / cloud)   Claude Code (local / cloud)   Codex   Cursor
            \                    |                 |            /
              +------------- remote MCP + REST (HTTPS) ---------+
                                 |
                   ┌─────────────┴──────────────┐
                   │   Workspace API (FastAPI)  │   Spec K
                   │   one service, Railway     │
                   └─────────────┬──────────────┘
                                 |
     ┌──────────┬────────────────┼────────────────┬──────────────┐
     v          v                v                v              v
 Portfolio   Research        Comparable       Evidence       Strategy Lab
  ledger     workspace     -setups engine      planes         experiments
  Spec L      Spec M           Spec N          Spec O          Spec Q
     +----------+----------------+----------------+--------------+
                                 |
                    ┌────────────┴─────────────┐
                    │  Postgres (Railway)      │  structured, bitemporal
                    │  + repo Markdown mirror  │  narrative, git-versioned
                    └────────────┬─────────────┘
                    ┌────────────┴─────────────┐
                    │  Scheduled Python jobs   │  zero inference
                    └──────────────────────────┘
```
(README §4)

### 1.1 Three load-bearing boundaries (README §4)

1. **No agent touches a broker.** Agents call `propose_order`; only the execution
   service, after a recorded human approval, calls a broker adapter (Spec L §6).
2. **No LLM output is promotion evidence.** Model text motivates a hypothesis and
   summarizes a result; it can never be the statistic (Spec N §8; Spec Q §10).
3. **Every fact carries `known_at_utc`.** A fact without one is `archival_reconstructed`
   and quarantined (Spec O §2).

### 1.2 What runs where

**Railway service `swingtrader-workspace`** is one FastAPI app: `/health`, `/mcp`
(MCP for agents), `/v1/...` (REST, for the bot and scripts), `/admin/...`
(owner-only). The Telegram bot stays separate and calls the workspace over REST rather
than importing it, so a workspace deploy never restarts the trading monitor (Spec K
§4). **Postgres on Railway** is the structured, bitemporal store, in the existing
project `e556a6d9-2023-4c81-a031-e32e160a33be` (README §3; Spec K §3.2), chosen over
SQLite for concurrent readers, network reachability, and real `JSONB` querying.
**Scheduled Python jobs** — sync, pricing, maturation, cohort statistics,
reconciliation, evaluation, reports — carry zero model calls; a job needing an LLM is
not a job, it's a proposal for a human turn (Spec K §5; Spec P §6). The **git mirror**
writes every dossier and thesis as Markdown under `research/`, synced Postgres → repo
only (Spec K §3.3), making `git clone` the offline fallback: API down, a session still
has every thesis, dossier, and past decision, just not live prices.

### 1.3 Client attachment and auth (Spec K §4.1, §4.3)

Every client attaches to `/mcp` by copy-paste config: `.mcp.json` (Claude Code),
`.cursor/mcp.json` / `.codex/config.toml` (Cursor, Codex), `docs/WORKSPACE_ACCESS.md`
(Claude web/cloud). `AGENTS.md`, a Linux Foundation standard read natively by Codex,
Cursor, and Copilot, is the shared entry point; `CLAUDE.md` line 1 is `@AGENTS.md`.

Auth is one long-lived **owner token** per client, hashed, revocable, scoped to `read` /
`research:write` / `propose` / `admin`. **There is no `execute` scope** — order
execution is unreachable by token — and `/admin` needs a separate token never placed in
an agent's environment. Limits: 60 read / 10 write calls per minute per token. Claude
Code's documented path is OAuth 2.1, Codex accepts a static bearer (verified), so the
server accepts **both** — one tool surface, one scope model.

### 1.4 The fifteen MCP tools (Spec K §4.2)

Read tools are side-effect-free; every one returns a `provenance` block (`as_of_utc`,
per-field source, staleness, data-quality tier), and a stale-past-budget tool returns
the value **with** the flag set, never a silent guess.

| Tool | Kind | Returns |
|---|---|---|
| `portfolio_overview` | read | positions, cash, exposure, sync freshness |
| `position_detail` | read | one position: lots, basis, unrealized, linked thesis |
| `orders_open` | read | pending and recently filled orders across brokers |
| `research_get` | read | dossier + current thesis + invalidators for a ticker |
| `research_search` | read | full-text over dossiers, theses, and decisions |
| `research_write` | write | upserts a dossier section or thesis, with provenance |
| `thesis_review` | write | records hold / weakened / invalidated |
| `compare_setups` | read | the Spec N cohort answer |
| `cohort_detail` | read | the constituent events behind a cohort answer |
| `filings_recent` | read | 13F/13D/G/Form 4 for a ticker or tracked investor |
| `macro_state` | read | current regime + the vintage-correct series |
| `news_timeline` | read | timestamped, deduped news for a ticker |
| `experiments_status` | read | Strategy Lab arms and scoreboard |
| `propose_order` | write | creates a `proposed` order row; places nothing |
| `journal_append` | write | appends a dated note to the decision journal |

## 2. Data model and provenance

Every plane writes into one table, `source_observations`, the bitemporal ledger
defined in Spec Q §8; a plane's whole job is producing rows in it with an honest
`known_at_utc` (Spec O §1). Its columns: entity/ticker, observation type, `valid_at`,
`known_at_utc`, provider, provider revision/as-of id, normalized payload JSON, payload
hash, optional raw-source reference, optional superseded-observation id, a
`replay_eligible` flag, and quality warnings; backfilled rows of unknown historical
availability are `archival_reconstructed` and `replay_eligible=false`.

The universal rule (Spec O §2): every fact carries `valid_at` (when it applies) and
`known_at_utc` (when we could first have known it); a guessed `known_at_utc` means
`archival_reconstructed`, and can never support a `clean_pit` cohort or a Spec Q
promotion. For SEC filings this is unusually clean, since the acceptance timestamp *is*
`known_at_utc` — verified live, a Form 4 with `filingDate` 2026-09-03 was accepted at
22:30 UTC that same day, after the close, so keying on filing date treats post-close
information as available intraday, in the direction that flatters results. **It is
`acceptanceDateTime`, never `filingDate`.**

Every row also carries **`precision`** (`second` | `day`) and a **provenance class**.
Several planes are dated, not timestamped, so a `day`-precision fact is known at the
**close** of that date, never its open (Spec O §2). Three provenance classes (Spec N
§8): **`observed_live`** (recorded when it happened), **`vendor_pit`** (a vendor's
filing- or acceptance-date stamp — reconstructed but defensible), **`archival_reconstructed`**
(a guessed `known_at_utc`). `observed_live` and `vendor_pit` may be pooled, with the
mix printed; `archival_reconstructed` is never pooled with either.

**How a fact becomes eligible.** The bitemporal filter (Spec N §4.1) requires every
qualifying fact to satisfy `known_at_utc <= event_cutoff`; facts lacking a trustworthy
`known_at_utc` land the whole cohort in the degraded tier. The engine never reads a
provider API at query time — only `source_observations`.

**News eligibility matrix** (Spec O §5.3), enforced by Spec N:

| Use | Requires | Provenance class |
|---|---|---|
| Date an event (event clock) | publisher timestamp + primary tier | `vendor_pit` |
| Qualify a cohort fact (e.g. `consensus_eps_news`) | publisher timestamp + tier ≥ established wire + deterministic extraction | `vendor_pit` |
| Covariate / novelty context | publisher timestamp | `vendor_pit` |
| Dossier evidence, invalidator trigger | any tier, rendered with its tier | — |
| Spec Q promotion evidence | never | — |

## 3. Portfolio ledger (Spec L)

### 3.1 Domain model (§3)

New Alembic-managed tables, tz-aware UTC: **`brokerage_accounts`** (broker slug,
external id, type, capability set, sync state); **`holdings`**, point-in-time by
append — a sync writes a new row rather than updating, so "what did I hold on date D"
is answerable, and any option position is stored with `instrument_type` and rendered
`unsupported_instrument_present`, never omitted; **`tax_lots`** (booking method from
`{STRICT, FIFO, LIFO, AVERAGE, NONE}`, borrowed from beancount; null means *unknown*,
never zero; a derived **`wash_sale_window`** flag fires on a buy inside the IRS
Publication 550 61-day window, informational only); **`cash_balances`**;
**`broker_orders`** (external orders appear too, with `origin='external'`);
**`portfolio_snapshots`** — daily rollup plus, computed with no vendor risk model,
beta over 60/250 sessions, realized volatility, and **max/average pairwise**
correlation among largest positions rather than a full matrix; **`exposure_tags`**
(narrative tags sourced from dossiers).

### 3.2 Sync job and failure policy (§4)

Hourly during market hours, on demand when freshness is exceeded, after close, and
pre-market; freshness budget 60 minutes intraday, past which every response carries
`stale=true`. **Failure policy:** a broker error leaves prior rows intact and marks the
account stale — it never zeroes a position; a sync that would delete >50% of known
holdings fails closed and pages instead of writing.

### 3.3 Broker capability set (§5)

```python
@dataclass(frozen=True)
class BrokerCapabilities:
    can_read_positions: bool
    can_read_tax_lots: bool
    can_read_orders: bool
    can_place_equity_market: bool
    can_place_equity_limit: bool
    can_place_attached_stop: bool      # a protective exit that survives our process
    can_place_bracket: bool
    supports_fractional: bool
    market_hours_only: bool
```

`can_place_attached_stop` is decisive: if a broker cannot place a protective exit that
outlives the process, no unattended entry may open on it. Callers check capabilities
before intent.

### 3.4 Robinhood — verified facts (§5.1)

The workspace talks to the *official* Robinhood Trading MCP
(`agent.robinhood.com/mcp/trading`); `execution/brokers/robinhood.py` calls nine tools,
all confirmed on Robinhood's published list — reads, quotes, tradability checks, and
the three write tools `review_equity_order`, `place_equity_order`, and
`cancel_equity_order`. Verified from Robinhood's own pages: it is **beta, not GA**, with no developer
documentation (no order-type table, no `place_equity_order` schema, no rate limits, no
token lifetime, no SLA; onboarding and re-auth are desktop-only); **placement is
confined to the Agentic account**, verbatim, "Your agent can only place trades in your
Robinhood Agentic account," while reads span all accounts, and a **cash** Agentic
account settles **T+1** with margin borrowing unavailable; options are readable **and
tradeable** but the workspace never uses the option or crypto write tools
(import-graph test); **no tool exposes dividends, cash movements, deposits, fees, or
corporate actions**, so the ledger reconstructs dividends from a market-data feed,
flagged `reconstructed`; and **order types, brackets, OCO, and GTC persistence are
undocumented** — the `tools/list` schema for `place_equity_order` /
`review_equity_order` is the only source, and dumping it is the **first Phase 1
checkpoint**, deciding `can_place_attached_stop`. **Unattended session survival is
also undocumented**; the second checkpoint runs the sync unattended on Railway for 30
days, logging every token refresh, and until proven, any read path feeding a proposal
refuses rather than serves stale data.

### 3.5 Execution path (§6)

```text
agent session ──propose_order──► broker_orders row (status='proposed')
                                          │  approval card, out-of-band channel
                                          │  (Telegram today) — never a tool an
                                          │  agent can call
                                          ▼
                                   owner approves
                                          ▼
                            execution service (not an agent)
                            risk check → reserve → place → verify protection
                                          ▼
                                    reconcile + ledger
```

Invariants: no agent-reachable code path calls a broker placement method (import-graph
test); risk is **re-evaluated from fresh state at approval time**, never reused;
approval is per-order, single-use, expiring, owner-bound; a breach lands the proposal in
`risk_rejected` with the reason; the kill switch (`/live_kill on`) survives restart; an
agent never chooses a quantity.

### 3.6 The fill-to-stop race

Since a stop is likely a **separate order placed after the fill**, the execution
service owns the race: entry fills → stop placed → stop **read back from the broker**
→ only then `protected`, and a fill not read back as protected within the configured
window pages immediately. Until proven, `can_place_attached_stop=False` (§5.1).

### 3.7 Position sizing (§6.6)

An agent never chooses a quantity. `propose_order` carries `entry`, `stop`, and a
`risk_fraction` of equity (default 0.5%, hard cap 1%); the execution service computes
size as `risk_dollars / (entry − stop)`, then applies concentration/sector/notional
caps.

A proposal carries at most one `cohort_answer_id`, which must resolve to a
`depth="full"` answer for the same ticker, computed within the last 5 sessions, with
`status="ok"` — `insufficient` or `inconclusive` does not count. Let `LB`, `PE` be the
lower 90% bootstrap bound and point estimate of the policy-simulated net return (§5.3)
at the nearest horizon, as fractions:

```text
m = clip(LB / PE, 0, 1)   when PE > 0 and LB > 0
m = 0                     when LB ≤ 0 or PE ≤ 0
risk_fraction_effective = risk_fraction × m
```

**Two budgets, advisory mode** (owner decision 2026-09-08, Spec L §6.6). Evidenced
proposals (a cited `full` answer with `status="ok"` and a positive lower bound) draw
from the evidenced budget, scaled by `m`. Everything else — an uncited proposal, a
citation that is `insufficient`, or a citation with `LB ≤ 0` — is re-labelled
`discretionary`, draws from a separate budget (`DISCRETIONARY_RISK_CAP` per trade,
`DISCRETIONARY_DAILY_NOTIONAL`), and carries the evidence on the card. Nothing is sized
to zero on evidence alone. `EVIDENCE_GATE_MODE=advisory` is the default; `strict`
restores zero-sizing and refuses uncited proposals with no code change. Kelly-style sizing is explicitly not used — it needs an edge estimate Spec
N has just spent proving is uncertain. The card shows `risk_fraction`, `m`, `LB`, `PE`,
horizon, and every cap that bound the final size.

## 4. Research workspace (Spec M)

**Dossiers** (`dossiers`, `dossier_sections`) — one per ticker, versioned free-text
sections, append-only revisions. Every section carries `updated_by` (`human`/model id)
and `sources`; unsourced sections render a warning everywhere (§3).

**Theses** carry a claim, direction, horizon, a **stated probability** (0–1) required
to reach `active` (without a number a thesis can never be scored), the argument as
numbered claims with evidence, a required non-empty bear case, status flow `draft →
active → weakened → invalidated → closed`, linked positions/cohorts, and
`next_review_at` (§3).

**Invalidators** (`thesis_invalidators`) are the load-bearing table — five types:

| Type | Example | Checkable |
|---|---|---|
| `metric_threshold` | gross margin below 45% for two quarters | yes, fundamentals |
| `price_level` | closes below $82 for three sessions | yes |
| `event` | guidance cut / CFO departure / 13D exits | yes, Spec O planes |
| `time_decay` | no re-rating within two quarters | yes |
| `qualitative` | the AI capex narrative breaks | no — human review |

Three rules (§4): a thesis with no invalidator cannot leave `draft`; at least one must
be machine-checkable (a thesis disprovable only by a change of mood is a feeling);
invalidators are set before the position, never after — post-entry ones are
`post_hoc=true` and excluded from honesty metrics. A scheduled job checks every
checkable invalidator daily and, on trigger, moves the thesis to `weakened` and pages;
**the system never auto-closes a position**.

**Decision journal** — append-only: decision type, the thesis and cohort evidence at
the time (hashed), sizing rationale, expected hold, and — filled in later — what
actually happened (§3).

**Probability and calibration** (§6): a Brier score with reliability/resolution/
uncertainty decomposition after ten resolved theses; a calibration table (three coarse
buckets) after forty. A revised probability keeps the *original* number for scoring,
with the revision kept in history.

**The Markdown mirror is deliberately partial** (§5): `research/` holds companies,
theses, journal, and open questions, front-matter linking back to Postgres. It cannot
losslessly round-trip everything, since Spec K §3.3 forbids news-derived content and
vendor series in a public repo — news- or vendor-tier sections export as a
withheld-content marker carrying the Postgres id, and the round-trip guarantee applies
only to permitted content. Postgres gives querying and joins; git gives durability,
diffability, and the offline fallback; neither alone suffices.

## 5. The comparable-setups engine (Spec N)

The centerpiece: every other spec either feeds it or delivers it. One query end to end:

### 5.1 SetupSpec (§4.0)

```python
@dataclass(frozen=True)
class SetupSpec:
    slug: str
    version: str
    conditions: tuple[Condition, ...]
    universe: str                       # e.g. "liquid_us_equity_v1"
    horizons_sessions: tuple[int, ...]  # trading sessions, not calendar days
    execution_policy: str | None
    match_covariates: tuple[str, ...]
    lookback_years: int
```

The spec is content-hashed; an answer is keyed by `(setup_hash, as_of_date,
data_snapshot_version)`, reproducible and cacheable. **Changing any condition creates a
new version**, so a question cannot be rewritten after seeing the answer. Setups are
**pre-registered, not improvised**: a small fixed roster ships in `comparables/setups/`
(earnings SUE, gap-and-go, insider cluster); a natural-language request is translated
by the agent into a `SetupSpec`, **shown back in full before the cohort is built**, and
counted as a trial against the nearest family (§4.0, §7).

### 5.2 Cohort construction (§4)

Facts must satisfy `known_at_utc <= event_cutoff` (§4.1). Universe membership is a
**stored** table, `universe_membership(universe_slug, ticker, member_from, member_to,
source, known_at_utc)`, never computed from today's data — "liquid US equities" from
*current* liquidity is survivorship bias in disguise, since names that died were
illiquid on the way down. No affordable Russell or S&P history exists, so the primary
universe is self-defined: rank-by-market-cap-and-volume applied point-in-time,
`liquid_us_equity_v1` (§4.2).

**Delisting: resolved vs. censored** (§4.4). A delisting with a known reason, an
acquisition with deal terms, or a to-zero name is **matured, not censored**: the
configured terminal return applies at the delisting date and carries flat through
every remaining horizon, counting toward `n_matured` and `delisting_rate`. An unmatured
event, or one ending with an *unknown* reason, is `n_censored`, reported separately,
never silently dropped. Terminal returns by reason are named and configured — **≈−30%
for NYSE/AMEX, ≈−55% for Nasdaq** performance-related delistings (Shumway 1997; Shumway
& Warther 1999) — because completeness is **unverified for every vendor** and the
failure is silent: a file can keep a delisted ticker's history and still stop at the
last quote. `scripts/audit_delisting_returns.py` checks twenty known delistings before
any vendor is paid for, the **within-cohort delisting rate** is a first-class output,
and a cohort met by survivors alone is refused on composition, not only on n.

**Three price series; replay is split-adjusted** (§4.3). Raw OHLC (actual fills),
split-adjusted (signals/covariates), and total-return (benchmark comparisons) are all
stored with factors and ex-dates. **Replay runs on split-adjusted**, because
`backtest/simulator.py` has fixed stop/target fractions with no corporate-action input,
and on raw prices a neutral 2-for-1 split mid-hold would fire the stop; a
split-invariance test asserts identical `TradeResult` with and without a mid-hold
split.

### 5.3 Matching (§4.5)

Two diagnostics, answering different questions: **is the query event typical of its
cohort?** — a single event has no variance, so the query's **standardized distance**
from the cohort mean and its **cohort percentile** are used (distance > 1.0 warns, >
2.0 is `atypical`); **is the matched cohort representative of the eligible pool?** —
here two groups exist, so SMD and variance ratio apply (|SMD| > 0.10 warns, > 0.25
`unbalanced`; variance ratio outside [0.5, 2] warns). Matching uses exact matching
where sample allows, coarsened exact matching (Iacus, King & Porro) as fallback;
propensity-score matching is not used, since it can *increase* imbalance. **Sector is a
current-vintage covariate**, never a required stratum — historical GICS is an
institutional licence, so a migrated name is matched by where it ended up.

### 5.4 Outcome measurement — §5.0 definitions

Fixed for every metric, reproduced to the cent by a hand-calculated fixture in `docs/`:

- **Event clock.** Session 0 is the first session whose open is at or after
  `known_at_utc`; entry is the open of session 0; horizon `h` ends at the close of
  session h−1.
- **CAR** = Σ over sessions 0..h−1 of (event daily return − benchmark daily return).
- **Calendar-time portfolio.** Each calendar session, the portfolio holds every event
  still in-window, equal-weighted; regressed `p_t − r_f,t = α + β(r_m,t − r_f,t) + ε_t`;
  the headline horizon estimate is **`α × h`**, CI from the block bootstrap of
  `{p_t − r_m,t}` scaled the same way.
- **Policy return** — the simulator's net P&L over entry, percent of entry notional,
  same event clock.

Four numbers are always reported together, because reporting one is how the failure
modes get in (§5.1–§5.4). The headline abnormal return is the calendar-time α × h,
cross-checked by cross-sectional mean CAR; where they disagree, calendar-time is the
tiebreak (Fama 1998; Mitchell & Stafford 2000).

**Policy-simulated return is net of costs**: a half-spread-by-liquidity-decile estimate
plus 10 bps adverse slippage at baseline, with sensitivity at 25 and 50 bps; gross is
shown too, labelled gross. T+1 settlement on a cash Agentic account is modelled as a
redeploy delay.

**Stability over time** (§5.5): every cohort splits chronologically (early/late
halves, per-year where n allows), because a cohort pooling 2010 with 2025 can report an
effect (like post-earnings drift) that has since decayed. `decayed=True` fires when the
late-half CI excludes the early-half point estimate toward zero, or the sign flips; the
pooled number is never rendered instead of the split.

### 5.5 Inference (§6)

**Uncertainty:** a stationary block bootstrap (Politis & Romano 1994) over calendar
time, block length **estimated** by `arch.bootstrap.optimal_block_length`, floored by
the horizon and printed. **Clustering:** the calendar-time portfolio is the **primary
estimator**, dissolving cross-sectional clustering instead of correcting for it;
two-way clustered CAR regression is the **cross-check**, not the headline; `n_eff`
(effective sample size from distinct dates and within-date correlation) is always
reported beside `n_matured`. With **fewer than ~30 clusters** the clustered SE is
`unreliable` and only the bootstrap CI renders; the two methods must agree in sign or
the response downgrades to `inconclusive`, and same-sign different-magnitude headlines
the calendar-time figure. **Null tests, automatic:** placebo dates, random cohorts
(empirical p-value), and a pre-event window (a large pre-drift is a leakage warning,
not a bonus). **Small-sample honesty:** v1 ships **Wilson intervals** for proportions
(never normal-approximation) and the bootstrap CI; empirical-Bayes shrinkage toward the
family's pooled estimate (`n/(n+k)`, `k=σ²/τ²` by method of moments) is **gated on a
family having at least five cohorts** — below that, `shrinkage: null` with the reason.
**The engine's own scoreboard** (§6.5) scores every cited `full` answer once matured on
two things only — the **sign** of the realized return versus the estimate, and its
**percentile within the cohort's outcome distribution** — and the CI on the cohort mean
is explicitly **never** scored as a predictive interval.

### 5.6 The floor and refusal schema (§8)

**Evidence tier and result status are separate axes.** Tier: `clean_pit` /
`vendor_pit` / `archival_reconstructed`. Status: `ok` / `inconclusive` /
`insufficient`. `insufficient` is a distinct schema with **no statistic fields**;
`inconclusive` is a full answer with both estimates shown. **One floor configuration**
— `COHORT_FLOOR_DISTINCT_DATES=20`, `COHORT_FLOOR_MATURED=30` — is checked on distinct
dates **first**, because clustered events are not independent observations.
`insufficient` is a valid, expected, frequently-correct answer and must render as a
refusal, never a hedge. `depth`: **`quick`** (raw, CAR, n, `n_distinct_dates`,
bootstrap CI, tier, balance, trial count) for fast iteration; **`full`** (everything)
is **mandatory** for any answer cited by `journal_append`, `research_write`, or a
promotion — a `quick` answer cannot be cited, enforced by the type system.

### 5.7 Trial accounting and StepM (§7)

Every query is logged in `comparable_queries`; the multiplicity equivalence class is
the **setup family slug** — the same key used for shrinkage, so renaming cannot reset
the count. Adjustment is a **Romano–Wolf stepdown** (`arch`'s StepM); raw and adjusted
p-values print with the trial count and **the number of cells examined**. **Deflated
Sharpe, PBO, and CPCV are not computed here** — they are defined over a strategy's
Sharpe under N trials, not a cohort's mean abnormal return; applying them to a CAR
"produces a number that looks rigorous and means nothing." They live in Spec Q §10.

### 5.8 Twelve failure modes and their defences (§2)

| # | Failure | Defence |
|---|---|---|
| 1 | Survivorship | §4.2 point-in-time universe; delisted names retained with terminal returns |
| 2 | Lookahead in facts | §4.1 `known_at_utc <= t` filter, no exceptions |
| 3 | Adjustment bias | §4.3 signals on adjusted, fills on unadjusted, both stored |
| 4 | "The market did it" | §5.2 benchmark and sector return subtracted, always |
| 5 | Overlapping windows | §6.1 block bootstrap **and** calendar-time cross-check |
| 6 | Cross-sectional clustering | §6.2 clustered/calendar-time standard errors |
| 7 | Regime confound | §5.4 regime stratification, refused if single-regime |
| 8 | Multiple testing | §7 every query logged; deflated significance; trial count printed |
| 9 | Small n as a percentage | §8 n and CI mandatory; refusal below a floor |
| 10 | Selection on outcome | §4.4 unresolved/censored events counted and reported |
| 11 | Wrong exit semantics | §5.3 outcomes under the *actual* execution policy too |
| 12 | LLM as the statistic | §9 model output can never be a number in a response |

### 5.9 Reused engine and its exit semantics

The engine reuses `backtest/simulator.py` because no open-source engine reproduces its
semantics and §10 requires bit-for-bit equality (§3). Per its module docstring: entry
fills at the **open of the bar after the signal** (T+1 open), never the signal-day
close; a stop crossing exits at the stop price, but a bar opening through the stop
fills at the worse open (gap-through); half exits at Target 1 (favorable gaps fill at
the open) with the rest riding the unchanged stop, and the remainder exits at Target 2;
a time exit closes whatever's left at the close of the first bar on or after
`entry_date + max_holding_days`, counted in **calendar days**; a same-bar stop/target
conflict resolves **pessimistically**, stop first; and slippage (default 10 bps)
applies adversely to every fill — this is exactly the semantics set Spec N §3 cites as
the reason no substitute engine qualifies.

## 6. Evidence planes (Spec O)

### 6.1 Filings

| Form | Content | Honest use |
|---|---|---|
| 13F-HR | Long positions of large managers | **Deferred from Phase 4** — quarterly, up to 45-day lag, nearly useless at 1–20 sessions |
| 13D | Beneficial ownership with intent | Activist situations; intent matters more than the stake |
| 13G | Passive above-threshold ownership | Ownership structure, float, index share |
| Form 4 | Insider transactions | **Genuine timeliness** — open-market purchases by insiders are informative |
| 8-K | Material events | Event detection and precise `known_at_utc` |
| N-PORT / N-CSR | Fund holdings | Cross-check on 13F |

Form 4 must distinguish by **transaction code**: `P`/`S` are informative; `A`/`M`/`F`/`G`
are never pooled with them; the 10b5-1 planned-sale checkbox is flagged separately —
conflating them inverts the signal (§3.1). **Entity resolution** (§3.2, a week of work)
rebuilds CIK ↔ ticker ↔ security history from `submissions` `formerNames`, since
EDGAR's current snapshot alone is insufficient, keeps a curated `tracked_investors`
list, and resolves CUSIP → ticker via OpenFIGI with unmapped **and ambiguous** rows
surfaced, never dropped.

**Sourcing** (§3.4): `edgartools` (MIT) plus SEC bulk sets and OpenFIGI. SEC XBRL
`companyfacts` joined to `submissions` `acceptanceDateTime` gives a `known_at_utc` to
the second, free, from the regulator; restatements arrive as new facts at later stamps.
**Coverage is per tag, not per company** — filers migrate tags (`Revenues` →
`RevenueFromContractWithCustomerExcludingAssessedTax`), dropping firms from cohorts
non-randomly since migration correlates with filer size — so `filings/xbrl_aliases.py`
holds alias maps and the ingest job **alerts on a coverage discontinuity** rather than
absorbing it.

### 6.2 Macro

`data/macro_data.py` reads current, revised FRED values; the fix is a **vintage-aware**
path via ALFRED so `macro_state(as_of=<past date>)` returns only what was published by
then — an unreleased series is **absent**, not back-filled (§4.1). Vintages are
**day-precision**, known at the close of the vintage date; `USREC` is usable only via
its ALFRED vintage, never the current series.

`regime_v1` is a **versioned, deterministic** classifier over vintage-correct inputs.
**Deterministic is deliberate, not simple** — HMM regime models overfit without
regularisation and collapse onto tiny-variance regions, so practitioners fall back to
fixed-parameter classifiers for the same backtest-reliability reason this system
needs. A threshold change creates `regime_v2`, never a silent relabel; no LLM assigns a
regime, though one may narrate it. It uses only **never-revised** inputs (price
trend/drawdown, realized vol, VIX, yield curve), so it is point-in-time by construction
and doesn't wait on Phase 4's macro-vintage work; revised series enter later as
`regime_v2` (§4.2).

### 6.3 News

**Alpaca News** (Benzinga-sourced, 2015→, free) is the primary timestamped source;
Finnhub is a cross-check; Gemini-search + Firecrawl cannot establish publication time
and is removed from every cohort path (§5). **Two `known_at_utc` values must not be
merged:** a story's is the earliest defensible publisher timestamp in its cluster, but
every **fact extracted from an article** carries that article's own timestamp, never
the cluster's earliest — otherwise a number first appearing in a 16:45 reaction piece
inherits a 07:00 preview's timestamp (§5.1). Near-duplicate coverage clusters
deterministically (canonical-URL, then MinHash/LSH), so twenty republications count
once (§5.2). **What may not leave Postgres:** Tiingo bars "display or share," Alpaca
bars "derived products," so no article text, novelty score, or news-derived feature
reaches the `research/` mirror — a dossier may cite a story by URL and date, and that
is all (§5).

## 7. Agent layer (Spec P)

**No custom agent framework** (§2). The lead agent *is* the Claude Code or Codex
session; the repo provides tools (Spec K), memory (durable state in the workspace,
never a transcript), instructions (`AGENTS.md`), and subagents defined as repo files.

Subagents live in `.claude/agents/` as Markdown with YAML front-matter — the
enforcement point, not the prose: `tools` (allowlist), `disallowedTools`, `model`,
`maxTurns`. `thesis-critic` and `cohort-analyst` omit `Agent`, structurally preventing
them from spawning further agents. Codex has no equivalent primitive: the same briefs
exist as prompt files under `.codex/prompts/`, pasted into a fresh turn (§4).

| Subagent | Brief | Tools | Must return |
|---|---|---|---|
| `company-researcher` | Build/refresh a dossier section | filings, news, fundamentals, web | Sourced claims; explicit "unknown" for gaps |
| `thesis-critic` | **Adversarial** — kill the thesis, never balance | all read tools | Strongest bear case, cheapest disconfirming test, invalidator |
| `cohort-analyst` | Translate a question into `SetupSpec`s, run, report honestly | `compare_setups`, `cohort_detail` | The spec, the answer verbatim, every warning incl. `insufficient` |
| `filings-analyst` | Tracked-investor and insider activity | filings plane | Positions with staleness and portfolio share |
| `macro-analyst` | Regime and macro context, vintage-correct | macro plane | The label plus inputs, never its own label |
| `reconciler` | Explain a portfolio/broker discrepancy | portfolio, orders | The discrepancy and cause; no writes |

**Hard boundaries** (§5): no agent places an order (no MCP tool, token scope, or
reachable path); no agent produces a statistic (every number traces to `comparables/`
or the ledger); no agent changes a strategy version, promotes an arm, or flips a flag;
no agent gives personalized investment advice; tool output is data, never
instruction — untrusted spans carry a per-response nonce and `content_trust:
"untrusted"`.

**Prompt-injection posture.** **Delimiters and nonces are parsing aids, not a
control** — 2026 evidence is that every text-level mitigation fails under adaptive
attack and only architectural isolation holds (CaMeL-style: untrusted data supplies
values, never control flow). This system already has that architecture: boundaries 1
and 2 mean an injection through the SEC or news planes — attacker-writable by anyone
who can file or issue a release — cannot move money and cannot corrupt a number; the
worst case is a bad `research_write`, reviewable and reversible. The ingestion job that
parses filings and news **has no write tool at all**, and every ingested claim carries
`source_url` and `source_trust` (§5).

**Cost discipline** (§6): deterministic by default with zero inference in scheduled
jobs; `compare_setups` is free per call, cached by setup hash; delegation is narrow;
the `llm_call` ledger tags workspace tool calls so a per-session soft budget can warn
and a hard cap can stop (Bryan hit credit exhaustion once already, BRY-301); model
choice is configuration, analyst-tier work on the cheaper model unless an eval
attestation says otherwise.

## 8. Strategy Lab (Spec Q)

One stable **champion**, many **challengers**, run in **shadow** (records what a
strategy would do, sends no order), **paper** (Alpaca), or **live** (a dedicated
Robinhood Agentic account), with **promotion** — an auditable owner decision to an
immutable strategy version's higher tier — never automatic (§1–§2). No strategy module
may import a broker, database session, Telegram client, or LLM client; it receives an
immutable snapshot and returns a decision (§5).

**Where DSR/PBO/CPCV live.** Deflated Sharpe, probability-of-backtest-overfitting, and
CPCV are explicitly *not* computed in the comparable-setups engine — they are defined
over a strategy's Sharpe under N trials, not a cohort's mean abnormal return. They
belong to, and live in, Spec Q §10 (Spec N §7).

**Promotion tiers**, gates rather than proof of profitability (§10): **clean replay** —
at least 100 matured events when bitemporal data exists, `archival_reconstructed`
never satisfies it; **shadow** — at least 60 calendar days and 100 matured decisions;
**paper** — at least 30 closed executions with zero unresolved reconciliation events;
**micro-live** — explicit owner approval, not a statistical auto-gate.

Live execution runs a state machine (proposed → owner-approved → risk-reserved →
submitted → accepted → filled → protection-pending → protected → closing → closed,
with `reconciliation_required` reachable from any state) that stays disabled for
Robinhood until tested against a fake broker and a supervised canary. If Robinhood
cannot provide a verifiable protective-exit primitive, all live entries remain
impossible — review-only analysis may continue, but the system must state the
limitation directly rather than simulate safety with an in-process watcher (§12).

## 9. Delivery

| Phase | Ships | Depends on |
|---|---|---|
| **0a. Schema discipline** | Alembic baseline; SQLite-ism audit; CI matrix on SQLite + Postgres | — (dispatch first) |
| **0b. Cutover** | Postgres provisioned, one-shot migration with parity report, service skeleton | 0a; blocks 1, 2, 4 |
| **1. See it** | Spec L ledger + Robinhood sync + Spec K read-only surface; checkpoint 1 dumps the order schema, checkpoint 2 starts the 30-day refresh log | 0b |
| **2. Remember it** | Spec M dossiers/theses + Markdown mirror | 0b |
| **3a. Minimum SEC ingestion** | XBRL `companyfacts`, `submissions` `acceptanceDateTime`, 8-K 2.02 index into `source_observations` | 0a only |
| **3b. Measure it** | Spec N engine, `quick` depth first, price-only setups before earnings roster | 3a |
| **4. Widen it** | Spec O filings (13F deferred), macro vintages, news timestamps | 0b |
| **5. Race it** | Spec Q Strategy Lab, its own six-PR plan | 0a only |
| **6. Act on it** | Spec L §6 proposal→approval→execution, live only per Spec Q §12 | 0–3 merged, Robinhood protective-exit proven |

Phases 3 and 5 depend only on 0a and run in parallel with 0b; Phase 3's `regime_v1`
split uses only never-revised inputs, so it doesn't wait on Phase 4 either (README §6).

**Standing rules** repeated in every phase's dispatch prompt (goal-prompts.md): never
weaken or narrow tests to pass a goal; don't refactor unrelated code or add
dependencies without saying why; every new capability ships behind a flag defaulting
off; never write code letting an agent place a broker order or a model produce a
statistic; pause before touching production capital limits, Railway flags, or
`execution/`. Phase 6 is explicitly meant to run with a human in the loop, not as an
autonomous goal dispatch.

## 10. Data sources

| Source | Gives | PIT quality | Access | Cost | Status |
|---|---|---|---|---|---|
| **Sharadar Prices** (decided) | Tickers with listing/delisting dates, corporate actions, S&P 500 constituent history, stock and fund prices, price-based metrics | Prices are PIT by nature; delisted terminal-price handling subject to the twenty-delisting audit | Nasdaq Data Link API / bulk | $9/mo "from" | Pricing verified by owner screenshot 2026-09-08; "from" tier and terms to confirm at checkout |
| EODHD All-World (runner-up) | Delisting-flagged EOD prices, splits, dividends | Vendor states delisted tickers retained; terminal-return completeness unverified | REST API | $19.99/mo | Price verified; not bought |
| Tiingo Power (runner-up) | EOD prices, splits, dividends | Same caveat as EODHD | REST API | $30/mo | Price verified; internal-use-only licence; not bought |
| SEC XBRL `companyfacts`/`submissions` | PIT fundamentals, `acceptanceDateTime` | `clean_pit`/`vendor_pit`, per-tag coverage | Free REST | $0 | Verified by live API call |
| `edgartools` + SEC bulk + OpenFIGI | Filings, entity resolution, CUSIP mapping | `vendor_pit` | Free, MIT | $0 | Verified on PyPI |
| FRED / ALFRED (`fredapi`) | Vintage-correct macro | Day-precision | Free API | $0 | Not re-verified this pass |
| Alpaca News (Benzinga) | Timestamped news, 2015→ | `vendor_pit` | Free, 200 req/min | $0 | Verified in Alpaca docs |
| `fja05680/sp500` | S&P membership history | Reproducible | Free, MIT repo | $0 | Verified repo |
| Railway (hosting) | Compute, Postgres | — | Managed platform | $20/mo + ~$10–20 overage | Verified pricing |

Verified total: **~$40–50/month** — Railway Pro $20 plus ~$10–20 overage plus Sharadar
Prices $9 (README §3, §5). Robinhood is also a
data source for portfolio state (Spec L §5.1): reads span all accounts, free, but the
product is beta with no developer documentation, so PIT quality beyond current
positions is unverified.

## 11. Open decisions and owner actions

**Decisions closed 2026-09-08** (README §3): data stack is free sources plus Sharadar
Prices at $9/mo, subject to the delisting audit; the Agentic account is a cash account
with no margin, funded with a loaded budget, so T+1 settlement is modelled; position
sizing is advisory with a separate discretionary budget, not a gate.

**The three owner actions** (README §9), each under half an hour:

1. Run `python -m scripts.dump_robinhood_tool_schemas` with the token store present.
   It writes every tool's JSON Schema to `docs/robinhood/tool_schemas.json` and prints
   `place_equity_order` / `review_equity_order` — the only source for order types,
   attached-stop support, time-in-force, and GTC persistence. Blocks Phase 6.
2. At Sharadar checkout, confirm what "from $9" gates (history depth or request volume)
   and whether bulk download and the needed terms are included.
3. Run the twenty-delisting audit (Spec N §4.2) against Sharadar before paying.
