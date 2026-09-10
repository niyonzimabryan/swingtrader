# The macro plane (Phase 4)

Spec O §4. ALFRED vintages and a deterministic, versioned regime classifier,
behind `PLANE_MACRO_VINTAGE_ENABLED` (default `false`).

## The problem being fixed

`data/macro_data.py` read current FRED values. **Current values are revised
values.** Using today's GDP print to characterise a 2024 event is lookahead,
and it silently corrupts every regime label in Spec N — silently, and in the
direction that flatters, because a revision moves toward the eventual truth.

```python
macro_state(session, as_of=date.today())    # current values, as now
macro_state(session, as_of=date(2024, 3, 15))
# the values a person could have seen on 2024-03-15, INCLUDING the release lag:
# if February CPI had not yet been published it is absent, not back-filled
```

A series absent at `as_of` appears in the response's `missing` list. Absent is
a real answer; zero is not.

## Two facts about ALFRED the code respects

**Vintages are dated, not timestamped.** `realtime_start` is the date a value
became the current value. So every macro observation is `precision='day'` and
is known at the **close** of that date, via
`filings.observations.day_precision_known_at`. A cohort cutting off at 10:00
cannot inherit a print that landed at 08:30 that morning. That is half a day of
deliberate conservatism, and it errs on the side that cannot flatter a result.

**`USREC` is vintage-only.** NBER announces a turning point months to years
after it happened and revises it, so the current series marks a recession
starting on a date nobody could have known it started.
`macro.series.require_vintage('USREC')` raises on the current-value path.

## The series registry

`macro/series.py` classifies every series this repository reads. There is no
default: an unregistered id raises, because the point of the never-revised
allowlist is that nothing gets in by omission.

| Never revised | Revised |
|---|---|
| `SP500`, `VIXCLS` | `USREC` (vintage-only), `GDPC1`, `CPIAUCSL` |
| `DGS1MO`, `DGS3MO`, `DGS1`, `DGS2`, `DGS5`, `DGS10`, `DGS30` | `PAYEMS`, `UNRATE`, `DFF`, `BAMLC0A0CM` |

"Never revised" is a claim about the publisher: a price index level and a
closing volatility index are observations of a market that happened, and the
Treasury par yield curve is published once from that day's quotes. Everything
in the right column is an *estimate* that gets restated.

`DFF` sits in the revised column deliberately. The rate itself is a published
quote, but the FRED series carries methodology revisions and that has not been
verified here — so it is treated as revised until someone checks. That is the
fail-closed direction.

## Restatements are new rows

One observation per (reference period, release). A restatement lands beside the
original with a later `known_at_utc`, never overwriting it, so reading with
`known_at_utc <= t` returns the number that was on the screen at `t` and
reading with `t = now` returns today's. That is the whole bitemporal contract.

## `regime_v1`

A **deterministic, versioned** classifier — `macro/regime_v1.py` — over inputs
that are all on the never-revised allowlist.

Deterministic is the deliberate choice, not the simple one. Hidden-Markov
regime models overfit without regularisation and produce short-lived,
unintuitive regimes; the recent statistical-jump-model literature reads as
evidence that *persistence* matters more than statistical sophistication
(verification §28). And a fixed rule has one property no fitted model can
offer: **yesterday's label never changes.** A refit moves state assignments
retroactively, which breaks the point-in-time guarantee everything else here is
built on.

### Inputs

`SP500`, `VIXCLS`, `DGS10`, `DGS3MO` — checked against the allowlist by
`test_regime_v1_inputs_unrevised`. Derived: trend versus the 200-session
average, drawdown from the trailing 252-session peak, annualised 20-session
realized volatility (population standard deviation — the sample version differs
by ~2.6% at that window, enough to move a value across the 25% threshold), the
VIX level, and 10y−3m in basis points.

Inflation, employment and credit-spread composites are revised and are
`regime_v2`'s business, once their vintages are stored.

### Labels, in the order the rules fire

