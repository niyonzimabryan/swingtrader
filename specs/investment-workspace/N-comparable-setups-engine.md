# Spec N — Comparable-setups engine

**Series:** [Investment Workspace K–Q](README.md) · **Status:** Draft v0.2 (research-upgraded) · **Date:** 2026-09-05
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

**Engine decision (research slot 1, README §5):** keep and extend the simulator. No
open-source engine reproduces its semantics (T+1 open entry, stop-before-target on the
same bar, gap-through fills at the open, half-out at T1 with the stop unchanged,
calendar-day time exit, slippage on every leg), and §10 requires bit-for-bit equality.
Extension work is a vectorised NumPy path for cohort-scale replay and an
`ExecutionPolicy` dataclass so Spec Q's named policies are data, not arguments.

**Inference libraries (research slot 7):** `arch` (stationary/circular block bootstrap,
`optimal_block_length`, StepM/SPA) and `statsmodels` (cluster-robust and two-way
clustered covariance). Wilson intervals, empirical-Bayes shrinkage, and the calendar-time
portfolio regression are vendored (~200 lines) with tests. **`mlfinlab` is not open
source** (all rights reserved, commercial licence) and must not appear in
`requirements.txt`; `purgedcv` or `ml4t-diagnostic` (both MIT) are the Spec Q options
for CPCV / deflated Sharpe / PBO, which do not belong in this spec (§7).

**The analog ranker is a generator, never a selector.** Its similarity score must use
only pre-event features, and the final statistics are computed over the matched set
(§4.5) invariant to the ranker's ordering or its fixed top-k. Otherwise similarity
becomes a hidden selection-on-outcome channel — failure mode 10 wearing a different hat.
`test_analog_generator_adds_no_bias` (§10) asserts this.

## 4. Cohort construction

### 4.0 A setup is a typed predicate, not a vibe

```python
@dataclass(frozen=True)
class SetupSpec:
    slug: str
    version: str
    conditions: tuple[Condition, ...]   # each references a fact type + operator + value
    universe: str                       # e.g. "liquid_us_equity_v1" (Spec Q §7)
    horizons_sessions: tuple[int, ...]  # e.g. (1, 3, 5, 10, 20) — trading sessions, not calendar days
    execution_policy: str | None        # e.g. "event_swing_14cal_v1"
    match_covariates: tuple[str, ...]
    lookback_years: int
```

Every field is required. Horizons are in **trading sessions**; the simulator's time exit
is in **calendar days** (it mirrors `order_monitor`). Both are fine; mixing them silently
is not, so the unit is in the field name and printed in every response. The spec is
content-hashed; a cohort answer is keyed by
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

**The universe must be a stored table, not a rule evaluated against today's data.**
`universe_membership(universe_slug, ticker, member_from, member_to, source, known_at_utc)`
is populated by a job from a source that carries history (index constituent changes,
or a delisting-complete price file — see README §5 slot 2). A universe defined as
"liquid US equities" computed from *current* liquidity is survivorship bias wearing a
disguise, because the names that died were illiquid on the way down. Delisting returns
are applied by delisting reason, with the convention **named, configured, and printed**:
approximately **−30% for NYSE/AMEX and −55% for Nasdaq** performance-related delistings
(Shumway 1997; Shumway & Warther 1999 — the Nasdaq bias is ~4.7× the NYSE/AMEX one,
and the corrected return makes the Nasdaq size effect disappear). Mergers and
acquisitions use the deal terms where stored. "Standard practice" left unnamed gets
implemented as −100% or 0% by whoever writes it; neither is right.

### 4.3 Prices

