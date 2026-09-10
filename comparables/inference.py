"""Inference (Spec N §6) — the part that decides how sure we are.

Assembled from parts, as the research said it would have to be
(`docs/research/2026-09-research-verification.md` §26): `arch` for the
stationary block bootstrap and the block-length estimator, `statsmodels` for
two-way clustered covariance, and vendored NumPy for Wilson intervals, the
effective sample size, the empirical-Bayes shrinkage estimator and the null
tests. `mlfinlab` is not open source and is never a dependency.

Everything here is seeded. The same inputs and the same seed give the same
numbers, which `test_determinism` asserts byte-for-byte.
"""

from __future__ import annotations

import math
import warnings
from statistics import NormalDist
from dataclasses import dataclass, field
from datetime import date
from typing import Mapping, Sequence

import numpy as np

from comparables import config
from comparables.setup_spec import SetupSpec

# --------------------------------------------------------------------------- #
# Block length and the block bootstrap (§6.1)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BlockLength:
    """The block length actually used, and where it came from."""

    estimated: float          # arch's Politis-White / PPW estimate
    horizon_floor: int        # the horizon, as a floor
    used: int                 # max(ceil(estimated), horizon), capped at n
    method: str = "politis_white_ppw_stationary"


def optimal_block_length_for(series: Sequence[float], horizon: int) -> BlockLength:
    """`arch.bootstrap.optimal_block_length`, bounded below by the horizon.

    The floor matters: overlapping event windows must always be resampled
    together, so a block shorter than the horizon would break the dependence the
    bootstrap exists to preserve (§6.1).
    """
    from arch.bootstrap import optimal_block_length

    values = np.asarray(list(series), dtype=float)
    n = len(values)
    method = "politis_white_ppw_stationary"
    if n < 8 or float(np.var(values)) == 0.0:
        # arch's estimator needs a series to work with; below that the horizon
        # floor is the whole answer, and the response says so.
        estimated = 1.0
        method = "horizon_floor_only_series_too_short"
    else:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                estimated = float(optimal_block_length(values)["stationary"].iloc[0])
            except (ValueError, IndexError):
                estimated = 1.0
                method = "horizon_floor_only_estimator_failed"
    used = max(int(math.ceil(estimated)), int(horizon), 1)
    used = min(used, max(n, 1))
    return BlockLength(estimated=estimated, horizon_floor=int(horizon),
                       used=used, method=method)


def overlapping_events_block(session_positions: Sequence[int], horizon: int) -> int:
    """The block floor for a series indexed by **event**, not by session.

    `optimal_block_length_for` floors the block at the horizon because
    overlapping event windows have to be resampled together, and in the
    calendar-time series one index step is one session, so `horizon` steps *is*
    one window. A per-event series does not work that way: one index step is one
    event, and the same rule in those units is "the most events whose
    `horizon`-session windows overlap". A cohort with two events a year apart
    has independent observations at any horizon and a floor of 1; a cohort whose
    events cluster on three dates does not, and this says so.

    `session_positions` are the events' entry sessions as indices into the
    calendar, in any order. Returns at least 1.
    """
    positions = sorted(int(p) for p in session_positions)
    span = max(int(horizon), 1)
    widest = 1
    start = 0
    for end in range(len(positions)):
        while positions[end] - positions[start] >= span:
            start += 1
        widest = max(widest, end - start + 1)
    return widest


@dataclass(frozen=True)
class ConfidenceInterval:
    """A point estimate can never be constructed without one (§6.1)."""

    estimate: float
    lower: float
    upper: float
    level: float
    method: str
    block_length: int
    reps: int
    seed: int

    @property
    def width(self) -> float:
        return self.upper - self.lower


def _percentile_ci(draws: np.ndarray, estimate: float, level: float,
                   method: str, block_length: int, reps: int,
                   seed: int) -> ConfidenceInterval:
    tail = (1.0 - level) / 2.0 * 100.0
    lower = float(np.percentile(draws, tail))
    upper = float(np.percentile(draws, 100.0 - tail))
    return ConfidenceInterval(estimate, lower, upper, level, method,
                              block_length, reps, seed)


