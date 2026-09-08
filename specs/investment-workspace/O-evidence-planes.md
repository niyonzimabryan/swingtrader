# Spec O — Evidence planes: filings, macro, news

**Series:** [Investment Workspace K–Q](README.md) · **Status:** Draft v0.1 · **Date:** 2026-09-05
**Flags:** `PLANE_FILINGS_ENABLED=false`, `PLANE_MACRO_VINTAGE_ENABLED=false`,
`PLANE_NEWS_TIMESTAMPS_ENABLED=false`

---

## 1. Problem

Three kinds of evidence are missing or untrustworthy:

- **What strong investors are doing.** Nothing in the system reads 13F, 13D/G, or Form 4.
- **Macro, correctly dated.** `data/macro_data.py` reads current FRED values. Current
  values are *revised* values. Using today's GDP print to characterize a 2024 event is
  lookahead, and it silently corrupts every regime label in Spec N.
- **News with real timestamps.** News exists but publication time fidelity, dedup, and
  novelty are not modelled, so "what was known on the day" is unanswerable.

All three feed the same place: `source_observations`, the bitemporal ledger defined in
Spec Q §8. That table is the contract. A plane's job is to produce rows in it with an
honest `known_at_utc`.

## 2. The universal rule

> Every fact carries `valid_at` (when it applies) and `known_at_utc` (when we could
> first have known it). A fact whose `known_at_utc` is a guess is marked
> `replay_eligible=false` and `archival_reconstructed`, and can never support a Spec N
> `clean_pit` cohort or a Spec Q promotion.

For SEC filings this is unusually clean: the acceptance timestamp *is* `known_at_utc`.
That is why filings are the highest-quality plane and are built first. **It is
`acceptanceDateTime`, never `filingDate`.** Verified live (verification §13): a Form 4
with `filingDate` 2026-09-03 was accepted at 22:30 UTC that day, after the close. Keying
on the filing date treats post-close information as available intraday, systematically
and in the direction that flatters results. A test rejects any `known_at_utc` derived
from `filingDate`.

Every `source_observations` row also carries **`precision`** (`second` | `day`) and a
**provenance class** (`observed_live` | `vendor_pit` | `archival_reconstructed`, Spec N
§8). Precision matters because several planes are dated, not timestamped: an ALFRED
vintage is a date, a Sharadar `datekey` is a date. A `day`-precision fact is treated as
known at the **close** of that date, never at its open, so an intraday cohort cannot
inherit six and a half hours of lookahead from a macro print. `vendor_pit` is the class
for a filing-date or acceptance-date stamp we did not observe ourselves but a regulator
or vendor did; it is defensible and it is not the same as having been there.

---

## 3. Filings plane — what strong investors are doing

### 3.1 What each form is actually good for

Getting this wrong is the standard way smart-money data destroys a portfolio. The
constraints are structural, not fixable by better parsing.

| Form | Content | Timeliness | Honest use |
|---|---|---|---|
| **13F-HR** | Long US-listed equity positions of managers over the reporting threshold | Quarter-end, filed up to ~45 days later | **Deferred from Phase 4.** At a 1–20 session horizon a quarterly snapshot with a 45-day lag is nearly useless, and it is the most work of the three planes. The table row stays because the rules below still apply when it is built. |
| **13D** | Beneficial ownership above the threshold with intent to influence | Days after crossing | Activist situations; the *intent* narrative matters more than the stake |
| **13G** | Passive above-threshold ownership | Slower than 13D | Ownership structure, float, index/passive share |
| **Form 4** | Insider transactions | Within ~2 business days | **The one with genuine timeliness.** Open-market purchases by operating insiders are the informative subset |
| **8-K** | Material events | Prompt | Event detection and precise `known_at_utc` for Spec N cohorts |
| **N-PORT / N-CSR** | Fund holdings incl. more instrument types | Delayed | Cross-check on 13F for fund complexes |

Rules encoded in code, not in a doc:

1. Every 13F-derived fact carries `as_of` = quarter end and `known_at_utc` = filing
   acceptance, and **any surface that displays it prints the staleness in days.**
2. 13F shows longs only. No short, no options detail, no cash. Nothing may infer
   "conviction" from position size without the denominator, and the denominator is not
   in the filing. Rendering a 13F position without its portfolio share is a defect.
3. Form 4 must distinguish by **transaction code**: `P` (open-market purchase) and `S`
   (sale) are the informative ones; `A` (award), `M` (option exercise), `F` (tax
   withholding) and `G` (gift) are never pooled with them, and the 10b5-1 plan checkbox
   (on the form since 2023) flags planned sales. Conflating them is the most common
   insider-data error and inverts the signal. `edgartools` exposes the codes; the
   discipline is ours.
