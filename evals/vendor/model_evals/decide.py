"""decide — turn (metric, CI, n, cost) into an advisory verdict.

v1 is report-driven: the verdict advises; a human reads it and edits config
(design §6). The verdict is deliberately conservative — the default is HOLD, and
a thin corpus is UNDERPOWERED, never PROMOTE (fail-loud, design §8).
"""
from __future__ import annotations

PROMOTE = "PROMOTE"
HOLD = "HOLD"
REJECT = "REJECT"
UNDERPOWERED = "UNDERPOWERED"


def decide(spec, value, ci_low, ci_high, n,
           cost_candidate=None, cost_incumbent=None, free_upgrade=False):
    """Return (verdict, reason).

    PARITY   PROMOTE iff CI-low ≥ threshold AND (candidate strictly cheaper OR a
             free same-price upgrade). REJECT iff CI-high < threshold. Else HOLD.
    DISCOVERY PROMOTE iff CI excludes 0 in the candidate's favour AND point ≥ min
             effect size. REJECT iff CI-high ≤ 0. Else HOLD.
    """
    if n < spec.min_n:
        return (UNDERPOWERED,
                f"n={n} < N_min={spec.min_n}: corpus too thin to promote (design §8). "
                f"{spec.primary_metric}={value:.3f} CI[{ci_low:.3f},{ci_high:.3f}] is directional only.")

    if spec.mode == "parity":
        cheaper = (cost_candidate is not None and cost_incumbent is not None
                   and cost_candidate < cost_incumbent)
        cost_ok = cheaper or free_upgrade
        if ci_high < spec.threshold:
            return (REJECT,
                    f"{spec.primary_metric} CI-high {ci_high:.3f} < floor {spec.threshold}: "
                    f"candidate is measurably worse.")
        if ci_low >= spec.threshold and cost_ok:
            why = "cheaper" if cheaper else "free same-price upgrade"
            return (PROMOTE,
                    f"{spec.primary_metric} CI-low {ci_low:.3f} ≥ floor {spec.threshold} and {why}.")
        if ci_low >= spec.threshold and not cost_ok:
            return (HOLD, f"parity holds ({spec.primary_metric} CI-low {ci_low:.3f} ≥ {spec.threshold}) "
                          f"but no cost win — not worth swapping.")
        return (HOLD, f"{spec.primary_metric}={value:.3f} CI[{ci_low:.3f},{ci_high:.3f}] straddles "
                      f"floor {spec.threshold}: not confidently at parity.")

    # discovery
    if ci_high <= 0:
        return (REJECT, f"candidate not better: Δ CI-high {ci_high:.3f} ≤ 0.")
    if ci_low > 0 and value >= spec.threshold:
        return (PROMOTE, f"candidate better: Δ={value:.3f} CI[{ci_low:.3f},{ci_high:.3f}] excludes 0, "
                         f"effect ≥ {spec.threshold}.")
    return (HOLD, f"Δ={value:.3f} CI[{ci_low:.3f},{ci_high:.3f}]: signal present but not decisive "
                  f"(need CI>0 and effect ≥ {spec.threshold}).")
