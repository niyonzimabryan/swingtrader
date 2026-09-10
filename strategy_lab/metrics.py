"""Honest measurement of an arm (Spec Q §10, §16).

Every number a scorecard prints is computed here or by ``comparables/`` — never
by a model (``AGENTS.md`` non-negotiable 2). This module is pure: stdlib only,
no session, no bootstrap library, no network. It states what is measured and
what the refusal rules are; the statistics that need resampling arrive through
two injected backends.

**Why the backends are injected.** Spec Q §5 keeps ``strategy_lab`` out of
``comparables`` (``tests/test_strategy_lab_import_graph.py``), and Spec N's
inference layer is the repository's one implementation of the stationary block
bootstrap, the effective sample size and the Romano–Wolf step-M family-wise
control. Reimplementing any of them here would be a second answer to the same
question. So :class:`UncertaintyBackend` and :class:`MultiplicityBackend` are
protocols, ``scripts/strategy_lab_scoreboard.py`` implements them over
``comparables.inference``, and a run without them prints
``uncertainty_unavailable`` and refuses to name a winner rather than quietly
falling back to a normal approximation.

**The refusals, in the order they fire.**

1. **Mixed evidence.** Clean replay and ``archival_reconstructed`` results are
   never combined in one statistic (Spec Q §10). A mixed observation set raises;
   the caller reports two sections.
2. **Missing costs.** A matured trade with no cost model produces a *blocked*
   result, not a zero-cost one (Spec Q §16: "costs missing -> result blocked").
   No return metric is computed at all in that state.
3. **Immature observations.** A position whose bars ran out before its time
   boundary is open, not flat: it is counted, labelled and excluded from every
   statistic. Spec Q §9: a long-horizon strategy "must not be ranked against a
   five-day strategy using incomplete open positions".
4. **Below the floors.** The result is computed and displayed *with its sample
   size*, and its status is ``insufficient_evidence``. Nothing below a floor can
   be ranked, and no ranking ever names a winner from raw return alone.

**The floors are configurable and printed beside every result**, as Spec Q §10
requires. The defaults are the spec's own operational minimums, and they gate a
*recommendation*: promotion is owner-only and no number here performs one.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Mapping, Protocol, Sequence

from strategy_lab.domain import StrategyLabError
from strategy_lab.replay import (
    EVIDENCE_CLEAN,
    EVIDENCE_EXPLORATORY,
    CostAssumptions,
    ReplayOutcome,
)

__all__ = [
    "MetricsError",
    "MixedEvidence",
    "HoldoutViolation",
    "STATUS_OK",
    "STATUS_INSUFFICIENT",
    "STATUS_BLOCKED",
    "EvidenceFloors",
    "RankingGate",
    "Interval",
    "UncertaintyBackend",
    "MultiplicityBackend",
    "TradeObservation",
    "Benchmark",
    "ArmMetrics",
    "Overlap",
    "Scoreboard",
    "observation_from",
    "benchmark_from_returns",
    "evaluate_arm",
    "overlap",
    "pairwise_overlaps",
    "rank_arms",
    "chronological_folds",
    "reserve_holdout",
]


class MetricsError(StrategyLabError):
    """A measurement was asked for something it cannot honestly produce."""


class MixedEvidence(MetricsError):
    """Clean and reconstructed evidence were handed to one statistic."""


class HoldoutViolation(MetricsError):
    """The reserved holdout was read during development."""


STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient_evidence"
STATUS_BLOCKED = "blocked"

# --- warning codes ---------------------------------------------------------- #
WARN_COSTS_MISSING = "costs_missing"
WARN_OPEN_OBSERVATIONS = "open_observations_excluded"
WARN_BELOW_MATURED_FLOOR = "below_matured_floor"
WARN_BELOW_DATE_FLOOR = "below_distinct_date_floor"
WARN_BELOW_CLOSED_FLOOR = "below_closed_floor"
WARN_UNCERTAINTY_UNAVAILABLE = "uncertainty_unavailable"
WARN_MULTIPLICITY_UNAVAILABLE = "multiple_testing_diagnostic_unavailable"
WARN_EXPLORATORY = "archival_reconstructed_exploratory_only"
WARN_NO_BENCHMARK = "benchmark_absent"
WARN_PROFIT_FACTOR_UNDEFINED = "profit_factor_undefined_no_losing_trades"
WARN_NO_LOSSES = "no_losing_trades_in_sample"
WARN_UNDECLARED_VARIANTS = "variants_run_beyond_preregistration"
WARN_SINGLE_DATE = "all_observations_share_one_event_date"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EvidenceFloors:
    """Spec Q §10's operational minimums. Configurable, and always displayed.

    "Operational minimums may gate tier movement, but they are not proof of
    profitability." Nothing here says a strategy works; these only say when a
    number is allowed to be compared with another number.
    """

    matured: int = 100
    distinct_dates: int = 20
    closed: int = 30
    shadow_calendar_days: int = 60

    def canonical(self) -> dict:
        return {
            "matured": self.matured,
            "distinct_dates": self.distinct_dates,
            "closed": self.closed,
            "shadow_calendar_days": self.shadow_calendar_days,
            "citation": "Spec Q §10 'Evidence controls' operational minimums",
        }


@dataclass(frozen=True)
class RankingGate:
    """What a leader must clear before the word "winner" is used at all.

    These are the project's stated recommendation rules, not a citation: Spec Q
    §10 says only "never select a winner from raw return alone" and "print
    ``insufficient_evidence`` instead of manufacturing a ranking". The specific
    conservatism below is ours, is displayed beside every scoreboard, and gates
    a *recommendation* — promotion remains owner-only (Spec Q §3).

    ``separation`` is the strict one: the leader's multiplicity-adjusted lower
    bound must exceed the runner-up's point estimate. Two arms whose intervals
    overlap have not been separated by the data, however different their means.
    """

    confidence_level: float = 0.90
    require_positive_adjusted_lower_bound: bool = True
    require_separation_from_runner_up: bool = True
    require_stepm_rejection: bool = True
    require_beating_benchmark: bool = True

    def canonical(self) -> dict:
        return {
            "confidence_level": self.confidence_level,
            "require_positive_adjusted_lower_bound":
                self.require_positive_adjusted_lower_bound,
            "require_separation_from_runner_up": self.require_separation_from_runner_up,
            "require_stepm_rejection": self.require_stepm_rejection,
            "require_beating_benchmark": self.require_beating_benchmark,
            "note": (
                "project recommendation gate; promotion is owner-only "
                "(Spec Q §3, §8)"
            ),
        }


# --------------------------------------------------------------------------- #
# Injected inference
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Interval:
    """A point estimate and its interval. Never one without the other."""

    estimate: float
    lower: float
    upper: float
    level: float
    method: str
    block_length: int
    reps: int
    seed: int
    n_eff: float

    def canonical(self) -> dict:
        return {
            "estimate": self.estimate,
            "lower": self.lower,
            "upper": self.upper,
            "level": self.level,
            "method": self.method,
            "block_length": self.block_length,
            "reps": self.reps,
            "seed": self.seed,
            "n_eff": self.n_eff,
        }


class UncertaintyBackend(Protocol):
    """The block bootstrap and the effective sample size, from Spec N §6."""

    def interval(
        self,
        series: Sequence[float],
        event_dates: Sequence[date],
        *,
        horizon: int,
        level: float,
    ) -> Interval:
        ...


class MultiplicityBackend(Protocol):
    """Family-wise control across every variant tried (Spec N §7, Spec Q §10)."""

    def family_level(self, level: float, n_trials: int) -> float:
        ...

    def stepm(
        self, family_series: Mapping[str, Sequence[float]], target: str
    ) -> tuple[bool, str]:
        ...


# --------------------------------------------------------------------------- #
# Observations
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TradeObservation:
    """One replayed or shadowed trade, as the measurement layer sees it.

    ``net_pct`` and ``costs`` are ``None`` when no cost model was applied. That
    is not the same as zero and is never treated as zero: it blocks the result.
    """

    arm: str
    ticker: str
    entry_date: date
    exit_date: date
    holding_days: int
    gross_pct: float
    matured: bool
    evidence_class: str
    rule_fired: str = ""
    net_pct: float | None = None
    r_multiple: float | None = None
    costs: float | None = None
    notional: float = 0.0

    def __post_init__(self) -> None:
        if self.evidence_class not in (EVIDENCE_CLEAN, EVIDENCE_EXPLORATORY):
            raise MetricsError(
                f"unknown evidence class {self.evidence_class!r}; the two Spec Q "
                f"§10 classes are {EVIDENCE_CLEAN!r} and {EVIDENCE_EXPLORATORY!r}"
            )
        if self.exit_date < self.entry_date:
            raise MetricsError(
                f"{self.ticker}: exit {self.exit_date} precedes entry {self.entry_date}"
            )

    @property
    def key(self) -> tuple[str, date]:
        """What makes two arms' trades "the same opportunity"."""
        return (self.ticker, self.entry_date)

    @property
    def has_costs(self) -> bool:
        return self.net_pct is not None and self.costs is not None

    def canonical(self) -> dict:
        return {
            "arm": self.arm,
            "ticker": self.ticker,
            "entry_date": self.entry_date.isoformat(),
            "exit_date": self.exit_date.isoformat(),
            "holding_days": self.holding_days,
            "gross_pct": self.gross_pct,
            "net_pct": self.net_pct,
            "r_multiple": self.r_multiple,
            "costs": self.costs,
            "notional": self.notional,
            "rule_fired": self.rule_fired,
            "matured": self.matured,
            "evidence_class": self.evidence_class,
        }