4. Amendments (`13F-HR/A`, `4/A`) supersede via `superseded_observation_id`; the
   original stays readable.

### 3.2 Entity resolution

The hard, unglamorous part — realistically a week of work. `filings/entities.py`:
- CIK ↔ ticker ↔ security, **with history** (tickers are reused; companies rename).
  EDGAR's `company_tickers.json` is a *current* snapshot; history is reconstructed from
  each CIK's `submissions` JSON (`formerNames`, ticker/exchange arrays) and stored with
  the date range it applied to.
- Manager CIK ↔ tracked-investor record, with a curated `tracked_investors` list Bryan
  maintains (name, CIK, style, why tracked, notes).
- CUSIP → ticker mapping for 13F holdings tables via **OpenFIGI** (free; accepts CUSIP as
  input, which is the direction needed), with unmapped **and ambiguous** rows surfaced
  rather than dropped — share classes and ADRs map to several FIGIs. Silent drops are
  how a "top holdings" view quietly lies.

### 3.3 Derived views

- **Position deltas** per tracked investor per quarter: new, added, trimmed, exited —
  each with the staleness stamp attached.
- **Consensus/crowding**: how many tracked investors hold a name; changes quarter over
  quarter. Presented as ownership context, explicitly not as a signal.
- **Insider clusters**: multiple distinct insiders buying on the open market within a
  window — the subset with the most support in the literature, and even so it is one
  covariate feeding a Spec N cohort, not a recommendation.
- **Ownership overlap with the portfolio** (Spec L): where Bryan is positioned alongside
  or against tracked managers.

Every one of these is a **cohort-eligible fact type** in Spec N — that is the point.
"Insiders bought" is not an answer; "setups with clustered insider buys returned X ± Y
over Z days, n=N" is.

### 3.4 Sourcing

EDGAR is free, authoritative, and carries acceptance timestamps — the primary source.
**Decision (research slot 4):** `edgartools` (MIT, actively released, rate-limit aware,
covers 13F / Form 4 with codes / XBRL / full-text search / CIK lookup) as the client;
the SEC's bulk **Form 13F**, **Insider Transactions**, and **Financial Statement** data
sets for backfill; OpenFIGI for CUSIP resolution. `sec-api.io` is the paid fallback only
if parser breakage costs more than an hour a month. The Robinhood MCP filing tools are
not relied on — the tool names cited in earlier drafts belong to an *unofficial*
community server (Spec L §5.1). **The adapter interface does not change with that
answer**: a plane produces `source_observations` rows, and nothing downstream knows
where they came from.

**Fundamentals are on the same plane.** The SEC XBRL `companyfacts` API gives every
reported fact with its accession number and filing date; joined to the `submissions`
`acceptanceDateTime`, that is a `known_at_utc` to the second, from the regulator, for
free — and restatements arrive as new facts with later stamps, so `known_at <= t`
returns the as-reported figure automatically. This is the `clean_pit`/`vendor_pit`
fundamentals source for Spec N (README §5 slot 3); FMP fundamentals carry no
availability stamp and stay exploratory. XBRL coverage is **per tag, not per company**: filers migrate tags (`Revenues` →
`RevenueFromContractWithCustomerExcludingAssessedTax`), so a naive pipeline shows gaps
that look like missing quarters and drops firms from cohorts non-randomly, since tag
migration correlates with filer size. `filings/xbrl_aliases.py` holds explicit tag-alias
maps with tests, and the ingest job **alerts on a coverage discontinuity** rather than
absorbing it. `edgartools` does much of the normalisation; the alias map is the part
that is ours. Two things to confirm in a REPL before relying on them: that the parsed
Form 4 object exposes the transaction code and the 10b5-1 flag as fields, and how
13F-HR/A amendments are represented (a naive union of HR and HR/A double-counts).

Access must respect the source's published rate limits and identification requirements;
`filings/client.py` owns throttling and retry in one place.

---

## 4. Macro plane — vintage-correct

### 4.1 The fix

`data/macro_data.py` reads current FRED values. Add a **vintage-aware** path: for any
historical query, fetch the series **as it was published at the time** (FRED's archival
vintage service, ALFRED — same API key, already provisioned as `FRED_API_KEY`).

Concretely:
- `macro_state(as_of=today)` → current values, as now.
- `macro_state(as_of=2024-03-15)` → the values a person could have seen on 2024-03-15,
  including the release lag: if February CPI had not yet been published, it is **absent**,
  not back-filled.
- Every macro observation lands in `source_observations` with `valid_at` = reference
  period and `known_at_utc` = release timestamp.

Without this, Spec N's regime labels are contaminated on every historical event and
every "how do setups like this do in this environment" answer is quietly wrong.

