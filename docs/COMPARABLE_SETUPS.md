# Comparable setups

> *"A liquid mid-cap semiconductor name just beat EPS by 12% with raised
> guidance, gapped up 6%, and is extended over its 50-day. How have setups
> genuinely like this performed over the next two weeks — and how sure are
> we?"*

Producing a number for that is easy. Producing one that is not misleading is
the whole job, and it is the difference between a research system and a
random-number generator with a nice font.

This page is the operator's half of that. The methodology lives in
`specs/investment-workspace/N-comparable-setups-engine.md`; what follows is
what the answers mean, why the most common one is a refusal, and how to ask.

- Flag: `COMPARABLE_SETUPS_ENABLED=false` by default. Off means the
  `compare_setups` and `cohort_detail` tools are **not registered at all** —
  an unregistered tool is a clearer refusal than a registered one that answers
  "disabled".
- Depends on: the price plane ([`PRICE_PLANE.md`](PRICE_PLANE.md)) and the SEC
  ingestion contract ([`SEC_INGESTION.md`](SEC_INGESTION.md)).
- Schema: `migrations/versions/0005_comparable_registry.py`.

---

## The two axes, and why they are two

Every answer carries a **status** and an **evidence tier**, and they answer
different questions. Reading one as the other is the most likely way to
misread an answer.

**Status — does an answer exist?**

| `status` | What it means |
|---|---|
| `ok` | The cohort cleared the floors and the two estimators agree in sign. There is a number. |
| `inconclusive` | The cohort cleared the floors and the calendar-time estimate and the clustered-CAR cross-check **disagree in sign**. Both are shown; neither is the answer. Disagreement is information, not something to average away. |
| `insufficient` | There is no number. Below the floor, or met only by survivors, or point-in-time integrity could not be established. It carries the reason and **no statistic fields at all**. |

**Evidence tier — how good is the data?**

| `evidence_tier` | What it means |
|---|---|
| `clean_pit` | Every fact was recorded when it happened. |
| `vendor_pit` | Every fact carries a vendor's filing-date or acceptance-date stamp — reconstructed but defensible. An EDGAR `acceptanceDateTime`, a Sharadar `datekey`. |
| `archival_reconstructed` | At least one `known_at_utc` is a guess. |

`clean_pit` and `vendor_pit` may be pooled, with the mix printed.
`archival_reconstructed` is **never pooled with either** — it comes back as a
separate `archival_block` beside the answer, measured on its own events, and
`provenance_mix` sums to the whole cohort across both. Conflating the middle
class with either neighbour lies in a different direction each time.

Most of the history will be the middle tier. That is not a defect; it is what
free, honest data looks like.

### What caps a tier

Three things, and each one says so in the `provenance` block:

1. **No `universe_membership` rows for the period.** Membership evaluated from
   today's list is survivorship bias wearing a disguise — the names that died
   were illiquid on the way down. Capped at `archival_reconstructed`.
2. **A membership source that is itself a reconstruction.** `sp500_wikipedia_v1`
   is a hand-maintained Wikipedia scrape with acknowledged gaps.
   `liquid_us_equity_v1`, computed point-in-time from stored bars, is
   `vendor_pit`.
3. **A price snapshot with no recorded delisting audit.** A price file that
   merely *stops* at the last quote looks identical to one that carried the
   terminal collapse, and the difference is the entire terminal return. Without
   the audit the engine does not know which it has, so it says so in the tier
   rather than picking one. Run
   `python -m scripts.audit_delisting_returns` and the snapshot carries the
   result.

---

## Why `insufficient` is frequent, and correct

The floor is **20 distinct event dates first, 30 matured events second** —
`COHORT_FLOOR_DISTINCT_DATES` and `COHORT_FLOOR_MATURED`, and those two
constants are the only place the numbers live.

Distinct *dates* first, because clustered events are not independent
observations. Thirty semiconductor names reporting on the same Tuesday are one
observation wearing thirty hats, and the event count is the wrong denominator:
a bootstrap over events comes out too narrow by roughly the square root of the
events per date.

Now the arithmetic that makes the refusal frequent. A 1% abnormal return with
8% cross-sectional dispersion has a standard error near **1.5% at n = 30**. The
95% interval on a 1% effect runs from about −2% to +4%. That interval contains
zero, contains double the effect, and contains its own negation. Printing "1.0%"
from it, however many decimal places you use, describes nothing.

