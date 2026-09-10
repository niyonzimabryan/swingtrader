"""One place for every configured number the engine reads (Spec N §8).

The floors in particular: Spec N §8 says "One floor configuration ... is the
only place the numbers live; every section that mentions a floor reads it from
there."  `get_floors()` reads the module globals on every call, so patching
either constant moves every floor check at once — which is what
`test_single_floor_config` asserts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time, timezone

# --------------------------------------------------------------------------- #
# The floors (Spec N §8).  Distinct event dates first, matured events second.
# --------------------------------------------------------------------------- #

COHORT_FLOOR_DISTINCT_DATES = 20
COHORT_FLOOR_MATURED = 30


@dataclass(frozen=True)
class FloorConfig:
    """The floor numbers as they stood when an answer was built."""

    distinct_dates: int
    matured: int


def get_floors() -> FloorConfig:
    """Read the floors *now*, so a patched constant moves every check."""
    return FloorConfig(
        distinct_dates=COHORT_FLOOR_DISTINCT_DATES,
        matured=COHORT_FLOOR_MATURED,
    )


# --------------------------------------------------------------------------- #
# Event clock
# --------------------------------------------------------------------------- #

#: A session's open, used to resolve `known_at_utc` to session 0 (Spec N §5.0).
SESSION_OPEN_UTC = time(14, 30, tzinfo=timezone.utc)

#: A session's close. Used only when reading *stored* facts: a covariate may be
#: computed from a session's bar only once that session has closed, so
#: `comparables/cohort.py` picks the last session whose close is at or before
#: the event cutoff. Without it, a fact stamped at 11:00 UTC would inherit the
#: close of a session that had not happened yet.
SESSION_CLOSE_UTC = time(21, 0, tzinfo=timezone.utc)

#: Seasonal-random-walk SUE (Spec N §4.0): how many year-on-year EPS
#: differences are needed before their standard deviation means anything.
#: Eight is two years of quarters beyond the pair being scaled; below that the
#: scale is one or two numbers and the "surprise" is an artefact of them.
SUE_MIN_SEASONAL_DIFFS = 8

#: How stale the fiscal period an earnings release reports may be before the
#: engine concludes it has no EPS for the quarter being announced. Releases come
#: two to eight weeks after the quarter they report (a 10-K's lag reaches ten);
#: a company on 91-day quarters whose latest stored period is more than this far
#: back is missing the announced quarter entirely, and qualifying the event on
#: the *previous* quarter's surprise would be a stale-but-real number, which is
#: the most convincing kind of wrong.
SUE_MAX_ANNOUNCEMENT_LAG_DAYS = 100

#: How far a stored period end may sit from exactly one year earlier and still
#: be treated as the same fiscal quarter a year ago. Fiscal quarters move by a
#: few days between years (52/53-week calendars); 45 days is inside a quarter
#: and outside a normal drift.
SUE_SEASONAL_MATCH_DAYS = 45


# --------------------------------------------------------------------------- #
# Cost model (Spec N §5.3)
# --------------------------------------------------------------------------- #

#: Half-spread in basis points by liquidity decile (1 = thinnest, 10 = deepest).
#: Configured table; replaced by stored quote data where it exists.
HALF_SPREAD_BPS_BY_DECILE = {
    1: 45.0, 2: 32.0, 3: 24.0, 4: 18.0, 5: 14.0,
    6: 11.0, 7: 8.0, 8: 6.0, 9: 4.0, 10: 3.0,
}

#: Named execution policies (Spec N §4.0 `execution_policy`, Spec Q §7).
#: Data, not arguments: a policy the cohort replayed under has to be nameable
#: in the stored query, and a policy slug the engine does not know is refused
#: rather than defaulted to something plausible.
EXECUTION_POLICIES = {
    "event_swing_14cal_v1": {
        "stop_frac": 0.94,
        "target1_frac": 1.04,
        "target2_frac": 1.08,
        "max_holding_days": 14,
        "direction": "long",
    },
}

BASELINE_SLIPPAGE_BPS = 10.0
SENSITIVITY_SLIPPAGE_BPS = (25.0, 50.0)


# --------------------------------------------------------------------------- #
# Delisting terminal returns (Spec N §4.2 — Shumway 1997; Shumway & Warther 1999)
# --------------------------------------------------------------------------- #

DELISTING_TERMINAL_RETURN = {
    "performance_nasdaq": -0.55,
    "performance_nyse_amex": -0.30,
}


# --------------------------------------------------------------------------- #
# Inference (Spec N §6)
# --------------------------------------------------------------------------- #

#: Cluster-robust asymptotics fail below roughly this many clusters (§6.2).
CLUSTERED_SE_MIN_CLUSTERS = 30

#: Below this many sessions the calendar-time beta is not identified; the
#: engine falls back to beta = 1 (market neutral) rather than estimating it.
MIN_CALENDAR_SESSIONS_FOR_BETA = 3

#: Empirical-Bayes shrinkage and SPA/MCS are gated on family size (§6.4, §7).
SHRINKAGE_MIN_FAMILY_COHORTS = 5

BOOTSTRAP_REPS = 10_000
CONFIDENCE_LEVEL = 0.95
DEFAULT_SEED = 20260908

#: The level of the interval on the **policy-simulated net return** (§5.3).
#: Deliberately not `CONFIDENCE_LEVEL`: Spec L §6.6 sizes an evidenced proposal
#: from the *lower 90%* bound of that one quantity, and `portfolio/evidence.py`
#: refuses an interval published at any other level rather than relabelling it.
#: Two levels in one answer is a wart, and the alternative — quietly handing the
#: sizing rule a 95% bound because that is what was to hand — is a model
#: characterising a statistic with extra steps (Spec N §9).
POLICY_CONFIDENCE_LEVEL = 0.90


# --------------------------------------------------------------------------- #
# Balance thresholds (Spec N §4.5)
# --------------------------------------------------------------------------- #

QUERY_DISTANCE_WARN = 1.0
QUERY_DISTANCE_ATYPICAL = 2.0
SMD_WARN = 0.10
SMD_UNBALANCED = 0.25
VARIANCE_RATIO_BOUNDS = (0.5, 2.0)

#: Covariates whose only available vintage is "today's value" (§4.5).
CURRENT_VINTAGE_COVARIATES = ("sector",)
