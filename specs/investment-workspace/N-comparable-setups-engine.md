# Spec N — Comparable-setups engine

**Series:** [Investment Workspace K–Q](README.md) · **Status:** Draft v0.1 · **Date:** 2026-09-05
**Flag:** `COMPARABLE_SETUPS_ENABLED=false`
**This is the centerpiece spec.** Every other spec in the series either feeds it or
delivers it.

---

## 1. The question

> *"A liquid mid-cap semiconductor name just beat EPS by 12% with raised guidance,
> gapped up 6%, and is extended over its 50-day. How have setups genuinely like this
> performed over the next two weeks — and how sure are we?"*

Producing a number for that question is easy. Producing a number that is *not
misleading* is the entire difficulty, and it is the difference between a research
system and a random-number generator with a nice font.

## 2. The failure modes this spec exists to prevent

Each of these produces a confident, wrong answer. Each has a named defence below.

| # | Failure | What it looks like | Defence |
|---|---|---|---|
| 1 | **Survivorship** | Cohort drawn from today's tickers; the ones that went to zero were never eligible | §4.2 point-in-time universe; delisted names retained with terminal returns |
| 2 | **Lookahead in facts** | Using a restated EPS or a revised macro print that did not exist on the day | §4.1 `known_at_utc <= t` filter, no exceptions |
| 3 | **Adjustment bias** | Split/dividend-adjusted prices restate history; the fill you model never existed | §4.3 signals on adjusted, fills on unadjusted, both stored |
| 4 | **The market did it** | "+4.1% average" during a period the index rose 4.0% | §5.2 benchmark and sector return subtracted, always |
| 5 | **Overlapping windows** | 200 events, 20-day windows, clustered in earnings season → t-stats inflated several-fold | §6.1 block bootstrap **and** a calendar-time portfolio cross-check |
| 6 | **Cross-sectional clustering** | 30 semis on the same day are one observation, not thirty | §6.2 clustered/calendar-time standard errors |
| 7 | **Regime confound** | Every matched event sits in one bull market | §5.4 regime stratification, reported per-regime, refused if single-regime |
| 8 | **Multiple testing** | Twelve setup variants tried, the best one reported | §7 every query logged; deflated significance; the trial count printed |
| 9 | **Small n as a percentage** | "70% win rate" over ten events | §8 n and CI mandatory in the response schema; refusal below a floor |
| 10 | **Selection on outcome** | The event library only contains events that resolved cleanly | §4.4 unresolved/censored events counted and reported |
| 11 | **Wrong exit semantics** | Raw +20d return quoted, but the bot exits on a 2-ATR stop | §5.3 outcomes computed under the *actual* execution policy too |
| 12 | **LLM as the statistic** | A model summarizes ten charts and calls it a base rate | §9 model output can never be a number in a cohort response |

## 3. Architecture

```text
  Setup definition  (typed, versioned, hashed)     §4.0
            │
            ▼
  Cohort construction   ← source_observations (bitemporal, Spec O)
            │              ← point-in-time universe
            │              ← delisting-complete price history
            ▼
  Covariate matching    (sector · size · liquidity · vol · regime · calendar)   §4.5
            │
            ▼
  Outcome measurement   raw · market-adjusted · sector-adjusted · policy-simulated   §5
            │
            ▼
  Inference             block bootstrap · calendar-time portfolio · placebo   §6
            │
            ▼
  Response              estimate + n + CI + tier + warnings + trial count   §8
```

New package `comparables/`:

```text
comparables/
  setup_spec.py       typed setup definition, validation, hashing
  cohort.py           PIT cohort construction
  matching.py         covariate matching and balance diagnostics
  outcomes.py         raw / abnormal / policy-simulated returns
  inference.py        bootstrap, calendar-time portfolio, placebo tests
  registry.py         query log, trial accounting
  report.py           the response contract
```

It reuses, and does not reimplement: `backtest/simulator.py` (exit semantics),
`backtest/event_replay.py` (event iteration), `data/analog_ranker.py` (the existing
similarity ranking, which becomes one *candidate generator* feeding §4.5 rather than
the answer itself), and `data/event_outcomes.py`.

## 4. Cohort construction

### 4.0 A setup is a typed predicate, not a vibe

```python
@dataclass(frozen=True)
class SetupSpec:
    slug: str
    version: str
    conditions: tuple[Condition, ...]   # each references a fact type + operator + value
    universe: str                       # e.g. "liquid_us_equity_v1" (Spec Q §7)
    horizons_days: tuple[int, ...]      # e.g. (1, 3, 5, 10, 20)
    execution_policy: str | None        # e.g. "event_swing_14cal_v1"
    match_covariates: tuple[str, ...]
    lookback_years: int
```

Every field is required. The spec is content-hashed; a cohort answer is keyed by
`(setup_hash, as_of_date, data_snapshot_version)` and is therefore reproducible and
cacheable. **Changing any condition creates a new setup version** — the same immutability
rule Spec Q applies to strategies, for the same reason: otherwise the question is
rewritten after seeing the answer.