def observation_from(
    outcome: ReplayOutcome, arm: str, *, notional: float = 0.0
) -> TradeObservation:
    """Lift a :class:`~strategy_lab.replay.ReplayOutcome` into an observation.

    ``net_pct`` stays ``None`` when the replay ran without a cost model, which
    is what carries "costs missing" all the way through to a blocked result
    instead of losing it at the boundary.
    """
    priced = outcome.costs is not None
    return TradeObservation(
        arm=arm,
        ticker=outcome.ticker,
        entry_date=outcome.net.entry_date,
        exit_date=outcome.net.exit_date,
        holding_days=outcome.holding_days,
        gross_pct=outcome.gross_pct,
        matured=outcome.matured,
        evidence_class=outcome.evidence_class,
        rule_fired=outcome.net.rule_fired,
        net_pct=outcome.net_pct if priced else None,
        r_multiple=outcome.r_multiple if priced else None,
        costs=(
            notional * (outcome.gross_pct - outcome.net_pct) / 100.0 if priced else None
        ),
        notional=notional,
    )


@dataclass(frozen=True)
class Benchmark:
    """A broad-market comparison over the same window (Spec Q §10).

    Reported *with* exposure, always: an arm that is in the market a tenth of
    the time and returns half the index has not underperformed it in any sense
    a reader should be allowed to infer from one number.
    """

    label: str
    start_date: date
    end_date: date
    return_pct: float

    def canonical(self) -> dict:
        return {
            "label": self.label,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "return_pct": self.return_pct,
        }