def stationary_bootstrap_ci(
    series: Sequence[float],
    block_length: int,
    *,
    scale: float = 1.0,
    reps: int = config.BOOTSTRAP_REPS,
    seed: int = config.DEFAULT_SEED,
    level: float = config.CONFIDENCE_LEVEL,
) -> ConfidenceInterval:
    """Percentile CI for `scale x mean(series)` under Politis-Romano (1994)."""
    from arch.bootstrap import StationaryBootstrap

    values = np.asarray(list(series), dtype=float)
    if values.size == 0:
        raise ValueError("empty series")
    estimate = float(values.mean()) * scale
    if values.size == 1:
        return ConfidenceInterval(estimate, estimate, estimate, level,
                                  "stationary_block_bootstrap", block_length, reps, seed)
    bs = StationaryBootstrap(max(int(block_length), 1), values, seed=int(seed))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        draws = np.asarray([
            float(np.asarray(data[0][0]).mean()) * scale
            for data, _ in bs.bootstrap(reps)
        ])
    return _percentile_ci(draws, estimate, level, "stationary_block_bootstrap",
                          int(block_length), reps, int(seed))


def iid_bootstrap_ci(
    series: Sequence[float],
    *,
    scale: float = 1.0,
    reps: int = config.BOOTSTRAP_REPS,
    seed: int = config.DEFAULT_SEED,
    level: float = config.CONFIDENCE_LEVEL,
) -> ConfidenceInterval:
    """The naive iid resample. Present only as the thing to compare against."""
    values = np.asarray(list(series), dtype=float)
    rng = np.random.default_rng(int(seed))
    idx = rng.integers(0, values.size, size=(reps, values.size))
    draws = values[idx].mean(axis=1) * scale
    return _percentile_ci(draws, float(values.mean()) * scale, level,
                          "iid_bootstrap", 1, reps, int(seed))


# --------------------------------------------------------------------------- #
# Clustering (§6.2)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ClusteredCARResult:
    """Two-way clustered CAR regression — the cross-check, never the headline."""

    estimate: float
    std_error: float
    t_stat: float
    p_value: float
    n_clusters_date: int
    n_clusters_ticker: int
    reliable: bool
    reliability_reason: str


def two_way_clustered_car(
    cars: Sequence[float],
    event_dates: Sequence[date],
    tickers: Sequence[str],
) -> ClusteredCARResult:
    """Mean CAR with SEs clustered on event date x ticker, via `statsmodels`.

    With fewer than ~30 clusters the cluster-robust asymptotics fail (§6.2), so
    the result is labelled `unreliable` and the caller renders only the
    bootstrap CI.
    """
    import statsmodels.api as sm

    y = np.asarray(list(cars), dtype=float)
    n = y.size
    if n == 0:
        raise ValueError("no CARs to regress")
    dates = list(event_dates)
    ticks = list(tickers)
    n_dates = len(set(dates))
    n_ticks = len(set(ticks))
    min_clusters = config.CLUSTERED_SE_MIN_CLUSTERS

    reliable = min(n_dates, n_ticks) >= min_clusters
    reason = (
        "ok" if reliable else
        f"n_distinct_dates={n_dates} and n_tickers={n_ticks}; cluster-robust "
        f"asymptotics need about {min_clusters} clusters, so the clustered SE is "
        f"unreliable and the bootstrap CI is the only interval rendered"
    )

    date_ids = {d: i for i, d in enumerate(sorted(set(dates)))}
    tick_ids = {t: i for i, t in enumerate(sorted(set(ticks)))}
    groups = np.column_stack([
        np.array([date_ids[d] for d in dates]),
        np.array([tick_ids[t] for t in ticks]),
    ])
    X = np.ones((n, 1))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            fit = sm.OLS(y, X).fit(cov_type="cluster",
                                   cov_kwds={"groups": groups, "use_correction": False})
            se = float(fit.bse[0])
            t = float(fit.tvalues[0])
            p = float(fit.pvalues[0])
        except Exception as exc:               # degenerate cluster structure
            se, t, p = float("nan"), float("nan"), float("nan")
            reliable = False
            reason = f"clustered covariance could not be computed: {exc}"
    return ClusteredCARResult(
        estimate=float(y.mean()),
        std_error=se,
        t_stat=t,
        p_value=p,
        n_clusters_date=n_dates,
        n_clusters_ticker=n_ticks,
        reliable=reliable,
        reliability_reason=reason,
    )


