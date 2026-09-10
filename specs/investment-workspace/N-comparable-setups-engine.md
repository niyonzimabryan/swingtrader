# Spec N — Comparable-setups engine

**Series:** [Investment Workspace K–Q](README.md) · **Status:** Draft v0.4 (post-review) · **Date:** 2026-09-05
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

Conditions reference *fact types*, never free text: `sue_seasonal`, `guidance_direction`,
`gap_pct`, `dollar_volume_20d`, `atr_pct`, `dist_from_sma50`, `market_cap_decile`,
`realized_vol_decile`, `days_since_prior_event`, and `sector` (current-vintage, see §4.5).

**Market cap needs a share count, and the price file does not carry one.** The input
is `shares_outstanding` from SEC XBRL `dei:EntityCommonStockSharesOutstanding` (cover-
page fact on every 10-K/10-Q, with `acceptanceDateTime` as `known_at_utc`), aggregated
across share classes by CIK, with the vendor's share count as a fallback labelled
`vendor_pit`. `market_cap_decile` is `price × shares_outstanding` as of the event date
using the latest share count known at that time, ranked within the universe that day.

**Minimum SEC ingestion contract — ships inside Phase 3, not Phase 4.** The earnings
roster, `shares_outstanding`, and event timestamps all need three free, credential-less
SEC feeds: XBRL `companyfacts` (facts with `accn`/`filed`), `submissions` (per-filing
`acceptanceDateTime`), and the 8-K index filtered to Item 2.02. Phase 3's first
checkpoint lands exactly those three into `source_observations` behind
`filings/sec_minimal.py`; Phase 4 builds the full planes (13D/G, Form 4, entity
history, news) on top of it. README §6 reflects this.

**Earnings surprise is defined without analyst estimates.** No retail source offers a
verifiable point-in-time consensus archive (verification §23): FMP, EODHD and the
Robinhood `get_earnings_results` tool are restated snapshots, Zacks point-in-time is
institutional. A restated consensus used as a pre-print fact is lookahead in the
direction that flatters the engine, wrapped in machinery that makes it believable. So
the v1 surprise fact is a **seasonal random-walk SUE** — actual EPS minus the same
quarter a year earlier, scaled by the standard deviation of that difference — computed
entirely from SEC XBRL `companyfacts` with `accn`/`acceptanceDateTime` for vintage. It
is a weaker proxy than analyst-based SUE and the literature says so; it is honest,
free, and reproducible. The announcement **timestamp** comes from the 8-K Item 2.02
`acceptanceDateTime`, exact to the second in UTC, falling back to `archival` when a
company press-released before filing. Any consensus-based surprise fact, if ever bought,
must pass the restatement test (download twice a month apart; if the stored history
changed, it is restated) before it can be anything but `archival_reconstructed`.

**Consensus recovered from timestamped news is a legitimate second source** (owner's
suggestion, 2026-09-06). Earnings previews and reaction pieces routinely state the
number — "analysts expect EPS of $1.23 on revenue of $4.1B", "beat the $1.18 consensus
by five cents" — and a Benzinga article via Alpaca carries a publisher timestamp, so the
figure is point-in-time by construction: `known_at_utc` is the article time, and a
post-print article still states the consensus *as it stood at the print*, which is the
right vintage. `filings/consensus_from_news.py` extracts it **deterministically** (a
regex/grammar over the article body, not a model — a model reading a number out of a
sentence is parsing, not producing a statistic, but the deterministic path is
reproducible and the model path is not) and stores `consensus_eps_news` with the source
article, the count of independent articles agreeing, and the spread between them when
they disagree (Zacks, FactSet and Refinitiv consensus differ, and articles cite
whichever their author uses). Provenance class is `vendor_pit`. Coverage will be
skewed to large, well-covered names and is an empirical question: the first checkpoint
of the earnings setup pulls fifty past prints, runs the extractor, and records the hit
rate before anything depends on it. `sue_seasonal` remains the fact every event has;
`consensus_eps_news` is the better fact where it exists, and a cohort can require it.
Nothing derived from the articles leaves Postgres (Spec O §5).