Two facts about ALFRED the code must respect: vintages are **dated, not timestamped**
(so `precision='day'`, known at the close of the vintage date — §2), and the NBER
recession indicator `USREC` is announced with a long lag and revised, so it is only
usable as its ALFRED vintage, never as the current series. `fredapi` already exposes
`get_series_as_of_date` / `get_series_all_releases` and stays; it is rated inactive
upstream, so it is pinned and wrapped in `data/macro_data.py` so a break is a one-file
fix (`pyfredapi` is the runner-up). Treasury par yield curves are published once and not
revised, so they are trivially point-in-time.

### 4.2 Regime labelling

A **versioned, deterministic** regime classifier — `macro/regime_v1.py` — over inputs
that are all vintage-correct: trend and drawdown state of the broad market, realized and
implied volatility level, the yield curve, credit spreads, and inflation/growth direction.

**Deterministic is the deliberate choice, not the simple one.** Hidden-Markov regime
models overfit without regularisation, collapse states onto tiny-variance regions, and
produce short-lived regimes that are unintuitive and underperform buy-and-hold in
practice; practitioners fall back to fixed-parameter, deterministic classifiers for
exactly the backtest-reliability reason this system needs. Nobody "upgrades" this to an
HMM later without a Spec Q experiment showing it helps.

**`regime_v1` uses only inputs that are never revised** — price trend and drawdown,
realized volatility, VIX level, and the Treasury yield curve — so it is point-in-time
by construction and Spec N's regime split (§5.4) does not wait on this plane's vintage
work. Revised macro series (inflation, employment, credit-spread composites) enter as
`regime_v2` once §4.1 has landed and their vintages are stored.

Rules:
- Output is a small, fixed label set with explicit thresholds, checked into code.
- The classifier is versioned exactly like a strategy (Spec Q §6): changing a threshold
  creates `regime_v2` and does not silently relabel history.
- **No LLM in the classifier.** A model may narrate the regime; it may not assign it.
- Regime is a covariate in Spec N §4.5 and a required stratification in §5.4.

### 4.3 Scope discipline

Macro is context, not a trade trigger. A macro-driven strategy would be a registered
Strategy Lab challenger competing on the same evidence as everything else — not a
special case that bypasses measurement.

---

## 5. News plane — timestamps, dedup, novelty

Extends `data/news_data.py` rather than replacing it. **Source decision (research slot
6):** the **Alpaca News API** (Benzinga-sourced, publisher timestamps, history to 2015,
free with the Alpaca account already configured) is the primary timestamped source;
Finnhub stays as a cross-check for the earliest-timestamp rule; Tiingo news is the
runner-up if Tiingo is bought for prices. GDELT stamps *ingest* time, not publication,
and may never supply `known_at_utc`. The incumbent Gemini-search + Firecrawl path cannot
establish publication time and is **removed from every cohort path**; it remains a live
research tool only. FNSPID is **dropped**: its licence is CC BY-NC-4.0 with an explicit prohibition on
commercial use, and a system that informs real trades is commercial use regardless of
aggregation. Alpaca's terms are the other constraint: they bar publishing the data "or
any derived products" and combining it with other sources for redistribution. So
**nothing derived from the news plane leaves Postgres** — no article text, no novelty
score, no news-derived feature appears in the `research/` mirror; a dossier may cite a
story by URL and date, and that is all (Spec K §3.3). Three additions:

### 5.1 Timestamp fidelity
Store publication timestamp, first-seen-by-us timestamp, and the *earliest* timestamp
across sources for a clustered story. **Two different `known_at_utc` values exist and
must not be merged:** the *story's* `known_at_utc` is the earliest defensible publisher
timestamp in the cluster; every *fact extracted from an article* (a consensus figure,
a guidance number, Spec N §4.0) carries the timestamp of **the article it was extracted
from**, never the cluster's. Otherwise a number that first appeared in a reaction piece
at 16:45 inherits the 07:00 preview's timestamp and becomes available before it
existed. Article revisions are stored as new rows, not overwrites. An article whose
publication time cannot be established is stored with `replay_eligible=false` and is
unusable in a `clean_pit` cohort. `test_fact_known_at_is_article_not_cluster` covers
the preview-then-reaction case.

### 5.2 Clustering, source tiering, novelty
- Cluster near-duplicate coverage into one story; a story republished by twenty outlets
  is one event, and counting it twenty times manufactures false momentum. The stack is
  deterministic: exact canonical-URL match, then MinHash/LSH over title-plus-lead
  shingles (`datasketch`, MIT) for wire pickups, with embedding cosine as an optional
  third pass for paraphrases. The cluster's `known_at_utc` is the minimum publisher
  timestamp across members, and applies to the story event only (§5.1).
- Tier sources: primary (company release, filing) > established wire/publication >
  aggregator > unattributed. The tier travels with the fact.