def benchmark_from_returns(
    label: str, closes: Sequence[tuple[date, float]]
) -> Benchmark:
    """Buy-and-hold over a total-return series, as a percentage."""
    if len(closes) < 2:
        raise MetricsError(
            f"{label}: a benchmark needs at least two closes to have a return"
        )
    series = sorted(closes)
    first, last = series[0], series[-1]
    if first[1] <= 0:
        raise MetricsError(f"{label}: the first close must be positive")
    return Benchmark(
        label=label,
        start_date=first[0],
        end_date=last[0],
        return_pct=(last[1] / first[1] - 1.0) * 100.0,
    )


# --------------------------------------------------------------------------- #
# Chronological splits, purging, embargo, holdout
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Fold:
    """One walk-forward split. Never a random shuffle (Spec Q §10)."""

    index: int
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    train: tuple[TradeObservation, ...]
    validation: tuple[TradeObservation, ...]
    purged: tuple[TradeObservation, ...]
    embargo_days: int

    def canonical(self) -> dict:
        return {
            "index": self.index,
            "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(),
            "validation_start": self.validation_start.isoformat(),
            "validation_end": self.validation_end.isoformat(),
            "n_train": len(self.train),
            "n_validation": len(self.validation),
            "n_purged": len(self.purged),
            "embargo_days": self.embargo_days,
        }


def chronological_folds(
    observations: Sequence[TradeObservation],
    *,
    n_folds: int = 3,
    embargo_days: int = 0,
) -> tuple[Fold, ...]:
    """Walk-forward splits with overlapping labels purged and an embargo.

    The validation blocks are contiguous, chronological and non-overlapping, cut
    by entry date. Training is everything *before* the validation block, minus
    two things:

    * any observation whose label window ``[entry, exit]`` reaches into the
      validation block or its embargo — an overlapping label leaks the
      validation period's returns into training (Spec Q §10: "Prevent
      overlapping label windows from leaking between train and validation
      sets");
    * anything inside the embargo itself.

    Random train/test shuffling is not offered, because it is the mistake this
    function exists to make impossible.
    """
    if n_folds < 2:
        raise MetricsError("a walk-forward split needs at least two folds")
    if embargo_days < 0:
        raise MetricsError("embargo_days must be >= 0")
    ordered = sorted(observations, key=lambda o: (o.entry_date, o.ticker, o.arm))
    if len(ordered) < n_folds:
        raise MetricsError(
            f"{len(ordered)} observations cannot make {n_folds} chronological "
            "folds; report insufficient_evidence rather than splitting anyway"
        )

    size = len(ordered) // n_folds
    folds: list[Fold] = []
    embargo = timedelta(days=embargo_days)
    for index in range(1, n_folds):
        block = ordered[index * size: (index + 1) * size] if index < n_folds - 1 \
            else ordered[index * size:]
        if not block:
            continue
        val_start = block[0].entry_date
        val_end = max(o.exit_date for o in block)
        train, purged = [], []
        for obs in ordered[: index * size]:
            if obs.exit_date >= val_start - embargo:
                purged.append(obs)
            else:
                train.append(obs)
        folds.append(Fold(
            index=index,
            train_start=ordered[0].entry_date,
            train_end=(train[-1].exit_date if train else ordered[0].entry_date),
            validation_start=val_start,
            validation_end=val_end,
            train=tuple(train),
            validation=tuple(block),
            purged=tuple(purged),
            embargo_days=embargo_days,
        ))
    return tuple(folds)