**Setups are pre-registered, not improvised, in v1.** A small fixed roster of typed
setups ships in `comparables/setups/` (earnings SUE, gap-and-go, insider cluster, and
whatever Spec Q's challengers need), each with its conditioning set frozen. The general
"any predicate" engine is the same code path but the roster is what the multiplicity
accounting (§7) counts against; a free-form request counts as a trial against the
nearest family. For one person with one strategy the roster delivers most of the value
with a countable trial budget. A natural-language request from
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
disguise, because the names that died were illiquid on the way down. No affordable source exists for Russell membership history, and S&P history is a
hand-maintained Wikipedia scrape (`fja05680/sp500`, MIT, 1996→) or a ~12-year vendor
window (EODHD, ≈2014→). So the primary universe is **self-defined and reproducible**:
a rank-by-market-cap and dollar-volume rule applied point-in-time to the delisting-
complete price file, documented as `liquid_us_equity_v1`. A documented rule beats a
badly reconstructed index. Index membership tables are stored as an additional
covariate where they exist, with their provenance.

**Vendor delisting completeness is unverified for every vendor** and the failure is
silent: a file can carry a delisted ticker's full history and still stop at the last
quote with no terminal collapse. Before any vendor is paid, and again on every price-
file refresh, `scripts/audit_delisting_returns.py` pulls twenty known performance-
related delistings (2015–2024) and checks whether the last observation is a collapse or
a stop; a stop means the terminal return below is synthesised, and the audit result is
recorded on the data snapshot. The **within-cohort delisting rate** is a first-class
output beside the return statistics, and a cohort whose floor is met by survivors alone
is refused on composition, not only on n.

Delisting returns
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
the adjustment provenance is inspectable. **Replay runs on the split-adjusted series**,
because `backtest/simulator.py` holds fixed stop, target, and position fractions with
no corporate-action input, and on raw prices an economically neutral 2-for-1 split
mid-hold would fire the stop. Split-adjusted OHLC makes a split invisible to the exit
logic, which is the correct economics; the raw series and the factor are kept so any
fill can be mapped back to the price that actually printed. Signals use split-adjusted;
abnormal returns compare total-return to a total-return benchmark. Parity with the
legacy simulator holds bit-for-bit on action-free fixtures, and a split-invariance test
asserts identical `TradeResult` on a fixture with and without a mid-hold split. A
price-return stock against a total-return index is a systematic negative bias equal to
the yield gap, and at least one major vendor supplies no dividend-adjusted series at all
(README §5 slot 2), so this is a storage rule, not a preference.

### 4.4 Censoring

Two different things, kept apart because conflating them is how terminal losses
disappear:

- **Resolved by terminal outcome.** A delisting with a known reason, an acquisition
  with deal terms, or a to-zero name is **matured, not censored**: the §4.2 terminal
  return is applied at the delisting date and carried flat through every remaining
  horizon, so a name that lost 55% in session 7 contributes −55% to the 10- and
  20-session statistics. It counts toward `n_matured` and toward `delisting_rate`.
- **Censored.** An event whose horizon has not yet matured, or whose history ends with
  an *unknown* reason (a halt with no resolution, a data gap), is counted in
  `n_censored` with its reason, reported separately, and never silently dropped.

`test_delisting_contributes_loss` asserts the first case changes the headline mean;
`test_unknown_end_is_censored` asserts the second does not enter it.

### 4.5 Matching

Raw filtering answers "events meeting these conditions." Matching answers "events
comparable to *this* one," which is the actual question. Default covariates, all
computable point-in-time from the price file with no licence and no vintage problem:

- market-cap decile (point-in-time)
- liquidity decile (20-day median dollar volume, point-in-time)
- realized volatility decile (20-day, point-in-time)
- price level bucket
- market regime label at the event date (Spec O §4) — reported, not required (§5.4)
- calendar proximity (to avoid a cohort that is one week of history)

**Sector is a current-vintage covariate and is labelled as one.** Historical GICS
assignment history is an institutional licence; every sector field available at retail
is the company's sector *today*. A name that migrated sectors is therefore matched by
where it ended up. Sector (from `config/peers.py` or the vendor field) may still be
used for matching — migration is rare and the contamination is small — but it carries
`vintage=current` in the balance block, is never a *required* stratum, and a cohort
whose comparability rests mainly on sector says so in its warnings.

Implementation: exact matching on sector and buckets where sample allows, coarsened
exact matching (Iacus, King & Porro) as the fallback — CEM bounds imbalance ex ante and
suits covariates that are already categorical or bucketed; propensity-score matching is
model-dependent and can *increase* imbalance, so it is not used. Two diagnostics are printed with every answer, and they answer different questions:

- **Is the query event typical of its cohort?** A single event has no variance, so the
  two-group SMD does not apply. Per covariate: the query's **standardized distance**
  from the cohort mean in cohort-SD units, and its **cohort percentile**. Distance > 1.0
  warns (*"your event is more liquid than its comparables"*); > 2.0 is a hard
  **`atypical`** label on the covariate.
- **Is the matched cohort representative of the eligible pool?** Here two groups exist,
  so the standard tools apply: SMD and variance ratio per covariate between the matched
  cohort and the point-in-time eligible universe. |SMD| > 0.10 warns, > 0.25 labels
  **`unbalanced`**; a variance ratio outside [0.5, 2] warns. This is what stops a
  "comparable" cohort from quietly being the twenty most liquid names in the pool.

Neither diagnostic is hidden; both are labelled.

## 5. Outcome measurement

Four numbers, always all four. Reporting one is how the failure modes get in.

### 5.0 Definitions, so the four numbers share units
Fixed for every metric in this section; `docs/` carries a hand-calculated fixture that
every implementation must reproduce to the cent.

- **Event clock.** Session 0 is the first session whose open is at or after
  `known_at_utc`. Entry is the **open of session 0** (or the simulator's fill on that
  open, for §5.3). Horizon `h` ends at the **close of session h−1** — so a 1-session
  horizon is open-to-close of session 0, a 5-session horizon spans five sessions.
- **Event return** `R_i(h)` = close(session h−1) / entry − 1, on the total-return series.
- **Benchmark return** `R_m(h)` over the identical sessions, same series basis.
- **CAR** `= Σ_s (r_i,s − r_m,s)` over sessions 0..h−1 (daily excess returns summed).
- **Calendar-time portfolio.** On each calendar session `t`, the portfolio holds every
  event whose window covers `t`, **equal-weighted**; its daily return is the mean of
  those events' daily returns; the daily abnormal return is `p_t − r_m,t`. The
  regression is `p_t − r_f,t = α + β (r_m,t − r_f,t) + ε_t` over the cohort's span; the
  horizon estimate reported as the headline is **`α × h`** (daily alpha scaled to the
  horizon), with the CI from the §6.1 block bootstrap of `{p_t − r_m,t}` scaled the
  same way. **When β is not identified** (fewer than three sessions in the series, or
  zero benchmark variance) the engine sets **β := 1** so α degrades to the mean daily
  abnormal return, reports `beta_estimated=False`, and warns. β := 0 would silently
  turn the abnormal headline into the raw return, which is failure mode 4. (Ruling
  2026-09-09, from the Phase 3b build.) Cross-sectional CAR (the mean of `CAR_i(h)`) is the cross-check, and both
  are printed in the same units: percent over `h` sessions.
- **Policy return** (§5.3) is the simulator's net P&L over entry, in percent of entry
  notional, on the same event clock.

### 5.1 Raw forward return
The mean of `R_i(h)` as defined above. Included for reference and because it is what
everyone else quotes.

### 5.2 Abnormal return — the headline
Market-adjusted **cumulative abnormal return**, headline from the calendar-time α × h
(§5.0, §6.2), cross-checked by the cross-sectional mean CAR. Two adjustments computed:
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

**The headline policy number is net of costs.** At swing-trading turnover, spread plus
impact can consume the entire measured edge, and a base rate reported gross describes a
strategy nobody can run. The cost model is explicit and stressed: a **half-spread
estimate by liquidity decile** (from stored quote data where available, otherwise a
configured table by decile), plus adverse slippage of 10 bps per fill at baseline, with
mandatory sensitivity at 25 and 50 bps rendered beside it; commissions zero. Gross is
shown too, labelled gross. Where the setup's execution policy involves a cash Agentic
account, T+1 settlement (Spec L §5.1) is modelled as a redeploy delay so turnover is
the achievable turnover.

### 5.4 Per-regime breakdown
Every metric additionally reported split by market regime where each cell clears the
floor. Regime is context, not a gate: with daily bars and realistic event counts, most
regime cells will be `insufficient`, and the cells that clear will be the common
regimes — which is still worth knowing. **If all events fall in one regime, the pooled
number carries a `single_regime` warning** and the docs say the answer describes one
market mood; v0.2's outright refusal on that condition spent the whole cohort to guard
against a caveat, and is dropped.

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
together). Fifty events across four reporting days are four observations wearing a
disguise, and a bootstrap over the *event* dimension does not fix same-date dependence:
the CI comes out too narrow by roughly the square root of events per date. So:

- **The calendar-time portfolio is the primary estimator.** Form the portfolio of all
  names currently inside their event window on each date, take its daily abnormal-return
  series, and regress on the benchmark. This dissolves cross-sectional clustering
  instead of correcting for it. The §6.1 stationary block bootstrap runs on *that*
  series and produces the headline CI.
- **Two-way clustered CAR regression (event date × ticker) is the cross-check**, not
  the headline.
- The response carries **`n_eff`**, an effective sample size from distinct event dates
  and the average within-date correlation, beside `n_matured`.

Clustered standard errors are computed two-way (event date × ticker) via `statsmodels`.
**With fewer than ~30 clusters the asymptotics fail**, and the §8 floor is 20 distinct
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

### 6.4 Small-sample honesty: Wilson now, shrinkage when a family is big enough
**v1 ships Wilson intervals and the bootstrap CI only.** Empirical-Bayes shrinkage as
specified below is gated on a family having **at least five cohorts**: below that,
`τ̂²` is noise and the "shrunk" number would be a fixed prior wearing a data-driven
costume. The response carries `shrinkage: null` with the reason until the gate is met.
The design is kept here so it is not reinvented as something less honest.
A cohort of 35 matured events clears the floor and still supports a wide, noisy
estimate. Two rules:
- Proportions (hit rate, share of events stopped out) are reported with **Wilson
  intervals**, never normal-approximation intervals, which are wrong exactly where it
  matters — small n and rates near 0 or 1.