1. **`crisis`** — drawdown ≤ −20%, or VIX ≥ 30.
2. **`stress`** — drawdown ≤ −10%, or VIX ≥ 22, or realized vol ≥ 25%.
3. **`late_cycle`** — above the 200-session average with 10y−3m inverted.
4. **`expansion`** — above the 200-session average, curve not inverted.
5. **`contraction`** — below the 200-session average.

Severity is checked before direction, so a market above its 200-session average
during a volatility shock is not called `expansion`.

Anything the inputs cannot decide is **`insufficient_data`**, and that is a
real answer. A regime guessed from a missing VIX is a covariate that is
silently wrong on exactly the days data was missing — which are the disorderly
days.

The thresholds are judgement calls, and are documented as such: verification
§28 looked for a canonical published threshold set and did not find one. What
matters is not that they are optimal (they are deliberately not tuned — a tuned
threshold is a fitted parameter wearing a constant's clothes) but that they are
fixed, visible and versioned.

### Versioning

`Thresholds` is a frozen dataclass with a SHA-256 fingerprint over its values,
pinned to a literal in the module. Change a threshold and
`test_regime_classifier_versioned` fails with a message saying what to do:

> Copy `macro/regime_v1.py` to `macro/regime_v2.py`, change it there, and label
> new observations with the new version. Do **not** update the pinned
> fingerprint to make this pass — every regime label ever computed under
> `regime_v1` would then silently mean something else.

Every label carries its version and fingerprint, so a stored label can never be
reinterpreted under new thresholds.

### No model, structurally

`macro/regime_v1.py` imports the standard library and `macro/series.py`, and
nothing else. `test_no_llm_in_regime` walks the transitive import graph and
asserts it reaches no model client and no first-party runtime package. "A model
may narrate the regime; it may not assign it" is only true if it is structural.

## Reading it

`macro/api.py` ships the stable Python API, and **`macro_state` is registered
as an MCP read tool** on the workspace service (`workspace/tools.py`, scope
`read`). `as_of` arrives as `YYYY-MM-DD` and means the **close** of that date,
so a tool call and a direct call agree about whether a print released that
morning is visible.

The response carries `series`, `missing`, `regime` and a `provenance` block.

## Running it

```bash
export PLANE_MACRO_VINTAGE_ENABLED=true FRED_API_KEY=your_key

python -m scripts.macro_backfill --regime-inputs
python -m scripts.macro_backfill --series CPIAUCSL USREC --since 2015-01-01
python -m scripts.macro_backfill --regime-inputs --fixtures tests/fixtures/macro \
    --as-of 2025-10-01
```

The report prints **which inputs are vintage-clean** — the Spec O §4.2
checkpoint — by splitting the requested series into the never-revised allowlist
and the revised set.

`--since` is a **release-date** floor, not a reference-period floor: it selects
vintages published on or after that date, which is the bitemporal reading.

## The past-vs-current demonstration

Spec O §7 asks for one worked example, recorded in the docs. **FRED is
unreachable from the environment this branch was built in**
(`api.stlouisfed.org` is refused at the egress proxy), so the example below is
against `tests/fixtures/macro/`, not against live ALFRED. It is stated as such
rather than dressed up.

`CPIAUCSL`, reference period 2024-01-01:

| Vintage | Value |
|---|---|
| as of 2024-02-13 (first release) | 300.8510 |
| as of 2024-03-12 (first revision) | 301.0917 |
| as of 2026-09-01 (current) | 301.1819 |

Same reference period, three different numbers, because two revisions landed
in between — and `macro_state(as_of=2024-02-13)` returns the first while
`macro_state(as_of=today)` returns the last. `DGS10` is the control: the same
value at every vintage, because Treasury par yields are published once.

`test_macro_vintage_differs_from_current` and
`test_macro_state_at_a_past_date_differs_from_the_current_print` assert exactly
this. **Re-run it against live ALFRED before relying on the plane** — one
command, `python -m scripts.record_macro_fixtures --series CPIAUCSL`, then the
same test.