@dataclass(frozen=True)
class Holdout:
    """A final untouched slice, reserved before any development measurement."""

    start_date: date
    development: tuple[TradeObservation, ...]
    reserved: tuple[TradeObservation, ...]

    def canonical(self) -> dict:
        return {
            "start_date": self.start_date.isoformat(),
            "n_development": len(self.development),
            "n_reserved": len(self.reserved),
        }


def reserve_holdout(
    observations: Sequence[TradeObservation], *, fraction: float = 0.25
) -> Holdout:
    """Cut the most recent ``fraction`` of observations off by entry date.

    Spec Q §10: "Reserve a final untouched holdout for each historical
    experiment family." :func:`evaluate_arm` refuses to read a reserved
    observation unless the caller says ``unseal_holdout=True``, which is a thing
    someone has to type once, at the end, on purpose.
    """
    if not 0 < fraction < 1:
        raise MetricsError("the holdout fraction is in (0, 1)")
    ordered = sorted(observations, key=lambda o: (o.entry_date, o.ticker, o.arm))
    if len(ordered) < 2:
        raise MetricsError("too few observations to reserve a holdout from")
    cut = max(1, int(round(len(ordered) * (1.0 - fraction))))
    cut = min(cut, len(ordered) - 1)
    start = ordered[cut].entry_date
    development = tuple(o for o in ordered if o.entry_date < start)
    reserved = tuple(o for o in ordered if o.entry_date >= start)
    if not development:
        raise MetricsError("the holdout would consume the whole sample")
    return Holdout(start_date=start, development=development, reserved=reserved)


# --------------------------------------------------------------------------- #
# The arm result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ArmMetrics:
    """One arm's evaluation. Every number carries its sample size."""

    arm: str
    status: str
    evidence_class: str
    window_start: date | None
    window_end: date | None
    n_observations: int
    n_matured: int
    n_open: int
    n_distinct_dates: int
    floors: EvidenceFloors
    warnings: tuple[str, ...]
    costs: CostAssumptions | None = None

    mean_net_pct: float | None = None
    median_net_pct: float | None = None
    mean_gross_pct: float | None = None
    total_return_pct: float | None = None
    mean_r: float | None = None
    median_r: float | None = None
    win_rate: float | None = None
    profit_factor: float | None = None
    max_drawdown_pct: float | None = None
    time_under_water_days: int | None = None
    exposure_positions_per_day: float | None = None
    turnover_trades_per_year: float | None = None
    total_costs: float | None = None
    benchmark: Benchmark | None = None
    benchmark_relative_pct: float | None = None
    uncertainty: Interval | None = None
    adjusted_lower: float | None = None
    adjusted_level: float | None = None
    n_trials: int = 1
    stepm_rejected: bool | None = None
    stepm_note: str = ""
    net_series: tuple[float, ...] = ()
    event_dates: tuple[date, ...] = ()
    open_tickers: tuple[str, ...] = ()

    @property
    def rankable(self) -> bool:
        return self.status == STATUS_OK

    def canonical(self) -> dict:
        return {
            "arm": self.arm,
            "status": self.status,
            "evidence_class": self.evidence_class,
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "n_observations": self.n_observations,
            "n_matured": self.n_matured,
            "n_open": self.n_open,
            "n_distinct_dates": self.n_distinct_dates,
            "open_tickers": list(self.open_tickers),
            "floors": self.floors.canonical(),
            "warnings": list(self.warnings),
            "costs": self.costs.canonical() if self.costs else None,
            "mean_net_pct": self.mean_net_pct,
            "median_net_pct": self.median_net_pct,
            "mean_gross_pct": self.mean_gross_pct,
            "total_return_pct": self.total_return_pct,
            "mean_r": self.mean_r,
            "median_r": self.median_r,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "max_drawdown_pct": self.max_drawdown_pct,
            "time_under_water_days": self.time_under_water_days,
            "exposure_positions_per_day": self.exposure_positions_per_day,
            "turnover_trades_per_year": self.turnover_trades_per_year,
            "total_costs": self.total_costs,
            "benchmark": self.benchmark.canonical() if self.benchmark else None,
            "benchmark_relative_pct": self.benchmark_relative_pct,
            "uncertainty": self.uncertainty.canonical() if self.uncertainty else None,
            "adjusted_lower": self.adjusted_lower,
            "adjusted_level": self.adjusted_level,
            "n_trials": self.n_trials,
            "stepm_rejected": self.stepm_rejected,
            "stepm_note": self.stepm_note,
        }