@dataclass(frozen=True)
class EffectiveSampleSize:
    """`n_eff` from distinct event dates and the within-date correlation (§6.2)."""

    n: int
    n_distinct_dates: int
    mean_cluster_size: float
    icc_raw: float
    icc_used: float
    n_eff: float


def effective_sample_size(
    values: Sequence[float], event_dates: Sequence[date]
) -> EffectiveSampleSize:
    """Design effect `n / (1 + (m - 1) rho)` with a one-way ANOVA ICC.

    A negative ICC — within-date dispersion exceeding between-date dispersion —
    is clamped to zero, so `n_eff` never exceeds `n`.
    """
    y = np.asarray(list(values), dtype=float)
    dates = list(event_dates)
    n = y.size
    if n == 0:
        raise ValueError("no values")
    groups: dict[date, list[float]] = {}
    for value, day in zip(y, dates):
        groups.setdefault(day, []).append(float(value))
    k = len(groups)
    sizes = np.array([len(v) for v in groups.values()], dtype=float)
    m_bar = float(n) / k

    if k <= 1:
        icc_raw = 1.0
    elif k >= n:
        icc_raw = 0.0
    else:
        grand = float(y.mean())
        ssb = sum(len(v) * (float(np.mean(v)) - grand) ** 2 for v in groups.values())
        ssw = sum(sum((x - float(np.mean(v))) ** 2 for x in v) for v in groups.values())
        msb = ssb / (k - 1)
        msw = ssw / (n - k)
        m0 = (n - float((sizes ** 2).sum()) / n) / (k - 1)
        denom = msb + (m0 - 1.0) * msw
        icc_raw = 0.0 if denom == 0 else (msb - msw) / denom
    icc = min(max(icc_raw, 0.0), 1.0)
    return EffectiveSampleSize(
        n=n,
        n_distinct_dates=k,
        mean_cluster_size=m_bar,
        icc_raw=float(icc_raw),
        icc_used=float(icc),
        n_eff=float(n) / (1.0 + (m_bar - 1.0) * icc),
    )


# --------------------------------------------------------------------------- #
# Proportions (§6.4)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProportionInterval:
    """A Wilson score interval. There is no normal-approximation counterpart."""

    successes: int
    n: int
    point: float
    lower: float
    upper: float
    level: float
    method: str = "wilson"


def wilson_interval(
    successes: int, n: int, level: float = config.CONFIDENCE_LEVEL
) -> ProportionInterval:
    """Wilson (1927). Never the normal approximation, which is wrong exactly
    where it matters — small n and rates near 0 or 1 (§6.4)."""
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= successes <= n:
        raise ValueError("successes must be in 0..n")
    z = NormalDist().inv_cdf(0.5 + level / 2.0)
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return ProportionInterval(successes, n, p, centre - half, centre + half, level)


# --------------------------------------------------------------------------- #
# Null tests (§6.3)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NullTestResult:
    name: str
    estimate: float
    lower: float
    upper: float
    p_value: float
    n_draws: int
    seed: int
    note: str = ""


@dataclass(frozen=True)
class NullTestResults:
    placebo_dates: NullTestResult
    random_cohorts: NullTestResult
    pre_event_window: NullTestResult


def _empirical_band(draws: np.ndarray, level: float) -> tuple[float, float]:
    tail = (1.0 - level) / 2.0 * 100.0
    return float(np.percentile(draws, tail)), float(np.percentile(draws, 100.0 - tail))