Conditions reference *fact types*, never free text: `earnings_surprise_pct`,
`guidance_direction`, `gap_pct`, `dollar_volume_20d`, `atr_pct`, `dist_from_sma50`,
`sector`, `market_cap_bucket`, `days_since_prior_event`. A natural-language request from
Bryan is translated into a `SetupSpec` by the agent, **shown back to him in full before
the cohort is built**, and stored. The translation is the only model involvement, and it
is visible and editable.

### 4.1 Bitemporal filter

Every fact used to qualify an event must satisfy `known_at_utc <= event_cutoff`. Facts
lacking a trustworthy `known_at_utc` are `archival_reconstructed` and land the whole
cohort in the degraded tier (§8). Facts are read from `source_observations` (Spec Q §8,
extended by Spec O) — the engine never reads a provider API directly at query time.

### 4.2 Universe and survivorship

Membership is evaluated as of the event date from the point-in-time universe, not from
today's list. Names that later delisted, were acquired, or went to zero **stay in the
cohort** with their terminal outcome recorded. A cohort built on a universe that cannot
reproduce point-in-time membership is capped at tier `archival_reconstructed` and says
so in every rendering.

### 4.3 Prices

Signals and covariates use corporate-action-adjusted series. Simulated fills use
unadjusted OHLC where available. Both are stored on the cohort so the discrepancy is
inspectable rather than assumed away.

### 4.4 Censoring

An event whose horizon has not fully matured, or whose price history ends early
(delisting, halt, acquisition), is **counted as censored**, reported separately, and
never silently dropped. The response carries `n_matured`, `n_censored`, and the
censoring reason distribution.

### 4.5 Matching

Raw filtering answers "events meeting these conditions." Matching answers "events
comparable to *this* one," which is the actual question. Default covariates:

- GICS-style sector (or the existing `config/peers.py` grouping)
- market-cap bucket (quintile, point-in-time)
- liquidity bucket (20-day median dollar volume, point-in-time)
- realized volatility bucket (20-day, point-in-time)
- market regime label at the event date (Spec O §4)
- calendar proximity (to avoid a cohort that is one week of history)

Implementation: exact matching on sector and buckets where sample allows, coarsened
exact matching as the fallback, and a **balance diagnostic printed with every answer** —
standardized mean difference per covariate between the query event and the cohort. A
covariate with |SMD| > 0.25 is listed as an explicit warning: *"your event is much more
liquid than its comparables."* An unbalanced cohort is not hidden; it is labelled.

## 5. Outcome measurement

Four numbers, always all four. Reporting one is how the failure modes get in.

### 5.1 Raw forward return
Simple close-to-close over each horizon from the first tradable open after the event.
Included for reference and because it is what everyone else quotes.

### 5.2 Abnormal return — the headline
Market-adjusted **cumulative abnormal return (CAR)** over each horizon: the event's
return minus the contemporaneous benchmark return over the identical window. Two
adjustments computed:
- vs. broad market (long-only US equity benchmark, matching Spec Q §10)
- vs. sector

For horizons beyond 20 sessions, buy-and-hold abnormal return (BHAR) is reported
alongside CAR, since compounding differences matter at longer horizons and the two can
disagree. Both are shown; neither is quietly preferred.

### 5.3 Policy-simulated return — the honest one
The same events replayed through `backtest/simulator.py` under the named
`execution_policy`, so the number reflects **the stop, targets, time exit, gap-through
semantics, and slippage the system would actually have used.** A cohort that looks
excellent raw and mediocre under a 2-ATR stop is telling you the edge is in the tail you
would have been stopped out of. That distinction is the whole point.

Cost assumptions are explicit and stressed: baseline 10 bps adverse per fill, with a
mandatory sensitivity at 25 and 50 bps rendered next to the baseline.

### 5.4 Per-regime breakdown
Every metric additionally reported split by market regime. **If all events fall in one
regime, the engine says so and declines to generalize** rather than reporting a single
pooled number that describes one market mood.

## 6. Inference

### 6.1 Uncertainty
- **Stationary block bootstrap** over calendar time (not naive resampling of events),
  producing percentile confidence intervals for every reported statistic. Block length
  chosen from the horizon so overlapping windows are resampled together.
- Confidence intervals are **required in the response schema**. A point estimate without
  one cannot be constructed by the type system.

### 6.2 Clustering
Events cluster in calendar time (earnings season) and cross-section (sectors move
together). Naive standard errors on such a cohort are wrong by a large multiple, not a
rounding error. Two defences, both required:
- standard errors clustered by event date;
- a **calendar-time portfolio cross-check**: form the portfolio of all names currently
  inside their event window on each date, compute the time series of its abnormal
  returns, and test that series. This absorbs cross-sectional correlation by
  construction.