def _equity_curve(returns: Sequence[float]) -> list[float]:
    """One-position-at-a-time compounding, starting at 1.0."""
    equity, curve = 1.0, [1.0]
    for pct in returns:
        equity *= 1.0 + pct / 100.0
        curve.append(equity)
    return curve


def _drawdown(curve: Sequence[float], dates: Sequence[date]) -> tuple[float, int]:
    """Maximum drawdown as a percentage, and the longest time under water.

    The curve is indexed by trade, so "time under water" is measured between the
    exit dates of the peak trade and the trade that first exceeded it — calendar
    days, which is what a reader means by the phrase.
    """
    peak, peak_index = curve[0], 0
    worst, longest = 0.0, 0
    for i, value in enumerate(curve):
        if value >= peak:
            if i > peak_index:
                span = _span(dates, peak_index, i)
                longest = max(longest, span)
            peak, peak_index = value, i
            continue
        worst = min(worst, (value / peak - 1.0) * 100.0)
    if peak_index < len(curve) - 1:
        longest = max(longest, _span(dates, peak_index, len(curve) - 1))
    return worst, longest


def _span(dates: Sequence[date], start_index: int, end_index: int) -> int:
    """Calendar days between two points of the equity curve.

    The curve has one more point than there are trades (it starts at 1.0), so a
    curve index ``i`` corresponds to ``dates[i - 1]``; index 0 has no date and
    anchors to the first exit.
    """
    if not dates:
        return 0
    start = dates[max(0, start_index - 1)]
    end = dates[min(len(dates) - 1, end_index - 1)]
    return max(0, (end - start).days)