- **Novelty score**: does this story contain information absent from the prior cluster,
  or is it a restatement? Computed by comparing extracted structured facts, not by asking
  a model whether it feels new.

### 5.3 What news may and may not do — the eligibility matrix
One matrix, enforced by Spec N and tested there (`test_news_eligibility_matrix`):

| Use | Requires | Provenance class |
|---|---|---|
| **Date an event** (Spec N event clock) | publisher timestamp + primary tier (release, filing) | `vendor_pit` |
| **Qualify a cohort** as a structured fact (e.g. `consensus_eps_news`) | publisher timestamp + tier ≥ established wire/publication + deterministic extraction (Spec N §4.0) | `vendor_pit` |
| **Covariate or novelty context** | publisher timestamp | `vendor_pit` |
| **Dossier evidence, invalidator trigger** (Spec M) | any tier, rendered with its tier | — |
| **Spec Q promotion evidence** | **never** | — |

An article missing a timestamp qualifies for nothing above the last row. The earlier
wording "never a qualifying fact unless primary-tier" was too strict for structured
facts and too loose about promotion; this table replaces it.

---

## 6. Test plan

| Test | Asserts |
|---|---|
| `test_13f_staleness_rendered` | No surface displays a 13F position without days-since-quarter-end |
| `test_13f_requires_denominator` | Rendering a position without portfolio share raises |
| `test_form4_transaction_types_distinguished` | An award and an open-market purchase are never pooled |
| `test_amendment_supersedes` | A `4/A` supersedes; the original stays queryable |
| `test_unmapped_cusip_surfaced` | An unmappable holding appears in a warnings list, is not dropped |
| `test_ticker_reuse_resolved_by_date` | A recycled ticker resolves to the right CIK for the event date |
| `test_macro_vintage_absent_before_release` | A series unreleased at `as_of` is absent, not back-filled |
| `test_macro_vintage_differs_from_current` | A revised series returns different values for a historical `as_of` |
| `test_regime_classifier_versioned` | Changing a threshold requires a new version; history is not relabelled |
| `test_no_llm_in_regime` | Import-graph: `macro/regime_v1.py` reaches no model client |
| `test_news_cluster_counts_once` | Twenty republications produce one story |
| `test_untimestamped_news_not_replay_eligible` | Such an article cannot qualify a `clean_pit` cohort |
| `test_day_precision_known_at_close` | A `precision='day'` fact is not visible to a cohort whose cutoff is 10:00 on that date |
| `test_ambiguous_cusip_surfaced` | A CUSIP mapping to several FIGIs lands in warnings with all candidates, not a silent pick |
| `test_form4_codes_never_pooled` | `P` and `S` rows are never aggregated with `A`/`M`/`F`/`G`; a 10b5-1 sale is flagged |
| `test_usrec_only_via_vintage` | Requesting `USREC` without a vintage raises |
| `test_regime_v1_inputs_unrevised` | Every `regime_v1` input series is on the never-revised allowlist |
| `test_xbrl_known_at_from_acceptance` | A `companyfacts` row's `known_at_utc` equals its filing's `acceptanceDateTime`, precision `second` |
| `test_adapter_schema_change_fails_loudly` | An adapter receiving an unexpected payload shape raises; it never writes nulls |
| `test_gemini_search_not_in_cohort_path` | Import-graph: `comparables/` reaches no Gemini or Firecrawl client |
| `test_filing_date_never_known_at` | A `source_observations` write whose `known_at_utc` equals a `filingDate` midnight for an EDGAR source is rejected |
| `test_xbrl_alias_coverage_alert` | A tag migration in a fixture company produces a continuous series and an alert, not a gap |
| `test_news_derivatives_stay_in_postgres` | The mirror job refuses any file containing a news body, novelty score, or news-derived feature |
| `test_8k_202_timestamp` | An earnings event's `known_at_utc` is the Item 2.02 8-K `acceptanceDateTime` |
| `test_fact_known_at_is_article_not_cluster` | A consensus figure first present in a later article carries that article's timestamp, not the cluster's earliest |
| `test_news_eligibility_matrix` | Each row of §5.3 is enforced: an established-tier article qualifies a cohort fact; an aggregator-tier one does not; nothing from news reaches a promotion |

## 7. Definition of done

- A tracked-investor list exists and its quarterly deltas render with staleness.
- Insider open-market clusters are a queryable Spec N fact type, and a real cohort has
  been run against them and reported honestly — including if the answer is
  `insufficient`.
- 13D/G, Form 4, and 8-K are live; 13F is documented as deferred with the reason.
- `macro_state(as_of=<past date>)` demonstrably differs from the current print on a
  revised series, verified by hand on one example and recorded in the docs.
- Regime labels are deterministic, versioned, and reproducible from stored vintages.
- News stories carry timestamps and cluster ids; the untimestamped ones are quarantined.