**The two methods must agree in sign and rough magnitude.** When they do not, the
response is downgraded to `inconclusive` with both numbers shown. Disagreement is
information, not something to average away.

### 6.3 Null tests
Run automatically with every cohort:
- **Placebo dates:** the same names on random non-event dates. A real effect should
  vanish.
- **Random cohorts:** same size, same period, random eligible names. Reports where the
  observed effect sits in that distribution — an empirical p-value that needs no
  distributional assumption.
- **Pre-event window:** abnormal return over the 10 sessions *before* the event.
  A large pre-drift is a leakage warning, not a bonus.

## 7. Researcher degrees of freedom

Every cohort query is logged in `comparable_queries` with its `SetupSpec`, timestamp,
requester, and result. The response reports **how many setup variants have been tried
against this fact pattern**, so the twelfth variant cannot present itself as the first.
Where the trial count and sample support it, a deflated-significance diagnostic is
computed and shown. The system will not stop Bryan from searching; it will refuse to let
him forget that he did.

## 8. Response contract and refusal

```python
@dataclass(frozen=True)
class CohortAnswer:
    setup: SetupSpec
    tier: Literal["clean_pit", "archival_reconstructed", "insufficient"]
    n_matured: int
    n_censored: int
    n_distinct_dates: int          # the honest sample size when events cluster
    horizons: dict[int, HorizonResult]   # each: raw, CAR, BHAR, policy, CI, p_empirical
    regime_breakdown: dict[str, dict[int, HorizonResult]]
    balance: tuple[CovariateBalance, ...]
    null_tests: NullTestResults
    trials_against_this_pattern: int
    warnings: tuple[str, ...]
    sources: tuple[SourceRef, ...]
```

Hard rules:

- **No field is optional.** A missing statistic is a failure, not a null.
- `tier="insufficient"` is returned — with the reason — whenever `n_matured` is below
  the configured floor (default 30 matured events **and** 15 distinct event dates,
  because clustered events are not independent observations), or the cohort spans a
  single regime, or point-in-time integrity cannot be established.
- **`insufficient` is a valid, expected, frequently-correct answer.** The engine prints
  it rather than manufacturing a ranking. Every consumer — Telegram, the weekly report,
  the MCP tool, an agent's prose — must render it as a refusal, never round it into a
  hedge.
- `archival_reconstructed` results are never combined with `clean_pit` results in the
  same statistic. They are displayed in a separate block and can never support a Spec Q
  promotion.

## 9. What the model may and may not do

May: translate Bryan's English into a `SetupSpec` (shown back before running); name
which of several cohorts is most relevant to a decision; write the prose around the
numbers; propose the next cohort to test.

May not: produce, adjust, round, select, or characterize a statistic. Every number in
any comparable-setups output traces to `comparables/` code and a stored query. If a
rendered answer contains a figure the engine did not compute, that is a defect with a
failing test attached (§10).

## 10. Test plan

| Test | Asserts |
|---|---|
| `test_setup_spec_is_hashed_and_immutable` | Same spec → same hash → cached identical answer |
| `test_lookahead_rejected` | A fact with `known_at_utc > cutoff` never enters a cohort |
| `test_delisted_names_retained` | A synthetic cohort with a to-zero name shows the loss; removing it changes the mean |
| `test_survivorship_downgrades_tier` | A universe without PIT membership caps the tier |
| `test_benchmark_subtracted` | Synthetic data where every name returns exactly the index → CAR ≈ 0 |
| `test_overlap_widens_ci` | Overlapping-window cohort produces a materially wider CI than the naive computation |
| `test_clustered_cohort_reports_distinct_dates` | 30 events on 2 dates reports `n_distinct_dates=2` and refuses on the floor |
| `test_methods_disagree_downgrades` | Divergent bootstrap and calendar-time results yield `inconclusive` |
| `test_placebo_null_effect` | Random-date placebo on real data produces no significant effect |
| `test_policy_matches_simulator` | Policy-simulated outcomes equal `backtest/simulator.py` on the same events, bit for bit |
| `test_single_regime_refuses_generalization` | A one-regime cohort is flagged and not pooled |
| `test_insufficient_is_returned_not_hedged` | Below the floor, `tier="insufficient"` and no point estimate is emitted |
| `test_trial_count_increments` | The twelfth variant reports `trials_against_this_pattern=12` |
| `test_no_model_number_in_output` | Response construction from an LLM string raises |

## 11. Definition of done

- `compare_setups` answers the §1 question end-to-end from any client, with n, CI, tier,
  balance diagnostics, regime split, null tests, and trial count.
- Every failure mode in §2 has a named, passing test in §10.
- Running it against the existing warmed event library produces at least one
  `insufficient` and one `clean_pit` answer on real data, both hand-verified.
- Policy-simulated outcomes reconcile exactly with the existing event-replay backtester.
- A written page in `docs/` explains, in Bryan's words, what the tiers mean and why an
  `insufficient` answer is the system working correctly.