def evaluate_arm(
    arm: str,
    observations: Sequence[TradeObservation],
    *,
    floors: EvidenceFloors | None = None,
    costs: CostAssumptions | None = None,
    benchmark: Benchmark | None = None,
    uncertainty: UncertaintyBackend | None = None,
    multiplicity: MultiplicityBackend | None = None,
    n_trials: int = 1,
    level: float = 0.90,
    horizon_days: int = 1,
    holdout: Holdout | None = None,
    unseal_holdout: bool = False,
) -> ArmMetrics:
    """Measure one arm. Returns a result in every case, including a refusal.

    Never raises for a small sample — a refusal is a result, and a caller that
    has to catch an exception to learn "n=7" will eventually stop catching it.
    It *does* raise for the two things that are caller errors rather than data
    facts: mixing evidence classes in one statistic, and reading a reserved
    holdout during development.
    """
    floors = floors or EvidenceFloors()
    observations = tuple(observations)

    classes = {o.evidence_class for o in observations}
    if len(classes) > 1:
        raise MixedEvidence(
            f"{arm}: this observation set mixes {sorted(classes)}. Spec Q §10 "
            "keeps archival_reconstructed results in a separate report; they are "
            "never combined with clean replay or forward-shadow evidence."
        )
    evidence_class = classes.pop() if classes else EVIDENCE_CLEAN

    if holdout is not None and not unseal_holdout:
        touched = [o for o in observations if o.entry_date >= holdout.start_date]
        if touched:
            raise HoldoutViolation(
                f"{arm}: {len(touched)} observation(s) from the reserved holdout "
                f"(entries on or after {holdout.start_date.isoformat()}) reached a "
                "development evaluation. A holdout read during development is no "
                "longer a holdout (Spec Q §10)."
            )

    warnings: set[str] = set()
    if evidence_class == EVIDENCE_EXPLORATORY:
        warnings.add(WARN_EXPLORATORY)

    matured = tuple(sorted(
        (o for o in observations if o.matured),
        key=lambda o: (o.exit_date, o.entry_date, o.ticker),
    ))
    open_obs = tuple(o for o in observations if not o.matured)
    if open_obs:
        warnings.add(WARN_OPEN_OBSERVATIONS)

    window_start = min((o.entry_date for o in observations), default=None)
    window_end = max((o.exit_date for o in observations), default=None)
    distinct_dates = len({o.entry_date for o in matured})
    if matured and distinct_dates == 1:
        warnings.add(WARN_SINGLE_DATE)

    base = dict(
        arm=arm,
        evidence_class=evidence_class,
        window_start=window_start,
        window_end=window_end,
        n_observations=len(observations),
        n_matured=len(matured),
        n_open=len(open_obs),
        n_distinct_dates=distinct_dates,
        floors=floors,
        costs=costs,
        benchmark=benchmark,
        n_trials=max(1, int(n_trials)),
        open_tickers=tuple(sorted({o.ticker for o in open_obs})),
    )

    unpriced = [o for o in matured if not o.has_costs]
    if unpriced:
        warnings.add(WARN_COSTS_MISSING)
        return ArmMetrics(
            status=STATUS_BLOCKED, warnings=tuple(sorted(warnings)), **base
        )

    if not matured:
        warnings.add(WARN_BELOW_MATURED_FLOOR)
        return ArmMetrics(
            status=STATUS_INSUFFICIENT, warnings=tuple(sorted(warnings)), **base
        )

    nets = [float(o.net_pct) for o in matured]
    grosses = [o.gross_pct for o in matured]
    rs = [o.r_multiple for o in matured if o.r_multiple is not None]
    exits = [o.exit_date for o in matured]
    curve = _equity_curve(nets)
    drawdown, under_water = _drawdown(curve, exits)

    wins = [pct for pct in nets if pct > 0]
    losses = [pct for pct in nets if pct < 0]
    if not losses:
        warnings.add(WARN_PROFIT_FACTOR_UNDEFINED)
        warnings.add(WARN_NO_LOSSES)

    span_days = max(1, ((window_end - window_start).days if window_start else 1))
    years = span_days / 365.25
    position_days = sum(max(o.holding_days, 0) for o in observations)

    if benchmark is None:
        warnings.add(WARN_NO_BENCHMARK)
    total_return = (curve[-1] - 1.0) * 100.0

    interval = None
    if uncertainty is None:
        warnings.add(WARN_UNCERTAINTY_UNAVAILABLE)
    else:
        interval = uncertainty.interval(
            nets, [o.entry_date for o in matured],
            horizon=max(1, int(horizon_days)), level=level,
        )

    adjusted_lower, adjusted_level = None, None
    if multiplicity is None:
        warnings.add(WARN_MULTIPLICITY_UNAVAILABLE)
    elif interval is not None and uncertainty is not None:
        adjusted_level = multiplicity.family_level(level, max(1, int(n_trials)))
        adjusted_lower = uncertainty.interval(
            nets, [o.entry_date for o in matured],
            horizon=max(1, int(horizon_days)), level=adjusted_level,
        ).lower

    if len(matured) < floors.matured:
        warnings.add(WARN_BELOW_MATURED_FLOOR)
    if distinct_dates < floors.distinct_dates:
        warnings.add(WARN_BELOW_DATE_FLOOR)
    if len(matured) < floors.closed:
        warnings.add(WARN_BELOW_CLOSED_FLOOR)

    below_floor = {
        WARN_BELOW_MATURED_FLOOR, WARN_BELOW_DATE_FLOOR, WARN_BELOW_CLOSED_FLOOR,
    } & warnings
    status = STATUS_INSUFFICIENT if below_floor else STATUS_OK

    return ArmMetrics(
        status=status,
        warnings=tuple(sorted(warnings)),
        mean_net_pct=statistics.fmean(nets),
        median_net_pct=statistics.median(nets),
        mean_gross_pct=statistics.fmean(grosses),
        total_return_pct=total_return,
        mean_r=statistics.fmean(rs) if rs else None,
        median_r=statistics.median(rs) if rs else None,
        win_rate=len(wins) / len(nets),
        profit_factor=(sum(wins) / abs(sum(losses))) if losses else None,
        max_drawdown_pct=drawdown,
        time_under_water_days=under_water,
        exposure_positions_per_day=position_days / span_days,
        turnover_trades_per_year=len(matured) / years if years > 0 else None,
        total_costs=sum(o.costs for o in matured if o.costs is not None),
        benchmark_relative_pct=(
            total_return - benchmark.return_pct if benchmark else None
        ),
        uncertainty=interval,
        adjusted_lower=adjusted_lower,
        adjusted_level=adjusted_level,
        stepm_rejected=None,
        net_series=tuple(nets),
        event_dates=tuple(o.entry_date for o in matured),
        **base,
    )


# --------------------------------------------------------------------------- #
# Overlap and correlation between arms
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Overlap:
    """How much two arms are the same bet (Spec Q §10).

    Two numbers because one is not enough: ``jaccard`` says how often they took
    the same opportunity, ``correlation`` says how similarly those shared trades
    turned out. A pair with low overlap and high correlation is a different
    warning from a pair with high overlap and low correlation.
    """

    left: str
    right: str
    n_left: int
    n_right: int
    n_shared: int
    jaccard: float
    shared_exposure_days: int
    correlation: float | None
    correlation_note: str

    def canonical(self) -> dict:
        return {
            "left": self.left,
            "right": self.right,
            "n_left": self.n_left,
            "n_right": self.n_right,
            "n_shared": self.n_shared,
            "jaccard": self.jaccard,
            "shared_exposure_days": self.shared_exposure_days,
            "correlation": self.correlation,
            "correlation_note": self.correlation_note,
        }


