"""Pure-python bootstrap CI — no numpy, so the lib stays zero-dependency.

A verdict without a confidence interval is invalid (design §8): every eval reports
a bootstrap CI over the per-item scores. The bootstrap is seeded so runs are
reproducible (same corpus + seed → same CI), which matters for the self-dating
report and for tests.
"""
from __future__ import annotations

import random
from statistics import mean


def bootstrap_ci(values, alpha: float = 0.05, iters: int = 2000, seed: int = 0):
    """Percentile bootstrap CI for the mean of `values`.

    Returns (point_estimate, ci_low, ci_high). For n < 2 the CI collapses to the
    point estimate (and the caller's N_min floor should have rejected it anyway).
    """
    vals = [float(v) for v in values]
    n = len(vals)
    if n == 0:
        return (0.0, 0.0, 0.0)
    point = mean(vals)
    if n == 1:
        return (point, point, point)
    rng = random.Random(seed)
    means = []
    for _ in range(iters):
        sample = [vals[rng.randrange(n)] for _ in range(n)]
        means.append(mean(sample))
    means.sort()
    lo = means[int((alpha / 2) * iters)]
    hi = means[min(iters - 1, int((1 - alpha / 2) * iters))]
    return (point, lo, hi)