So `insufficient` will be the answer a lot of the time — especially early, when
the event library is small, and especially for any setup specific enough to be
interesting. **That is the system working.** Every consumer — the approval card,
the weekly review, the MCP tool, an agent's prose — must render it as a refusal,
never round it into a hedge. "Limited evidence suggests" is exactly the sentence
this design exists to prevent.

There are three ways out of an `insufficient`, and only two of them are honest:
loosen the setup so more events qualify, extend the lookback, or lower the
floor. The first two change the question you are asking; the third changes what
you are willing to believe. The third is a configuration change, visible, and
it should be argued for rather than nudged.

### The other refusals

- **Survivor-only composition.** A cohort whose delisting rate is zero while the
  universe's over the same period is not is refused on *composition*, not on
  sample size. Meeting the floor with only the names that survived is the
  failure mode dressed as a pass.
- **`pending_plane`.** Not an `insufficient`. `insider_cluster_v1` conditions on
  a Form 4 fact that Phase 4 supplies; the engine has not looked, and says so.
  Rounding "we have not looked" into "we found nothing" is the worse error.

---

## The decay split, and why it exists

Every cohort is also split chronologically — halves by event date, and per
calendar year where n allows — and the headline is reported per slice. The
response carries a `stability` block with both halves and a `decayed` flag,
set when the late-half interval excludes the early-half point estimate in the
direction of zero, or the sign flips. A decayed cohort is **not refused**, but
the pooled number is rendered *after* the split, never instead of it.

The reason is the best-documented "comparable setup" in the literature.
Post-earnings-announcement drift has shrunk materially since it was published:

- **Chordia, Subrahmanyam & Tong (2014)** find anomaly returns, PEAD among
  them, attenuating with the rise in liquidity and algorithmic trading.
- **Martineau (2022)**, *"Rest in Peace Post-Earnings Announcement Drift"*,
  argues the drift has largely disappeared in recent samples as prices respond
  faster to earnings news.
- **Meursault, Liang, Routledge & Scanlon (2023)** contest that: with a
  text-based surprise measure the drift survives in conditional subsets.

Note what the disagreement is *about* — not whether the effect once existed,
but whether it still does, and in which subsets. A cohort that pools 2010 with
2025 can report an average of a real effect and a dead one and call it a base
rate. That is why the split is mandatory rather than optional, and why the
recent literature's "surviving mainly in conditional subsets" is also why §7
counts how many subsets you looked at.

---

## Asking a question

Over MCP (`compare_setups`, scope `read`):

```json
{
  "setup": "gap_and_go_v1",
  "parameters": {"gap_pct": 4.0, "horizons_sessions": [1, 3, 5, 10]},
  "as_of": "2026-09-01",
  "depth": "quick"
}
```

Or with an explicit setup, for a question the roster does not cover:

```json
{
  "setup_spec": {
    "slug": "ad_hoc_semis_gap_v1",
    "version": "1.0.0",
    "conditions": [
      {"fact": "sue_seasonal", "op": ">", "value": 1.5},
      {"fact": "gap_pct", "op": ">", "value": 3.0}
    ],
    "universe": "liquid_us_equity_v1",
    "horizons_sessions": [1, 5, 10, 20],
    "execution_policy": "event_swing_14cal_v1",
    "match_covariates": ["market_cap_decile", "liquidity_decile", "realized_vol_decile"],
    "lookback_years": 5
  },
  "as_of": "2026-09-01",
  "depth": "full"
}
```

Every field of a `SetupSpec` is required. A setup is a typed predicate, not a
vibe, and the whole thing is content-hashed so the same question gives the same
answer and a changed threshold is a *new* setup rather than a rewritten one.

**`depth`.** `quick` returns raw, CAR, n, distinct dates, the bootstrap
interval, tier, balance and the trial count — enough to iterate in seconds, and
**not citable**. `full` returns everything, and is **mandatory** for anything
`journal_append`, `research_write` or a strategy promotion will reference. The
refusal is structural: a `quick` answer has no fields to cite.

**What comes back.** The §8 discriminated answer (three shapes on `status`),
plus a `provenance` block naming the price snapshot and its audit result, the
universe and its sources, the evidence cap, and the **query id**. Every answer
is stored in `cohort_answers` and every query in `comparable_queries`, and the
`citation_id` (`cohort:41`) is what a journal entry carries.