#: Fewer pairs than this and a correlation is noise wearing a number's clothes.
MIN_PAIRS_FOR_CORRELATION = 3


def overlap(
    left: str,
    left_obs: Sequence[TradeObservation],
    right: str,
    right_obs: Sequence[TradeObservation],
) -> Overlap:
    """Ticker/time overlap and the correlation over the shared trades."""
    a = {o.key: o for o in left_obs if o.matured}
    b = {o.key: o for o in right_obs if o.matured}
    shared = sorted(set(a) & set(b))
    union = set(a) | set(b)

    paired = [
        (a[key].net_pct, b[key].net_pct)
        for key in shared
        if a[key].net_pct is not None and b[key].net_pct is not None
    ]
    correlation, note = None, ""
    if len(paired) < MIN_PAIRS_FOR_CORRELATION:
        note = (
            f"{len(paired)} shared matured trade(s); a correlation needs at "
            f"least {MIN_PAIRS_FOR_CORRELATION}"
        )
    else:
        xs = [p[0] for p in paired]
        ys = [p[1] for p in paired]
        if len(set(xs)) < 2 or len(set(ys)) < 2:
            note = "one side has no variation across the shared trades"
        else:
            correlation = statistics.correlation(xs, ys)
            note = f"Pearson over {len(paired)} shared matured trades"

    exposure = sum(
        _overlap_days(a[key], b[key]) for key in shared
    )
    return Overlap(
        left=left, right=right,
        n_left=len(a), n_right=len(b), n_shared=len(shared),
        jaccard=(len(shared) / len(union)) if union else 0.0,
        shared_exposure_days=exposure,
        correlation=correlation,
        correlation_note=note,
    )


def _overlap_days(left: TradeObservation, right: TradeObservation) -> int:
    start = max(left.entry_date, right.entry_date)
    end = min(left.exit_date, right.exit_date)
    return max(0, (end - start).days)


def pairwise_overlaps(
    by_arm: Mapping[str, Sequence[TradeObservation]]
) -> tuple[Overlap, ...]:
    """Every unordered pair, in a stable order."""
    labels = sorted(by_arm)
    return tuple(
        overlap(left, by_arm[left], right, by_arm[right])
        for i, left in enumerate(labels)
        for right in labels[i + 1:]
    )


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Scoreboard:
    """The ranked arms, and whether the ranking is allowed to name a winner."""

    evidence_class: str
    primary_metric: str
    rows: tuple[ArmMetrics, ...]
    overlaps: tuple[Overlap, ...]
    winner: str | None
    label: str
    reasons: tuple[str, ...]
    n_trials: int
    gate: RankingGate
    floors: EvidenceFloors
    variants_declared: int = 1

    def canonical(self) -> dict:
        return {
            "evidence_class": self.evidence_class,
            "primary_metric": self.primary_metric,
            "winner": self.winner,
            "label": self.label,
            "reasons": list(self.reasons),
            "n_trials": self.n_trials,
            "variants_declared": self.variants_declared,
            "gate": self.gate.canonical(),
            "floors": self.floors.canonical(),
            "arms": [row.canonical() for row in self.rows],
            "overlaps": [pair.canonical() for pair in self.overlaps],
        }