Three series are stored per name, not two: **raw** OHLC (what fills happened at),
**split-adjusted** (what signals and covariates are computed on), and **total-return**
(split + dividend, what the benchmark comparison is made on). The split and dividend
factors are stored with their ex-dates so any series reconstructs from the others and
the adjustment provenance is inspectable. Simulated fills use raw OHLC; signals use
split-adjusted; abnormal returns compare total-return to a total-return benchmark. A
price-return stock against a total-return index is a systematic negative bias equal to
the yield gap, and at least one major vendor supplies no dividend-adjusted series at all
(README §5 slot 2), so this is a storage rule, not a preference.

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
exact matching (Iacus, King & Porro) as the fallback — CEM bounds imbalance ex ante and
suits covariates that are already categorical or bucketed; propensity-score matching is
model-dependent and can *increase* imbalance, so it is not used. A **balance diagnostic
is printed with every answer**: standardized mean difference and variance ratio per
covariate between the query event and the cohort. Thresholds follow current matched-
cohort practice, not Rubin's older 0.25 rule: |SMD| > 0.10 is a **warning** (*"your event
is more liquid than its comparables"*), |SMD| > 0.25 is a hard **`unbalanced`** label on
the covariate. A variance ratio outside [0.5, 2] is also warned, because two groups can
share a mean and differ entirely in their tails. An unbalanced cohort is not hidden; it
is labelled.

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

For horizons beyond 20 sessions only, buy-and-hold abnormal return (BHAR) is reported
alongside CAR. The literature is not neutral here: BHAR compounds the expected-return
model's errors and its conventional test statistics are biased by cross-sectional
correlation (Fama 1998; Mitchell & Stafford 2000), so when CAR, BHAR and the
calendar-time portfolio (§6.2) disagree, **the calendar-time portfolio is the tiebreak**
and the docs say so. Short-horizon methods are the reliable ones (Kothari & Warner 2007);
the default horizons all sit inside that regime, so BHAR is not implemented until a
>20-session horizon is actually requested.

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

### 5.5 Stability over time — has the edge decayed?
The best-documented "comparable setup" in the literature, post-earnings-announcement
drift, has shrunk materially since it was published. A cohort that pools 2010 with 2025
can report an effect that no longer exists. So every cohort is also split
chronologically — halves by event date, and per calendar year where n allows — and the
headline metric is reported per slice. The response carries a `stability` block:
the early-half and late-half estimates with CIs, and a flag `decayed=True` when the
late-half CI excludes the early-half point estimate in the direction of zero, or the
sign flips. A decayed cohort is not refused, but the pooled number is rendered *after*
the split, never instead of it.

## 6. Inference

### 6.1 Uncertainty
- **Stationary block bootstrap** (Politis & Romano 1994) over calendar time, not naive
  resampling of events, producing percentile confidence intervals for every reported
  statistic. Block length is **estimated, not assumed**: `arch.bootstrap.optimal_block_length`
  (Politis & White 2004 with the Patton–Politis–White 2009 correction) on the
  calendar-time abnormal-return series, with the horizon as a floor so overlapping
  windows are always resampled together. The chosen length is printed in the response.
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

Clustered standard errors are computed two-way (event date × ticker) via `statsmodels`.
**With fewer than ~30 clusters the asymptotics fail**, and the §8 floor is 15 distinct
dates, so when `n_distinct_dates < 30` the clustered SE is labelled `unreliable` and the
bootstrap CI is the only interval rendered.

**The two methods must agree in sign.** When they do not, the response is downgraded to
`inconclusive` with both numbers shown. When they agree in sign but differ materially in
magnitude, the calendar-time portfolio estimate is the headline (it is the preferred
estimator, §5.2) and the bootstrap figure is shown beside it. Disagreement is
information, not something to average away — but a symmetric rule that discards both
was throwing away the better one.

### 6.3 Null tests
Run automatically with every cohort:
- **Placebo dates:** the same names on random non-event dates. A real effect should
  vanish.
- **Random cohorts:** same size, same period, random eligible names. Reports where the
  observed effect sits in that distribution — an empirical p-value that needs no
  distributional assumption.
- **Pre-event window:** abnormal return over the 10 sessions *before* the event.
  A large pre-drift is a leakage warning, not a bonus.