def null_from_draws(
    name: str,
    draws: Sequence[float],
    observed: float,
    seed: int,
    level: float = config.CONFIDENCE_LEVEL,
    note: str = "",
) -> NullTestResult:
    """Where the observed effect sits in a null distribution — no distributional
    assumption, just the empirical two-sided p-value (§6.3)."""
    values = np.asarray(list(draws), dtype=float)
    if values.size == 0:
        raise ValueError("no draws")
    lower, upper = _empirical_band(values, level)
    p = float((np.abs(values) >= abs(observed)).mean())
    return NullTestResult(name, float(values.mean()), lower, upper, p,
                          int(values.size), int(seed), note)


# --------------------------------------------------------------------------- #
# Stability over time (§5.5)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SliceEstimate:
    label: str
    n: int
    estimate: float
    lower: float
    upper: float


@dataclass(frozen=True)
class StabilityResult:
    early: SliceEstimate
    late: SliceEstimate
    per_year: tuple[SliceEstimate, ...]
    decayed: bool
    reason: str
    method: str


def decayed_flag(early: SliceEstimate, late: SliceEstimate) -> tuple[bool, str]:
    """`decayed` when the late CI excludes the early point estimate in the
    direction of zero, or the sign flips (§5.5)."""
    if early.estimate == 0.0:
        return False, "early half is exactly zero; nothing to decay from"
    if (early.estimate > 0) != (late.estimate > 0):
        return True, "the sign of the headline flips between the halves"
    if early.estimate > 0 and late.upper < early.estimate:
        return True, "the late-half CI lies entirely below the early-half estimate"
    if early.estimate < 0 and late.lower > early.estimate:
        return True, "the late-half CI lies entirely above the early-half estimate"
    return False, "the late half is consistent with the early half"


# --------------------------------------------------------------------------- #
# Multiplicity (§7)
# --------------------------------------------------------------------------- #


@dataclass
class TrialRegistry:
    """How many setup variants have been tried against this fact pattern.

    Keyed by `SetupSpec.family_slug`, which is derived from the universe and the
    primary condition and **not** from the slug — so renaming a setup within the
    same family does not reset its count (§7).
    """

    _variants: dict[str, list[str]] = field(default_factory=dict)
    _queries: dict[str, int] = field(default_factory=dict)

    def record(self, spec: SetupSpec) -> int:
        family = spec.family_slug
        seen = self._variants.setdefault(family, [])
        digest = spec.content_hash
        if digest not in seen:
            seen.append(digest)
        self._queries[family] = self._queries.get(family, 0) + 1
        return len(seen)

    def trials(self, spec: SetupSpec) -> int:
        return len(self._variants.get(spec.family_slug, []))

    def queries(self, spec: SetupSpec) -> int:
        return self._queries.get(spec.family_slug, 0)


@dataclass(frozen=True)
class MultiplicityResult:
    """Raw and adjusted significance, printed together with the trial count."""

    family_slug: str
    n_trials: int
    p_raw: float
    p_sidak: float
    stepm_rejected: bool
    stepm_note: str
    method: str = "romano_wolf_stepm"


def sidak_adjusted(p_raw: float, n_trials: int) -> float:
    """The cheap fallback: 1 - (1 - p)^m, capped at 1."""
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    return float(min(1.0, 1.0 - (1.0 - p_raw) ** n_trials))


def romano_wolf_stepm(
    family_series: Mapping[str, Sequence[float]],
    target: str,
    *,
    size: float = 0.05,
    reps: int = 1000,
    block_size: int | None = None,
    seed: int = config.DEFAULT_SEED,
) -> tuple[bool, str]:
    """`arch`'s StepM over a family's daily abnormal-return series.

    Each family member is a "model" whose loss is the negative of its daily
    abnormal return; the benchmark is a zero-abnormal-return strategy. A member
    in `superior_models` beats zero after controlling family-wise error across
    the whole family.
    """
    from arch.bootstrap import StepM

    keys = sorted(family_series)
    if target not in keys:
        raise KeyError(f"{target!r} is not in the family")
    lengths = {len(family_series[k]) for k in keys}
    if len(lengths) != 1:
        return False, "family members have different series lengths; StepM skipped"
    n = lengths.pop()
    if n < 4:
        return False, f"only {n} sessions in the series; StepM skipped"

    losses = np.column_stack([-np.asarray(family_series[k], dtype=float) for k in keys])
    benchmark = np.zeros(n)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        step = StepM(benchmark, losses, size=size, reps=reps,
                     block_size=block_size, seed=int(seed))
        step.compute()
        superior = set(step.superior_models)
    idx = keys.index(target)
    rejected = idx in superior or str(idx) in {str(s) for s in superior}
    return bool(rejected), (
        f"StepM over {len(keys)} family members at size={size}, reps={reps}"
    )


