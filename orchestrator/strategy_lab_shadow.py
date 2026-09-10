"""The Strategy Lab's one connection to the production scan (Spec Q §14, §15 PR 4).

Everything the flags gate lives here, so that `orchestrator/pipeline.py` carries
a hook and not an integration. Four entry points, and every one of them is a
no-op returning a *reason* when a flag is off:

:func:`run_shadow_for_scan`
    called once at the end of a full scan, after the existing analysis, the
    memos and the paper auto-approve pass have all finished. It builds one
    ticker snapshot per scored name plus — separately gated — one universe
    snapshot for the whole cutoff, and runs every active shadow arm over them
    through PR 3's :func:`strategy_lab.runner.run_snapshot`.
:func:`mature_shadow_decisions`
    the nightly job. A shadow decision cannot be executed on the day it is made:
    a forward simulation needs the bars that came *after* the snapshot, and at
    scan time those do not exist. So the decisions land first and the executions
    are opened and settled later, once the bars are there.
:func:`scoreboard`
    the operator's numbers, computed by `scripts/strategy_lab_scoreboard.py`
    over `strategy_lab/metrics.py`. Nothing here computes a statistic.
:func:`ensure_experiment`
    idempotent registration of the pre-registered plan below.

**Isolation is the contract.** Spec Q §14: "Strategy Lab writes are best-effort
in shadow mode and must not break memo generation." Every entry point catches
its own exceptions, records them on the summary it returns, and returns
normally. The pipeline hook then wraps the call again, because a defect in this
module's own error handling must not be the thing that kills a scan either.

**No broker, ever.** This module imports no broker and no order manager, and
`strategy_lab/` cannot import one at all
(`tests/test_strategy_lab_import_graph.py`). Shadow arms are refused by
`strategy_lab.shadow` unless their immutable mode is `shadow`, so "places no
order" is structural rather than conditional — and
`tests/test_strategy_lab_integration.py` proves it with a broker double that
raises on attribute access.

**The plan is a constant, not a setting.** :data:`EXPERIMENT_HYPOTHESIS` and its
neighbours are the pre-registration Spec Q §8 freezes at registration time. They
are in the repository, under review, rather than in the environment, because an
analysis plan that a deployment variable can move is not a pre-registration.
Changing the roster or the planned variant count changes the experiment's
content hash, and re-registering the same name with a changed plan is refused by
`registry.register_experiment` — the remedy is a new `STRATEGY_LAB_EXPERIMENT`
name, which is exactly the behaviour the freeze exists to produce.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping, Sequence

from utils.logger import get_logger
from utils.timeutils import utcnow_naive

log = get_logger("strategy_lab_shadow")

__all__ = [
    "EXPERIMENT_HYPOTHESIS",
    "PLANNED_VARIANTS",
    "UNIVERSE_SCOPED_SLUGS",
    "TICKER_SCOPED_SLUGS",
    "ShadowRunSummary",
    "MaturationSummary",
    "lab_enabled",
    "shadow_enabled",
    "experiment_spec",
    "arm_plans",
    "ensure_experiment",
    "run_shadow_for_scan",
    "mature_shadow_decisions",
    "portfolio_context",
    "scoreboard",
]


# --------------------------------------------------------------------------- #
# The pre-registration (Spec Q §8)
# --------------------------------------------------------------------------- #

EXPERIMENT_HYPOTHESIS = (
    "Run the V1 roster concurrently in shadow over the same opportunities the "
    "production scan already surfaces, to find out whether any challenger's "
    "net-of-cost return distribution separates from the incumbent composite's "
    "by enough to justify paper execution. No arm is expected to win; the "
    "experiment exists to measure, and an inconclusive result after the "
    "stopping rule is a result."
)

#: Every roster strategy runs exactly one shadow arm, so the pre-registered
#: variant count is the roster size. It is stated as a literal rather than
#: `len(ROSTER)` on purpose: `planned_variants` is the multiple-testing
#: denominator (Spec Q §10), and a denominator that silently grows when someone
#: adds a strategy is how a tournament launders luck into evidence. Adding an
#: arm must be a visible edit here — which changes the experiment's content hash
#: and forces a new experiment name.
PLANNED_VARIANTS = 4

PRIMARY_METRIC = "mean_net_pct"
BENCHMARKS = ("equal_weight_universe", "spy_total_return")
GUARDRAIL_METRICS = ("max_drawdown_pct", "time_under_water_days", "turnover_trades_per_year")

END_CRITERIA = {
    "matured_trades_per_arm": 100,
    "distinct_entry_dates_per_arm": 20,
    "min_shadow_days": 60,
    "note": (
        "Duration is an information count, not a calendar date (Spec Q §9). The "
        "experiment ends when every arm clears these floors or when an arm is "
        "retired; it does not end when the numbers look best."
    ),
}

START_CRITERIA = {
    "requires": "STRATEGY_LAB_ENABLED and STRATEGY_LAB_SHADOW_ENABLED",
    "mode": "shadow",
    "broker_orders": "none — no arm above shadow exists in this experiment",
}

PREREGISTRATION = {
    "spec": "Q §8, §9, §10",
    "registered_by": "orchestrator/strategy_lab_shadow.py",
    "analysis": (
        "Ranked by the primary metric after explicit costs, with the stationary "
        "block bootstrap interval, the Sidak family adjustment over the variant "
        "ledger and Romano-Wolf step-M, all computed by comparables/inference.py "
        "through scripts/strategy_lab_scoreboard.py. A winner may be named only "
        "when every condition in metrics.RankingGate holds."
    ),
    "evidence_class": (
        "price_bars carry no availability or revision provenance, so every "
        "snapshot built from them is archival_reconstructed and lands in the "
        "exploratory section, which can never satisfy a promotion gate "
        "(Spec Q §6, §10)."
    ),
    "promotion": "owner-only; nothing in this experiment performs or authorises one",
}

#: Which roster strategies take a universe-scoped snapshot. Restated here rather
#: than read off the version, because `StrategyVersion` carries no scope field —
#: the scope lives in each strategy's `evaluate`, which raises on a mismatch.
#: `tests/test_strategy_lab_integration.py` asserts this table agrees with what
#: every roster strategy actually accepts, the same way
#: `strategy_lab/snapshot_builder.py` restates the observation predicate and has
#: a test hold the two together.
UNIVERSE_SCOPED_SLUGS: frozenset[str] = frozenset({
    "momentum_v1",
    "short_term_reversal_v1",
})

TICKER_SCOPED_SLUGS: frozenset[str] = frozenset({
    "swingtrader_composite_v1",
    "earnings_drift_v1",
})

#: How far past a decision's expected horizon the maturation job keeps looking
#: for forward bars before it gives up on settling that snapshot. A decision
#: whose bars never arrive stays a decision with no execution, which is the
#: honest record.
MATURATION_GRACE_DAYS = 10

SKIP_LAB_DISABLED = "strategy_lab_disabled"
SKIP_SHADOW_DISABLED = "strategy_lab_shadow_disabled"
SKIP_NO_TICKERS = "no_scored_tickers"
SKIP_UNIVERSE_DISABLED = "universe_arms_disabled"


# --------------------------------------------------------------------------- #
# Flags
# --------------------------------------------------------------------------- #


def lab_enabled(settings) -> bool:
    """The master switch. False means nothing here touches the database."""
    return bool(getattr(settings, "strategy_lab_enabled", False))


def shadow_enabled(settings) -> bool:
    """The second gate, on the one path that writes."""
    return lab_enabled(settings) and bool(
        getattr(settings, "strategy_lab_shadow_enabled", False)
    )


def _experiment_name(settings) -> str:
    return str(getattr(settings, "strategy_lab_experiment", "") or "shadow_roster_v1")


def _int(settings, name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


def _float(settings, name: str, default: float) -> float:
    try:
        return float(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Summaries
# --------------------------------------------------------------------------- #


@dataclass
class ShadowRunSummary:
    """What one post-scan shadow pass did. Every field is a count or a reason."""

    enabled: bool = False
    experiment: str = ""
    skipped_reason: str = ""
    ticker_snapshots: int = 0
    universe_snapshots: int = 0
    arms: int = 0
    decisions: int = 0
    n_long: int = 0
    n_flat: int = 0
    n_abstain: int = 0
    n_deduplicated: int = 0
    snapshot_failures: int = 0
    refusals: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ran(self) -> bool:
        return self.enabled and not self.skipped_reason

    def as_log_fields(self) -> dict:
        """Counts and reason codes only — never a ticker list, never a payload."""
        return {
            "experiment": self.experiment,
            "skipped_reason": self.skipped_reason,
            "ticker_snapshots": self.ticker_snapshots,
            "universe_snapshots": self.universe_snapshots,
            "arms": self.arms,
            "decisions": self.decisions,
            "long": self.n_long,
            "flat": self.n_flat,
            "abstain": self.n_abstain,
            "deduplicated": self.n_deduplicated,
            "snapshot_failures": self.snapshot_failures,
            "refused_arms": len(self.refusals),
            "errors": len(self.errors),
        }


@dataclass
class MaturationSummary:
    """What one nightly settlement pass did."""

    enabled: bool = False
    experiment: str = ""
    skipped_reason: str = ""
    snapshots_considered: int = 0
    snapshots_settled: int = 0
    executions_opened: int = 0
    executions_blocked: int = 0
    executions_closed: int = 0
    still_pending: int = 0
    errors: list[str] = field(default_factory=list)

    def as_log_fields(self) -> dict:
        return {
            "experiment": self.experiment,
            "skipped_reason": self.skipped_reason,
            "snapshots_considered": self.snapshots_considered,
            "snapshots_settled": self.snapshots_settled,
            "executions_opened": self.executions_opened,
            "executions_blocked": self.executions_blocked,
            "executions_closed": self.executions_closed,
            "still_pending": self.still_pending,
            "errors": len(self.errors),
        }


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def experiment_spec(settings):
    """The frozen plan. Deterministic, so re-registering it is a no-op.

    ``data_cutoff_utc`` is deliberately ``None``: this is a forward shadow
    experiment with no historical window, and a cutoff derived from the clock
    would change the content hash on every scan and make the plan un-freezable.
    """
    from strategy_lab.domain import ExperimentSpec

    return ExperimentSpec(
        name=_experiment_name(settings),
        hypothesis=EXPERIMENT_HYPOTHESIS,
        universe_spec=str(
            getattr(settings, "strategy_lab_universe_slug", "") or "liquid_us_equity_v1"
        ),
        primary_metric=PRIMARY_METRIC,
        benchmarks=BENCHMARKS,
        guardrail_metrics=GUARDRAIL_METRICS,
        end_criteria=END_CRITERIA,
        start_criteria=START_CRITERIA,
        preregistration=PREREGISTRATION,
        planned_variants=PLANNED_VARIANTS,
        owner=str(getattr(settings, "strategy_lab_experiment_owner", "") or "bryan"),
        data_cutoff_utc=None,
    )


def arm_plans(settings) -> tuple:
    """One shadow arm per roster strategy, in a stable order.

    Every arm's mode is ``shadow`` and is fixed at creation: a tier move creates
    another arm and records a promotion event, which is PR 6's work. Nothing
    here can produce a paper or live arm.
    """
    from strategy_lab import strategies
    from strategy_lab.domain import ExecutionMode
    from strategy_lab.runner import ArmPlan

    budget = _float(settings, "strategy_lab_shadow_risk_budget", 0.01)
    return tuple(
        ArmPlan(
            slug=slug,
            mode=ExecutionMode.SHADOW,
            version=version.version,
            risk_budget=budget,
            activate=True,
        )
        for slug, version in sorted(strategies.build_versions().items())
    )


def ensure_experiment(session, settings):
    """Register the plan, the versions and the arms. Idempotent by construction.

    Called at the start of every shadow pass rather than once at boot, so a
    container that restarted mid-experiment resumes without an operator step and
    a database that was restored from a backup re-registers what it is missing.
    """
    from strategy_lab import registry, runner, strategies
    from strategy_lab.domain import ExperimentStatus

    versions = strategies.build_versions()
    spec = experiment_spec(settings)
    # Re-assert the status the row is already in rather than the status a first
    # registration would take. `registered -> registered` is a no-op and
    # `running -> running` is a no-op; `running -> registered` is not a legal
    # transition at all, and asking for it would make the second scan of the day
    # fail registration on an experiment the first scan started.
    existing = registry.get_experiment(session, spec.name)
    status = (
        ExperimentStatus(existing.status)
        if existing is not None
        else ExperimentStatus.REGISTERED
    )
    registered = runner.register_experiment(
        session, spec, arm_plans(settings), versions=versions, status=status
    )
    runner.promote_versions_to_shadow(session, versions)
    if ExperimentStatus(registered.status) is ExperimentStatus.REGISTERED:
        runner.start_experiment(session, registered.name)
    return registered


def active_shadow_arms(session, settings) -> tuple:
    """The arms a run may use: active, shadow-mode, under this experiment.

    A paused arm is excluded here as well as refused by the runner, so pausing
    an arm stops the work rather than only the write.
    """
    from strategy_lab import registry
    from strategy_lab.domain import ArmStatus, ExecutionMode

    out = []
    for arm in registry.arms_for_experiment(
        session, _experiment_name(settings), statuses=(ArmStatus.ACTIVE,)
    ):
        if ExecutionMode(arm.mode) is not ExecutionMode.SHADOW:
            continue
        version = registry.strategy_version_for_arm(session, arm.id)
        out.append((arm.id, version.slug, version.version))
    return tuple(sorted(out))


# --------------------------------------------------------------------------- #
# The post-scan pass
# --------------------------------------------------------------------------- #


def run_shadow_for_scan(
    settings,
    *,
    tickers: Sequence[str],
    run_id: str = "",
    cutoff: datetime | None = None,
) -> ShadowRunSummary:
    """Build the scan's snapshots and run every active shadow arm over them.

    Never raises. A failure anywhere — registration, one snapshot, one arm — is
    recorded on the summary and the pass carries on, because the caller is a
    production scan that has already delivered its memos and must not be taken
    down by an experiment (Spec Q §14).

    Snapshot discipline, Spec Q §6 and PR 4 requirement 8: the cross-sectional
    snapshot is built **once for the cutoff** and shared by every universe arm.
    It is never assembled per ticker, because ranks taken from snapshots built at
    different times are not a cross-section.
    """
    summary = ShadowRunSummary(experiment=_experiment_name(settings))
    if not lab_enabled(settings):
        summary.skipped_reason = SKIP_LAB_DISABLED
        return summary
    if not shadow_enabled(settings):
        summary.skipped_reason = SKIP_SHADOW_DISABLED
        return summary
    summary.enabled = True

    names = _unique_upper(tickers)[: max(0, _int(settings, "strategy_lab_max_tickers_per_scan", 40))]
    universe_wanted = bool(getattr(settings, "strategy_lab_universe_enabled", False))
    if not names and not universe_wanted:
        summary.skipped_reason = SKIP_NO_TICKERS
        return summary

    try:
        _run_shadow(settings, summary, names=names, cutoff=cutoff or utcnow_naive())
    except Exception as exc:  # pragma: no cover - defence in depth
        summary.errors.append(f"{type(exc).__name__}: {exc}")
        log.error("strategy_lab_shadow_failed", error=str(exc)[:300])

    log.info("strategy_lab_shadow_funnel", run_id=run_id, **summary.as_log_fields())
    return summary


def _run_shadow(settings, summary: ShadowRunSummary, *, names: Sequence[str], cutoff: datetime):
    from database.db import get_session
    from strategy_lab import runner, snapshot_builder, strategies

    versions = strategies.build_versions()
    with get_session() as session:
        try:
            ensure_experiment(session, settings)
        except Exception as exc:
            summary.errors.append(f"register: {type(exc).__name__}: {exc}")
            log.error("strategy_lab_registration_failed", error=str(exc)[:300])
            return

        arms = active_shadow_arms(session, settings)
        summary.arms = len(arms)
        if not arms:
            summary.skipped_reason = "no_active_shadow_arms"
            return

        ticker_arms = [a for a in arms if a[1] in TICKER_SCOPED_SLUGS]
        universe_arms = [a for a in arms if a[1] in UNIVERSE_SCOPED_SLUGS]

        for ticker in names:
            if not ticker_arms:
                break
            try:
                snapshot = snapshot_builder.build_ticker_snapshot(
                    session, ticker, cutoff=cutoff, with_composite=True
                )
            except Exception as exc:
                summary.snapshot_failures += 1
                log.info(
                    "strategy_snapshot_blocked",
                    scope="ticker", ticker=ticker, reason=str(exc)[:200],
                )
                continue
            summary.ticker_snapshots += 1
            _record(summary, runner.run_snapshot(
                session, snapshot, [a[0] for a in ticker_arms], versions=versions
            ))

        if not universe_arms:
            return
        if not bool(getattr(settings, "strategy_lab_universe_enabled", False)):
            summary.refusals.append((
                "universe_arms",
                "STRATEGY_LAB_UNIVERSE_ENABLED is false; no cross-sectional "
                "snapshot was built for this cutoff",
            ))
            return
        try:
            snapshot = snapshot_builder.build_universe_snapshot(
                session,
                cutoff=cutoff,
                universe_slug=str(
                    getattr(settings, "strategy_lab_universe_slug", "")
                    or "liquid_us_equity_v1"
                ),
            )
        except Exception as exc:
            summary.snapshot_failures += 1
            log.info("strategy_snapshot_blocked", scope="universe", reason=str(exc)[:200])
            return
        summary.universe_snapshots += 1
        _record(summary, runner.run_snapshot(
            session, snapshot, [a[0] for a in universe_arms], versions=versions
        ))


def _record(summary: ShadowRunSummary, report) -> None:
    """Fold one :class:`strategy_lab.runner.RunReport` into the summary."""
    for arm_run in report.arm_runs:
        if arm_run.refused:
            summary.refusals.append((arm_run.arm.label, arm_run.refused))
            continue
        summary.decisions += len(arm_run.decisions)
        summary.n_long += arm_run.n_long
        summary.n_flat += arm_run.n_flat
        summary.n_abstain += arm_run.n_abstain
        summary.n_deduplicated += arm_run.n_deduplicated


def _unique_upper(tickers: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in tickers or ():
        symbol = (raw or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
    return out


# --------------------------------------------------------------------------- #
# Maturation
# --------------------------------------------------------------------------- #


def portfolio_context(settings, *, as_of: datetime, open_tickers: Sequence[str] = ()):
    """The shadow book's virtual context (Spec Q §11).

    Independent of the real portfolio on purpose: a shadow arm's hypothetical
    performance must not change because a live position was opened elsewhere.
    """
    from strategy_lab.shadow import PortfolioContext

    return PortfolioContext(
        as_of_utc=as_of,
        equity=_float(settings, "strategy_lab_shadow_equity", 100_000.0),
        open_tickers=tuple(open_tickers),
        max_open_positions=_int(settings, "strategy_lab_shadow_max_open_positions", 10),
        max_position_fraction=_float(
            settings, "strategy_lab_shadow_max_position_fraction", 0.2
        ),
    )


def cost_assumptions(settings):
    """The explicit cost model. Never absent, never zero by accident."""
    from strategy_lab.replay import CostAssumptions

    return CostAssumptions(
        slippage_bps=_float(settings, "strategy_lab_slippage_bps", 10.0),
        half_spread_bps=_float(settings, "strategy_lab_half_spread_bps", 5.0),
        commission_bps=_float(settings, "strategy_lab_commission_bps", 0.0),
    )


def mature_shadow_decisions(settings, *, now: datetime | None = None) -> MaturationSummary:
    """Settle shadow decisions whose forward bars have arrived. Never raises.

    A decision made at a scan cannot be executed at that scan: a forward
    simulation needs the sessions that came *after* its snapshot, and at scan
    time there are none. So this job runs nightly, finds `long` decisions whose
    snapshot is old enough for its policy horizon to have elapsed, and hands the
    arm to `strategy_lab.shadow.execute_arm` — the same state machine PR 5's
    live path walks, with the lab standing in for owner, risk desk and broker.

    A decision whose bars never arrive is left as a decision with no execution.
    That is the honest record: it is neither a win, a loss, nor a zero.
    """
    summary = MaturationSummary(experiment=_experiment_name(settings))
    if not shadow_enabled(settings):
        summary.skipped_reason = (
            SKIP_LAB_DISABLED if not lab_enabled(settings) else SKIP_SHADOW_DISABLED
        )
        return summary
    summary.enabled = True
    try:
        _mature(settings, summary, now=now or utcnow_naive())
    except Exception as exc:
        summary.errors.append(f"{type(exc).__name__}: {exc}")
        log.error("strategy_lab_maturation_failed", error=str(exc)[:300])
    log.info("strategy_lab_maturation", **summary.as_log_fields())
    return summary


def _mature(settings, summary: MaturationSummary, *, now: datetime):
    from database.db import get_session
    from strategy_lab import registry, shadow, strategies
    from strategy_lab.domain import ExecutionState

    versions = strategies.build_versions()
    budget = max(1, _int(settings, "strategy_lab_maturation_max_snapshots", 200))
    costs = cost_assumptions(settings)

    with get_session() as session:
        arms = active_shadow_arms(session, settings)
        for arm_id, slug, _version in arms:
            version = versions.get(slug)
            if version is None:
                continue
            horizon = timedelta(
                days=version.expected_holding_days + MATURATION_GRACE_DAYS
            )
            pending = _pending_by_snapshot(session, arm_id)
            for snapshot_id, decision_ids in sorted(pending.items()):
                if summary.snapshots_considered >= budget:
                    return
                summary.snapshots_considered += 1
                snapshot_row = registry.snapshot_row(session, snapshot_id)
                if snapshot_row is None:
                    continue
                if snapshot_row.data_cutoff_utc + horizon > now:
                    summary.still_pending += len(decision_ids)
                    continue
                snapshot = registry.load_snapshot(snapshot_row)
                forward = _forward_bars(
                    session,
                    tickers=[
                        registry.decision_row(session, d).ticker for d in decision_ids
                    ],
                    after=snapshot.data_cutoff_utc,
                    until=now,
                )
                if not forward:
                    summary.still_pending += len(decision_ids)
                    continue
                try:
                    executions, _outcomes = shadow.execute_arm(
                        session, arm_id, version, snapshot, decision_ids, forward,
                        context=portfolio_context(
                            settings, as_of=snapshot.data_cutoff_utc
                        ),
                        costs=costs,
                    )
                except Exception as exc:
                    summary.errors.append(f"arm {arm_id}: {type(exc).__name__}: {exc}")
                    log.error(
                        "strategy_trade_settlement_failed",
                        arm_id=arm_id, snapshot_id=snapshot_id, error=str(exc)[:200],
                    )
                    continue
                summary.snapshots_settled += 1
                for execution in executions:
                    if execution.blocked:
                        summary.executions_blocked += 1
                    else:
                        summary.executions_opened += 1
                    if execution.status is ExecutionState.CLOSED:
                        summary.executions_closed += 1
                settled = {e.decision_id for e in executions}
                summary.still_pending += len(
                    [d for d in decision_ids if d not in settled]
                )


def _pending_by_snapshot(session, arm_id: int) -> dict[int, list[int]]:
    """`long` decisions for one arm that have no execution row yet."""
    from strategy_lab import registry
    from strategy_lab.domain import DecisionAction

    pending: dict[int, list[int]] = {}
    for row in registry.decisions_for_arm(
        session, arm_id, actions=(DecisionAction.LONG.value,)
    ):
        if registry.open_execution_for(session, row.id) is not None:
            continue
        if _has_any_execution(session, row.id):
            continue
        pending.setdefault(row.snapshot_id, []).append(row.id)
    return {key: sorted(value) for key, value in pending.items()}


def _has_any_execution(session, decision_id: int) -> bool:
    from database import models

    return (
        session.query(models.StrategyTrade)
        .filter(models.StrategyTrade.decision_id == decision_id)
        .first()
        is not None
    )


def _forward_bars(
    session, *, tickers: Sequence[str], after: datetime, until: datetime
) -> dict:
    """Bars strictly after a snapshot's cutoff, per ticker.

    Built from `price_bars` with the same normalisation
    `strategy_lab/snapshot_builder.py` applies when it reads the plane backwards
    from a cutoff, and with the same "completed sessions only" rule: a session
    whose close has not arrived is not a bar yet.
    """
    from sqlalchemy import select

    from database import models
    from strategy_lab import snapshots

    out: dict[str, tuple] = {}
    for ticker in sorted({(t or "").strip().upper() for t in tickers if t}):
        rows = session.execute(
            select(models.PriceBar)
            .where(
                models.PriceBar.ticker == ticker,
                models.PriceBar.session_date > after.date(),
                models.PriceBar.session_date <= until.date(),
            )
            .order_by(models.PriceBar.session_date)
        ).scalars().all()
        bars = tuple(
            snapshots.SnapshotBar(
                session_date=row.session_date,
                raw_open=row.raw_open,
                raw_high=row.raw_high,
                raw_low=row.raw_low,
                raw_close=row.raw_close,
                volume=row.volume,
                split_adjusted_close=row.split_adjusted_close,
                total_return_close=row.total_return_close,
            )
            for row in rows
            if snapshots.session_close_utc(row.session_date) <= until
        )
        if len(bars) >= 2:
            out[ticker] = bars
    return out


# --------------------------------------------------------------------------- #
# The operator's numbers
# --------------------------------------------------------------------------- #


def scoreboard(settings, *, cutoff: datetime | None = None) -> dict | None:
    """The scorecard payload, or ``None`` when the lab is off or has no arms.

    Every number in it is produced by `scripts/strategy_lab_scoreboard.py` over
    `strategy_lab/metrics.py` and `comparables/inference.py`. This function
    supplies the inputs and passes the payload through; it computes nothing, and
    neither does anything that renders it (Spec Q §10, AGENTS.md §1.2).
    """
    if not lab_enabled(settings):
        return None
    from database.db import get_session
    from scripts import strategy_lab_scoreboard as board
    from strategy_lab import metrics, registry, runner

    name = _experiment_name(settings)
    inputs = board.ScoreboardInputs(
        experiment=name,
        cutoff_utc=cutoff or utcnow_naive(),
        floors=metrics.EvidenceFloors(
            matured=_int(settings, "strategy_lab_floor_matured", 100),
            distinct_dates=_int(settings, "strategy_lab_floor_distinct_dates", 20),
            closed=_int(settings, "strategy_lab_floor_closed", 30),
        ),
        gate=metrics.RankingGate(
            confidence_level=_float(settings, "strategy_lab_confidence_level", 0.90)
        ),
        costs=cost_assumptions(settings),
        primary_metric=PRIMARY_METRIC,
    )
    with get_session() as session:
        if registry.get_experiment(session, name) is None:
            return None
        by_arm, warnings = board.collect(session, name)
        ledger = runner.variant_ledger(session, name)

    reps = _int(settings, "strategy_lab_report_bootstrap_reps", 1000)
    seed = _int(settings, "strategy_lab_report_seed", 20260910)
    return board.build_scorecard(
        inputs,
        by_arm,
        ledger,
        uncertainty=board.ComparablesUncertainty(reps=reps, seed=seed),
        multiplicity=board.ComparablesMultiplicity(seed=seed),
        read_warnings=warnings,
    )


def experiment_overview(settings) -> Mapping | None:
    """Experiments, arms and their tier distribution, for ``/experiments``.

    Read-only, and every field is a stored column: statuses, modes, counts. No
    number here is computed — the scorecard is where numbers come from.
    """
    if not lab_enabled(settings):
        return None
    from database import models
    from database.db import get_session
    from strategy_lab import registry

    with get_session() as session:
        rows = (
            session.query(models.Experiment)
            .order_by(models.Experiment.name)
            .all()
        )
        out = []
        for row in rows:
            arms = []
            for arm in registry.arms_for_experiment(session, row.name):
                version = registry.strategy_version_for_arm(session, arm.id)
                arms.append({
                    "arm_id": arm.id,
                    "slug": version.slug,
                    "version": version.version,
                    "mode": arm.mode,
                    "status": arm.status,
                    "risk_budget": float(arm.risk_budget or 0.0),
                    "decisions": len(registry.decisions_for_arm(session, arm.id)),
                })
            out.append({
                "name": row.name,
                "status": row.status,
                "planned_variants": int(row.planned_variants or 0),
                "primary_metric": row.primary_metric,
                "owner": row.owner,
                "arms": arms,
                "configured": row.name == _experiment_name(settings),
            })
    return {"experiments": out, "configured": _experiment_name(settings)}


def strategy_overview(settings) -> Mapping | None:
    """The roster and every registered version's status, for ``/strategies``."""
    if not lab_enabled(settings):
        return None
    from database.db import get_session
    from strategy_lab import registry, strategies

    versions = strategies.build_versions()
    with get_session() as session:
        out = []
        for slug, version in sorted(versions.items()):
            row = registry.get_strategy_version(session, slug, version.version)
            out.append({
                "slug": slug,
                "version": version.version,
                "scope": "universe" if slug in UNIVERSE_SCOPED_SLUGS else "ticker",
                "status": row.status if row is not None else "unregistered",
                "policy": version.execution_policy_version,
                "expected_holding_days": version.expected_holding_days,
                "historically_replayable": bool(version.historically_replayable),
                "hypothesis": version.hypothesis,
                "champion": slug == "swingtrader_composite_v1",
            })
    return {"strategies": out}