`cohort_detail` takes that `query_id` or `citation_id` and returns the
constituent events: each one's ticker, the instant it became knowable, its
provenance class, its matched covariates, how its history ended, and its
outcome at every horizon — matured, or censored with the reason. It also lists
the candidates that were *excluded* and why, which is usually the more
interesting half.

### The pre-registered roster

Setups are pre-registered, not improvised. `comparables/setups/` holds them,
frozen and hashed:

| Slug | Facts it needs | State |
|---|---|---|
| `gap_and_go_v1` | Price only | Available |
| `earnings_sue_seasonal_v1` | XBRL `eps_diluted` + the 8-K Item 2.02 acceptance timestamp | Available |
| `insider_cluster_v1` | Form 4 (Phase 4) | Refuses with `pending_plane` |

A free-form `setup_spec` is the same code path, and it counts as a trial
against the nearest family — the family slug is derived from the universe and
the primary condition, so an ad-hoc spec lands in the right family whatever it
calls itself.

### Two conventions worth knowing before quoting a number

**`gap_and_go_v1` enters on the session *after* the gap.** Spec N §5.0 puts
entry at the open of session 0, which is right for an 8-K accepted overnight:
the fact exists before the open you trade. A gap *is* an open, so an entry at
the same print is a fill nobody could have got. The qualifying fact is stamped
with the ledger's day-precision convention — a dated fact is known at the close
of its day — which puts session 0 on the following session. One session of
drift, in exchange for not being systematically optimistic in every cohort this
setup ever produces.

**`earnings_sue_seasonal_v1` dates an event at the instant its SUE was
knowable, which is not always the announcement.** XBRL EPS usually arrives with
the 10-Q, days or weeks after the release. When it does, the event is stamped
*there*, carries a `sue_known_after_announcement` warning naming both instants,
and the 8-K acceptance is still what identifies the announcement. Dating the
event at the announcement and qualifying it on a number that did not exist yet
is the failure mode the whole bitemporal filter exists to prevent. And the SUE
is always the *announced* quarter's: if the announced quarter is missing from
the ledger, the event is dropped rather than qualified on last quarter's
surprise, which is a stale-but-real number and therefore the most convincing
kind of wrong.

---

## The trial count

Every query is logged. The response prints how many setup variants have been
tried against this fact pattern, so the twelfth cannot present itself as the
first. "This fact pattern" is defined, not vibes: the **family slug**, derived
from the universe and the primary condition's fact and operator. Renaming a
setup does not reset the count, and moving a threshold is a new trial in the
same family — which is the point.

Refused queries are logged exactly like answered ones. A search that stopped
counting when it stopped succeeding would be no accounting at all.

The response also prints `cells_examined` — `n_horizons × (1 + n_regime_cells +
n_stability_cells)` — because an engine that can slice on all of them will find
significance somewhere. Raw and adjusted p-values appear together with the
trial count, and the reader sees all three.

The system will not stop you searching. It will refuse to let you forget that
you did.

---

## The engine's own track record

Every cited `full` answer is written to `cohort_predictions` and scored later
against the realized outcome of the query event, on exactly two things:

- the **sign** of the realized return against the sign of the point estimate;
- the realized return's **percentile** within the cohort's stored outcome
  distribution. Over many predictions those percentiles should be uniform; a
  pile-up at the tails means the cohorts are not describing the trades being
  taken.

**Not coverage.** The confidence interval is on the cohort *mean*. One trade
landing outside the interval for the average of a hundred trades says nothing,
and scoring it that way would manufacture a failure out of a category error.
There is no column in the table where a coverage figure could be stored, and a
test asserts there is not.

Sign hit rate and the percentile-uniformity check print in the weekly review
once twenty predictions have matured. After a year, that log is the only real
evidence about whether any of this machinery works, and it is the one
measurement no amount of methodological rigour substitutes for.

---

## The lookahead harness

`comparables/lookahead.py`, borrowed from freqtrade's `lookahead-analysis`. For
each cohort it rebuilds the answer with every fact table **physically truncated**
at each event's cutoff — observations, membership rows and price bars deleted
inside a savepoint that is always rolled back — and compares the result byte for
byte.

