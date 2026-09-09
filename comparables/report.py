"""The response contract (Spec N §8) and the assembly that fills it.

Three shapes, a discriminated union on `status`:

  * `RefusedAnswer`      — `status="insufficient"`, **no statistic fields at all**
  * `CohortAnswer`       — `depth="full"`, `status` in {"ok", "inconclusive"}
  * `QuickCohortAnswer`  — `depth="quick"`, the fast loop; it structurally lacks
    the fields a citation requires, which is how `test_quick_answer_not_citable`
    is enforced by the type system rather than by a runtime flag.

Evidence tier and result status are separate axes: tier says how good the data
is, status says whether an answer exists.

Nothing here may be handed a number a model produced. Numeric fields reject
strings at construction, and `cohort_answer_from_text` exists only to raise.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, fields
from datetime import date, datetime
from typing import Any, Literal, Mapping, Sequence

from comparables import balance as balance_mod
from comparables import config
from comparables.config import FloorConfig, get_floors
from comparables.inference import (
    BlockLength,
    ClusteredCARResult,
    ConfidenceInterval,
    CohortMoments,
    EffectiveSampleSize,
    MultiplicityResult,
    NullTestResult,
    NullTestResults,
    ProportionInterval,
    ShrinkageSpec,
    SliceEstimate,
    StabilityResult,
    TrialRegistry,
    decayed_flag,
    effective_sample_size,
    null_from_draws,
    optimal_block_length_for,
    sidak_adjusted,
    shrinkage_for_family,
    stationary_bootstrap_ci,
    two_way_clustered_car,
    wilson_interval,
)
from comparables.outcomes import (
    CostModel,
    EventRecord,
    PolicySpec,
    PriceSeries,
    TradingCalendar,
    calendar_time_alpha,
    calendar_time_series,
    delisting_rate,
    distinct_event_dates,
    horizon_outcomes,
    policy_aggregate,
    provenance_mix,
)
from comparables.setup_spec import SetupSpec

EVIDENCE_TIERS = ("clean_pit", "vendor_pit", "archival_reconstructed")
STATUSES = ("ok", "inconclusive", "insufficient")

#: The fields a `full` answer carries and a `quick` one structurally does not.
CITATION_REQUIRED_FIELDS = (
    "policy", "cost_model", "regime_breakdown", "stability", "shrinkage",
    "null_tests", "n_eff", "delisting_rate", "sources",
)


class ModelNumberError(TypeError):
    """Raised when something tries to build a response out of model prose."""


def cohort_answer_from_text(text: str) -> "CohortAnswer":
    """There is no such path, and there never will be (Spec N §9).

    Every number in a comparable-setups output traces to `comparables/` code and
    a stored query. This function exists so the attempt fails loudly.
    """
    raise ModelNumberError(
        "a cohort answer cannot be constructed from text: a model may not "
        "produce, adjust, round, select or characterize a statistic (Spec N §9)"
    )


_NUMERIC_TYPES = {"float", "int", "float | None", "int | None"}


def _reject_string_numbers(obj: Any) -> None:
    for f in fields(obj):
        if f.type in _NUMERIC_TYPES and isinstance(getattr(obj, f.name), str):
            raise ModelNumberError(
                f"{type(obj).__name__}.{f.name} was given the string "
                f"{getattr(obj, f.name)!r}; statistics come from comparables/, "
                f"never from prose (Spec N §9)"
            )


# --------------------------------------------------------------------------- #
# Leaf records
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SourceRef:
    kind: str
    identifier: str
    known_at_utc: datetime


@dataclass(frozen=True)
class PolicySummary:
    """The §5.3 numbers, as fractions of entry notional. Net is the headline."""

    policy_slug: str
    net: float
    gross: float
    net_by_slippage_bps: tuple[tuple[float, float], ...]
    stopped_out: ProportionInterval

    def __post_init__(self) -> None:
        _reject_string_numbers(self)


@dataclass(frozen=True)
class HorizonResult:
    """Everything measured at one horizon. Percentages are fractions of entry."""

    horizon_sessions: int
    n_matured: int
    n_censored: int
    censored_reasons: tuple[tuple[str, str], ...]
    mean_raw: float
    mean_benchmark: float
    mean_car: float
    mean_car_shrunk: float
    headline: ConfidenceInterval          # calendar-time alpha x h — the headline
    calendar_time_beta: float
    calendar_time_beta_estimated: bool
    calendar_time_sessions: int
    clustered_car: ClusteredCARResult     # the cross-check, never the headline
    block_length: BlockLength
    hit_rate: ProportionInterval
    policy: PolicySummary
    n_eff: EffectiveSampleSize
    p_empirical: float
    multiplicity: MultiplicityResult
    units: str = "fraction_of_entry_over_h_sessions"

    def __post_init__(self) -> None:
        _reject_string_numbers(self)


@dataclass(frozen=True)
class QuickHorizonResult:
    """What `quick` returns: raw, CAR, n, the bootstrap CI. Nothing citable."""

    horizon_sessions: int
    n_matured: int
    n_censored: int
    mean_raw: float
    mean_car: float
    headline: ConfidenceInterval
    block_length: BlockLength
    units: str = "fraction_of_entry_over_h_sessions"

    def __post_init__(self) -> None:
        _reject_string_numbers(self)


@dataclass(frozen=True)
class RefusedCell:
    """A regime x horizon cell below the floor. No statistics, by construction."""

    regime: str
    horizon_sessions: int
    status: Literal["insufficient"]
    refusal_reason: str
    n_matured: int
    n_distinct_dates: int


@dataclass(frozen=True)
class CohortPrediction:
    """The engine's own track record (§6.5).

    The CI is on the cohort *mean* and is never scored as a predictive interval,
    so this record carries the **sign** of the realized return against the sign
    of the point estimate and the realized return's **percentile** within the
    cohort's outcome distribution — and **no coverage field**.
    """

    setup_hash: str
    as_of: date
    horizon_sessions: int
    point_estimate: float
    realized_return: float
    sign_correct: bool
    realized_percentile: float

    def __post_init__(self) -> None:
        _reject_string_numbers(self)


# --------------------------------------------------------------------------- #
# The three response shapes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RefusedAnswer:
    """`insufficient` is a valid, expected, frequently-correct answer (§8).

    It carries the composition facts and the reason, and **no statistic fields**,
    so the "no field is optional" rule and the "no estimate below the floor"
    rule stop fighting.
    """

    setup: SetupSpec
    depth: Literal["quick", "full"]
    status: Literal["insufficient"]
    evidence_tier: str
    refusal_reason: str
    n_matured: int
    n_distinct_dates: int
    delisting_rate: float
    trials_against_this_pattern: int
    floors: FloorConfig

    def __post_init__(self) -> None:
        _reject_string_numbers(self)


@dataclass(frozen=True)
class QuickCohortAnswer:
    """The ten-times-faster loop. Structurally not citable."""

    setup: SetupSpec
    depth: Literal["quick"]
    status: Literal["ok", "inconclusive"]
    evidence_tier: str
    refusal_reason: str | None
    provenance_mix: tuple[tuple[str, int], ...]
    n_matured: int
    n_censored: int
    n_distinct_dates: int
    horizons: tuple[QuickHorizonResult, ...]
    balance: balance_mod.BalanceBlock
    trials_against_this_pattern: int
    cells_examined: int
    floors: FloorConfig
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        _reject_string_numbers(self)


@dataclass(frozen=True)
class CohortAnswer:
    """The full §8 contract."""

    setup: SetupSpec
    depth: Literal["full"]
    status: Literal["ok", "inconclusive"]
    evidence_tier: str
    refusal_reason: str | None
    provenance_mix: tuple[tuple[str, int], ...]
    block_length: BlockLength
    n_eff: float
    delisting_rate: float
    cells_examined: int
    cost_model: CostModel
    n_matured: int
    n_censored: int
    n_distinct_dates: int
    horizons: tuple[HorizonResult, ...]
    policy: tuple[PolicySummary, ...]
    regime_breakdown: tuple[tuple[str, tuple[HorizonResult | RefusedCell, ...]], ...]
    stability: tuple[tuple[int, StabilityResult], ...]
    shrinkage: ShrinkageSpec | None
    shrinkage_reason: str
    balance: balance_mod.BalanceBlock
    null_tests: tuple[tuple[int, NullTestResults], ...]
    trials_against_this_pattern: int
    floors: FloorConfig
    warnings: tuple[str, ...]
    sources: tuple[SourceRef, ...]

    def __post_init__(self) -> None:
        _reject_string_numbers(self)


Answer = CohortAnswer | QuickCohortAnswer | RefusedAnswer


class NotCitableError(ValueError):
    """A `quick` answer cannot back a journal entry, a write-up, or a promotion."""


def assert_citable(answer: Answer) -> CohortAnswer:
    """`full` is mandatory for anything that cites a number (§8)."""
    if isinstance(answer, RefusedAnswer):
        raise NotCitableError(
            f"an insufficient answer carries no statistic to cite: "
            f"{answer.refusal_reason}"
        )
    missing = [f for f in CITATION_REQUIRED_FIELDS if not hasattr(answer, f)]
    if missing:
        raise NotCitableError(
            f"a depth={answer.depth!r} answer lacks {', '.join(missing)}; "
            f"only a full answer can be cited (Spec N §8)"
        )
    return answer  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Canonical serialisation, for the determinism test
# --------------------------------------------------------------------------- #


def _plain(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _plain(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, float):
        return repr(obj)
    return obj


def to_json(answer: Answer) -> str:
    """Canonical JSON. Same inputs and seed -> byte-identical output."""
    return json.dumps(_plain(answer), sort_keys=True, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def evidence_tier_for(events: Sequence[EventRecord]) -> str:
    classes = {e.provenance for e in events}
    if "archival_reconstructed" in classes:
        return "archival_reconstructed"
    if classes == {"observed_live"}:
        return "clean_pit"
    return "vendor_pit"


def _pre_event_cars(
    events: Sequence[EventRecord],
    benchmark: PriceSeries,
    calendar: TradingCalendar,
    sessions: int = 10,
) -> list[float]:
    """CAR over the `sessions` sessions *before* session 0 (§6.3)."""
    from comparables.outcomes import benchmark_daily_returns

    out: list[float] = []
    for event in events:
        zero = calendar.session_zero(event.known_at_utc)
        start = zero - sessions
        if start < 1:
            continue
        days = calendar.window(start, sessions)
        rm = benchmark_daily_returns(benchmark, calendar, start, sessions)
        series = event.series.total_return
        total = 0.0
        try:
            for i, day in enumerate(days):
                pos = event.series.index_of(day)
                ri = series[pos].close / series[pos - 1].close - 1.0
                total += ri - rm[i]
        except (KeyError, IndexError):
            continue
        out.append(total)
    return out


def run_null_tests(
    events: Sequence[EventRecord],
    benchmark: PriceSeries,
    calendar: TradingCalendar,
    horizon: int,
    observed: float,
    *,
    pool: Sequence[EventRecord],
    draws: int,
    seed: int,
) -> NullTestResults:
    import numpy as np

    rng = np.random.default_rng(seed + horizon)
    occupied: set[int] = set()
    for e in events:
        zero = calendar.session_zero(e.known_at_utc)
        occupied.update(range(zero, zero + horizon))
    eligible = [
        i for i in range(1, len(calendar.sessions) - horizon)
        if i not in occupied
    ]
    if not eligible:
        eligible = [i for i in range(1, len(calendar.sessions) - horizon)]

    placebo: list[float] = []
    for _ in range(draws):
        picks = rng.choice(eligible, size=len(events), replace=True)
        vals = []
        for event, zero in zip(events, picks):
            try:
                vals.append(_car_at(event, benchmark, calendar, int(zero), horizon))
            except (KeyError, IndexError, ValueError):
                continue
        if vals:
            placebo.append(float(np.mean(vals)))
    if not placebo:
        placebo = [0.0]

    pool_events = list(pool) if pool else list(events)
    zeros = [calendar.session_zero(e.known_at_utc) for e in events]
    random_cohorts: list[float] = []
    for _ in range(draws):
        idx = rng.integers(0, len(pool_events), size=len(events))
        vals = []
        for j, zero in zip(idx, zeros):
            try:
                vals.append(_car_at(pool_events[int(j)], benchmark, calendar, zero, horizon))
            except (KeyError, IndexError, ValueError):
                continue
        if vals:
            random_cohorts.append(float(np.mean(vals)))
    if not random_cohorts:
        random_cohorts = [0.0]

    pre = _pre_event_cars(events, benchmark, calendar)
    if pre:
        pre_mean = float(np.mean(pre))
        lo, hi = (float(np.percentile(pre, 2.5)), float(np.percentile(pre, 97.5)))
        pre_result = NullTestResult(
            "pre_event_window", pre_mean, lo, hi,
            p_value=float(np.mean(np.abs(np.array(pre)) >= abs(pre_mean))),
            n_draws=len(pre), seed=seed,
            note="a large pre-drift is a leakage warning, not a bonus",
        )
    else:
        pre_result = NullTestResult(
            "pre_event_window", 0.0, 0.0, 0.0, 1.0, 0, seed,
            note="not enough pre-event sessions in the calendar",
        )

    return NullTestResults(
        placebo_dates=null_from_draws(
            "placebo_dates", placebo, observed, seed,
            note="the same names on random non-event dates; a real effect should vanish",
        ),
        random_cohorts=null_from_draws(
            "random_cohorts", random_cohorts, observed, seed,
            note=("same size, same period, random eligible names"
                  if pool else "pool defaulted to the cohort itself"),
        ),
        pre_event_window=pre_result,
    )


def _car_at(
    event: EventRecord,
    benchmark: PriceSeries,
    calendar: TradingCalendar,
    zero_index: int,
    horizon: int,
) -> float:
    """CAR for one name at an arbitrary session index — the placebo machinery."""
    from comparables.outcomes import benchmark_daily_returns

    days = calendar.window(zero_index, horizon)
    series = event.series.total_return
    rm = benchmark_daily_returns(benchmark, calendar, zero_index, horizon)
    total = 0.0
    prev: float | None = None
    for i, day in enumerate(days):
        pos = event.series.index_of(day)
        bar = series[pos]
        ri = bar.close / bar.open - 1.0 if i == 0 else bar.close / prev - 1.0
        prev = bar.close
        total += ri - rm[i]
    return total


def _stability(
    events: Sequence[EventRecord],
    benchmark: PriceSeries,
    calendar: TradingCalendar,
    horizon: int,
    *,
    reps: int,
    seed: int,
) -> StabilityResult:
    ordered = sorted(events, key=lambda e: (e.known_at_utc, e.event_id))
    half = len(ordered) // 2
    early_events, late_events = ordered[:half], ordered[half:]

    def slice_estimate(label: str, subset: Sequence[EventRecord]) -> SliceEstimate:
        if not subset:
            return SliceEstimate(label, 0, 0.0, 0.0, 0.0)
        series = calendar_time_series(subset, benchmark, calendar, horizon)
        result = calendar_time_alpha(series, horizon)
        block = optimal_block_length_for(series.abnormal, horizon)
        ci = stationary_bootstrap_ci(series.abnormal, block.used, scale=horizon,
                                     reps=reps, seed=seed)
        return SliceEstimate(label, len(subset), result.alpha_times_h,
                             ci.lower, ci.upper)

    early = slice_estimate("early_half", early_events)
    late = slice_estimate("late_half", late_events)
    per_year: list[SliceEstimate] = []
    by_year: dict[int, list[EventRecord]] = {}
    for e in ordered:
        by_year.setdefault(e.known_at_utc.year, []).append(e)
    for year in sorted(by_year):
        per_year.append(slice_estimate(str(year), by_year[year]))

    decayed, reason = decayed_flag(early, late)
    return StabilityResult(early, late, tuple(per_year), decayed, reason,
                           method="calendar_time_alpha_times_h")


def _horizon_result(
    events: Sequence[EventRecord],
    benchmark: PriceSeries,
    calendar: TradingCalendar,
    horizon: int,
    *,
    setup: SetupSpec,
    policy: PolicySpec,
    costs: CostModel,
    shrinkage: ShrinkageSpec | None,
    trials: int,
    pool: Sequence[EventRecord],
    reps: int,
    null_draws: int,
    seed: int,
    family_series: Mapping[str, Sequence[float]] | None,
) -> tuple[HorizonResult, NullTestResults]:
    outcomes = horizon_outcomes(events, benchmark, calendar, horizon)
    kept = set(outcomes.event_ids)
    matured = [e for e in events if e.event_id in kept]

    series = calendar_time_series(matured, benchmark, calendar, horizon)
    ct = calendar_time_alpha(series, horizon)
    block = optimal_block_length_for(series.abnormal, horizon)
    headline = stationary_bootstrap_ci(series.abnormal, block.used, scale=horizon,
                                       reps=reps, seed=seed)
    headline = dataclasses.replace(headline, estimate=ct.alpha_times_h)

    clustered = two_way_clustered_car(
        outcomes.car, outcomes.event_dates,
        [e.ticker for e in matured],
    )
    hits = sum(1 for c in outcomes.car if c > 0)
    hit_rate = wilson_interval(hits, max(len(outcomes.car), 1))

    agg = policy_aggregate(matured, calendar, policy, costs)
    policy_summary = PolicySummary(
        policy_slug=agg.policy_slug,
        net=agg.mean_net_pct / 100.0,
        gross=agg.mean_gross_pct / 100.0,
        net_by_slippage_bps=tuple((bps, v / 100.0) for bps, v in agg.mean_net_by_slippage_bps),
        stopped_out=wilson_interval(agg.stopped_out_count, agg.n),
    )

    neff = effective_sample_size(outcomes.car, outcomes.event_dates)
    nulls = run_null_tests(matured, benchmark, calendar, horizon, ct.alpha_times_h,
                        pool=pool, draws=null_draws, seed=seed)
    p_raw = nulls.random_cohorts.p_value

    stepm_rejected, stepm_note = False, "family has one member; StepM not run"
    if family_series and len(family_series) > 1 and setup.slug in family_series:
        from comparables.inference import romano_wolf_stepm
        stepm_rejected, stepm_note = romano_wolf_stepm(
            family_series, setup.slug, block_size=block.used, seed=seed)
    multiplicity = MultiplicityResult(
        family_slug=setup.family_slug,
        n_trials=trials,
        p_raw=p_raw,
        p_sidak=sidak_adjusted(p_raw, max(trials, 1)),
        stepm_rejected=stepm_rejected,
        stepm_note=stepm_note,
    )

    shrunk = (shrinkage.shrink(outcomes.mean_car, outcomes.n_matured)
              if shrinkage is not None else outcomes.mean_car)

    result = HorizonResult(
        horizon_sessions=horizon,
        n_matured=outcomes.n_matured,
        n_censored=outcomes.n_censored,
        censored_reasons=outcomes.censored_reasons,
        mean_raw=outcomes.mean_raw,
        mean_benchmark=outcomes.mean_benchmark,
        mean_car=outcomes.mean_car,
        mean_car_shrunk=shrunk,
        headline=headline,
        calendar_time_beta=ct.beta,
        calendar_time_beta_estimated=ct.beta_estimated,
        calendar_time_sessions=ct.n_sessions,
        clustered_car=clustered,
        block_length=block,
        hit_rate=hit_rate,
        policy=policy_summary,
        n_eff=neff,
        p_empirical=p_raw,
        multiplicity=multiplicity,
    )
    return result, nulls


def _quick_horizon_result(
    events: Sequence[EventRecord],
    benchmark: PriceSeries,
    calendar: TradingCalendar,
    horizon: int,
    *,
    reps: int,
    seed: int,
) -> QuickHorizonResult:
    outcomes = horizon_outcomes(events, benchmark, calendar, horizon)
    matured = [e for e in events if e.event_id in set(outcomes.event_ids)]
    series = calendar_time_series(matured, benchmark, calendar, horizon)
    ct = calendar_time_alpha(series, horizon)
    block = optimal_block_length_for(series.abnormal, horizon)
    ci = stationary_bootstrap_ci(series.abnormal, block.used, scale=horizon,
                                 reps=reps, seed=seed)
    return QuickHorizonResult(
        horizon_sessions=horizon,
        n_matured=outcomes.n_matured,
        n_censored=outcomes.n_censored,
        mean_raw=outcomes.mean_raw,
        mean_car=outcomes.mean_car,
        headline=dataclasses.replace(ci, estimate=ct.alpha_times_h),
        block_length=block,
    )


def cells_examined(
    n_horizons: int, n_regime_cells: int, n_stability_cells: int
) -> int:
    """Regime x horizon x conditioning split, all of it counted (§7)."""
    return n_horizons * (1 + n_regime_cells + n_stability_cells)


def floor_check(
    events: Sequence[EventRecord],
    calendar: TradingCalendar,
    horizons: Sequence[int],
    *,
    floors: FloorConfig,
    universe_delisting_rate: float | None = None,
) -> str | None:
    """The refusal rules of §8, in order. Returns a reason, or None to proceed."""
    dates = distinct_event_dates(events, calendar)
    n_dates = len(dates)
    if n_dates < floors.distinct_dates:
        return (
            f"{n_dates} distinct event dates against a floor of "
            f"{floors.distinct_dates}: clustered events are not independent "
            f"observations and the event count is the wrong denominator "
            f"(Spec N §8)"
        )
    worst = max(horizons)
    matured = horizon_outcomes_count(events, calendar, worst)
    if matured < floors.matured:
        return (
            f"{matured} matured events at the {worst}-session horizon against a "
            f"floor of {floors.matured} (Spec N §8)"
        )
    classes = {e.provenance for e in events}
    if "archival_reconstructed" in classes and len(classes) > 1:
        return (
            "the cohort mixes archival_reconstructed facts with point-in-time "
            "ones; archival results are never combined with the other classes "
            "in the same statistic (Spec N §8)"
        )
    rate = delisting_rate(events)
    if universe_delisting_rate is not None and rate == 0.0 and universe_delisting_rate > 0.0:
        return (
            f"the cohort meets the floor on survivors alone: its delisting rate "
            f"is 0.0 against {universe_delisting_rate:.3f} in the universe over "
            f"the same period, which is a composition failure, not a sample-size "
            f"one (Spec N §4.2)"
        )
    return None


def horizon_outcomes_count(
    events: Sequence[EventRecord], calendar: TradingCalendar, horizon: int
) -> int:
    from comparables.outcomes import maturity

    return sum(1 for e in events if maturity(e, calendar, horizon).matured)


def build_answer(
    setup: SetupSpec,
    events: Sequence[EventRecord],
    benchmark: PriceSeries,
    calendar: TradingCalendar,
    *,
    depth: Literal["quick", "full"] = "full",
    policy: PolicySpec,
    costs: CostModel | None = None,
    registry: TrialRegistry | None = None,
    query_covariates: Mapping[str, float] | None = None,
    pool: Sequence[EventRecord] = (),
    family_cohorts: Sequence[CohortMoments] = (),
    family_series: Mapping[str, Sequence[float]] | None = None,
    include_regimes: bool = True,
    universe_delisting_rate: float | None = None,
    sources: Sequence[SourceRef] = (),
    reps: int = config.BOOTSTRAP_REPS,
    null_draws: int = 200,
    seed: int = config.DEFAULT_SEED,
    floors: FloorConfig | None = None,
) -> Answer:
    """Build the §8 response for a cohort that is already constructed.

    Cohort construction from stored facts is a later phase; this takes the
    events it is given and measures them.
    """
    floors = floors or get_floors()
    costs = costs or CostModel.default()
    registry = registry if registry is not None else TrialRegistry()
    trials = registry.record(setup)

    horizons = setup.horizons_sessions
    tier = evidence_tier_for(events)
    dates = distinct_event_dates(events, calendar)
    worst = max(horizons)
    n_matured = horizon_outcomes_count(events, calendar, worst)

    reason = floor_check(events, calendar, horizons, floors=floors,
                         universe_delisting_rate=universe_delisting_rate)
    if reason is not None:
        return RefusedAnswer(
            setup=setup,
            depth=depth,
            status="insufficient",
            evidence_tier=tier,
            refusal_reason=reason,
            n_matured=n_matured,
            n_distinct_dates=len(dates),
            delisting_rate=delisting_rate(events),
            trials_against_this_pattern=trials,
            floors=floors,
        )

    mix = tuple(sorted(provenance_mix(events).items()))
    warnings_out: list[str] = []
    regimes = sorted({e.regime for e in events})
    if len(regimes) == 1:
        warnings_out.append(
            f"single_regime: every event in this cohort sits in the {regimes[0]!r} "
            f"regime, so the answer describes one market mood (Spec N §5.4)"
        )

    cov_names = list(setup.match_covariates)
    block = balance_mod.balance_block(
        cov_names,
        dict(query_covariates or {}),
        [e.covariate_map for e in events],
        [e.covariate_map for e in (pool or events)],
    )
    warnings_out.extend(block.warnings)

    if depth == "quick":
        quick = tuple(
            _quick_horizon_result(events, benchmark, calendar, h, reps=reps, seed=seed)
            for h in horizons
        )
        return QuickCohortAnswer(
            setup=setup,
            depth="quick",
            status="ok",
            evidence_tier=tier,
            refusal_reason=None,
            provenance_mix=mix,
            n_matured=n_matured,
            n_censored=quick[-1].n_censored if quick else 0,
            n_distinct_dates=len(dates),
            horizons=quick,
            balance=block,
            trials_against_this_pattern=trials,
            cells_examined=cells_examined(len(horizons), 0, 0),
            floors=floors,
            warnings=tuple(warnings_out),
        )

    shrinkage, shrinkage_reason = shrinkage_for_family(setup.family_slug, family_cohorts)

    measured = [
        _horizon_result(
            events, benchmark, calendar, h,
            setup=setup, policy=policy, costs=costs, shrinkage=shrinkage,
            trials=trials, pool=pool, reps=reps, null_draws=null_draws,
            seed=seed, family_series=family_series,
        )
        for h in horizons
    ]
    results = tuple(r for r, _ in measured)
    nulls = tuple((r.horizon_sessions, n) for r, n in measured)

    regime_cells: list[tuple[str, tuple[HorizonResult | RefusedCell, ...]]] = []
    n_regime_cells = 0
    if include_regimes:
        for regime in regimes:
            subset = [e for e in events if e.regime == regime]
            cells: list[HorizonResult | RefusedCell] = []
            for h in horizons:
                n_regime_cells += 1
                sub_dates = distinct_event_dates(subset, calendar)
                sub_matured = horizon_outcomes_count(subset, calendar, h)
                cell_reason = floor_check(subset, calendar, [h], floors=floors)
                if cell_reason is not None:
                    cells.append(RefusedCell(regime, h, "insufficient", cell_reason,
                                             sub_matured, len(sub_dates)))
                    continue
                cell, _ = _horizon_result(
                    subset, benchmark, calendar, h,
                    setup=setup, policy=policy, costs=costs, shrinkage=shrinkage,
                    trials=trials, pool=pool, reps=reps, null_draws=null_draws,
                    seed=seed, family_series=family_series,
                )
                cells.append(cell)
            regime_cells.append((regime, tuple(cells)))

    stability = tuple(
        (h, _stability(events, benchmark, calendar, h, reps=reps, seed=seed))
        for h in horizons
    )
    n_stability_cells = 2 + len({e.known_at_utc.year for e in events})

    status: Literal["ok", "inconclusive"] = "ok"
    refusal: str | None = None
    for r in results:
        head, cross = r.headline.estimate, r.clustered_car.estimate
        if head != 0.0 and cross != 0.0 and (head > 0) != (cross > 0):
            status = "inconclusive"
            refusal = (
                f"at the {r.horizon_sessions}-session horizon the calendar-time "
                f"estimate ({head:+.6f}) and the clustered CAR cross-check "
                f"({cross:+.6f}) disagree in sign; both are shown and neither is "
                f"the answer (Spec N §6.2)"
            )
            warnings_out.append(refusal)
            break

    if shrinkage is None:
        warnings_out.append(shrinkage_reason)
    for r in results:
        if not r.clustered_car.reliable:
            warnings_out.append(
                f"h={r.horizon_sessions}: {r.clustered_car.reliability_reason}"
            )
        if not r.calendar_time_beta_estimated:
            warnings_out.append(
                f"h={r.horizon_sessions}: the calendar-time series has "
                f"{r.calendar_time_sessions} sessions, so beta was not estimated "
                f"and was set to 1 (market neutral)"
            )

    return CohortAnswer(
        setup=setup,
        depth="full",
        status=status,
        evidence_tier=tier,
        refusal_reason=refusal,
        provenance_mix=mix,
        block_length=results[0].block_length,
        n_eff=results[0].n_eff.n_eff,
        delisting_rate=delisting_rate(events),
        cells_examined=cells_examined(len(horizons), n_regime_cells, n_stability_cells),
        cost_model=costs,
        n_matured=n_matured,
        n_censored=results[-1].n_censored,
        n_distinct_dates=len(dates),
        horizons=results,
        policy=tuple(r.policy for r in results),
        regime_breakdown=tuple(regime_cells),
        stability=stability,
        shrinkage=shrinkage,
        shrinkage_reason=shrinkage_reason,
        balance=block,
        null_tests=nulls,
        trials_against_this_pattern=trials,
        floors=floors,
        warnings=tuple(warnings_out),
        sources=tuple(sources),
    )