### 6.4 Small-sample honesty: shrink, and show both
A cohort of 35 matured events clears the floor and still supports a wide, noisy
estimate. Two rules:
- Proportions (hit rate, share of events stopped out) are reported with **Wilson
  intervals**, never normal-approximation intervals, which are wrong exactly where it
  matters — small n and rates near 0 or 1.
- Means are additionally reported **shrunk toward the pooled estimate of the setup
  family** (all cohorts sharing the same primary condition, e.g. every
  `earnings_surprise_pct > x` cohort) by an empirical-Bayes weight proportional to
  `n / (n + k)`, with `k` fixed in config and printed. The raw and the shrunk estimate
  both appear; the shrunk one is what an agent quotes when `n_matured < 100`. Shrinkage
  is a stated prior, not a hidden one — the family, `k`, and the pooled value are in
  the response. Shrinkage estimates are themselves unstable at small n, which is why
  `k` is fixed in config rather than estimated per cohort.

Later, not v1: a conformal predictive interval for a *single next outcome* ("if I take
this trade, what range should I expect") is a genuine addition to the CI on the mean and
is distribution-free. Noted so it is not reinvented as something less honest.

## 7. Researcher degrees of freedom

Every cohort query is logged in `comparable_queries` with its `SetupSpec`, timestamp,
requester, and result. The response reports **how many setup variants have been tried
against this fact pattern**, so the twelfth variant cannot present itself as the first.
"This fact pattern" is defined, not vibes: the equivalence class is the **setup family
slug** from §6.4 — the same key used for shrinkage. One concept, two uses; without a
defined class the trial count is gameable by renaming.

The adjusted significance is a **Romano–Wolf stepdown** (`arch`'s StepM), which controls
family-wise error while accounting for the heavy dependence between overlapping setup
variants; a Šidák correction on the effective number of independent trials is the cheap
fallback. Both the raw empirical p-value and the adjusted one are printed with the trial
count, and the reader sees all three. **Deflated Sharpe, PBO and CPCV are not computed
here** — they are defined over a strategy's Sharpe under N trials, not over a cohort's
mean abnormal return, and applying them to a CAR produces a number that looks rigorous
and means nothing. They live in Spec Q §10, where they belong. The system will not stop
Bryan from searching; it will refuse to let him forget that he did.

## 8. Response contract and refusal

```python
@dataclass(frozen=True)
class CohortAnswer:
    setup: SetupSpec
    depth: Literal["quick", "full"]
    tier: Literal["clean_pit", "vendor_pit", "archival_reconstructed", "insufficient"]
    provenance_mix: dict[str, int]   # events per provenance class: observed_live / vendor_pit / archival
    block_length: int                # §6.1, the estimated length actually used
    n_matured: int
    n_censored: int
    n_distinct_dates: int          # the honest sample size when events cluster
    horizons: dict[int, HorizonResult]   # each: raw, CAR, BHAR, policy, CI (raw + shrunk), p_empirical
    regime_breakdown: dict[str, dict[int, HorizonResult]]
    stability: StabilityResult           # §5.5: early/late halves, per-year where n allows, decayed flag
    shrinkage: ShrinkageSpec             # §6.4: family slug, k, pooled estimate used
    balance: tuple[CovariateBalance, ...]
    null_tests: NullTestResults
    trials_against_this_pattern: int
    warnings: tuple[str, ...]
    sources: tuple[SourceRef, ...]
```

Provenance has three classes, not two, because most of the history will be the middle
one: **`observed_live`** (we recorded the fact when it happened), **`vendor_pit`** (a
vendor's filing-date or acceptance-date stamp, reconstructed but defensible — a Sharadar
`datekey`, an EDGAR `acceptanceDateTime`), and **`archival_reconstructed`** (a guessed
`known_at_utc`). `observed_live` and `vendor_pit` may be pooled, with the mix printed;
`archival_reconstructed` is never pooled with either. Conflating the middle class with
either neighbour lies in a different direction each time.

`depth` exists because a response with a dozen mandatory nested blocks is the most
likely place this spec stalls. **`quick`** returns raw, CAR, n, n_distinct_dates,
bootstrap CI, tier, balance, and the trial count — enough to iterate on a setup in
seconds. **`full`** returns everything below. `full` is **mandatory** for any answer
referenced by `journal_append`, `research_write`, or a Spec Q promotion; a `quick`
answer cannot be cited, and the type system enforces it (`test_quick_answer_not_citable`).
Same rigour where it matters; a ten-times faster loop where it does not.

Hard rules:

- **No field is optional** at the declared depth. A missing statistic is a failure, not a null.
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
| `test_universe_is_stored_not_computed` | A cohort request against a universe with no `universe_membership` rows for the period is capped at `archival_reconstructed` |
| `test_delisting_return_applied` | A performance-delisted name carries the configured terminal return, not its last print |
| `test_decay_split_reported` | A synthetic cohort with a real early effect and zero late effect sets `decayed=True` and renders both halves |
| `test_wilson_not_normal` | A 3-of-10 hit rate reports the Wilson interval, and no normal-approximation interval exists in the schema |
| `test_shrinkage_declared` | Every `HorizonResult` carries raw and shrunk means and the `ShrinkageSpec` names family, `k`, and pooled value |
| `test_truncated_data_identical_answer` | **Lookahead harness** (borrowed from freqtrade's `lookahead-analysis`): re-running any cohort with every fact table truncated at each event's cutoff yields a byte-identical answer. Runs on every cohort in CI, not only on synthetic fixtures |
| `test_analog_generator_adds_no_bias` | Final statistics are invariant to the analog ranker's ordering and top-k; the ranker's features are all pre-event |
| `test_smd_thresholds` | An SMD of 0.15 warns; 0.30 labels `unbalanced`; a variance ratio of 3 warns |
| `test_block_length_estimated_and_printed` | `block_length` comes from `optimal_block_length` bounded below by the horizon, and appears in the response |
| `test_few_clusters_marks_clustered_se_unreliable` | `n_distinct_dates=20` renders the bootstrap CI only |
| `test_calendar_time_is_tiebreak` | Same-sign, different-magnitude estimates headline the calendar-time figure |
| `test_trial_count_keyed_by_family` | Renaming a setup slug within the same family does not reset `trials_against_this_pattern` |
| `test_vendor_pit_never_pooled_with_archival` | Mixed provenance yields separate blocks; `provenance_mix` sums to n |
| `test_quick_answer_not_citable` | `journal_append` / `research_write` refuse a `depth="quick"` answer |
| `test_three_price_series_stored` | A cohort's price inputs carry raw, split-adjusted, and total-return series plus factors with ex-dates |
| `test_horizons_are_sessions` | A 5-session horizon over a holiday week spans the correct calendar dates |

## 11. Definition of done

- `compare_setups` answers the §1 question end-to-end from any client, with n, CI, tier,
  balance diagnostics, regime split, null tests, and trial count.
- Every failure mode in §2 has a named, passing test in §10.
- Running it against the existing warmed event library produces at least one
  `insufficient` and one `clean_pit` answer on real data, both hand-verified.
- Policy-simulated outcomes reconcile exactly with the existing event-replay backtester.
- A written page in `docs/` explains, in Bryan's words, what the tiers mean and why an
  `insufficient` answer is the system working correctly — and says in advance that with
  a floor of 30 events it will be a frequent answer (a 1% abnormal return with 8%
  cross-sectional dispersion has a standard error near 1.5% at n=30). The page cites the
  post-earnings-drift literature on decay (Chordia, Subrahmanyam & Tong 2014; Martineau
  2022; contested by Meursault et al. 2023) as the reason §5.5 exists.
