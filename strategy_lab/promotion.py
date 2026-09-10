"""The promotion workflow: what an owner is shown, and what binds (Spec Q §8, §13).

PR 1 already holds the two halves a promotion is made of —
:func:`strategy_lab.domain.authorize_promotion`, which says whether a request is
*coherent*, and :func:`strategy_lab.registry.record_promotion`, which appends the
event and activates the target arm. What was missing is the step between them:
the thing an owner actually interacts with.

This module is that step, and it adds four rules the two halves could not:

**The request states what the owner believes they are approving.** A
:class:`PromotionRequest` carries a ``requested_mode`` and a
``requested_risk_budget`` alongside the arm ids, and both are checked against the
target arm's own immutable columns. Without that, the card an owner reads ("this
promotes momentum_v1 to *paper* at 0.5%") and the row that gets activated are two
independent facts that happen to agree — and a target arm edited between the
render and the confirmation would activate something the owner never saw.

**Evidence is not reusable.** One ``experiment_metric_snapshots`` row may
authorize exactly one target arm. :func:`evidence_binding_refusal` refuses a
second target outright, because "the evidence that justified paper now justifies
live" is precisely the reasoning Spec Q §8 forbids: each tier needs its own
evidence, collected in that tier.

**Evidence has to be complete.** :func:`evidence_completeness` checks the
snapshot for the fields Spec Q §10 requires beside any number — the cost model,
the benchmark, the uncertainty interval, and the sample counts against the tier's
operational floor — and whether the owner acknowledged the warnings. An
incomplete snapshot is not weaker evidence; it is not evidence.

**The system recommends; it never promotes.** :func:`recommendation` returns one
of three labels derived arithmetically from the counts and floors, and nothing in
this module writes anything until :func:`confirm` is called with an owner
identity and a reason. There is no scheduled job and no code path that reaches
:func:`confirm` without a human (Spec Q §3: promotion authority is owner-only).

Two things are deliberately *not* here, because they cannot be: the feature
flags, the kill switch, the broker's declared exit capability and the current
risk gates. Those need ``config``, ``portfolio`` and ``execution``, which this
package may not import (``tests/test_strategy_lab_import_graph.py``) — and that
is the right shape, not a limitation. They arrive as an
``external_refusals`` sequence from ``orchestrator/strategy_lab_promotion.py``,
which can see all three, and a refusal from either side blocks the confirmation
identically.

Like ``runner.py`` and ``shadow.py``, every function here takes a session and
holds none: the writes go through ``registry.py``, which stays the one module in
the package that knows what an ORM row looks like.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from strategy_lab import registry
from strategy_lab.domain import (
    ArmStatus,
    ExecutionMode,
    PromotionKind,
    PromotionRefused,
    authorize_promotion,
    tier_move,
)
from utils.logger import get_logger

log = get_logger("strategy_lab_promotion")


#: Recommendation labels. Three, and none of them is "promote": the strongest
#: thing this module will say is that the evidence is complete enough for an
#: owner to look at (Spec Q §10, "never select a winner from raw return alone").
READY_FOR_OWNER_REVIEW = "ready_for_owner_review"
INSUFFICIENT_EVIDENCE = "insufficient_evidence"
BLOCKED = "blocked"

#: Budgets compare as floats, so they compare with a tolerance. A tenth of a
#: basis point is far below any budget anyone would set and far above the error
#: a round trip through JSON or a float column introduces.
BUDGET_TOLERANCE = 1e-9


class PromotionWorkflowError(PromotionRefused):
    """A request that cannot be turned into a plan at all (a missing row)."""


@dataclass(frozen=True)
class PromotionRequest:
    """What the owner asked for, including what they believe they are approving.

    ``requested_mode`` and ``requested_risk_budget`` are the whole reason this
    object exists rather than four positional arguments: they are checked against
    the target arm's immutable columns, so a card rendered against one arm cannot
    be confirmed against a different one.
    """

    source_arm_id: int
    target_arm_id: int
    evidence_metric_snapshot_id: int
    requested_mode: ExecutionMode
    requested_risk_budget: float
    owner: str
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "requested_mode", ExecutionMode(self.requested_mode))
        object.__setattr__(self, "requested_risk_budget", float(self.requested_risk_budget))

    @property
    def is_live(self) -> bool:
        return self.requested_mode is ExecutionMode.LIVE

    def as_dict(self) -> dict:
        return {
            "source_arm_id": self.source_arm_id,
            "target_arm_id": self.target_arm_id,
            "evidence_metric_snapshot_id": self.evidence_metric_snapshot_id,
            "requested_mode": self.requested_mode.value,
            "requested_risk_budget": self.requested_risk_budget,
            "owner": self.owner,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class EvidenceCompleteness:
    """Whether one metric snapshot is complete enough to authorize a tier change."""

    metric_snapshot_id: int
    arm_id: int
    complete: bool
    missing: tuple[str, ...]
    warnings: tuple[str, ...]
    warnings_acknowledged: bool
    n_decisions: int
    n_matured: int
    n_closed: int
    floor_name: str
    floor_value: int
    floor_met: bool
    cutoff_utc: datetime | None = None

    def as_dict(self) -> dict:
        return {
            "metric_snapshot_id": self.metric_snapshot_id,
            "arm_id": self.arm_id,
            "complete": self.complete,
            "missing": list(self.missing),
            "warnings": list(self.warnings),
            "warnings_acknowledged": self.warnings_acknowledged,
            "n_decisions": self.n_decisions,
            "n_matured": self.n_matured,
            "n_closed": self.n_closed,
            "floor": {
                "name": self.floor_name,
                "value": self.floor_value,
                "met": self.floor_met,
            },
            "cutoff_utc": self.cutoff_utc.isoformat() if self.cutoff_utc else None,
        }


@dataclass(frozen=True)
class PromotionPlan:
    """Everything the owner is shown, and nothing written.

    ``refusals`` is the authoritative answer: an empty tuple means
    :func:`confirm` would proceed, and a non-empty one names every reason it
    would not. :func:`confirm` recomputes the plan rather than trusting a stored
    one, so a plan that goes stale between the render and the tap is refused at
    the tap.
    """

    request: PromotionRequest
    kind: PromotionKind
    from_mode: ExecutionMode
    to_mode: ExecutionMode
    source: dict
    target: dict
    evidence: EvidenceCompleteness
    refusals: tuple[str, ...] = ()
    external_refusals: tuple[str, ...] = ()
    recommendation: str = BLOCKED
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def confirmable(self) -> bool:
        return not self.refusals and not self.external_refusals

    def as_dict(self) -> dict:
        return {
            "request": self.request.as_dict(),
            "kind": self.kind.value,
            "from_mode": self.from_mode.value,
            "to_mode": self.to_mode.value,
            "source": dict(self.source),
            "target": dict(self.target),
            "evidence": self.evidence.as_dict(),
            "refusals": list(self.refusals),
            "external_refusals": list(self.external_refusals),
            "recommendation": self.recommendation,
            "notes": list(self.notes),
            "confirmable": self.confirmable,
        }


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #


def _floor_for(to_mode: ExecutionMode, floors) -> tuple[str, int, str]:
    """``(name, value, counter)`` — which count this tier's floor applies to.

    Spec Q §10's operational minimums are stated per tier: shadow evidence is
    counted in matured decisions, paper evidence in closed executions. Promoting
    *to* paper reads shadow evidence, so it is the matured floor that applies;
    promoting to live reads paper evidence and the closed floor applies.
    """
    if to_mode is ExecutionMode.LIVE:
        return (
            "paper_closed_executions",
            int(getattr(floors, "closed", 0) or 0),
            "n_closed",
        )
    return (
        "shadow_matured_decisions",
        int(getattr(floors, "matured", 0) or 0),
        "n_matured",
    )


@dataclass(frozen=True)
class EvidenceFloors:
    """The two operational minimums, passed in so they are displayed and testable."""

    matured: int = 100
    closed: int = 30


def _loads(blob: str, default):
    try:
        value = json.loads(blob or "")
    except (TypeError, ValueError):
        return default
    return value if value or value == 0 else default


def evidence_completeness(
    session,
    metric_snapshot_id: int,
    *,
    to_mode: ExecutionMode,
    floors: EvidenceFloors | None = None,
) -> EvidenceCompleteness:
    """Check one ``experiment_metric_snapshots`` row against Spec Q §10.

    "Complete" is not a judgement about the numbers; it is the presence of the
    things §10 says a number may never be shown without — the sample size, the
    cost model the return was computed after, the benchmark it is relative to,
    and an uncertainty interval — plus the owner's acknowledgement of the
    warnings, which Spec Q §8 requires for a tier change.
    """
    floors = floors or EvidenceFloors()
    to_mode = ExecutionMode(to_mode)
    row = session.get(registry.models.ExperimentMetricSnapshot, metric_snapshot_id)
    if row is None:
        raise PromotionWorkflowError(
            f"metric snapshot {metric_snapshot_id} does not exist; a tier change "
            "binds a stored evidence snapshot, never a figure quoted at it"
        )

    warnings = tuple(_loads(row.warnings_json, []) or ())
    missing: list[str] = []
    if not _loads(row.cost_assumptions_json, {}):
        missing.append("cost_assumptions")
    if not (row.benchmark or "").strip():
        missing.append("benchmark")
    if not _loads(row.uncertainty_json, {}):
        missing.append("uncertainty")
    if not _loads(row.metrics_json, {}):
        missing.append("metrics")
    if int(row.n_decisions or 0) <= 0:
        missing.append("n_decisions")
    if not (row.content_hash or "").strip():
        missing.append("content_hash")

    floor_name, floor_value, counter = _floor_for(to_mode, floors)
    count = int(getattr(row, counter, 0) or 0)
    return EvidenceCompleteness(
        metric_snapshot_id=row.id,
        arm_id=row.arm_id,
        complete=not missing,
        missing=tuple(missing),
        warnings=warnings,
        warnings_acknowledged=bool(row.warnings_acknowledged),
        n_decisions=int(row.n_decisions or 0),
        n_matured=int(row.n_matured or 0),
        n_closed=int(row.n_closed or 0),
        floor_name=floor_name,
        floor_value=floor_value,
        floor_met=count >= floor_value,
        cutoff_utc=row.evaluation_cutoff_utc,
    )


def evidence_binding_refusal(session, metric_snapshot_id: int, target_arm_id: int) -> str | None:
    """Refuse evidence that already authorized a *different* target arm.

    Spec Q §8: evidence collected by one arm in one tier authorizes one tier
    change. Re-presenting the paper arm's snapshot to promote a second target —
    or to re-promote the same target after a demotion — would let one body of
    evidence justify an unbounded amount of capital. The same ``(evidence,
    target)`` pair is *not* refused here: that case is idempotency, and the
    target's own ``inactive`` requirement is what stops a double activation.
    """
    rows = (
        session.query(registry.models.PromotionEvent)
        .filter(
            registry.models.PromotionEvent.evidence_metric_snapshot_id
            == metric_snapshot_id
        )
        .order_by(registry.models.PromotionEvent.id)
        .all()
    )
    for row in rows:
        if row.target_arm_id != target_arm_id:
            return (
                f"evidence snapshot {metric_snapshot_id} already authorized arm "
                f"{row.target_arm_id} (promotion_event {row.id}); one evidence "
                "snapshot authorizes one tier change, and each tier needs "
                "evidence collected in that tier (Spec Q §8)"
            )
        return (
            f"evidence snapshot {metric_snapshot_id} has already been used to "
            f"promote arm {row.target_arm_id} (promotion_event {row.id}). A "
            "second activation of the same arm needs fresh evidence from its own "
            "tier, not the snapshot that put it there."
        )
    return None


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #


def _arm_payload(session, arm_id: int) -> dict:
    row = registry.require_arm(session, arm_id)
    version = registry.strategy_version_for_arm(session, arm_id)
    experiment = registry.experiment_for_arm(session, arm_id)
    return {
        "arm_id": row.id,
        "experiment": experiment.name if experiment else "",
        "slug": version.slug,
        "version": version.version,
        "strategy_version_id": row.strategy_version_id,
        "mode": row.mode,
        "status": row.status,
        "risk_budget": float(row.risk_budget),
        "promoted_from_arm_id": row.promoted_from_arm_id,
    }


def _binding_refusals(request: PromotionRequest, source: dict, target: dict) -> list[str]:
    """The two bindings the domain layer cannot check: mode and budget as *requested*."""
    refusals: list[str] = []
    if ExecutionMode(target["mode"]) is not request.requested_mode:
        refusals.append(
            f"arm {target['arm_id']} runs in {target['mode']!r}, but "
            f"{request.requested_mode.value!r} was requested. An arm's mode is "
            "immutable; confirming this would activate a tier the owner did not "
            "authorize (Spec Q §8)"
        )
    if abs(float(target["risk_budget"]) - request.requested_risk_budget) > BUDGET_TOLERANCE:
        refusals.append(
            f"arm {target['arm_id']} carries risk budget "
            f"{float(target['risk_budget']):.6f}, but "
            f"{request.requested_risk_budget:.6f} was requested. The approved "
            "budget is part of the authorization, so a budget changed after the "
            "card was rendered invalidates it (Spec Q §12 invariant 2)"
        )
    return refusals


def recommendation(evidence: EvidenceCompleteness, refusals) -> str:
    """A label, computed arithmetically from the counts and the floor.

    Never "promote". The system may recommend that an owner *look*; Spec Q §3
    keeps the decision with them, and §10 forbids selecting a winner from a
    return figure.
    """
    if refusals:
        return BLOCKED
    if not evidence.complete or not evidence.floor_met:
        return INSUFFICIENT_EVIDENCE
    return READY_FOR_OWNER_REVIEW


def plan(
    session,
    request: PromotionRequest,
    *,
    floors: EvidenceFloors | None = None,
    external_refusals=(),
) -> PromotionPlan:
    """Assemble the owner's card. Writes nothing, and never raises on a refusal.

    A missing row still raises — a plan for an arm that does not exist is not a
    refusal, it is a bad reference. Everything else lands in ``refusals`` so the
    card can show *all* the reasons at once rather than the first one.
    """
    source = _arm_payload(session, request.source_arm_id)
    target = _arm_payload(session, request.target_arm_id)
    evidence = evidence_completeness(
        session,
        request.evidence_metric_snapshot_id,
        to_mode=request.requested_mode,
        floors=floors,
    )

    from_mode = ExecutionMode(source["mode"])
    to_mode = ExecutionMode(target["mode"])
    refusals: list[str] = []
    try:
        kind = tier_move(from_mode, to_mode)
    except PromotionRefused as exc:
        kind = PromotionKind.PROMOTION
        refusals.append(str(exc))

    # The domain's own bindings: distinct arms, evidence belongs to the source,
    # shared immutable strategy version, inactive target, warnings acknowledged.
    try:
        authorize_promotion(
            registry.arm_facts(session, request.source_arm_id),
            registry.arm_facts(session, request.target_arm_id),
            registry.evidence_facts(session, request.evidence_metric_snapshot_id),
            owner=request.owner,
            reason=request.reason,
        )
    except PromotionRefused as exc:
        refusals.append(str(exc))

    refusals.extend(_binding_refusals(request, source, target))
    reuse = evidence_binding_refusal(
        session, request.evidence_metric_snapshot_id, request.target_arm_id
    )
    if reuse:
        refusals.append(reuse)
    if not evidence.complete:
        refusals.append(
            "the evidence snapshot is incomplete: "
            + ", ".join(evidence.missing)
            + ". Spec Q §10 forbids showing a result without its sample size, "
            "cost model, benchmark and uncertainty, and an incomplete snapshot "
            "is not weaker evidence — it is not evidence"
        )
    if not evidence.floor_met:
        refusals.append(
            f"the {evidence.floor_name} floor is {evidence.floor_value}; this "
            f"snapshot has n_matured={evidence.n_matured}, "
            f"n_closed={evidence.n_closed}. Spec Q §10's operational minimum for "
            "this tier is not met"
        )

    notes: list[str] = [
        "Promotion is not entry approval: every proposed live execution still "
        "needs its own signed, expiring, single-use owner callback (Spec Q §13).",
    ]
    if to_mode is ExecutionMode.LIVE:
        notes.append(
            "Live activation replaces the one global champion atomically, or "
            "leaves the prior champion exactly as it was (Spec Q §3, §8)."
        )
    if evidence.warnings and not evidence.warnings_acknowledged:
        notes.append(
            "Acknowledge the evidence warnings before confirming: "
            + ", ".join(evidence.warnings)
        )

    external = tuple(str(item) for item in (external_refusals or ()) if str(item).strip())
    return PromotionPlan(
        request=request,
        kind=kind,
        from_mode=from_mode,
        to_mode=to_mode,
        source=source,
        target=target,
        evidence=evidence,
        refusals=tuple(refusals),
        external_refusals=external,
        recommendation=recommendation(evidence, tuple(refusals) + external),
        notes=tuple(notes),
    )


# --------------------------------------------------------------------------- #
# The write
# --------------------------------------------------------------------------- #


def confirm(
    session,
    request: PromotionRequest,
    *,
    floors: EvidenceFloors | None = None,
    external_refusals=(),
):
    """Append the owner's audited tier change, or refuse. Nothing else calls this.

    The plan is recomputed here rather than passed in: the gap between rendering
    a card and tapping it is exactly where an arm gets activated by something
    else, a budget gets edited, or a kill switch gets engaged, and a confirmation
    that trusted a stored plan would be a confirmation of the world as it was.

    A demotion additionally stands the *source* arm down, which
    ``registry.record_promotion`` does not do and should not: promoting does not
    imply anything about the arm you promoted from, but demoting does — leaving
    the demoted arm active would mean the tier change changed nothing.
    """
    proposed = plan(
        session, request, floors=floors, external_refusals=external_refusals
    )
    if not proposed.confirmable:
        raise PromotionRefused(
            "; ".join(proposed.refusals + proposed.external_refusals)
        )

    event = registry.record_promotion(
        session,
        request.source_arm_id,
        request.target_arm_id,
        request.evidence_metric_snapshot_id,
        owner=request.owner,
        reason=request.reason,
        activate=True,
    )
    if proposed.kind is PromotionKind.DEMOTION:
        source = registry.require_arm(session, request.source_arm_id)
        if ArmStatus(source.status) is ArmStatus.ACTIVE:
            registry.set_arm_status(session, source.id, ArmStatus.PAUSED)
    log.info(
        "strategy_promotion_confirmed",
        promotion_id=event.id,
        kind=proposed.kind.value,
        source_arm_id=request.source_arm_id,
        target_arm_id=request.target_arm_id,
        from_mode=proposed.from_mode.value,
        to_mode=proposed.to_mode.value,
        owner=request.owner,
    )
    return event


def promotion_history(session, *, limit: int = 20) -> tuple[dict, ...]:
    """The append-only audit trail, newest first. A read, for the operator card."""
    rows = (
        session.query(registry.models.PromotionEvent)
        .order_by(registry.models.PromotionEvent.id.desc())
        .limit(max(1, int(limit)))
        .all()
    )
    return tuple(
        {
            "promotion_id": row.id,
            "kind": row.kind,
            "source_arm_id": row.source_arm_id,
            "target_arm_id": row.target_arm_id,
            "from_mode": row.from_mode,
            "to_mode": row.to_mode,
            "owner": row.owner,
            "reason": row.reason,
            "evidence_metric_snapshot_id": row.evidence_metric_snapshot_id,
            "previous_risk_budget": float(row.previous_risk_budget or 0.0),
            "new_risk_budget": float(row.new_risk_budget or 0.0),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    )
