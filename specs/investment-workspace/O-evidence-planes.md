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
That is why filings are the highest-quality plane and are built first.

---

## 3. Filings plane — what strong investors are doing

### 3.1 What each form is actually good for

Getting this wrong is the standard way smart-money data destroys a portfolio. The
constraints are structural, not fixable by better parsing.

| Form | Content | Timeliness | Honest use |
|---|---|---|---|
| **13F-HR** | Long US-listed equity positions of managers over the reporting threshold | Quarter-end, filed up to ~45 days later | Idea generation; conviction and concentration context. **Never timing.** A position may be closed before you read it. |
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
3. Form 4 must distinguish **open-market purchase** from award, option exercise,
   10b5-1 planned sale, and tax withholding. Conflating them is the most common
   insider-data error and inverts the signal.
4. Amendments (`13F-HR/A`, `4/A`) supersede via `superseded_observation_id`; the
   original stays readable.

### 3.2 Entity resolution

The hard, unglamorous part. `filings/entities.py`:
- CIK ↔ ticker ↔ security, **with history** (tickers are reused; companies rename).
- Manager CIK ↔ tracked-investor record, with a curated `tracked_investors` list Bryan
  maintains (name, CIK, style, why tracked, notes).
- CUSIP → ticker mapping for 13F holdings tables, with unmapped rows surfaced rather
  than dropped. Silent drops are how a "top holdings" view quietly lies.

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
The Robinhood MCP surface also exposes filing, filing-index, and filing-facts tools that
may cover part of this; the research (README §5) decides whether a library, a paid API,
or direct EDGAR access is the implementation. **The adapter interface does not change
with that answer**: a plane produces `source_observations` rows, and nothing downstream
knows where they came from.

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

### 4.2 Regime labelling

A **versioned, deterministic** regime classifier — `macro/regime_v1.py` — over inputs
that are all vintage-correct: trend and drawdown state of the broad market, realized and
implied volatility level, the yield curve, credit spreads, and inflation/growth direction.

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

Extends `data/news_data.py` rather than replacing it. Three additions:

### 5.1 Timestamp fidelity
Store publication timestamp, first-seen-by-us timestamp, and the *earliest* timestamp
across sources for a clustered story. `known_at_utc` is the earliest defensible one. An
article whose publication time cannot be established is stored with
`replay_eligible=false` and is unusable in a `clean_pit` cohort.

### 5.2 Clustering, source tiering, novelty
- Cluster near-duplicate coverage into one story; a story republished by twenty outlets
  is one event, and counting it twenty times manufactures false momentum.
- Tier sources: primary (company release, filing) > established wire/publication >
  aggregator > unattributed. The tier travels with the fact.
- **Novelty score**: does this story contain information absent from the prior cluster,
  or is it a restatement? Computed by comparing extracted structured facts, not by asking
  a model whether it feels new.

### 5.3 What news may not do
News never supports a Spec Q promotion and is never a cohort's qualifying fact unless it
carries a defensible timestamp and a primary-tier source. Its jobs are: precise event
dating, dossier evidence (Spec M), and the invalidator triggers of Spec M §4.

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

## 7. Definition of done

- A tracked-investor list exists and its quarterly deltas render with staleness.
- Insider open-market clusters are a queryable Spec N fact type, and a real cohort has
  been run against them and reported honestly — including if the answer is
  `insufficient`.
- `macro_state(as_of=<past date>)` demonstrably differs from the current print on a
  revised series, verified by hand on one example and recorded in the docs.
- Regime labels are deterministic, versioned, and reproducible from stored vintages.
- News stories carry timestamps and cluster ids; the untimestamped ones are quarantined.