- Means are additionally reported **shrunk toward the pooled estimate of the setup
  family** (all cohorts sharing the same primary condition, e.g. every
  `sue_seasonal > x` cohort) by an empirical-Bayes weight `n / (n + k)`. **`k` is not a
  tuning knob**: under the normal-normal hierarchical model it equals within-cohort
  variance over between-cohort variance, `σ²/τ²`, estimated by method of moments across
  the family (pooled within-cohort variance; observed variance of cohort means minus
  average sampling variance for `τ²`). When `τ̂²` comes out at or below zero the cohorts
  differ no more than noise predicts, `k` is infinite, and every cohort in the family is
  reported as the family mean — that is the correct answer, not a bug, and it is printed
  as such. The raw and the shrunk estimate both appear; the shrunk one is what an agent
  quotes when `n_matured < 100`. The family, `k̂`, `σ̂²`, `τ̂²`, and the pooled value are
  in the response. Thirty lines of NumPy, unit-tested against a simulation with known
  `τ²`. Shrinkage assumes cohorts are exchangeable draws from the family; a family
  defined by a variable chosen *because it looked predictive* is not exchangeable, and
  shrinkage then understates the selection problem rather than fixing it — §7 is not
  optional.

Later, not v1: a conformal predictive interval for a *single next outcome* ("if I take
this trade, what range should I expect") is a genuine addition to the CI on the mean and
is distribution-free. Noted so it is not reinvented as something less honest.

### 6.5 The engine's own track record
The decision journal (Spec M) scores Bryan's predictions. Nothing in v0.2 scored the
engine's. Every `full` `CohortAnswer` that is cited — point estimate, CI, horizon,
`as_of` — is written to `cohort_predictions`, and a scheduled job scores it against the
realized outcome of the *query event* once the horizon matures. **The CI is on the
cohort mean and is never scored as a predictive interval** — one trade landing outside
the interval for the average of a hundred trades says nothing, and advertising
"coverage" against it would manufacture a failure. Two scores instead: the **sign** of
the realized return against the sign of the point estimate, and the realized return's
**percentile within the cohort's stored outcome distribution**. Over many predictions
those percentiles should be uniform; a pile-up at the tails means the cohorts are not
describing the trades being taken. Sign hit rate and a percentile-uniformity check are
printed in the weekly review once twenty predictions have matured.
`test_mean_ci_not_scored_as_prediction_interval` asserts no coverage figure exists in
the schema. After a year that log is the only real evidence about whether any of this
machinery works, and it is the one measurement no methodological rigour substitutes for.

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
count, and the reader sees all three. The response also prints **the number of cells
examined**, defined as `n_horizons × (1 + n_regime_cells + n_stability_cells)` — because the recent
post-earnings-drift literature finds drift surviving mainly in conditional subsets, and
an engine that can slice on all of them will find significance somewhere. StepM is the one
multiplicity correction in v1; `arch`'s SPA and MCS over a family's cells are deferred
with shrinkage (§6.4) until families are large enough to rank. **Deflated Sharpe, PBO and CPCV are not computed
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
    status: Literal["ok", "inconclusive", "insufficient"]        # result state
    evidence_tier: Literal["clean_pit", "vendor_pit", "archival_reconstructed"]  # data quality
    refusal_reason: str | None       # non-null iff status != "ok"
    provenance_mix: dict[str, int]   # events per provenance class: observed_live / vendor_pit / archival
    block_length: int                # §6.1, the estimated length actually used
    n_eff: float                     # §6.2, effective sample size from distinct dates
    delisting_rate: float            # §4.2, share of cohort members that delisted in-window
    cells_examined: int              # §7
    cost_model: CostModel            # §5.3, half-spread table + slippage used
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

**Evidence tier and result status are separate axes.** Tier says how good the data is;
status says whether an answer exists. An `insufficient` answer is a distinct schema
(`RefusedAnswer`: setup, depth, status, evidence_tier, refusal_reason, n_matured,
n_distinct_dates, delisting_rate, trials) with **no statistic fields at all**, so the
"no field is optional" rule and the "no estimate below the floor" rule stop fighting.
`inconclusive` (§6.2 sign disagreement) is a full answer with both estimates shown and
`status="inconclusive"`. The three shapes form a discriminated union on `status`.
**One floor configuration** — `COHORT_FLOOR_DISTINCT_DATES=20`,
`COHORT_FLOOR_MATURED=30` — is the only place the numbers live; every section that
mentions a floor reads it from there.

Hard rules:

- **No field is optional** at the declared depth and status. A missing statistic is a
  failure, not a null.
- `tier="insufficient"` is returned — with the reason — whenever the cohort is below
  the floor, **which is on distinct event dates first** (default 20 distinct dates) and
  matured events second (default 30), because clustered events are not independent
  observations and the event count is the wrong denominator; or when the floor is met
  only by survivors (§4.2 delisting-rate composition check); or when point-in-time
  integrity cannot be established. A single-regime cohort is warned, not refused (§5.4).
- **`insufficient` is a valid, expected, frequently-correct answer.** The engine prints
  it rather than manufacturing a ranking. Every consumer — the approval card, the weekly review,
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
| `test_single_regime_warned_not_refused` | A one-regime cohort carries the `single_regime` warning and is still answered (§5.4) |
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
| `test_floor_is_on_distinct_dates` | 60 events on 12 dates is `insufficient`; 30 events on 20 dates is not |
| `test_calendar_time_is_primary` | The headline estimate and CI come from the calendar-time series; the clustered CAR is a cross-check field |
| `test_n_eff_reported` | `n_eff` falls as within-date correlation rises, holding `n_matured` fixed |
| `test_survivor_only_cohort_refused` | A cohort meeting the floor only after excluding delisted members is `insufficient` on composition |
| `test_delisting_audit_recorded` | A price snapshot without a delisting-audit result cannot back a `clean_pit` or `vendor_pit` cohort |
| `test_policy_return_is_net` | The headline policy return includes the half-spread and slippage; gross is a separately labelled field |
| `test_sue_from_xbrl_only` | `sue_seasonal` is computed from `companyfacts` rows and never from a vendor estimate field |
| `test_consensus_from_news_is_deterministic` | The extractor yields identical output on repeated runs and never imports a model client; `known_at_utc` equals the article timestamp |
| `test_consensus_news_disagreement_recorded` | Two articles citing different consensus figures store both and the spread, not a silent pick |
| `test_announcement_time_from_8k_acceptance` | An event dated from `filingDate` rather than `acceptanceDateTime` is rejected |
| `test_sector_is_current_vintage` | The balance block marks `sector` `vintage=current`; sector is never a required stratum |
| `test_shrinkage_k_method_of_moments` | Simulated families with known `τ²` recover `k` within tolerance; `τ̂² <= 0` yields full shrinkage, printed |
| `test_engine_predictions_scored` | A matured cited answer produces a `cohort_predictions` row with in-interval and sign fields |
| `test_cells_examined_counted` | Adding a regime split to a query increments `cells_examined` by the number of regimes |
| `test_headline_fixture_to_the_cent` | The §5.0 hand-calculated fixture (six events, two dates, one split, one delisting) reproduces every number in `docs/` exactly |
| `test_delisting_contributes_loss` | A −55% terminal return in session 7 moves the 10- and 20-session headline; `n_censored` is unchanged |
| `test_unknown_end_is_censored` | A halt with no resolution increments `n_censored` and does not enter the mean |
| `test_split_invariance` | A mid-hold 2-for-1 split yields an identical `TradeResult` to the no-split fixture |
| `test_market_cap_has_share_source` | A `market_cap_decile` condition on a name with no known share count at the event date is refused, not computed from a later count |
| `test_query_vs_cohort_uses_distance` | The balance block for a single query event carries standardized distance and percentile, never an SMD |
| `test_status_and_tier_separate` | An `insufficient` answer has no statistic fields; an `inconclusive` answer has both estimates and `status="inconclusive"` |
| `test_single_floor_config` | Changing `COHORT_FLOOR_DISTINCT_DATES` moves every floor check at once |
| `test_mean_ci_not_scored_as_prediction_interval` | `cohort_predictions` carries sign and percentile fields and no coverage field |
| `test_shrinkage_gated_on_family_size` | A family with four cohorts returns `shrinkage=null` with the reason |
| `test_sec_minimal_contract_in_phase3` | `comparables/` imports only `filings/sec_minimal.py` from the filings package, never the Phase 4 planes |

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

---

## 12. Rulings log (post-build)

Ratified 2026-09-09 from the Phase 3p build (PR #48):

- **Collapse threshold for the delisting audit is −60% over 10 sessions**,
  configurable, deliberately below the −55% Nasdaq correction so a drift *to* the
  correction level is not credited as a collapse.
- **The stored total-return series is derived from raw closes and stored factors;
  the vendor's own adjusted close is not stored.** A series that cannot be
  reproduced from stored factors is not inspectable. Cross-check against the vendor
  series once a key exists.
- **`liquid_us_equity_v1` is liquidity-only** (20-session median dollar volume,
  month-end ranks, a name ranks only if its window ends on the ranking date). A
  market-cap leg is a new slug once the share-count feed is joined.
- Sharadar SEP carries no raw OHLC (only `closeunadj`); raw OHLC is reconstructed as
  `field × (closeunadj / close)`. TICKERS carries no delisting reason; the reason
  comes from ACTIONS where present, else `unknown`, which §4.4 treats as censored.
- `consensus_eps_news` lives in `news/`, not `filings/` as §4.0 named it: it parses
  article bodies and belongs on the licence-bounded side. Same code, different file.

Ratified 2026-09-10 from the evidenced-budget closure (Spec L §6.6, §8):

- **The policy leg carries its own interval, at 0.90, and the headline stays at
  0.95.** `PolicySummary.net_ci` is a lower 90% bound on the §5.3
  policy-simulated net return, from the same stationary block bootstrap the
  headline uses but over the **per-event** net returns rather than the
  calendar-time series. Two confidence levels in one answer is a wart, and it is
  the smaller of two: Spec L §6.6 sizes a live order from `clip(LB/PE, 0, 1)` on
  this quantity and names the lower *90%* bound, so the alternative is handing
  the sizing rule a 95% bound because that is what was to hand — a statistic
  substituted for the one the rule names, which §9 forbids. The gate refuses an
  interval at any other level rather than relabelling it.
- **The block floor for a per-event series is in event units.** §6.1's floor is
  the horizon because one index step of the calendar-time series is one session.
  One index step of the per-event series is one *event*, so the same rule reads
  "the most events whose `horizon`-session windows overlap"
  (`inference.overlapping_events_block`). Using the horizon there would resample
  a 40-event cohort in blocks of 20 and call the result an interval.
- **A subject ticker is a property of the query, not of the setup.** §4.0 is
  right that a `SetupSpec` is a pattern and names no security; Spec L §6.6 is
  right that an evidenced proposal needs an answer "for the same ticker". Both
  hold only if the ticker is recorded when the question is asked, so
  `compare_setups` takes an optional `subject_ticker`, runs that one name
  through the same `cohort.py` qualification pass the members went through, and
  stores `subject_ticker` / `subject_qualifies` / `subject_reason` /
  `subject_event_date` on the query and the answer. An answer whose subject did
  not qualify is a valid answer that is not evidence for that name.
- **Qualification is judged at the subject's most recent candidate on or before
  `as_of`,** and the date of that candidate is stored rather than bounded. "The
  pattern last fired for this name and it qualified" is what the check asserts;
  how long ago it fired is printed for the reader. The 5-session citation-age
  budget bounds the age of the *answer*, and an event-recency budget is a
  separate decision nobody has made yet.
- **The subject is part of the answer cache key.** The statistics do not depend
  on it — two subjects give byte-identical `answer_json` — but the citation
  does: `cohort:<row id>` is what a journal entry and a proposal carry, and one
  row serving two subjects would silently re-point a citation already written
  down. Two subjects are two rows and two ids.
- **An event whose entry session is after `as_of` is censored, not excluded.**
  It qualified on facts that were true, so it is a cohort member — making
  membership depend on when the question was asked is precisely the difference
  §10's lookahead harness exists to catch, and the harness rejected the
  excluding version. `maturity` returns `entry_session_after_as_of`,
  `distinct_event_dates` skips it, and `calendar_time_series` drops any event
  whose window does not fit. Before this, one such member raised a `ValueError`
  out of the middle of the response assembly and took the whole answer with it,
  which made `depth='full'` on any day a universe member qualified an error
  rather than an answer.
