# Comparable-setups engine — the hand-calculated fixture (Spec N §5.0)

**Status:** normative. **Date:** 2026-09-08.
**Spec:** [`specs/investment-workspace/N-comparable-setups-engine.md`](../../specs/investment-workspace/N-comparable-setups-engine.md) §5.0.
**Code:** the fixture data lives in [`comparables/fixtures.py`](../../comparables/fixtures.py);
`tests/test_comparables_fixture.py::test_headline_fixture_to_the_cent` asserts the
implementation reproduces every number below.

Spec N §5.0 requires that "`docs/` carries a hand-calculated fixture that every
implementation must reproduce to the cent." This is that document. Every number here was
derived from the §5.0 definitions by explicit arithmetic — the sums and quotients are
shown — and cross-checked with exact rational arithmetic (`fractions.Fraction`) in a
throwaway script that shares no code with `comparables/`. If the engine and this page
disagree, **the page is right until someone shows the arithmetic is wrong.**

Percentages are given to six decimal places so a reader can check the last digit; "to
the cent" means the headline figures agree to 0.01 percentage points, and the tests
assert agreement to 1e-9 in fractional units.

---

## 1. What the fixture contains

Exactly what Spec N §5.0 asks for, and nothing more:

| Requirement | In this fixture |
|---|---|
| six events | AAA, BBB, CCC (event date A) · DDD, EEE, FFF (event date B) |
| two event dates | **2024-01-04** and **2024-01-17** |
| one mid-hold 2-for-1 split | **CCC**, ex-date 2024-01-10 (event-clock session 4) |
| one performance-related delisting, −55% terminal | **EEE**, Nasdaq, delisting date 2024-01-26 (event-clock session 7) |
| a benchmark series | `BENCH`, a total-return index level starting at 400.00 |
| horizons | **(1, 5, 10) trading sessions** |

The cohort is deliberately **below both floors** (`COHORT_FLOOR_DISTINCT_DATES=20`,
`COHORT_FLOOR_MATURED=30`): 6 matured events on 2 distinct dates. So the *report* layer
refuses it (`status="insufficient"`, a `RefusedAnswer` with no statistic fields). This
document is about the **measurement** layer — the numbers `outcomes.py` and
`inference.py` compute regardless of whether the report is allowed to publish them.
That separation is the point of §8: the floor governs what may be said, not what is
computed.

---

## 2. The trading calendar

32 sessions, 2023-12-18 → 2024-02-02. Weekends are absent; so are **2023-12-25**
(Christmas) and **2024-01-15** (MLK Day). Session indices are absolute (`t`); the event
clock (`s`) is per-event and defined in §4.

```
 t  date         t  date         t  date         t  date
 0  2023-12-18   8  2023-12-29  16  2024-01-11  24  2024-01-24
 1  2023-12-19   9  2024-01-02  17  2024-01-12  25  2024-01-25
 2  2023-12-20  10  2024-01-03  18  2024-01-16  26  2024-01-26
 3  2023-12-21  11  2024-01-04  19  2024-01-17  27  2024-01-29
 4  2023-12-22  12  2024-01-05  20  2024-01-18  28  2024-01-30
 5  2023-12-26  13  2024-01-08  21  2024-01-19  29  2024-01-31
 6  2023-12-27  14  2024-01-09  22  2024-01-22  30  2024-02-01
 7  2023-12-28  15  2024-01-10  23  2024-01-23  31  2024-02-02
```

**This calendar is what makes `test_horizons_are_sessions` meaningful.** Event A's
10-session window runs `t = 11..20`, i.e. 2024-01-04 → **2024-01-18**: fifteen calendar
days for ten sessions, because it steps over two weekends *and* MLK Day. A horizon
counted in calendar days would have ended on 2024-01-13, a Saturday, two sessions early.

---

## 3. The benchmark

`BENCH` is a **total-return** index. Its session-0 open is 400.00 and it has no
overnight gaps (`open_t = close_{t-1}`), which is a simplification of the fixture, not
of the definition: in general the benchmark's session-0 return is measured from *its*
open, because the position is entered at an open.

Closes move in steps of **0.40** (0.1% of the base level). The step sequence, in units
of 0.40, is:

```
+1 +2 −1 +3 −2 +1 +4 −3   +2 +1 −1 +3 −2 +2 +1 −3   +4 −1 +2 +1 −2 +3 −1 +2   +1 −2 +3 +1 −1 +2 −3 +2
```

so `close_t = 400.00 + 0.40 × (cumulative steps)`. Daily return
`r_m,t = close_t / close_{t−1} − 1` (with `close_{−1} := 400.00`). The sessions that
matter for the two event windows:

| t | date | open | close | `r_m,t` |
|---:|---|---:|---:|---:|
| 11 | 2024-01-04 | 402.80 | 404.00 | 1.20/402.80 = **+0.297915%** |
| 12 | 2024-01-05 | 404.00 | 403.20 | −0.80/404.00 = **−0.198020%** |
| 13 | 2024-01-08 | 403.20 | 404.00 | +0.80/403.20 = **+0.198413%** |
| 14 | 2024-01-09 | 404.00 | 404.40 | +0.40/404.00 = **+0.099010%** |
| 15 | 2024-01-10 | 404.40 | 403.20 | −1.20/404.40 = **−0.296736%** |
| 16 | 2024-01-11 | 403.20 | 404.80 | +1.60/403.20 = **+0.396825%** |
| 17 | 2024-01-12 | 404.80 | 404.40 | −0.40/404.80 = **−0.098814%** |
| 18 | 2024-01-16 | 404.40 | 405.20 | +0.80/404.40 = **+0.197824%** |
| 19 | 2024-01-17 | 405.20 | 405.60 | +0.40/405.20 = **+0.098717%** |
| 20 | 2024-01-18 | 405.60 | 404.80 | −0.80/405.60 = **−0.197239%** |
| 21 | 2024-01-19 | 404.80 | 406.00 | +1.20/404.80 = **+0.296443%** |
| 22 | 2024-01-22 | 406.00 | 405.60 | −0.40/406.00 = **−0.098522%** |
| 23 | 2024-01-23 | 405.60 | 406.40 | +0.80/405.60 = **+0.197239%** |
| 24 | 2024-01-24 | 406.40 | 406.80 | +0.40/406.40 = **+0.098425%** |
| 25 | 2024-01-25 | 406.80 | 406.00 | −0.80/406.80 = **−0.196657%** |
| 26 | 2024-01-26 | 406.00 | 407.20 | +1.20/406.00 = **+0.295567%** |
| 27 | 2024-01-29 | 407.20 | 407.60 | +0.40/407.20 = **+0.098232%** |
| 28 | 2024-01-30 | 407.60 | 407.20 | −0.40/407.60 = **−0.098135%** |

The risk-free rate is **0 for the whole fixture**, stated here so `α + β(r_m − r_f)` has
no hidden input.

---

## 4. The event clock (§5.0)

- **Session 0** is the first session whose open is at or after `known_at_utc`. The
  fixture treats a session's open as **14:30 UTC** on that date.
- **Entry** is the open of session 0.
- Horizon `h` ends at the **close of session h−1**.
- `R_i(h) = close(session h−1) / entry − 1` on the total-return series.
- `r_i,0 = close_0 / entry − 1`; `r_i,s = close_s / close_{s−1} − 1` for `s ≥ 1`. So
  `R_i(h) = ∏_{s<h}(1 + r_i,s) − 1` exactly, and the overnight gap into session 0 is
  **not** part of the event return — it was not capturable.
- `CAR_i(h) = Σ_{s<h} (r_i,s − r_m,s)` — a **sum of daily excess returns**, not
  `R_i(h) − R_m(h)`. The two differ at large magnitudes, and EEE below shows how much.

| event | `known_at_utc` | session 0 | t of session 0 | provenance | liquidity decile |
|---|---|---|---:|---|---:|
| AAA | 2024-01-03 21:05:00Z | 2024-01-04 | 11 | `vendor_pit` | 8 |
| BBB | 2024-01-03 21:05:00Z | 2024-01-04 | 11 | `vendor_pit` | 5 |
| CCC | 2024-01-03 21:05:00Z | 2024-01-04 | 11 | `vendor_pit` | 9 |
| DDD | 2024-01-16 21:10:00Z | 2024-01-17 | 19 | `observed_live` | 7 |
| EEE | 2024-01-16 21:10:00Z | 2024-01-17 | 19 | `vendor_pit` | 3 |
| FFF | 2024-01-16 21:10:00Z | 2024-01-17 | 19 | `observed_live` | 6 |

`provenance_mix = {observed_live: 2, vendor_pit: 4, archival_reconstructed: 0}`, which
sums to 6. The two poolable classes are pooled; nothing archival is present.

Each event gaps into session 0 (that is what an earnings print does), and is gapless
afterwards (`open_s = close_{s−1}`), so the tables below need only the closes.

---

## 5. Prices