def rank_arms(
    rows: Sequence[ArmMetrics],
    *,
    by_arm: Mapping[str, Sequence[TradeObservation]] | None = None,
    primary_metric: str = "mean_net_pct",
    gate: RankingGate | None = None,
    floors: EvidenceFloors | None = None,
    n_trials: int = 1,
    variants_declared: int = 1,
    multiplicity: MultiplicityBackend | None = None,
) -> Scoreboard:
    """Order the arms, and name a winner only when every gate clears.

    The ordering is always produced — hiding the arms would be its own kind of
    dishonesty — but ``winner`` is ``None`` and ``label`` is
    ``insufficient_evidence`` unless *all* of these hold: no arm is blocked,
    every arm clears its floors, the evidence is clean rather than
    reconstructed, the leader's multiplicity-adjusted lower bound is above zero,
    the leader beats its benchmark, the leader's adjusted lower bound exceeds
    the runner-up's point estimate, and the family-wise step-M test rejects for
    the leader.

    A small-sample leader is therefore never called a winner, which is the one
    property this function exists to guarantee (Spec Q §10, §16).
    """
    gate = gate or RankingGate()
    floors = floors or EvidenceFloors()
    rows = tuple(rows)
    if not rows:
        raise MetricsError("a scoreboard needs at least one arm")

    classes = {row.evidence_class for row in rows}
    if len(classes) > 1:
        raise MixedEvidence(
            f"a scoreboard ranks one evidence class at a time; got {sorted(classes)}"
        )
    evidence_class = classes.pop()

    ordered = tuple(sorted(
        rows,
        key=lambda r: (
            -(getattr(r, primary_metric) if getattr(r, primary_metric) is not None
              else -math.inf),
            r.arm,
        ),
    ))

    if multiplicity is not None:
        family = {
            row.arm: row.net_series for row in ordered if row.net_series
        }
        scored: list[ArmMetrics] = []
        for row in ordered:
            if row.arm in family and len(family) > 1:
                rejected, note = multiplicity.stepm(family, row.arm)
            else:
                rejected, note = (
                    None,
                    "step-M needs at least two family members with a series",
                )
            scored.append(_with_stepm(row, rejected, note))
        ordered = tuple(scored)

    reasons: list[str] = []
    if evidence_class == EVIDENCE_EXPLORATORY:
        reasons.append(
            "the evidence is archival_reconstructed: exploratory only, and it "
            "can never rank a winner or satisfy a promotion gate (Spec Q §10)"
        )
    for row in ordered:
        if row.status == STATUS_BLOCKED:
            reasons.append(f"{row.arm} is blocked: {', '.join(row.warnings)}")
        elif row.status == STATUS_INSUFFICIENT:
            reasons.append(
                f"{row.arm} is below its floors "
                f"(n_matured={row.n_matured}/{floors.matured}, "
                f"distinct_dates={row.n_distinct_dates}/{floors.distinct_dates})"
            )

    leader = ordered[0]
    if not reasons:
        reasons.extend(_gate_failures(ordered, gate))

    if reasons:
        return Scoreboard(
            evidence_class=evidence_class,
            primary_metric=primary_metric,
            rows=ordered,
            overlaps=pairwise_overlaps(by_arm or {}),
            winner=None,
            label=STATUS_INSUFFICIENT,
            reasons=tuple(reasons),
            n_trials=max(1, int(n_trials)),
            gate=gate,
            floors=floors,
            variants_declared=variants_declared,
        )
    return Scoreboard(
        evidence_class=evidence_class,
        primary_metric=primary_metric,
        rows=ordered,
        overlaps=pairwise_overlaps(by_arm or {}),
        winner=leader.arm,
        label="leader_separated",
        reasons=(),
        n_trials=max(1, int(n_trials)),
        gate=gate,
        floors=floors,
        variants_declared=variants_declared,
    )


def _with_stepm(row: ArmMetrics, rejected: bool | None, note: str) -> ArmMetrics:
    payload = dict(row.__dict__)
    payload["stepm_rejected"] = rejected
    payload["stepm_note"] = note
    return ArmMetrics(**payload)


def _gate_failures(ordered: Sequence[ArmMetrics], gate: RankingGate) -> list[str]:
    """Every reason the leader has not earned the word "winner"."""
    leader = ordered[0]
    failures: list[str] = []
    if leader.uncertainty is None:
        failures.append(
            f"{leader.arm} has no uncertainty interval; without one a point "
            "estimate is not evidence (Spec N §6.1)"
        )
        return failures
    if leader.adjusted_lower is None:
        failures.append(
            f"{leader.arm} has no multiplicity-adjusted interval; the "
            "multiple-testing diagnostic did not run"
        )
        return failures
    if gate.require_positive_adjusted_lower_bound and leader.adjusted_lower <= 0:
        failures.append(
            f"{leader.arm}'s multiplicity-adjusted lower bound is "
            f"{leader.adjusted_lower:.4f} at level {leader.adjusted_level:.4f} "
            f"across {leader.n_trials} trial(s); it does not exclude zero"
        )
    if gate.require_beating_benchmark:
        if leader.benchmark is None:
            failures.append(f"{leader.arm} has no benchmark to be compared against")
        elif (leader.benchmark_relative_pct or 0.0) <= 0:
            failures.append(
                f"{leader.arm} did not beat {leader.benchmark.label} "
                f"({leader.benchmark_relative_pct:.2f} pp)"
            )
    if gate.require_separation_from_runner_up and len(ordered) > 1:
        runner_up = ordered[1]
        point = runner_up.mean_net_pct
        if point is None:
            failures.append(f"{runner_up.arm} has no point estimate to separate from")
        elif leader.adjusted_lower <= point:
            failures.append(
                f"{leader.arm}'s adjusted lower bound ({leader.adjusted_lower:.4f}) "
                f"does not clear {runner_up.arm}'s point estimate ({point:.4f}); "
                "the data has not separated them"
            )
    if gate.require_stepm_rejection and not leader.stepm_rejected:
        failures.append(
            f"the family-wise step-M test did not reject for {leader.arm} "
            f"({leader.stepm_note or 'not run'})"
        )
    return failures


def variant_warnings(declared: int, tried: int) -> tuple[str, ...]:
    """A warning when more variants ran than the pre-registration declared."""
    return (WARN_UNDECLARED_VARIANTS,) if tried > declared else ()
