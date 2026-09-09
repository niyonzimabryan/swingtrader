"""Balance diagnostics (Spec N §4.5) — two questions, two different tools.

  * **Is the query event typical of its cohort?** One event has no variance, so
    a two-group SMD does not apply. Per covariate: the query's standardized
    distance from the cohort mean in cohort-SD units, and its cohort percentile.
  * **Is the matched cohort representative of the eligible pool?** Two groups
    exist, so SMD and variance ratio apply.

Neither is hidden and both are labelled, including the covariates whose only
available vintage is "today's value".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from comparables import config


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _var(xs: Sequence[float], ddof: int = 1) -> float:
    n = len(xs)
    if n - ddof <= 0:
        return 0.0
    m = _mean(xs)
    return sum((x - m) ** 2 for x in xs) / (n - ddof)


def _vintage(covariate: str) -> str:
    return "current" if covariate in config.CURRENT_VINTAGE_COVARIATES else "point_in_time"


@dataclass(frozen=True)
class QueryCohortBalance:
    """The query event against its cohort. Never an SMD (§4.5)."""

    covariate: str
    query_value: float
    cohort_mean: float
    cohort_sd: float
    standardized_distance: float
    percentile: float
    label: str            # "typical" | "warn" | "atypical"
    vintage: str
    method: str = "standardized_distance"


def query_vs_cohort(
    covariate: str, query_value: float, cohort_values: Sequence[float]
) -> QueryCohortBalance:
    """Standardized distance in cohort-SD units, plus the cohort percentile."""
    if not cohort_values:
        raise ValueError("empty cohort")
    mean = _mean(cohort_values)
    sd = math.sqrt(_var(cohort_values))
    distance = 0.0 if sd == 0.0 else abs(query_value - mean) / sd
    below = sum(1 for v in cohort_values if v < query_value)
    ties = sum(1 for v in cohort_values if v == query_value)
    percentile = (below + 0.5 * ties) / len(cohort_values) * 100.0

    if distance > config.QUERY_DISTANCE_ATYPICAL:
        label = "atypical"
    elif distance > config.QUERY_DISTANCE_WARN:
        label = "warn"
    else:
        label = "typical"
    return QueryCohortBalance(covariate, float(query_value), mean, sd, distance,
                              percentile, label, _vintage(covariate))


@dataclass(frozen=True)
class PoolBalance:
    """The matched cohort against the eligible pool. SMD and variance ratio."""

    covariate: str
    matched_mean: float
    pool_mean: float
    smd: float
    variance_ratio: float
    label: str            # "balanced" | "warn" | "unbalanced"
    vintage: str
    method: str = "standardized_mean_difference"


def matched_vs_pool(
    covariate: str, matched: Sequence[float], pool: Sequence[float]
) -> PoolBalance:
    """|SMD| > 0.10 warns, > 0.25 labels `unbalanced`; a variance ratio outside
    [0.5, 2] warns (§4.5)."""
    if not matched or not pool:
        raise ValueError("both groups must be non-empty")
    m_mean, p_mean = _mean(matched), _mean(pool)
    m_var, p_var = _var(matched), _var(pool)
    pooled_sd = math.sqrt((m_var + p_var) / 2.0)
    smd = 0.0 if pooled_sd == 0.0 else (m_mean - p_mean) / pooled_sd
    if p_var == 0.0:
        ratio = float("inf") if m_var > 0 else 1.0
    else:
        ratio = m_var / p_var

    lo, hi = config.VARIANCE_RATIO_BOUNDS
    if abs(smd) > config.SMD_UNBALANCED:
        label = "unbalanced"
    elif abs(smd) > config.SMD_WARN or not (lo <= ratio <= hi):
        label = "warn"
    else:
        label = "balanced"
    return PoolBalance(covariate, m_mean, p_mean, smd, ratio, label, _vintage(covariate))


@dataclass(frozen=True)
class BalanceBlock:
    """Both diagnostics, side by side, for every matched covariate."""

    query_vs_cohort: tuple[QueryCohortBalance, ...]
    matched_vs_pool: tuple[PoolBalance, ...]

    @property
    def warnings(self) -> tuple[str, ...]:
        out: list[str] = []
        for q in self.query_vs_cohort:
            if q.label == "atypical":
                out.append(
                    f"query event is atypical on {q.covariate}: "
                    f"{q.standardized_distance:.2f} cohort SDs from the cohort mean"
                )
            elif q.label == "warn":
                out.append(
                    f"query event sits {q.standardized_distance:.2f} cohort SDs from "
                    f"the cohort mean on {q.covariate}"
                )
        for p in self.matched_vs_pool:
            if p.label == "unbalanced":
                out.append(
                    f"matched cohort is unbalanced against the pool on "
                    f"{p.covariate}: SMD {p.smd:+.2f}"
                )
            elif p.label == "warn":
                out.append(
                    f"matched cohort differs from the pool on {p.covariate}: "
                    f"SMD {p.smd:+.2f}, variance ratio {p.variance_ratio:.2f}"
                )
            if p.vintage == "current":
                out.append(
                    f"{p.covariate} carries vintage=current and is never a "
                    f"required stratum (Spec N §4.5)"
                )
        return tuple(out)


def balance_block(
    covariates: Sequence[str],
    query: Mapping[str, float],
    cohort: Sequence[Mapping[str, float]],
    pool: Sequence[Mapping[str, float]],
) -> BalanceBlock:
    """Build both diagnostics for every covariate that every group carries."""
    q_out: list[QueryCohortBalance] = []
    p_out: list[PoolBalance] = []
    for name in covariates:
        cohort_values = [c[name] for c in cohort if name in c]
        pool_values = [p[name] for p in pool if name in p]
        if name in query and cohort_values:
            q_out.append(query_vs_cohort(name, query[name], cohort_values))
        if cohort_values and pool_values:
            p_out.append(matched_vs_pool(name, cohort_values, pool_values))
    return BalanceBlock(tuple(q_out), tuple(p_out))
