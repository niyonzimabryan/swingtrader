"""Real Spec N answers for tests that need to *cite* one.

The citation rules in Spec N §8 and Spec L §6.6 are about the answer's shape —
``depth`` and ``status`` — so a test of them is only worth anything against
answers the engine actually produced. These are built once per process from
``comparables.fixtures`` synthetic cohorts (about two seconds in total) and
cached, rather than hand-rolled stubs that would pass a duck-typed check while
proving nothing.

The name is deliberately not ``test_*``: ``unittest discover -p "test_*.py"``
would otherwise import it as a test module.
"""

from __future__ import annotations

import dataclasses

_CACHE: dict = {}

#: Bootstrap settings from `tests/test_comparables_report.py`: enough
#: replications for a real answer, few enough to keep the suite fast.
FAST = dict(reps=200, null_draws=25)


def _spec():
    from comparables.setup_spec import Condition, SetupSpec

    return SetupSpec(
        slug="syn_v1",
        version="1.0.0",
        conditions=(Condition("sue_seasonal", ">", 1.0),),
        universe="liquid_us_equity_v1",
        horizons_sessions=(5,),
        execution_policy="event_swing_14cal_v1",
        match_covariates=("market_cap_decile", "liquidity_decile"),
        lookback_years=5,
    )


def _clearing_cohort():
    from comparables import fixtures as fx

    if "cohort" not in _CACHE:
        _CACHE["cohort"] = fx.synthetic_cohort(
            n_dates=20, per_date=2, sessions=400, seed=3, effect_daily=0.0008
        )
    return _CACHE["cohort"]


def full_answer(*, status: str = "ok"):
    """A ``depth="full"`` answer. ``status="inconclusive"`` for the other case."""
    from comparables import fixtures as fx
    from comparables.report import build_answer

    if "full" not in _CACHE:
        calendar, benchmark, events = _clearing_cohort()
        _CACHE["full"] = build_answer(
            _spec(), events, benchmark, calendar, policy=fx.POLICY, **FAST
        )
    answer = _CACHE["full"]
    assert answer.depth == "full" and answer.status == "ok", answer.status
    return answer if status == "ok" else dataclasses.replace(answer, status=status)


def quick_answer():
    """The ten-times-faster loop. Structurally not citable."""
    from comparables import fixtures as fx
    from comparables.report import build_answer

    if "quick" not in _CACHE:
        calendar, benchmark, events = _clearing_cohort()
        _CACHE["quick"] = build_answer(
            _spec(), events, benchmark, calendar, depth="quick",
            policy=fx.POLICY, **FAST,
        )
    return _CACHE["quick"]


def refused_answer():
    """``status="insufficient"`` — a valid, frequently-correct answer with no
    statistic to cite."""
    from comparables import fixtures as fx
    from comparables.report import build_answer

    if "refused" not in _CACHE:
        _CACHE["refused"] = build_answer(
            fx.SETUP, fx.cohort(), fx.benchmark_series(), fx.CALENDAR,
            policy=fx.POLICY, **FAST,
        )
    return _CACHE["refused"]