def strategy_detail(settings, slug: str, *, recent: int = 5) -> Mapping | None:
    """One strategy: its arms, its decision counts and its recent decisions.

    Counts are `len()` over stored rows and nothing else; the performance
    numbers come from :func:`scoreboard`, which is the only place they may come
    from.
    """
    if not lab_enabled(settings):
        return None
    from database.db import get_session
    from strategy_lab import registry, strategies

    versions = strategies.build_versions()
    version = versions.get(slug)
    if version is None:
        return {"unknown": slug, "roster": sorted(versions)}

    with get_session() as session:
        row = registry.get_strategy_version(session, slug, version.version)
        arms = []
        decisions = []
        for arm in registry.arms_for_experiment(session, _experiment_name(settings)):
            arm_version = registry.strategy_version_for_arm(session, arm.id)
            if arm_version.slug != slug:
                continue
            rows = registry.decisions_for_arm(session, arm.id)
            executions = registry.executions_for_arm(session, arm.id)
            arms.append({
                "arm_id": arm.id,
                "mode": arm.mode,
                "status": arm.status,
                "decisions": len(rows),
                "long": sum(1 for r in rows if r.action == "long"),
                "flat": sum(1 for r in rows if r.action == "flat"),
                "abstain": sum(1 for r in rows if r.action == "abstain"),
                "executions": len(executions),
                "closed": sum(1 for e in executions if e.status == "closed"),
                "open": sum(
                    1 for e in executions
                    if e.status not in ("closed", "cancelled", "rejected", "failed")
                ),
            })
            for record in sorted(rows, key=lambda r: r.id, reverse=True)[:recent]:
                decisions.append({
                    "arm_id": arm.id,
                    "ticker": record.ticker,
                    "action": record.action,
                    "signal_strength": record.signal_strength,
                    "decided_at": record.created_at,
                })
    return {
        "slug": slug,
        "version": version.version,
        "status": row.status if row is not None else "unregistered",
        "scope": "universe" if slug in UNIVERSE_SCOPED_SLUGS else "ticker",
        "policy": version.execution_policy_version,
        "historically_replayable": bool(version.historically_replayable),
        "replayability_reason": version.replayability_reason,
        "hypothesis": version.hypothesis,
        "arms": arms,
        "recent_decisions": sorted(
            decisions, key=lambda d: (d["arm_id"], d["ticker"])
        )[: recent * 2],
    }


def set_experiment_paused(settings, name: str, *, paused: bool) -> Mapping:
    """Pause or resume an experiment. Owner-only at the call site.

    A paused experiment is not in `registry.RUNNABLE_EXPERIMENT_STATUSES`, so
    every arm under it refuses at the runner — pausing stops the work, not only
    the write.
    """
    from database.db import get_session
    from strategy_lab import registry
    from strategy_lab.domain import ExperimentStatus, StrategyLabError

    target = ExperimentStatus.PAUSED if paused else ExperimentStatus.RUNNING
    with get_session() as session:
        row = registry.get_experiment(session, name)
        if row is None:
            return {"ok": False, "error": f"no experiment named {name!r}"}
        before = row.status
        if ExperimentStatus(before) is target:
            return {"ok": True, "name": name, "status": before, "changed": False}
        try:
            updated = registry.set_experiment_status(session, name, target)
        except StrategyLabError as exc:
            return {"ok": False, "error": str(exc), "name": name, "status": before}
        status = updated.status
    log.info(
        "experiment_arm_paused" if paused else "experiment_arm_started",
        experiment=name, status=status,
    )
    return {"ok": True, "name": name, "status": status, "changed": True}
