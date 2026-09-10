"""The Strategy Lab service boundary: register, read, and record (Spec Q §8).

Everything that writes an experiment table goes through here, and this is the
only module in the package allowed to hold a database session — a strategy
module receives an immutable snapshot and returns a decision, and cannot reach
a session, a broker, Telegram, or a model client at all (Spec Q §5, enforced by
``tests/test_strategy_lab_import_graph.py``).

Four rules are enforced here rather than hoped for:

**Registered versions are immutable.** Re-registering ``(slug, version)`` with
different content is refused, with the diff of what changed. Status may move
along its lifecycle; rules, config, manifest and dependencies may not. A
parameter change is a new version (Spec Q §3, shared rule 8).

**Registered experiments are immutable.** A ``draft`` experiment can still be
edited; once it registers, its hypothesis and analysis plan are frozen, because
an experiment whose primary metric can be chosen after seeing the results is not
an experiment.

**Decisions are idempotent, and deterministic.** ``(arm, snapshot, ticker)`` is
unique. Re-recording the identical decision returns the stored row; recording a
*different* decision for the same triple is refused — same version, same
snapshot, different answer means the version's code changed under it, which
Spec Q §6 requires to fail closed rather than overwrite the record.

**One globally active live arm.** Activation is idempotent, and the database
holds the invariant with a partial unique index; the checks here exist to turn
an ``IntegrityError`` into a sentence someone can act on, not to replace it.

The functions take a ``session`` as their first argument and ``flush()`` rather
than commit, matching ``research_workspace/store.py``: the caller owns the
transaction, which is what makes "deactivate the old champion and activate the
new one, or neither" a single atomic step.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError

from database import models
from strategy_lab.domain import (
    ARM_TRANSITIONS,
    EXECUTION_TRANSITIONS,
    EXPERIMENT_TRANSITIONS,
    STRATEGY_VERSION_TRANSITIONS,
    ArmFacts,
    ArmStatus,
    Direction,
    EvidenceFacts,
    ExecutionMode,
    ExperimentSpec,
    ExperimentStatus,
    ImmutabilityError,
    MarketSnapshot,
    PromotionRefused,
    SnapshotScope,
    StrategyDecision,
    ExecutionState,
    TERMINAL_EXECUTION_STATES,
    StrategyLabError,
    StrategyVersion,
    StrategyVersionStatus,
    authorize_promotion,
    canonical_json,
    naive_utc,
    require_transition,
)
from strategy_lab.execution import (
    BLOCKING_EXECUTION_STATES,
    RESERVING_EXECUTION_STATES,
    ExecutionConflict,
    holds_reservation,
    redact,
)
from strategy_lab.validation import verify_manifest
from utils.logger import get_logger
from utils.timeutils import utcnow_naive

log = get_logger("strategy_lab_registry")


class NotFound(StrategyLabError):
    """A referenced experiment, version, arm, or evidence snapshot does not exist."""


def _loads(blob: str, default):
    try:
        loaded = json.loads(blob or "")
    except (ValueError, TypeError):
        return default
    return loaded


def new_execution_id() -> str:
    """The idempotency key Phase 5 commits before any reservation or broker call."""
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- #
# Strategy versions
# --------------------------------------------------------------------------- #


def get_strategy_version(session, slug: str, version: str):
    return (
        session.query(models.StrategyVersion)
        .filter(models.StrategyVersion.slug == slug, models.StrategyVersion.version == version)
        .first()
    )


def require_strategy_version(session, slug: str, version: str):
    row = get_strategy_version(session, slug, version)
    if row is None:
        raise NotFound(f"strategy version {slug}@{version} is not registered")
    return row


def register_strategy_version(session, spec: StrategyVersion):
    """Store an immutable version, or return the identical stored one.

    Idempotent by content: registering the same rules twice is a no-op, and
    registering different rules under the same ``(slug, version)`` is refused
    with the fields that differ. There is no update path on purpose.
    """
    if not isinstance(spec, StrategyVersion):
        raise StrategyLabError("register_strategy_version takes a domain StrategyVersion")

    existing = get_strategy_version(session, spec.slug, spec.version)
    if existing is not None:
        if existing.content_hash != spec.content_hash:
            stored = load_strategy_version(existing)
            changed = sorted(
                key
                for key, value in spec.canonical().items()
                if stored.canonical().get(key) != value
            )
            raise ImmutabilityError(
                f"strategy version {spec.identity} is already registered with "
                f"different content (changed: {changed}). Any rule or parameter "
                "change is a new version (Spec Q §3); bump the version instead."
            )
        return existing

    now = utcnow_naive()
    row = models.StrategyVersion(
        slug=spec.slug,
        version=spec.version,
        hypothesis=spec.hypothesis,
        universe=spec.universe,
        direction=spec.direction.value,
        required_snapshot_fields_json=canonical_json(list(spec.required_snapshot_fields)),
        execution_policy_version=spec.execution_policy_version,
        expected_holding_days=spec.expected_holding_days,
        max_data_staleness_seconds=spec.max_data_staleness_seconds,
        historically_replayable=spec.historically_replayable,
        replayability_reason=spec.replayability_reason,
        implementation_manifest_json=canonical_json(dict(spec.implementation_manifest)),
        implementation_manifest_hash=spec.manifest_hash,
        config_json=canonical_json(dict(spec.config)),
        dependencies_json=canonical_json(list(spec.dependencies)),
        content_hash=spec.content_hash,
        status=spec.status.value,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    log.info(
        "strategy_version_registered",
        slug=spec.slug,
        version=spec.version,
        status=spec.status.value,
        replayable=spec.historically_replayable,
    )
    return row


def load_strategy_version(row) -> StrategyVersion:
    """Rehydrate the domain object from a row, hash included."""
    return StrategyVersion(
        slug=row.slug,
        version=row.version,
        hypothesis=row.hypothesis,
        universe=row.universe,
        direction=Direction(row.direction),
        required_snapshot_fields=tuple(_loads(row.required_snapshot_fields_json, [])),
        execution_policy_version=row.execution_policy_version,
        expected_holding_days=row.expected_holding_days,
        max_data_staleness_seconds=row.max_data_staleness_seconds,
        historically_replayable=bool(row.historically_replayable),
        replayability_reason=row.replayability_reason,
        implementation_manifest=_loads(row.implementation_manifest_json, {}),
        config=_loads(row.config_json, {}),
        dependencies=tuple(_loads(row.dependencies_json, [])),
        status=StrategyVersionStatus(row.status),
    )


def set_strategy_version_status(session, slug: str, version: str, status):
    """Move a version along its lifecycle. Content is untouched."""
    row = require_strategy_version(session, slug, version)
    target = require_transition(
        STRATEGY_VERSION_TRANSITIONS, row.status, status,
        label=f"strategy version {slug}@{version}",
    )
    row.status = target.value
    row.updated_at = utcnow_naive()
    session.flush()
    return row


# --------------------------------------------------------------------------- #
# Experiments
# --------------------------------------------------------------------------- #


def get_experiment(session, name: str):
    return session.query(models.Experiment).filter(models.Experiment.name == name).first()


def require_experiment(session, name: str):
    row = get_experiment(session, name)
    if row is None:
        raise NotFound(f"experiment {name!r} is not registered")
    return row


def load_experiment(row) -> ExperimentSpec:
    return ExperimentSpec(
        name=row.name,
        hypothesis=row.hypothesis,
        universe_spec=row.universe_spec,
        primary_metric=row.primary_metric,
        benchmarks=tuple(_loads(row.benchmarks_json, [])),
        guardrail_metrics=tuple(_loads(row.guardrail_metrics_json, [])),
        end_criteria=_loads(row.end_criteria_json, {}),
        start_criteria=_loads(row.start_criteria_json, {}),
        preregistration=_loads(row.preregistration_json, {}),
        planned_variants=row.planned_variants,
        owner=row.owner,
        data_cutoff_utc=row.data_cutoff_utc,
        status=ExperimentStatus(row.status),
    )


def register_experiment(session, spec: ExperimentSpec, *, status=ExperimentStatus.REGISTERED):
    """Register an experiment, freezing its analysis plan.

    A ``draft`` row may still be revised — that is what draft means. Once it has
    registered, a differing plan is refused: the hypothesis, metrics,
    benchmarks, universe and end criteria are the question, and rewriting the
    question after seeing the answer is the failure mode the whole experiment
    system exists to prevent (Spec Q §10).
    """
    if not isinstance(spec, ExperimentSpec):
        raise StrategyLabError("register_experiment takes a domain ExperimentSpec")
    status = ExperimentStatus(status)

    existing = get_experiment(session, spec.name)
    now = utcnow_naive()
    if existing is not None:
        if existing.content_hash != spec.content_hash:
            if ExperimentStatus(existing.status) is not ExperimentStatus.DRAFT:
                stored = load_experiment(existing)
                changed = sorted(
                    key
                    for key, value in spec.canonical().items()
                    if stored.canonical().get(key) != value
                )
                raise ImmutabilityError(
                    f"experiment {spec.name!r} is {existing.status} and its plan "
                    f"is frozen (changed: {changed}). Register a new experiment "
                    "rather than editing a running one (Spec Q §8)."
                )
            _apply_experiment_spec(existing, spec)
        target = require_transition(
            EXPERIMENT_TRANSITIONS, existing.status, status, label=f"experiment {spec.name}"
        )
        if target is not ExperimentStatus(existing.status):
            existing.status = target.value
            if target is ExperimentStatus.REGISTERED:
                existing.registered_at = existing.registered_at or now
        existing.updated_at = now
        session.flush()
        return existing

    row = models.Experiment(name=spec.name, created_at=now, updated_at=now)
    _apply_experiment_spec(row, spec)
    row.status = status.value
    row.registered_at = now if status is not ExperimentStatus.DRAFT else None
    session.add(row)
    session.flush()
    log.info(
        "experiment_registered",
        experiment=spec.name,
        status=row.status,
        planned_variants=spec.planned_variants,
    )
    return row


def _apply_experiment_spec(row, spec: ExperimentSpec) -> None:
    row.hypothesis = spec.hypothesis
    row.universe_spec = spec.universe_spec
    row.primary_metric = spec.primary_metric
    row.benchmarks_json = canonical_json(list(spec.benchmarks))
    row.guardrail_metrics_json = canonical_json(list(spec.guardrail_metrics))
    row.start_criteria_json = canonical_json(dict(spec.start_criteria))
    row.end_criteria_json = canonical_json(dict(spec.end_criteria))
    row.preregistration_json = canonical_json(dict(spec.preregistration))
    row.planned_variants = spec.planned_variants
    row.owner = spec.owner
    row.data_cutoff_utc = spec.data_cutoff_utc
    row.content_hash = spec.content_hash


def set_experiment_status(session, name: str, status):
    row = require_experiment(session, name)
    target = require_transition(
        EXPERIMENT_TRANSITIONS, row.status, status, label=f"experiment {name}"
    )
    now = utcnow_naive()
    if target is ExperimentStatus.RUNNING and row.started_at is None:
        row.started_at = now
    if target in (ExperimentStatus.COMPLETED, ExperimentStatus.CANCELLED):
        row.ended_at = now
    row.status = target.value
    row.updated_at = now
    session.flush()
    return row


# --------------------------------------------------------------------------- #
# Arms
# --------------------------------------------------------------------------- #

#: An arm may run only while its experiment is one of these (Spec Q §8).
RUNNABLE_EXPERIMENT_STATUSES = frozenset({
    ExperimentStatus.REGISTERED, ExperimentStatus.RUNNING, ExperimentStatus.EVALUATING,
})


def get_arm(session, arm_id: int):
    return session.get(models.ExperimentArm, arm_id)


def require_arm(session, arm_id: int):
    row = get_arm(session, arm_id)
    if row is None:
        raise NotFound(f"experiment arm {arm_id} does not exist")
    return row


def arm_facts(session, arm_id: int) -> ArmFacts:
    """The row reduced to the fields a promotion decision depends on."""
    row = require_arm(session, arm_id)
    return ArmFacts(
        arm_id=row.id,
        experiment_id=row.experiment_id,
        strategy_version_id=row.strategy_version_id,
        mode=ExecutionMode(row.mode),
        status=ArmStatus(row.status),
        risk_budget=float(row.risk_budget),
    )


def create_arm(
    session,
    experiment_name: str,
    slug: str,
    version: str,
    mode,
    *,
    risk_budget: float = 0.0,
    promoted_from_arm_id: int | None = None,
):
    """Create an inactive arm. Activation is a separate, audited step.

    The mode is fixed here and never changes: a tier move creates *another* arm
    and records a promotion event against it (Spec Q §8). That is what makes
    "execution mode is an immutable input, not a global setting" true of the
    data and not only of the code.
    """
    mode = ExecutionMode(mode)
    if risk_budget < 0:
        raise StrategyLabError("risk_budget must be >= 0")
    experiment = require_experiment(session, experiment_name)
    strategy = require_strategy_version(session, slug, version)

    existing = (
        session.query(models.ExperimentArm)
        .filter(
            models.ExperimentArm.experiment_id == experiment.id,
            models.ExperimentArm.strategy_version_id == strategy.id,
            models.ExperimentArm.mode == mode.value,
            models.ExperimentArm.status.in_((ArmStatus.INACTIVE.value, ArmStatus.ACTIVE.value)),
        )
        .order_by(models.ExperimentArm.id)
        .first()
    )
    if existing is not None:
        return existing

    now = utcnow_naive()
    row = models.ExperimentArm(
        experiment_id=experiment.id,
        strategy_version_id=strategy.id,
        mode=mode.value,
        risk_budget=float(risk_budget),
        status=ArmStatus.INACTIVE.value,
        promoted_from_arm_id=promoted_from_arm_id,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    log.info(
        "experiment_arm_created",
        arm_id=row.id,
        experiment=experiment_name,
        strategy=f"{slug}@{version}",
        mode=mode.value,
    )
    return row


def set_arm_status(session, arm_id: int, status):
    """Move an arm's status, holding the single-live-champion invariant.

    Idempotent: activating an already-active arm is a no-op, which is what a
    retried scheduler run does. The database index is the real guarantee; the
    pre-check here exists so the refusal names the arm that is already live
    instead of surfacing a constraint name.
    """
    row = require_arm(session, arm_id)
    target = require_transition(ARM_TRANSITIONS, row.status, status, label=f"arm {arm_id}")
    if ArmStatus(row.status) is target:
        return row

    now = utcnow_naive()
    if target is ArmStatus.ACTIVE:
        experiment = session.get(models.Experiment, row.experiment_id)
        if ExperimentStatus(experiment.status) not in RUNNABLE_EXPERIMENT_STATUSES:
            raise StrategyLabError(
                f"experiment {experiment.name!r} is {experiment.status}; no arm "
                "may run until it is registered (Spec Q §8)"
            )
        if ExecutionMode(row.mode) is ExecutionMode.LIVE:
            champion = active_live_arm(session)
            if champion is not None and champion.id != row.id:
                raise StrategyLabError(
                    f"arm {champion.id} is already the active live champion; V1 "
                    "runs one live arm at a time (Spec Q §3). Use "
                    "replace_live_champion() to swap it atomically."
                )
        row.started_at = row.started_at or now

    if target in (ArmStatus.RETIRED, ArmStatus.INACTIVE, ArmStatus.PAUSED):
        if ArmStatus(row.status) is ArmStatus.ACTIVE:
            row.ended_at = now

    row.status = target.value
    row.updated_at = now
    try:
        session.flush()
    except IntegrityError as exc:
        raise StrategyLabError(
            f"arm {arm_id} could not become {target.value}: another active arm "
            f"already holds that slot ({exc.orig})"
        ) from exc
    log.info("experiment_arm_status_changed", arm_id=arm_id, status=target.value)
    return row


def activate_arm(session, arm_id: int):
    return set_arm_status(session, arm_id, ArmStatus.ACTIVE)


def active_live_arm(session):
    """The one globally active live arm, or ``None``. Spec Q §3, §8."""
    return (
        session.query(models.ExperimentArm)
        .filter(
            models.ExperimentArm.mode == ExecutionMode.LIVE.value,
            models.ExperimentArm.status == ArmStatus.ACTIVE.value,
        )
        .first()
    )


def replace_live_champion(session, target_arm_id: int):
    """Swap the global live champion in one transaction, or change nothing.

    The old champion is deactivated before the new one is activated because the
    partial unique index is checked per statement; the caller's transaction is
    what makes the pair atomic, so a failure anywhere below rolls back and
    leaves the prior champion exactly as it was (Spec Q §8).
    """
    target = require_arm(session, target_arm_id)
    if ExecutionMode(target.mode) is not ExecutionMode.LIVE:
        raise StrategyLabError(
            f"arm {target_arm_id} is a {target.mode} arm; only a live arm can be "
            "the live champion, and an arm's mode is immutable"
        )
    champion = active_live_arm(session)
    if champion is not None and champion.id == target.id:
        return target
    if champion is not None:
        set_arm_status(session, champion.id, ArmStatus.INACTIVE)
    activated = set_arm_status(session, target.id, ArmStatus.ACTIVE)
    log.info(
        "live_champion_replaced",
        previous_arm_id=champion.id if champion else None,
        arm_id=activated.id,
    )
    return activated


# --------------------------------------------------------------------------- #
# Snapshots and decisions
# --------------------------------------------------------------------------- #


def record_snapshot(session, snapshot: MarketSnapshot):
    """Store a snapshot, or return the stored one with the same content hash.

    Idempotent by content: the same inputs under the same cutoff *are* the same
    snapshot, so two arms built from one rebalance share a row and every
    cross-sectional rank references one snapshot id (Spec Q §7).
    """
    if not isinstance(snapshot, MarketSnapshot):
        raise StrategyLabError("record_snapshot takes a domain MarketSnapshot")

    existing = (
        session.query(models.MarketSnapshot)
        .filter(models.MarketSnapshot.content_hash == snapshot.content_hash)
        .first()
    )
    if existing is not None:
        return existing

    row = models.MarketSnapshot(
        scope=snapshot.scope.value,
        ticker=snapshot.ticker,
        universe_version=snapshot.universe_version,
        as_of_utc=snapshot.as_of_utc,
        data_cutoff_utc=snapshot.data_cutoff_utc,
        normalized_inputs_json=canonical_json(dict(snapshot.normalized_inputs)),
        constituents_json=canonical_json(list(snapshot.constituents)),
        source_observation_ids_json=canonical_json(list(snapshot.source_observation_ids)),
        provenance_json=canonical_json(dict(snapshot.provenance)),
        data_quality_json=canonical_json({
            "warnings": list(snapshot.quality_warnings),
            "replay_eligible": snapshot.replay_eligible,
        }),
        content_hash=snapshot.content_hash,
        created_at=utcnow_naive(),
    )
    session.add(row)
    session.flush()
    log.info(
        "strategy_snapshot_built",
        snapshot_id=row.id,
        scope=snapshot.scope.value,
        replay_eligible=snapshot.replay_eligible,
        warnings=list(snapshot.quality_warnings),
    )
    return row


def load_snapshot(row) -> MarketSnapshot:
    quality = _loads(row.data_quality_json, {})
    return MarketSnapshot(
        scope=SnapshotScope(row.scope),
        as_of_utc=row.as_of_utc,
        data_cutoff_utc=row.data_cutoff_utc,
        provenance=_loads(row.provenance_json, {}),
        ticker=row.ticker,
        universe_version=row.universe_version,
        constituents=tuple(_loads(row.constituents_json, [])),
        normalized_inputs=_loads(row.normalized_inputs_json, {}),
        source_observation_ids=tuple(_loads(row.source_observation_ids_json, [])),
        quality_warnings=tuple(quality.get("warnings", []) if isinstance(quality, dict) else []),
    )


def record_decision(session, arm_id: int, snapshot_id: int, decision: StrategyDecision):
    """Persist one decision, idempotently.

    Returns the stored row when the identical decision is recorded again. A
    *different* decision for the same ``(arm, snapshot, ticker)`` is refused
    rather than overwritten: one strategy version over one snapshot must produce
    one answer, so a second answer means the code changed under the version and
    is exactly what Spec Q §6 requires to fail closed.
    """
    if not isinstance(decision, StrategyDecision):
        raise StrategyLabError("record_decision takes a domain StrategyDecision")

    arm = require_arm(session, arm_id)
    snapshot = session.get(models.MarketSnapshot, snapshot_id)
    if snapshot is None:
        raise NotFound(f"market snapshot {snapshot_id} does not exist")
    if snapshot.content_hash != decision.snapshot_hash:
        raise StrategyLabError(
            f"decision references snapshot content {decision.snapshot_hash[:12]} "
            f"but snapshot {snapshot_id} holds {snapshot.content_hash[:12]}; a "
            "decision must be recorded against the snapshot it was made from"
        )
    strategy = session.get(models.StrategyVersion, arm.strategy_version_id)
    if (strategy.slug, strategy.version) != (decision.strategy_slug, decision.strategy_version):
        raise StrategyLabError(
            f"arm {arm_id} runs {strategy.slug}@{strategy.version}, but the "
            f"decision came from {decision.strategy_slug}@{decision.strategy_version}"
        )

    existing = (
        session.query(models.StrategyDecision)
        .filter(
            models.StrategyDecision.arm_id == arm_id,
            models.StrategyDecision.snapshot_id == snapshot_id,
            models.StrategyDecision.ticker == decision.ticker,
        )
        .first()
    )
    if existing is not None:
        if existing.decision_hash != decision.decision_hash:
            raise ImmutabilityError(
                f"arm {arm_id} already recorded a different decision for "
                f"{decision.ticker} under snapshot {snapshot_id} "
                f"({existing.decision_hash[:12]} vs {decision.decision_hash[:12]}). "
                "One strategy version over one snapshot decides one thing; a "
                "second answer means the implementation changed and needs a new "
                "version (Spec Q §6)."
            )
        log.info(
            "strategy_decision_deduplicated",
            arm_id=arm_id, snapshot_id=snapshot_id, ticker=decision.ticker,
        )
        return existing

    row = models.StrategyDecision(
        arm_id=arm_id,
        snapshot_id=snapshot_id,
        ticker=decision.ticker,
        action=decision.action.value,
        reason_codes_json=canonical_json(list(decision.reason_codes)),
        signal_strength=decision.signal_strength,
        confidence=decision.confidence,
        decision_json=canonical_json(decision.canonical()),
        decision_hash=decision.decision_hash,
        blocked_reasons_json=canonical_json(list(decision.blocked_reasons)),
        created_at=utcnow_naive(),
    )
    session.add(row)
    session.flush()
    log.info(
        "strategy_decision_recorded",
        arm_id=arm_id, snapshot_id=snapshot_id, ticker=decision.ticker,
        action=decision.action.value,
    )
    return row


def decisions_for(session, arm_id: int, snapshot_id: int):
    return (
        session.query(models.StrategyDecision)
        .filter(
            models.StrategyDecision.arm_id == arm_id,
            models.StrategyDecision.snapshot_id == snapshot_id,
        )
        .order_by(models.StrategyDecision.ticker)
        .all()
    )


# --------------------------------------------------------------------------- #
# Evidence and promotion
# --------------------------------------------------------------------------- #


def record_metric_snapshot(
    session,
    arm_id: int,
    evaluation_cutoff_utc: datetime,
    *,
    n_decisions: int = 0,
    n_matured: int = 0,
    n_closed: int = 0,
    warnings=(),
    metrics=None,
    cost_assumptions=None,
    uncertainty=None,
    benchmark: str = "",
    **columns,
):
    """Store an arm's evaluation at a cutoff, idempotently.

    Unique on ``(arm, cutoff)``: an evaluation is a function of those two, so
    re-running it returns the stored row rather than producing a second,
    friendlier answer for the same period. Phase 3 computes the numbers; this
    only records them.
    """
    require_arm(session, arm_id)
    cutoff = naive_utc(evaluation_cutoff_utc, field_name="evaluation_cutoff_utc")
    existing = (
        session.query(models.ExperimentMetricSnapshot)
        .filter(
            models.ExperimentMetricSnapshot.arm_id == arm_id,
            models.ExperimentMetricSnapshot.evaluation_cutoff_utc == cutoff,
        )
        .first()
    )
    if existing is not None:
        return existing

    payload = {
        "arm_id": arm_id,
        "cutoff": cutoff.isoformat(),
        "n_decisions": n_decisions,
        "n_matured": n_matured,
        "n_closed": n_closed,
        "metrics": metrics or {},
        "cost_assumptions": cost_assumptions or {},
        "warnings": sorted(warnings),
        **{key: value for key, value in sorted(columns.items())},
    }
    row = models.ExperimentMetricSnapshot(
        arm_id=arm_id,
        evaluation_cutoff_utc=cutoff,
        n_decisions=n_decisions,
        n_matured=n_matured,
        n_closed=n_closed,
        benchmark=benchmark,
        uncertainty_json=canonical_json(uncertainty or {}),
        cost_assumptions_json=canonical_json(cost_assumptions or {}),
        warnings_json=canonical_json(sorted(warnings)),
        metrics_json=canonical_json(metrics or {}),
        content_hash=hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest(),
        created_at=utcnow_naive(),
        **columns,
    )
    session.add(row)
    session.flush()
    log.info(
        "strategy_metric_snapshot_created",
        arm_id=arm_id, cutoff=cutoff.isoformat(), n_matured=n_matured,
    )
    return row


def acknowledge_metric_warnings(session, metric_snapshot_id: int, owner: str):
    """Record that the owner read this evidence's warnings (Spec Q §8).

    A tier change requires it, which is the point: a promotion that can be
    confirmed without seeing "n=7, insufficient_evidence" is a promotion made
    without evidence.
    """
    if not (owner or "").strip():
        raise StrategyLabError("acknowledging warnings requires an owner identity")
    row = session.get(models.ExperimentMetricSnapshot, metric_snapshot_id)
    if row is None:
        raise NotFound(f"metric snapshot {metric_snapshot_id} does not exist")
    row.warnings_acknowledged = True
    row.acknowledged_by = owner.strip()
    row.acknowledged_at = utcnow_naive()
    session.flush()
    return row


def evidence_facts(session, metric_snapshot_id: int) -> EvidenceFacts:
    row = session.get(models.ExperimentMetricSnapshot, metric_snapshot_id)
    if row is None:
        raise NotFound(f"metric snapshot {metric_snapshot_id} does not exist")
    return EvidenceFacts(
        metric_snapshot_id=row.id,
        arm_id=row.arm_id,
        warnings_acknowledged=bool(row.warnings_acknowledged),
    )


def record_promotion(
    session,
    source_arm_id: int,
    target_arm_id: int,
    evidence_metric_snapshot_id: int,
    *,
    owner: str,
    reason: str,
    activate: bool = True,
    require_warning_acknowledgement: bool = True,
):
    """Append an owner's audited tier change and activate the target arm.

    The bindings are checked in :func:`strategy_lab.domain.authorize_promotion`,
    which is pure; this function supplies the rows and performs the two writes.
    A live activation goes through :func:`replace_live_champion`, so the global
    champion is swapped atomically or not at all.

    This records an authorization the owner has already given. Nothing in this
    package decides to promote anything — Spec Q §3 keeps promotion authority
    with the owner, and Phase 6 adds the confirmation callback that reaches
    them.
    """
    source = arm_facts(session, source_arm_id)
    target = arm_facts(session, target_arm_id)
    evidence = evidence_facts(session, evidence_metric_snapshot_id)
    authorization = authorize_promotion(
        source, target, evidence,
        owner=owner, reason=reason,
        require_warning_acknowledgement=require_warning_acknowledgement,
    )
    if target.experiment_id != source.experiment_id:
        raise PromotionRefused(
            "source and target arms belong to different experiments; a "
            "promotion moves one experiment's arm up a tier"
        )

    row = models.PromotionEvent(
        source_arm_id=authorization.source_arm_id,
        target_arm_id=authorization.target_arm_id,
        strategy_version_id=authorization.strategy_version_id,
        evidence_metric_snapshot_id=authorization.evidence_metric_snapshot_id,
        from_mode=authorization.from_mode.value,
        to_mode=authorization.to_mode.value,
        kind=authorization.kind.value,
        owner=authorization.owner,
        reason=authorization.reason,
        previous_risk_budget=authorization.previous_risk_budget,
        new_risk_budget=authorization.new_risk_budget,
        created_at=utcnow_naive(),
    )
    session.add(row)
    session.flush()

    if activate:
        arm = require_arm(session, target_arm_id)
        arm.promoted_from_arm_id = source_arm_id
        if authorization.to_mode is ExecutionMode.LIVE:
            replace_live_champion(session, target_arm_id)
        else:
            set_arm_status(session, target_arm_id, ArmStatus.ACTIVE)

    log.info(
        "strategy_promotion_confirmed",
        promotion_id=row.id,
        source_arm_id=source_arm_id,
        target_arm_id=target_arm_id,
        from_mode=authorization.from_mode.value,
        to_mode=authorization.to_mode.value,
        owner=authorization.owner,
    )
    return row


def promotions_for(session, arm_id: int):
    return (
        session.query(models.PromotionEvent)
        .filter(models.PromotionEvent.target_arm_id == arm_id)
        .order_by(models.PromotionEvent.created_at, models.PromotionEvent.id)
        .all()
    )


# --------------------------------------------------------------------------- #
# Implementation-manifest drift (Spec Q §6, Phase 2)
# --------------------------------------------------------------------------- #


def verify_registered_manifest(session, version: StrategyVersion) -> None:
    """Refuse to proceed when the code on disk is not what was registered.

    Spec Q §6: "On activation and before every run, the registry recomputes the
    manifest hash and compares it with
    ``strategy_versions.implementation_manifest_hash``." ``version`` is the
    freshly rebuilt domain object — ``strategy_lab.strategies.build_versions()``
    produces one per slug from the source as it is right now — and this is the
    comparison. A mismatch raises ``validation.ManifestDrift``.

    Registration itself already refuses a *content* change under an existing
    identity; this is the other half, and it is the half that runs on every
    evaluation rather than only when someone tries to re-register.
    """
    row = require_strategy_version(session, version.slug, version.version)
    verify_manifest(version, row.implementation_manifest_hash)


def activate_strategy_version(session, version: StrategyVersion, status):
    """Recompute the manifest, then move the version's status.

    The order matters: an activation that skipped the check would let a shadow
    arm start running on edited code under an identity whose evidence was
    collected from different code.
    """
    verify_registered_manifest(session, version)
    return set_strategy_version_status(session, version.slug, version.version, status)


# --------------------------------------------------------------------------- #
# Executions (`strategy_trades`) — Spec Q §8, §12
# --------------------------------------------------------------------------- #


def arms_for_experiment(session, experiment_name: str, *, statuses=None):
    """Every arm of an experiment, oldest first. ``statuses`` filters if given."""
    experiment = require_experiment(session, experiment_name)
    query = session.query(models.ExperimentArm).filter(
        models.ExperimentArm.experiment_id == experiment.id
    )
    if statuses is not None:
        query = query.filter(
            models.ExperimentArm.status.in_([ArmStatus(s).value for s in statuses])
        )
    return query.order_by(models.ExperimentArm.id).all()


def open_execution_for(session, decision_id: int):
    """The one non-terminal execution for a decision, or ``None``.

    The database holds this with a partial unique index; the query exists so a
    retry can *find* the row it must reuse rather than discovering the
    constraint by violating it (Spec Q §12 invariant 6).
    """
    terminal = [state.value for state in TERMINAL_EXECUTION_STATES]
    return (
        session.query(models.StrategyTrade)
        .filter(
            models.StrategyTrade.decision_id == decision_id,
            ~models.StrategyTrade.status.in_(terminal),
        )
        .order_by(models.StrategyTrade.id)
        .first()
    )


def record_execution(
    session,
    arm_id: int,
    decision_id: int,
    *,
    status=ExecutionState.PROPOSED,
    portfolio_context_hash: str = "",
    blocked_reason: str = "",
    execution_id: str | None = None,
    **columns,
):
    """Create the execution row for a decision, idempotently.

    Two idempotency rules, applied in this order:

    * **An attempt is identified by ``(decision, portfolio_context_hash)``.**
      Re-running an evaluation against the same portfolio returns the stored
      row, whatever state it has since reached. That is what makes a whole
      shadow run re-runnable: a settled, closed execution is not a reason to
      open a second one, and a refusal is not appended twice.
    * **A decision has at most one non-terminal execution.** A retry against a
      *different* context while one is still open reuses that open row and its
      ``execution_id`` rather than creating a second placement. That is the
      database constraint's application-level twin, and it is why the row is
      written before any reservation or broker call would be (Spec Q §12
      invariant 6).

    A refusal against a genuinely different portfolio context is a new attempt
    and gets its own row, which is how "the same decision was blocked on
    Tuesday" stays answerable.

    The mode is copied from the arm and never passed in: Spec Q §11 makes an
    arm's mode the execution's mode, and an argument here would be somewhere for
    a global setting to leak in.
    """
    arm = require_arm(session, arm_id)
    decision = session.get(models.StrategyDecision, decision_id)
    if decision is None:
        raise NotFound(f"strategy decision {decision_id} does not exist")
    if decision.arm_id != arm_id:
        raise StrategyLabError(
            f"decision {decision_id} belongs to arm {decision.arm_id}, not to "
            f"arm {arm_id}; an execution is an attempt at its own arm's decision"
        )
    state = ExecutionState(status)

    existing = (
        session.query(models.StrategyTrade)
        .filter(
            models.StrategyTrade.decision_id == decision_id,
            models.StrategyTrade.portfolio_context_hash == portfolio_context_hash,
        )
        .order_by(models.StrategyTrade.id)
        .first()
    )
    if existing is not None:
        return existing
    if state not in TERMINAL_EXECUTION_STATES:
        existing = open_execution_for(session, decision_id)
        if existing is not None:
            return existing

    now = utcnow_naive()
    row = models.StrategyTrade(
        execution_id=execution_id or new_execution_id(),
        arm_id=arm_id,
        decision_id=decision_id,
        mode=arm.mode,
        status=state.value,
        portfolio_context_hash=portfolio_context_hash,
        blocked_reason=blocked_reason,
        proposed_at=now,
        created_at=now,
        updated_at=now,
        **columns,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        raise StrategyLabError(
            f"decision {decision_id} already has a non-terminal execution; a "
            f"second broker placement for one decision is what the partial "
            f"unique index exists to prevent ({exc.orig})"
        ) from exc
    log.info(
        "strategy_trade_intended",
        execution_id=row.execution_id,
        arm_id=arm_id,
        decision_id=decision_id,
        mode=row.mode,
        status=row.status,
    )
    return row


def execution_row(session, execution_id: str):
    """One ``strategy_trades`` row by its idempotency key, or a refusal."""
    row = (
        session.query(models.StrategyTrade)
        .filter(models.StrategyTrade.execution_id == execution_id)
        .first()
    )
    if row is None:
        raise NotFound(f"execution {execution_id} does not exist")
    return row


def set_execution_state(session, execution_id: str, status, **columns):
    """Move one execution along the Spec Q §12 machine, or refuse the move."""
    row = execution_row(session, execution_id)
    target = require_transition(
        EXECUTION_TRANSITIONS, row.status, status, label=f"execution {execution_id}"
    )
    for key, value in columns.items():
        setattr(row, key, value)
    row.status = target.value
    row.updated_at = utcnow_naive()
    session.flush()
    log.info(
        "live_order_state_changed",
        execution_id=execution_id, status=target.value, mode=row.mode,
    )
    return row


def executions_for_arm(session, arm_id: int):
    """Every execution an arm has produced, oldest first."""
    return (
        session.query(models.StrategyTrade)
        .filter(models.StrategyTrade.arm_id == arm_id)
        .order_by(models.StrategyTrade.id)
        .all()
    )


def decisions_for_arm(session, arm_id: int, *, actions=None):
    """Every decision an arm has recorded, ordered by snapshot then ticker."""
    query = session.query(models.StrategyDecision).filter(
        models.StrategyDecision.arm_id == arm_id
    )
    if actions is not None:
        query = query.filter(models.StrategyDecision.action.in_(list(actions)))
    return query.order_by(
        models.StrategyDecision.snapshot_id, models.StrategyDecision.ticker
    ).all()


def strategy_version_for_arm(session, arm_id: int):
    """The ``strategy_versions`` row an arm runs. Rows stay behind this module."""
    arm = require_arm(session, arm_id)
    row = session.get(models.StrategyVersion, arm.strategy_version_id)
    if row is None:  # pragma: no cover - a foreign key makes this unreachable
        raise NotFound(f"arm {arm_id} references a strategy version that is gone")
    return row


def experiment_for_arm(session, arm_id: int):
    """The ``experiments`` row an arm belongs to."""
    arm = require_arm(session, arm_id)
    row = session.get(models.Experiment, arm.experiment_id)
    if row is None:  # pragma: no cover - a foreign key makes this unreachable
        raise NotFound(f"arm {arm_id} references an experiment that is gone")
    return row


def decision_row(session, decision_id: int):
    """One ``strategy_decisions`` row, or a refusal naming it."""
    row = session.get(models.StrategyDecision, decision_id)
    if row is None:
        raise NotFound(f"strategy decision {decision_id} does not exist")
    return row


def snapshot_row(session, snapshot_id: int):
    """One ``market_snapshots`` row, or a refusal naming it."""
    row = session.get(models.MarketSnapshot, snapshot_id)
    if row is None:
        raise NotFound(f"market snapshot {snapshot_id} does not exist")
    return row


# --------------------------------------------------------------------------- #
# The Phase 5 half of executions: reuse-on-race, the reservation, the blockers
# --------------------------------------------------------------------------- #
#
# PR 3 put `record_execution`, `set_execution_state` and `open_execution_for`
# above; PR 5 adds the four functions the Spec Q §12 live path needs on top of
# them, here rather than in a second module, because this file is the service
# boundary — "everything that writes an experiment table goes through here" —
# and a second writer is exactly how two workers end up with two placements.
#
# The *rules* they enforce live in `strategy_lab/execution.py`, which holds no
# session and cannot read a setting (Spec Q §12 invariant 11). This module
# imports those rules; the dependency never points the other way.


def open_execution(
    session,
    arm_id: int,
    decision_id: int,
    *,
    mode,
    portfolio_context_hash: str = "",
    **columns,
):
    """:func:`record_execution`, but a lost race returns the winner's row.

    `record_execution` raises when the partial unique index refuses a second
    non-terminal execution, which is right for the shadow runner: there, a
    collision means a bug in the caller's loop and should be loud. The live path
    wants the other half of Spec Q §12 invariant 6 — "retries and concurrent
    workers **reuse** or reject the existing `execution_id`" — because a retried
    owner approval must resolve to the one placement rather than fail.

    So the insert runs inside a SAVEPOINT: losing the race rolls back only the
    failed insert, never the caller's transaction, and the row the constraint
    protected is returned instead. The database is the arbiter either way; this
    only chooses what to do with its answer.

    ``mode`` is checked against the arm rather than written from the argument.
    An arm's mode is its identity (Spec Q §8), and the check exists so a caller
    that thinks it is executing a paper arm cannot quietly open a live one.
    """
    arm = require_arm(session, arm_id)
    mode = ExecutionMode(mode)
    if ExecutionMode(arm.mode) is not mode:
        raise ExecutionConflict(
            "mode_mismatch",
            f"arm {arm_id} runs in {arm.mode!r}; an execution was requested in "
            f"{mode.value!r}. The arm carries the mode (Spec Q §8, §12 "
            "invariant 11).",
        )

    existing = open_execution_for(session, decision_id)
    if existing is not None and ExecutionMode(existing.mode) is not mode:
        raise ExecutionConflict(
            "open_execution_mode_conflict",
            f"decision {decision_id} already has a non-terminal execution "
            f"{existing.execution_id} in {existing.mode!r}; it cannot also run "
            f"in {mode.value!r}.",
        )

    try:
        with session.begin_nested():
            return record_execution(
                session,
                arm_id,
                decision_id,
                portfolio_context_hash=portfolio_context_hash,
                **columns,
            )
    except (StrategyLabError, IntegrityError):
        winner = open_execution_for(session, decision_id)
        if winner is None:
            raise
        log.info(
            "strategy_execution_race_lost",
            decision_id=int(decision_id),
            winner=winner.execution_id,
        )
        return winner


def advance_execution(session, execution_id: str, status, *, reason: str = "", now=None, **columns):
    """:func:`set_execution_state` plus the lifecycle timestamps and the reason.

    Idempotent: re-asserting the state a row is already in updates the supplied
    columns and returns, which is how a retry behaves
    (:func:`strategy_lab.domain.require_transition` allows the no-op move).

    The reservation needs no bookkeeping here. Release *is* the terminal
    transition — see :data:`strategy_lab.execution.RESERVING_EXECUTION_STATES` —
    and a terminal state has no outgoing edge, so it happens once. What this
    does log is the release, so an audit can see where a notional went back.
    """
    moment = now or utcnow_naive()
    row = execution_row(session, execution_id)
    was = ExecutionState(row.status)
    target = ExecutionState(status)

    if reason:
        columns.setdefault("blocked_reason", reason[:80])
    if target is not was:
        stamp = {
            ExecutionState.OWNER_APPROVED: "approved_at",
            ExecutionState.SUBMITTED: "submitted_at",
            ExecutionState.FILLED: "filled_at",
            ExecutionState.PARTIALLY_FILLED: "filled_at",
            ExecutionState.CLOSED: "closed_at",
        }.get(target)
        if stamp and getattr(row, stamp, None) is None:
            columns.setdefault(stamp, moment)

    row = set_execution_state(session, execution_id, target, **columns)
    if target is not was and target in TERMINAL_EXECUTION_STATES and holds_reservation(was):
        log.info(
            "strategy_reservation_released",
            **redact({
                "execution_id": execution_id,
                "notional": float(row.notional or 0.0),
                "state": target.value,
                "reason": reason,
            }),
        )
    return row


def reserved_notional(session, *, mode, on_date=None) -> float:
    """Notional this mode currently holds reserved, optionally for one day.

    Counted from the status column, so an approval racing another one cannot
    overspend the cap: the first to reach ``risk_reserved`` is visible to the
    second the moment its transaction commits (Spec Q §12 invariant 6).
    """
    mode = ExecutionMode(mode).value
    query = (
        session.query(models.StrategyTrade)
        .filter(models.StrategyTrade.mode == mode)
        .filter(
            models.StrategyTrade.status.in_(
                [state.value for state in RESERVING_EXECUTION_STATES]
            )
        )
    )
    if on_date is not None:
        start = datetime(on_date.year, on_date.month, on_date.day)
        query = query.filter(
            models.StrategyTrade.created_at >= start,
            models.StrategyTrade.created_at < start + timedelta(days=1),
        )
    return sum(float(row.notional or 0.0) for row in query.all())


def blocking_executions(session):
    """Executions whose state blocks every further live entry, newest first."""
    return (
        session.query(models.StrategyTrade)
        .filter(
            models.StrategyTrade.status.in_(
                [state.value for state in BLOCKING_EXECUTION_STATES]
            )
        )
        .order_by(models.StrategyTrade.id.desc())
        .all()
    )


def resumable_executions(session, *, mode=None):
    """Non-terminal executions, oldest first — what a restart has to resolve."""
    query = session.query(models.StrategyTrade).filter(
        ~models.StrategyTrade.status.in_(
            [state.value for state in TERMINAL_EXECUTION_STATES]
        )
    )
    if mode is not None:
        query = query.filter(models.StrategyTrade.mode == ExecutionMode(mode).value)
    return query.order_by(models.StrategyTrade.id.asc()).all()