It exists because a point-in-time filter can be written correctly and still be
bypassed. The `known_at_utc <= t` clause is one line and it is right; the leak,
when there is one, is a join three modules away. Building this harness caught
two real ones in the cohort builder before either reached a number anybody would
have read: an eligible pool that stretched past the last event in the cohort, so
a universe rebuilt a month later moved a balance diagnostic; and the stale-SUE
case described above. Both would have passed every other test in this repository.

It runs in CI over the fixture cohorts (`tests/test_comparables_lookahead.py`)
and on demand against a real database:

```bash
python -m scripts.cohort_lookahead_check --as-of 2026-09-01
```

Exit status 1 means a cohort moved. That is a defect, not a warning.

---

## Running it

```bash
python -m scripts.cohort_smoke --as-of 2026-09-01 --depth quick
python -m scripts.cohort_smoke --dry-run          # composition only, stores nothing
```

`cohort_smoke` runs the roster against whatever `DATABASE_URL` points at and
prints the status, tier, composition and refusal reason for each. It leaves the
same trail a real question does — a row in `comparable_queries` and one in
`cohort_answers` — because a smoke run that stored nothing would not be testing
the thing that matters.

**As shipped, everything above was exercised against fixtures only.** The
warmed event library lives in the production database; nothing in this
repository has been run against a vendor price file or a real SEC ingest.

---

## Environment variables

| Variable | Default | What it does |
|---|---|---|
| `COMPARABLE_SETUPS_ENABLED` | `false` | Registers `compare_setups` and `cohort_detail`. Off means absent, not disabled. |
| `COMPARABLE_UNIVERSE_SLUG` | `liquid_us_equity_v1` | The stored universe membership is read from, as of each event date. |
| `COMPARABLE_PRICE_SNAPSHOT` | `dev` | The named price-file vintage cohorts run against. Its delisting audit must be recorded to reach `vendor_pit`. |
| `COMPARABLE_BENCHMARK_SECURITY_UID` | *(empty)* | The `security_uid` in `price_bars` every abnormal return is measured against. **Required** — a missing benchmark is a configuration error, not a default. |
| `COMPARABLE_EXECUTION_POLICY` | `event_swing_14cal_v1` | The named policy the §5.3 leg replays under. An unknown slug is refused rather than defaulted. |
| `COMPARABLE_QUICK_BOOTSTRAP_REPS` | `1000` | Bootstrap replications for a `quick` answer. |
| `COMPARABLE_FULL_BOOTSTRAP_REPS` | `10000` | Bootstrap replications for a `full` answer. |
| `COMPARABLE_CIK_MAP` | *(empty)* | `TICKER:CIK,TICKER:CIK` — the join `market_cap_decile` needs, because the price plane's security master has no CIK column and the SEC feed stamps a ticker. Empty means every name is *refused* a market-cap decile rather than given one computed from a count it could not have had. Phase 4's entity-history plane replaces it. |

The floors are constants, not environment variables, and deliberately:
`COHORT_FLOOR_DISTINCT_DATES = 20` and `COHORT_FLOOR_MATURED = 30` in
`comparables/config.py`. Lowering a floor changes what the system is willing to
call evidence, and that belongs in a diff somebody reviews rather than in an
environment somebody edits.

Two more that this engine reads from the phases it sits on:
`PRICE_PLANE_ENABLED` ([`PRICE_PLANE.md`](PRICE_PLANE.md)) and
`PLANE_SEC_MINIMAL_ENABLED` ([`SEC_INGESTION.md`](SEC_INGESTION.md)) gate the
*ingestion* that fills the tables. The cohort engine reads stored rows and does
not need either flag on to answer.

---

## What the model may and may not do

**May:** translate English into a `SetupSpec`, shown back in full before the
cohort is built; name which of several cohorts is most relevant; write the prose
around the numbers; propose the next cohort to test.

**May not:** produce, adjust, round, select, or characterize a statistic.

That is structural rather than promised. No model client is importable from
`comparables/` — a test walks the import graph and a subprocess checks
`sys.modules`. `CohortAnswer` cannot be constructed from text; the function
that would do it exists only to raise, and every numeric field rejects a string
at construction. Every number in any comparable-setups output traces to
`comparables/` code and a stored query.

If a rendered answer contains a figure the engine did not compute, that is a
defect with a failing test attached.