Each name is stored in three series (§4.3). For every name except CCC the three
coincide: no splits, no dividends in the window, so raw = split-adjusted =
total-return. **CCC has a 2-for-1 split with ex-date 2024-01-10 (`t = 15`)**, so its raw
series is *twice* the adjusted series before `t = 15` and equal to it from `t = 15` on;
the split factor 2.0 is stored with its ex-date, and either series reconstructs from the
other. Signals, replay and returns run on the **adjusted** series (§4.3); the raw series
exists so a fill can be mapped back to the price that printed.

Entry opens: AAA 50.00 · BBB 20.00 · CCC 80.00 · DDD 30.00 · EEE 10.00 · FFF 40.00
(CCC's *raw* open on 2024-01-04 is 160.00).

Closes over each event's 10 sessions:

| s | AAA | BBB | CCC (adj) | | s | DDD | EEE | FFF |
|--:|--:|--:|--:|--|--:|--:|--:|--:|
| 0 | 50.50 | 19.80 | 81.60 | | 0 | 30.60 | 9.90 | 40.40 |
| 1 | 51.00 | 19.60 | 82.40 | | 1 | 30.30 | 10.10 | 40.80 |
| 2 | 50.75 | 19.90 | 81.60 | | 2 | 30.90 | 9.80 | 41.20 |
| 3 | 51.50 | 19.50 | 83.20 | | 3 | 31.20 | 9.60 | 40.80 |
| 4 | 52.00 | 19.40 | 84.00 | | 4 | 30.60 | 9.50 | 41.60 |
| 5 | 51.75 | 19.60 | 83.20 | | 5 | 31.50 | 9.40 | 42.00 |
| 6 | 52.50 | 19.30 | 84.80 | | 6 | 31.80 | 9.20 | 41.60 |
| 7 | 53.00 | 19.20 | 86.40 | | 7 | 31.20 | **delisted** | 42.40 |
| 8 | 52.75 | 19.40 | 85.60 | | 8 | 32.10 | *(flat)* | 42.80 |
| 9 | 53.50 | 19.00 | 88.00 | | 9 | 32.40 | *(flat)* | 43.20 |

**EEE's terminal outcome (§4.2, §4.4).** EEE's last regular print is 9.20 at session 6
(2024-01-25). It is a **Nasdaq performance-related delisting** on 2024-01-26, so the
configured Nasdaq terminal return of **−55%** (Shumway 1997; Shumway & Warther 1999)
applies at session 7 and is **carried flat** through sessions 8 and 9:

```
terminal value = 9.20 × (1 − 0.55) = 9.20 × 0.45 = 4.14
r_EEE,7 = −0.55        r_EEE,8 = 0        r_EEE,9 = 0
```

EEE is **matured, not censored**: `n_matured = 6`, `n_censored = 0`,
`delisting_rate = 1/6 = 0.166667`.

Bars carry a high and a low, needed only by the policy leg (§8), by the stated rule

```
high = ROUND_HALF_UP(max(open, close) × 1.005, 2)
low  = ROUND_HALF_UP(min(open, close) × 0.995, 2)
```

---

## 6. Event returns, benchmark returns, and CAR

### 6.1 The per-session arithmetic

Each row is `r_i,s` (close over previous close, or over the entry open at `s = 0`),
`r_m,s` from §3, their difference, and the running `CAR`.

**AAA** — entry 50.00

| s | date | close | `r_i,s` | `r_m,s` | excess | cum CAR |
|--:|---|--:|--:|--:|--:|--:|
| 0 | 2024-01-04 | 50.50 | 0.50/50.00 = +1.000000% | +0.297915% | +0.702085% | +0.702085% |
| 1 | 2024-01-05 | 51.00 | 0.50/50.50 = +0.990099% | −0.198020% | +1.188119% | +1.890204% |
| 2 | 2024-01-08 | 50.75 | −0.25/51.00 = −0.490196% | +0.198413% | −0.688609% | +1.201595% |
| 3 | 2024-01-09 | 51.50 | 0.75/50.75 = +1.477833% | +0.099010% | +1.378823% | +2.580418% |
| 4 | 2024-01-10 | 52.00 | 0.50/51.50 = +0.970874% | −0.296736% | +1.267610% | **+3.848028%** |
| 5 | 2024-01-11 | 51.75 | −0.25/52.00 = −0.480769% | +0.396825% | −0.877595% | +2.970433% |
| 6 | 2024-01-12 | 52.50 | 0.75/51.75 = +1.449275% | −0.098814% | +1.548090% | +4.518523% |
| 7 | 2024-01-16 | 53.00 | 0.50/52.50 = +0.952381% | +0.197824% | +0.754557% | +5.273080% |
| 8 | 2024-01-17 | 52.75 | −0.25/53.00 = −0.471698% | +0.098717% | −0.570415% | +4.702665% |
| 9 | 2024-01-18 | 53.50 | 0.75/52.75 = +1.421801% | −0.197239% | +1.619040% | **+6.321705%** |

**BBB** — entry 20.00

| s | close | `r_i,s` | `r_m,s` | excess | cum CAR |
|--:|--:|--:|--:|--:|--:|
| 0 | 19.80 | −1.000000% | +0.297915% | −1.297915% | **−1.297915%** |
| 1 | 19.60 | −1.010101% | −0.198020% | −0.812081% | −2.109996% |
| 2 | 19.90 | +1.530612% | +0.198413% | +1.332200% | −0.777796% |
| 3 | 19.50 | −2.010050% | +0.099010% | −2.109060% | −2.886856% |
| 4 | 19.40 | −0.512821% | −0.296736% | −0.216085% | **−3.102941%** |
| 5 | 19.60 | +1.030928% | +0.396825% | +0.634102% | −2.468839% |
| 6 | 19.30 | −1.530612% | −0.098814% | −1.431798% | −3.900637% |
| 7 | 19.20 | −0.518135% | +0.197824% | −0.715959% | −4.616595% |
| 8 | 19.40 | +1.041667% | +0.098717% | +0.942950% | −3.673645% |
| 9 | 19.00 | −2.061856% | −0.197239% | −1.864617% | **−5.538262%** |

**CCC** — entry 80.00 (adjusted); the 2-for-1 split at session 4 is invisible here by
construction, which is the whole point of §4.3

| s | close | `r_i,s` | `r_m,s` | excess | cum CAR |
|--:|--:|--:|--:|--:|--:|
| 0 | 81.60 | +2.000000% | +0.297915% | +1.702085% | **+1.702085%** |
| 1 | 82.40 | +0.980392% | −0.198020% | +1.178412% | +2.880497% |
| 2 | 81.60 | −0.970874% | +0.198413% | −1.169286% | +1.711211% |
| 3 | 83.20 | +1.960784% | +0.099010% | +1.861774% | +3.572985% |
| 4 | 84.00 | +0.961538% | −0.296736% | +1.258274% | **+4.831260%** |
| 5 | 83.20 | −0.952381% | +0.396825% | −1.349206% | +3.482053% |
| 6 | 84.80 | +1.923077% | −0.098814% | +2.021891% | +5.503944% |
| 7 | 86.40 | +1.886792% | +0.197824% | +1.688969% | +7.192913% |
| 8 | 85.60 | −0.925926% | +0.098717% | −1.024643% | +6.168270% |
| 9 | 88.00 | +2.803738% | −0.197239% | +3.000977% | **+9.169247%** |

**DDD** — entry 30.00

| s | close | `r_i,s` | `r_m,s` | excess | cum CAR |
|--:|--:|--:|--:|--:|--:|
| 0 | 30.60 | +2.000000% | +0.098717% | +1.901283% | **+1.901283%** |
| 1 | 30.30 | −0.980392% | −0.197239% | −0.783153% | +1.118130% |
| 2 | 30.90 | +1.980198% | +0.296443% | +1.683755% | +2.801885% |
| 3 | 31.20 | +0.970874% | −0.098522% | +1.069396% | +3.871281% |
| 4 | 30.60 | −1.923077% | +0.197239% | −2.120316% | **+1.750966%** |
| 5 | 31.50 | +2.941176% | +0.098425% | +2.842751% | +4.593717% |
| 6 | 31.80 | +0.952381% | −0.196657% | +1.149038% | +5.742755% |
| 7 | 31.20 | −1.886792% | +0.295567% | −2.182359% | +3.560396% |
| 8 | 32.10 | +2.884615% | +0.098232% | +2.786384% | +6.346779% |
| 9 | 32.40 | +0.934579% | −0.098135% | +1.032715% | **+7.379494%** |

**EEE** — entry 10.00, delisted at session 7

| s | close | `r_i,s` | `r_m,s` | excess | cum CAR |
|--:|--:|--:|--:|--:|--:|
| 0 | 9.90 | −1.000000% | +0.098717% | −1.098717% | **−1.098717%** |
| 1 | 10.10 | +2.020202% | −0.197239% | +2.217441% | +1.118724% |
| 2 | 9.80 | −2.970297% | +0.296443% | −3.266740% | −2.148016% |
| 3 | 9.60 | −2.040816% | −0.098522% | −1.942294% | −4.090310% |
| 4 | 9.50 | −1.041667% | +0.197239% | −1.238905% | **−5.329215%** |
| 5 | 9.40 | −1.052632% | +0.098425% | −1.151057% | −6.480272% |
| 6 | 9.20 | −2.127660% | −0.196657% | −1.931003% | −8.411275% |
| 7 | *4.14* | **−55.000000%** | +0.295567% | −55.295567% | −63.706841% |
| 8 | *4.14* | 0.000000% | +0.098232% | −0.098232% | −63.805073% |
| 9 | *4.14* | 0.000000% | −0.098135% | +0.098135% | **−63.706938%** |

**FFF** — entry 40.00

| s | close | `r_i,s` | `r_m,s` | excess | cum CAR |
|--:|--:|--:|--:|--:|--:|
| 0 | 40.40 | +1.000000% | +0.098717% | +0.901283% | **+0.901283%** |
| 1 | 40.80 | +0.990099% | −0.197239% | +1.187338% | +2.088621% |
| 2 | 41.20 | +0.980392% | +0.296443% | +0.683949% | +2.772570% |
| 3 | 40.80 | −0.970874% | −0.098522% | −0.872352% | +1.900219% |
| 4 | 41.60 | +1.960784% | +0.197239% | +1.763546% | **+3.663764%** |
| 5 | 42.00 | +0.961538% | +0.098425% | +0.863113% | +4.526878% |
| 6 | 41.60 | −0.952381% | −0.196657% | −0.755724% | +3.771154% |
| 7 | 42.40 | +1.923077% | +0.295567% | +1.627510% | +5.398664% |
| 8 | 42.80 | +0.943396% | +0.098232% | +0.845164% | +6.243828% |
| 9 | 43.20 | +0.934579% | −0.098135% | +1.032715% | **+7.276543%** |

### 6.2 `R_i(h)`, `R_m(h)`, `CAR_i(h)`

`R_i(h) = close(h−1)/entry − 1`, and every one of them lands on a round number by
design so the table can be checked at a glance:

| event | `R_i(1)` | `R_i(5)` | `R_i(10)` | | `CAR(1)` | `CAR(5)` | `CAR(10)` |
|---|--:|--:|--:|--|--:|--:|--:|
| AAA | +1.000000% | +4.000000% | +7.000000% | | +0.702085% | +3.848028% | +6.321705% |
| BBB | −1.000000% | −3.000000% | −5.000000% | | −1.297915% | −3.102941% | −5.538262% |
| CCC | +2.000000% | +5.000000% | +10.000000% | | +1.702085% | +4.831260% | +9.169247% |
| DDD | +2.000000% | +2.000000% | +8.000000% | | +1.901283% | +1.750966% | +7.379494% |
| EEE | −1.000000% | −5.000000% | **−58.600000%** | | −1.098717% | −5.329215% | **−63.706938%** |
| FFF | +1.000000% | +4.000000% | +8.000000% | | +0.901283% | +3.663764% | +7.276543% |

EEE's `R_i(10)` is `4.14/10.00 − 1 = −58.60%`; its `CAR(10)` is `−63.706938%`. **They
are not the same number and neither is wrong** — CAR is a sum of daily excess returns,
which does not compound, so at −55% in a day the arithmetic and geometric measures part
company by five percentage points. This is why §5.0 fixes one definition per metric.

Benchmark returns over the identical sessions, `R_m(h) = ∏(1 + r_m,s) − 1`:

| window | `R_m(1)` | `R_m(5)` | `R_m(10)` |
|---|--:|--:|--:|
| event date A (t = 11…) | +0.297915% | +0.099305% | +0.496524% |
| event date B (t = 19…) | +0.098717% | +0.296150% | +0.493583% |

### 6.3 Cross-sectional means (§5.1, §5.2 cross-check)

| h | mean `R_i` (raw, §5.1) | mean `R_m` | **mean `CAR` (cross-check)** |
|--:|--:|--:|--:|
| 1 | +0.666667% | +0.198316% | **+0.468351%** |
| 5 | +1.166667% | +0.197727% | **+0.943644%** |
| 10 | −5.100000% | +0.495054% | **−6.516368%** |

Worked for `h = 10`:
`(6.321705 − 5.538262 + 9.169247 + 7.379494 − 63.706938 + 7.276543)/6 = −39.098211/6 = −6.516368%`.

---

## 7. The calendar-time portfolio and the `α × h` headline (§5.0, §6.2)

On each session `t` the portfolio holds every event whose window covers `t`,
equal-weighted; `p_t` is the mean of those events' daily returns; the daily abnormal
return is `p_t − r_m,t`. Then `p_t − r_f,t = α + β(r_m,t − r_f,t) + ε_t` with
`r_f = 0`, and **the headline is `α × h`**.

### 7.1 Portfolio membership

| h | sessions in the series | holdings per session |
|--:|---|---|
| 1 | 2024-01-04, 2024-01-17 | 3, 3 |
| 5 | 2024-01-04…2024-01-10 (5), 2024-01-17…2024-01-23 (5) | 3 each |
| 10 | 2024-01-04…2024-01-30 (18 sessions) | 3, except **6** on 2024-01-17 and 2024-01-18 where the two windows overlap |

Those two 6-name sessions are the overlap the block bootstrap exists for.

### 7.2 `h = 10` — the full series

| t | date | holdings | `p_t` | `r_m,t` | `p_t − r_m,t` |
|--:|---|--:|--:|--:|--:|
| 11 | 2024-01-04 | 3 | +0.666667% | +0.297915% | +0.368752% |
| 12 | 2024-01-05 | 3 | +0.320130% | −0.198020% | +0.518150% |
| 13 | 2024-01-08 | 3 | +0.023181% | +0.198413% | −0.175232% |
| 14 | 2024-01-09 | 3 | +0.476189% | +0.099010% | +0.377179% |
| 15 | 2024-01-10 | 3 | +0.473197% | −0.296736% | +0.769933% |
| 16 | 2024-01-11 | 3 | −0.134074% | +0.396825% | −0.530900% |
| 17 | 2024-01-12 | 3 | +0.613913% | −0.098814% | +0.712728% |
| 18 | 2024-01-16 | 3 | +0.773680% | +0.197824% | +0.575856% |
| 19 | 2024-01-17 | **6** | +0.274007% | +0.098717% | +0.175290% |
| 20 | 2024-01-18 | **6** | +0.698932% | −0.197239% | +0.896171% |
| 21 | 2024-01-19 | 3 | −0.003236% | +0.296443% | −0.299678% |
| 22 | 2024-01-22 | 3 | −0.680272% | −0.098522% | −0.581750% |
| 23 | 2024-01-23 | 3 | −0.334653% | +0.197239% | −0.531892% |
| 24 | 2024-01-24 | 3 | +0.950028% | +0.098425% | +0.851603% |
| 25 | 2024-01-25 | 3 | −0.709220% | −0.196657% | −0.512563% |
| 26 | 2024-01-26 | 3 | **−18.321239%** | +0.295567% | **−18.616805%** |
| 27 | 2024-01-29 | 3 | +1.276004% | +0.098232% | +1.177772% |
| 28 | 2024-01-30 | 3 | +0.623053% | −0.098135% | +0.721188% |

Session 11 checks by hand: `(+1.000000 − 1.000000 + 2.000000)/3 = +0.666667%`.
Session 19 checks: `(−0.471698 + 1.041667 − 0.925926 + 2.000000 − 1.000000 + 1.000000)/6 = 1.644043/6 = +0.274007%`.
Session 26 checks: `(−1.886792 − 55.000000 + 1.923077)/3 = −54.963715/3 = −18.321239%`.

OLS over the 18 sessions (population moments; `α` and `β` are identical either way):

```
mean p   = −0.722984%      mean r_m = +0.060583%
Var(r_m) = 4.142385747532e−06
β = Cov(p, r_m)/Var(r_m) = −5.840135951
α = mean p − β · mean r_m = −0.369174% per session
α × h    = −0.369174 × 10 = **−3.691740%**
```

**Read the β.** One session — the delisting — moves the portfolio −18.3% while the
market is up 0.30%, and an 18-point regression duly reports a beta of −5.84. It is not a
coding error; it is what OLS does with one dominant observation, and it is precisely the
instability that `COHORT_FLOOR_DISTINCT_DATES = 20` exists to keep out of a published
answer. The engine computes it, prints it, and the report layer refuses to publish it.
For comparison, the unadjusted mean daily abnormal return over the same series is
−0.783567%, i.e. **−7.835666%** over ten sessions.

### 7.3 All three horizons

| h | sessions | β | β estimated? | α per session | **`α × h`** |
|--:|--:|--:|:--|--:|--:|
| 1 | 2 | +1.000000000 | **no — fallback** | +0.468351% | **+0.468351%** |
| 5 | 10 | −0.257249404 | yes | +0.238669% | **+1.193345%** |
| 10 | 18 | −5.840135951 | yes | −0.369174% | **−3.691740%** |

**The β fallback is a choice the spec left open and it is named here.** With fewer than
three sessions in the calendar-time series, or zero variance in the benchmark series, β
is not identified. The engine then sets **β := 1** (market-neutral) rather than
estimating it, so `α = mean(p_t − r_m,t)` and the headline degrades to the mean daily
abnormal return — which for `h = 1` is exactly the mean CAR, +0.468351%. Setting β := 0
instead would have made the "abnormal" headline the *raw* return, which is the failure
mode §5.2 exists to prevent. The response carries `beta_estimated=False` so the
fallback is never invisible.

---

## 8. The policy-simulated leg (§5.3)

Policy `event_swing_14cal_v1`, derived from the entry reference (the open of session 0):

```
stop = 0.94 × open_0      target_1 = 1.04 × open_0      target_2 = 1.08 × open_0
max_holding_days = 14 calendar days      direction = long
```

Replay is `backtest/simulator.py::simulate_trade` **unmodified**, on the split-adjusted
bars, with `entry_idx` set to the session *before* session 0 so the simulator's T+1-open
rule puts the fill on session 0's open — the same entry the event clock uses.

**The cost model.** Half-spread by liquidity decile, from a configured table:

```
decile   1   2   3   4   5   6   7   8   9  10
hs(bps) 45  32  24  18  14  11   8   6   4   3
```

plus adverse slippage of **10 bps per fill** at baseline, with mandatory sensitivity at
**25** and **50** bps. Commissions zero. Both costs are adverse per-fill price
adjustments in basis points, which is exactly the simulator's own slippage model, so
they are applied by running the simulator at `slippage_bps = slippage + half_spread`.
**Gross is the same replay at `slippage_bps = 0`**, and is a separately labelled field —
never the headline.

| event | decile | hs (bps) | stop | T1 | T2 | rule fired | exit date |
|---|--:|--:|--:|--:|--:|---|---|
| AAA | 8 | 6 | 47.00 | 52.00 | 54.00 | `t1_then_time` | 2024-01-18 |
| BBB | 5 | 14 | 18.80 | 20.80 | 21.60 | `time` | 2024-01-18 |
| CCC | 9 | 4 | 75.20 | 83.20 | 86.40 | `t1_then_t2` | 2024-01-16 |
| DDD | 7 | 8 | 28.20 | 31.20 | 32.40 | `t1_then_t2` | 2024-01-30 |
| EEE | 3 | 24 | 9.40 | 10.40 | 10.80 | `stop` | 2024-01-24 |
| FFF | 6 | 11 | 37.60 | 41.60 | 43.20 | `t1_then_t2` | 2024-01-30 |

Per-event `pnl_pct` (the simulator rounds to 2 decimals; these are its numbers):

| event | gross | net @10 bps | net @25 bps | net @50 bps |
|---|--:|--:|--:|--:|
| AAA | +5.50% | +5.16% | +4.85% | +4.32% |
| BBB | −5.00% | −5.45% | −5.74% | −6.21% |
| CCC | +6.00% | +5.70% | +5.39% | +4.86% |
| DDD | +6.00% | +5.62% | +5.30% | +4.78% |
| EEE | −6.00% | −6.64% | −6.92% | −7.38% |
| FFF | +6.00% | +5.56% | +5.24% | +4.71% |
| **mean** | **+2.083333%** | **+1.658333%** | **+1.353333%** | **+0.846667%** |

Three things this table is here to say:

1. **The headline policy number is the net one, +1.658333%.** Gross overstates it by 43
   basis points on a two-week hold, and at 50 bps of slippage 59% of the gross edge is
   gone. A base rate reported gross describes a strategy nobody can run.
2. **EEE stops out at −6.00% on 2024-01-24, two sessions before it delists.** The policy
   never sees the −55%. The buy-and-hold CAR does (−63.7%). That gap between the raw
   number and the policy number is exactly the distinction §5.3 exists to make, in the
   opposite direction from the usual one: here the stop *saved* the cohort.
3. **CCC takes T1 on 2024-01-09 and T2 on 2024-01-16, straddling the 2-for-1 split on
   2024-01-10.** On the raw series that ex-date is a −50% overnight gap and the stop at
   75.20 fires on an economically neutral corporate action. On the split-adjusted series
   the `TradeResult` is identical to the no-split fixture, which is what
   `test_split_invariance` asserts.

---

## 9. Composition, proportions, and the derived diagnostics

```
n_events            = 6
n_matured           = 6
n_censored          = 0
n_distinct_dates    = 2
delisting_rate      = 1/6 = 0.166667
provenance_mix      = {observed_live: 2, vendor_pit: 4, archival_reconstructed: 0}
```

**Hit rate** (share of matured events with `CAR_i(h) > 0`) is 4/6 at every horizon
(AAA, CCC, DDD, FFF positive; BBB, EEE negative). At `p̂ = 2/3`, `n = 6`,
`z = 1.959963985`, the **Wilson** interval is

```
denominator = 1 + z²/n = 1 + 3.841458821/6            = 1.640243137
centre      = (p̂ + z²/2n)/denominator = (0.666667 + 0.320121)/1.640243137 = 0.601611
half-width  = z/denominator × √(p̂(1−p̂)/n + z²/4n²)
            = 1.194923 × √(0.037037 + 0.026677) = 1.194923 × 0.252416 = 0.301618
Wilson 95%  = (0.299993, 0.903229)
```

A normal-approximation interval would have been `0.666667 ± 1.959964 × 0.192450 =
(0.289471, 1.043862)` — an upper bound above 1, which is why §6.4 forbids it. **No
normal-approximation field exists in the schema.**

**`n_eff` (§6.2).** The design effect `n_eff = n / (1 + (m̄ − 1)ρ̄)` with `m̄` the mean
events per date (3) and `ρ̄` the one-way-ANOVA intraclass correlation of `CAR_i(h)`
across event dates, clamped to `[0, 1]`. In this fixture the within-date dispersion
exceeds the between-date dispersion at every horizon, so `ρ̂` is **negative** (−0.481109
at h=1, −0.336763 at h=5, −0.125343 at h=10), clamps to 0, and

```
n_eff = 6.000000 at h = 1, 5 and 10.
```

That is the honest answer for this fixture — these six events genuinely do not move
together within a date — and it is *not* evidence that clustering can be ignored in
general. `test_n_eff_reported` uses a purpose-built cohort with positive within-date
correlation to assert `n_eff` falls as `ρ̄` rises.

**Chronological decay split (§5.5).** Halves by event date: early = {AAA, BBB, CCC},
late = {DDD, EEE, FFF}. Reported with the headline estimator (calendar-time `α × h`):

| h | early half | late half | `decayed` |
|--:|--:|--:|:--|
| 1 | +0.368752% | +0.567950% | False |
| 5 | +1.956637% | +0.478972% | False |
| 10 | +4.074952% | −8.967479% | **True** (sign flip) |

Mean-CAR per half, for the cross-check: h=1 +0.368752% / +0.567950%; h=5 +1.858782% /
+0.028505%; h=10 +3.317563% / −16.350300%.

**Pre-event window (§6.3).** CAR over the 10 sessions *before* session 0:

| AAA | BBB | CCC | DDD | EEE | FFF | **mean** |
|--:|--:|--:|--:|--:|--:|--:|
| +1.610350% | −2.476567% | +2.159302% | +2.857773% | −2.846215% | +1.970902% | **+0.545924%** |

+0.55% of pre-drift over ten sessions against a +0.47% event-window headline at h=1 is
worth printing, which is the point of running the test on every cohort.

---

## 10. The two counterfactuals the tests turn on

**`test_delisting_contributes_loss`.** If EEE's last print (9.20) were carried flat
instead of the −55% terminal return being applied — the silent failure §4.2 exists to
prevent — the h=10 headline moves from **−6.516368%** to **+2.650298%**, a swing of 9.17
percentage points on a six-name cohort, and the cohort flips from a large loss to a
respectable gain. `n_censored` is **0 in both cases**: a delisting with a known reason is
matured, not censored. The h=5 mean CAR is **+0.943644% either way**, because the
delisting falls at session 7, outside the five-session window.

**`test_unknown_end_is_censored`.** A variant of the fixture halts EEE after session 6
with **no resolution**. Then `n_matured = 5`, `n_censored = 1` with reason
`unknown_end`, and the h=10 mean CAR is taken over the five matured events only:
`(6.321705 − 5.538262 + 9.169247 + 7.379494 + 7.276543)/5 = 24.608727/5 =` **+4.921745%**.
The censored event contributes nothing to the mean and is never silently dropped from
the counts.

---

## 11. What is *not* hand-calculated here

Three response fields are machine-computed and deterministic under a fixed seed rather
than derived by hand, and the tests assert reproducibility rather than a literal value:

- **`block_length`** — `arch.bootstrap.optimal_block_length` on the calendar-time
  abnormal series, then `max(that, h)` so overlapping windows resample together
  (§6.1). The value used is printed in every response.
- **Bootstrap percentile CIs** — 10,000 stationary-bootstrap resamples at
  `seed = 20260908`. `test_determinism` asserts the same inputs and seed give a
  byte-identical response.
- **Null-test empirical p-values** — placebo dates and random cohorts, same seed.

Everything else on this page is arithmetic, and the engine reproduces it to the cent.