# --------------------------------------------------------------------------- #
# Empirical-Bayes shrinkage (§6.4) — gated on family size
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CohortMoments:
    """What the family estimator needs from one sibling cohort."""

    slug: str
    n: int
    mean: float
    within_variance: float


@dataclass(frozen=True)
class ShrinkageSpec:
    """`k = sigma^2 / tau^2`, estimated by method of moments — not a knob."""

    family_slug: str
    n_cohorts: int
    k: float
    sigma2: float
    tau2: float
    pooled: float
    full_shrinkage: bool
    note: str

    def shrink(self, mean: float, n: int) -> float:
        if self.full_shrinkage or not math.isfinite(self.k):
            return self.pooled
        weight = n / (n + self.k)
        return weight * mean + (1.0 - weight) * self.pooled


def method_of_moments_shrinkage(
    family_slug: str, cohorts: Sequence[CohortMoments]
) -> ShrinkageSpec:
    """Pooled within-cohort variance over between-cohort variance.

    `tau^2` is the observed variance of the cohort means less the average
    sampling variance. When it comes out at or below zero the cohorts differ no
    more than noise predicts, `k` is infinite, and every cohort is reported as
    the family mean — the correct answer, printed as such (§6.4).
    """
    if len(cohorts) < 2:
        raise ValueError("need at least two cohorts to estimate tau^2")
    ns = np.array([c.n for c in cohorts], dtype=float)
    means = np.array([c.mean for c in cohorts], dtype=float)
    within = np.array([c.within_variance for c in cohorts], dtype=float)

    dof = ns - 1.0
    sigma2 = float((dof * within).sum() / dof.sum()) if dof.sum() > 0 else float(within.mean())
    pooled = float((ns * means).sum() / ns.sum())
    observed = float(means.var(ddof=1))
    tau2 = observed - float((sigma2 / ns).mean())

    if tau2 <= 0.0:
        return ShrinkageSpec(
            family_slug=family_slug, n_cohorts=len(cohorts), k=float("inf"),
            sigma2=sigma2, tau2=tau2, pooled=pooled, full_shrinkage=True,
            note=("tau^2 estimated at or below zero: these cohorts are "
                  "indistinguishable, so every cohort is reported as the family mean"),
        )
    return ShrinkageSpec(
        family_slug=family_slug, n_cohorts=len(cohorts), k=sigma2 / tau2,
        sigma2=sigma2, tau2=tau2, pooled=pooled, full_shrinkage=False,
        note="k = sigma^2 / tau^2 by method of moments across the family",
    )


def shrinkage_for_family(
    family_slug: str,
    cohorts: Sequence[CohortMoments],
    min_cohorts: int | None = None,
) -> tuple[ShrinkageSpec | None, str]:
    """The §6.4 gate. Below `min_cohorts` the answer is `None` plus the reason."""
    threshold = (config.SHRINKAGE_MIN_FAMILY_COHORTS
                 if min_cohorts is None else min_cohorts)
    if len(cohorts) < threshold:
        return None, (
            f"shrinkage is gated on a family of at least {threshold} cohorts; "
            f"family {family_slug!r} has {len(cohorts)}. Below that, tau-hat "
            f"squared is noise and the shrunk number would be a fixed prior "
            f"wearing a data-driven costume (Spec N §6.4)."
        )
    return method_of_moments_shrinkage(family_slug, cohorts), ""
