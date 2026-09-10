"""Promotion, wired: the gates that need a deployment, and the owner's confirmation.

:mod:`strategy_lab.promotion` holds the part of a tier change that is a property
of the *data* — the bindings, the evidence, the ladder — and it cannot see a
feature flag, a kill switch or a broker, because ``strategy_lab/`` may not import
``config``, ``portfolio`` or ``execution``
(``tests/test_strategy_lab_import_graph.py``). That boundary is deliberate: the
module that says "execution mode is an immutable input, never inferred from a
global setting" must not be able to read a global setting.

This module is the other half. It can see all three, so it contributes the
refusals Spec Q §12 invariant 2 and the PR 6 brief require of a *live* promotion —
every feature flag, kill-switch clearance, the broker's declared exit capability,
and Phase 6's own live gates — and hands them to
:func:`strategy_lab.promotion.plan` as ``external_refusals``. A refusal from
either side blocks the confirmation identically, and the card shows both lists.

**The confirmation.** Spec Q §13: ``/promote_arm`` renders the source arm's
immutable version, its evidence snapshot, the warnings, the *proposed inactive
target arm* and the budget, and then requires a confirmation callback. Rendering
may create the inactive target arm — that places nothing, activates nothing and
is idempotent — and the confirmation is what activates it.

The pending confirmation is held **in this process and nowhere else**
(:class:`PendingPromotions`). That is a choice, not an omission: a promotion
confirmation that survived a restart would be a confirmation of a world that no
longer exists, and re-running one command is cheap. It is signed with
``portfolio.approvals.sign`` rather than a scheme of its own, so there is exactly
one HMAC in the codebase and one place to reason about it, and it is single-use,
expiring and owner-bound for the same reasons an execution approval is.

**A promotion is never an entry approval.** Confirming one appends a
``promotion_events`` row and activates an arm. Every live execution that arm then
proposes still needs its own signed, expiring, single-use owner callback through
Phase 6's approval path (Spec Q §13, Spec L §6.3), and
``tests/test_strategy_lab_promotion.py`` asserts a promoted live arm places
nothing until that second, separate approval.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from strategy_lab import promotion as pm
from utils.logger import get_logger
from utils.timeutils import utcnow_naive

log = get_logger("strategy_lab_promotion")

#: The confirmation callback prefix. Four characters, like Phase 6's, because it
#: shares Telegram's 64-byte ``callback_data`` budget.
CONFIRM_PREFIX = "slpr"
CANCEL_PREFIX = "slpx"

#: Characters of the hex digest the callback carries. Phase 6's reasoning applies
#: unchanged: 72 bits is far beyond brute force in the minutes a confirmation
#: lives, the full signature stays in this process, and it is one control of
#: four (owner binding, expiry, single use, signature).
SIGNATURE_PREFIX_CHARS = 18

DEFAULT_TTL_SECONDS = 300


class PromotionGateRefused(Exception):
    """A promotion that cannot even be planned (a bad argument, a missing row)."""


def _float(settings, name: str, default: float) -> float:
    try:
        return float(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


def _int(settings, name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


def floors(settings) -> pm.EvidenceFloors:
    """The two operational minimums, read from settings so the card prints them."""
    return pm.EvidenceFloors(
        matured=_int(settings, "strategy_lab_promotion_floor_shadow_matured", 100),
        closed=_int(settings, "strategy_lab_promotion_floor_paper_closed", 30),
    )


def target_risk_budget(settings, mode) -> float:
    """The budget a newly prepared arm is given at ``mode``.

    Live defaults to zero, and that is load-bearing rather than unfinished: a
    live champion with a zero budget sizes to nothing, so the owner has to set
    ``STRATEGY_LAB_LIVE_RISK_BUDGET`` as its own deliberate step (Spec Q §3 — the
    dollar budget is deployment configuration).
    """
    from strategy_lab.domain import ExecutionMode

    mode = ExecutionMode(mode)
    if mode is ExecutionMode.LIVE:
        return _float(settings, "strategy_lab_live_risk_budget", 0.0)
    if mode is ExecutionMode.PAPER:
        return _float(settings, "strategy_lab_paper_risk_budget", 0.005)
    return _float(settings, "strategy_lab_shadow_risk_budget", 0.01)


# --------------------------------------------------------------------------- #
# The deployment-side gates
# --------------------------------------------------------------------------- #


def external_refusals(session, settings, *, to_mode, adapters=None) -> tuple[str, ...]:
    """Every refusal that needs a flag, a switch or a broker to see.

    Ordered cheapest-first and *accumulated* rather than short-circuited: an
    owner looking at a blocked promotion card should see every reason at once,
    because fixing one of three and re-running is how an afternoon disappears.
    """
    from execution import lifecycle as p6
    from portfolio import killswitch
    from strategy_lab.domain import ExecutionMode
    from strategy_lab.execution import MODE_VENUES

    to_mode = ExecutionMode(to_mode)
    out: list[str] = []

    if not bool(getattr(settings, "strategy_lab_enabled", False)):
        out.append(
            "STRATEGY_LAB_ENABLED is false. Every tier requires every gate below "
            "it (Spec Q §14), so no tier change can be confirmed."
        )
    if to_mode is not ExecutionMode.SHADOW and not bool(
        getattr(settings, "phase6_execution_enabled", False)
    ):
        out.append(
            "PHASE6_EXECUTION_ENABLED is false, so there is no execution path a "
            f"{to_mode.value} arm could use."
        )
    if to_mode is ExecutionMode.PAPER and not bool(
        getattr(settings, "strategy_lab_paper_enabled", False)
    ):
        out.append(
            "STRATEGY_LAB_PAPER_ENABLED is false; a paper arm could be activated "
            "and would then refuse every execution. Turn the tier on first."
        )
    if to_mode is ExecutionMode.LIVE:
        if not bool(getattr(settings, "strategy_lab_live_enabled", False)):
            out.append(
                "STRATEGY_LAB_LIVE_ENABLED is false. Absence or invalidity of a "
                "flag never means live (Spec Q §12 invariant 1)."
            )
        refusal = p6.live_gate_refusal(settings)
        if refusal is not None:
            out.append(f"{refusal[0]}: {refusal[1]}")
        blocked = killswitch.entry_block(session)
        if blocked is not None:
            out.append(f"{blocked[0]}: {blocked[1]}")
        out.extend(
            _capability_refusals(adapters, venue=MODE_VENUES[ExecutionMode.LIVE])
        )
    elif to_mode is ExecutionMode.PAPER:
        blocked = killswitch.entry_block(session)
        if blocked is not None:
            out.append(f"{blocked[0]}: {blocked[1]}")
    return tuple(out)


def _capability_refusals(adapters, *, venue: str) -> list[str]:
    """Ask the adapter the arm will actually reach whether it can protect a position.

    This is PR 5's capability gate, asked at promotion time rather than only at
    placement time. The reason to ask twice is in Spec Q §12's closing paragraph:
    if Robinhood cannot provide a verifiable protective exit, Strategy Lab live
    entries are *impossible*, and an owner should learn that from the promotion
    card rather than from a refused order after they thought they had promoted.
    """
    from portfolio.capabilities import CapabilityRefused, OrderIntent, gate_intent

    adapter = (adapters or {}).get(venue)
    if adapter is None:
        return [
            f"no adapter is registered for the {venue!r} venue, so the exit "
            "capability cannot be verified. A capability that cannot be checked "
            "is treated as absent (Spec L §5)."
        ]
    getter = getattr(adapter, "capabilities", None)
    capabilities = getter() if callable(getter) else None
    if capabilities is None:
        return [
            f"the {venue!r} adapter declares no capabilities. An adapter that "
            "cannot say whether it can protect a position is treated as one that "
            "cannot (Spec L §5)."
        ]
    try:
        gate_intent(
            capabilities,
            OrderIntent(
                symbol="",
                side="buy",
                order_type="limit",
                requires_protective_exit=True,
                extended_hours=False,
            ),
        )
    except CapabilityRefused as exc:
        return [f"capability_refused: {exc.reason}"]
    return []


# --------------------------------------------------------------------------- #
# Preparing the target arm and finding the evidence
# --------------------------------------------------------------------------- #


def latest_evidence_id(session, arm_id: int) -> int | None:
    """The newest ``experiment_metric_snapshots`` row for an arm, or ``None``."""
    from strategy_lab import registry

    row = (
        session.query(registry.models.ExperimentMetricSnapshot)
        .filter(registry.models.ExperimentMetricSnapshot.arm_id == arm_id)
        .order_by(
            registry.models.ExperimentMetricSnapshot.evaluation_cutoff_utc.desc(),
            registry.models.ExperimentMetricSnapshot.id.desc(),
        )
        .first()
    )
    return row.id if row else None


def prepare_target(session, settings, source_arm_id: int, *, to_mode):
    """Create-or-return the inactive arm a tier change would activate.

    ``registry.create_arm`` already returns the existing inactive-or-active arm
    for the same experiment, strategy version and mode, so this is idempotent and
    rendering a card twice prepares one arm. It writes a row and **activates
    nothing**: an inactive arm is not in any tournament and reaches no broker.
    """
    from strategy_lab import registry
    from strategy_lab.domain import ExecutionMode

    to_mode = ExecutionMode(to_mode)
    source = registry.require_arm(session, source_arm_id)
    if ExecutionMode(source.mode) is to_mode:
        raise PromotionGateRefused(
            f"arm {source_arm_id} already runs in {to_mode.value}; a promotion "
            "event records a tier *change*."
        )
    experiment = registry.experiment_for_arm(session, source_arm_id)
    version = registry.strategy_version_for_arm(session, source_arm_id)
    return registry.create_arm(
        session,
        experiment.name,
        version.slug,
        version.version,
        to_mode,
        risk_budget=target_risk_budget(settings, to_mode),
        promoted_from_arm_id=source_arm_id,
    )


def build_request(
    session,
    settings,
    *,
    source_arm_id: int,
    to_mode,
    owner: str,
    reason: str,
    evidence_metric_snapshot_id: int | None = None,
) -> pm.PromotionRequest:
    """Turn ``/promote_arm <source> <tier>`` into a fully bound request."""
    from strategy_lab.domain import ExecutionMode

    to_mode = ExecutionMode(to_mode)
    target = prepare_target(session, settings, source_arm_id, to_mode=to_mode)
    evidence_id = evidence_metric_snapshot_id or latest_evidence_id(session, source_arm_id)
    if evidence_id is None:
        raise PromotionGateRefused(
            f"arm {source_arm_id} has no evidence snapshot. A tier change binds a "
            "stored `experiment_metric_snapshots` row; run the evaluator first "
            "(Spec Q §8)."
        )
    return pm.PromotionRequest(
        source_arm_id=source_arm_id,
        target_arm_id=target.id,
        evidence_metric_snapshot_id=evidence_id,
        requested_mode=to_mode,
        requested_risk_budget=float(target.risk_budget),
        owner=owner,
        reason=reason,
    )


# --------------------------------------------------------------------------- #
# Plan and confirm
# --------------------------------------------------------------------------- #


def plan(settings, request: pm.PromotionRequest, *, adapters=None, session=None) -> pm.PromotionPlan:
    """The owner's card, with both refusal lists filled in. Writes nothing."""
    if session is not None:
        return _plan(session, settings, request, adapters=adapters)
    from database.db import get_session

    with get_session() as owned:
        return _plan(owned, settings, request, adapters=adapters)


def _plan(session, settings, request, *, adapters):
    return pm.plan(
        session,
        request,
        floors=floors(settings),
        external_refusals=external_refusals(
            session, settings, to_mode=request.requested_mode, adapters=adapters
        ),
    )


def confirm(settings, request: pm.PromotionRequest, *, adapters=None) -> dict:
    """Append the owner's tier change, or refuse with every reason.

    The plan is recomputed inside :func:`strategy_lab.promotion.confirm`, and the
    deployment gates are recomputed here, so a kill switch engaged between the
    render and the tap refuses the tap.
    """
    from database.db import get_session

    with get_session() as session:
        event = pm.confirm(
            session,
            request,
            floors=floors(settings),
            external_refusals=external_refusals(
                session, settings, to_mode=request.requested_mode, adapters=adapters
            ),
        )
        payload = {
            "promotion_id": event.id,
            "kind": event.kind,
            "source_arm_id": event.source_arm_id,
            "target_arm_id": event.target_arm_id,
            "from_mode": event.from_mode,
            "to_mode": event.to_mode,
            "owner": event.owner,
            "new_risk_budget": float(event.new_risk_budget or 0.0),
        }
        session.commit()
    return payload


# --------------------------------------------------------------------------- #
# The pending confirmation
# --------------------------------------------------------------------------- #


@dataclass
class _Pending:
    request: pm.PromotionRequest
    owner_id: str
    nonce: str
    signature: str
    expires_at: datetime


class PendingPromotions:
    """In-process, single-use, expiring, owner-bound promotion confirmations.

    Not a table, and not a second HMAC: the signature comes from
    :func:`portfolio.approvals.sign`, and the state it signs over lives only
    here. A restart drops every pending confirmation, which is the safe
    direction — the owner re-runs ``/promote_arm`` against the world as it now is.
    """

    def __init__(self, settings):
        self.settings = settings
        self._pending: dict[int, _Pending] = {}
        self._seq = 0

    def _ttl(self) -> int:
        return max(
            30, _int(self.settings, "strategy_lab_promotion_ttl_seconds", DEFAULT_TTL_SECONDS)
        )

    def offer(self, request: pm.PromotionRequest, *, owner_id: str, now: datetime | None = None):
        """Register a confirmation and return ``(token, callback_data, expires_at)``."""
        from portfolio import approvals

        now = now or utcnow_naive()
        self._prune(now)
        self._seq += 1
        token = self._seq
        nonce = secrets.token_hex(16)
        expires_at = (now + timedelta(seconds=self._ttl())).replace(microsecond=0)
        signature = approvals.sign(
            proposal_uid=self._uid(token, request),
            nonce=nonce,
            expires_at=expires_at,
            owner_id=str(owner_id or ""),
            secret=approvals.signing_secret(self.settings),
        )
        self._pending[token] = _Pending(
            request=request,
            owner_id=str(owner_id or ""),
            nonce=nonce,
            signature=signature,
            expires_at=expires_at,
        )
        callback = f"{CONFIRM_PREFIX}:{token}:{signature[:SIGNATURE_PREFIX_CHARS]}"
        return token, callback, f"{CANCEL_PREFIX}:{token}:x", expires_at

    @staticmethod
    def _uid(token: int, request: pm.PromotionRequest) -> str:
        """Everything the confirmation binds, in one string.

        The target arm, the evidence, the mode and the budget are all in here, so
        a card signed against one plan cannot confirm another — the signature
        fails before any row is read.
        """
        return "\x1f".join(
            (
                "promotion",
                str(token),
                str(request.source_arm_id),
                str(request.target_arm_id),
                str(request.evidence_metric_snapshot_id),
                request.requested_mode.value,
                f"{request.requested_risk_budget:.10f}",
            )
        )

    def _prune(self, now: datetime) -> None:
        for token in [t for t, p in self._pending.items() if p.expires_at < now]:
            self._pending.pop(token, None)

    def parse(self, data: str) -> tuple[str, int, str]:
        parts = (data or "").split(":")
        if len(parts) != 3 or parts[0] not in (CONFIRM_PREFIX, CANCEL_PREFIX):
            raise PromotionGateRefused(f"unrecognised promotion callback {data!r}.")
        try:
            token = int(parts[1])
        except ValueError:
            raise PromotionGateRefused(
                f"promotion callback {data!r} carries no token."
            ) from None
        return ("confirm" if parts[0] == CONFIRM_PREFIX else "cancel"), token, parts[2]

    def cancel(self, token: int) -> bool:
        return self._pending.pop(token, None) is not None

    def take(self, token: int, *, presented: str, owner_id: str, now: datetime | None = None):
        """Consume one confirmation, or refuse. Single use even when it refuses late."""
        import hmac

        from portfolio import approvals

        now = now or utcnow_naive()
        pending = self._pending.get(token)
        if pending is None:
            raise PromotionGateRefused(
                "this confirmation is unknown, already used, or was dropped by a "
                "restart. Re-run /promote_arm; nothing was changed."
            )
        if str(owner_id or "") != pending.owner_id:
            raise PromotionGateRefused(
                "this confirmation is bound to a different owner (Spec Q §13)."
            )
        if now > pending.expires_at:
            self._pending.pop(token, None)
            raise PromotionGateRefused(
                f"this confirmation expired at {pending.expires_at.isoformat()}Z. "
                "The card described the world as it was; re-run /promote_arm."
            )
        presented = (presented or "").strip()
        expected = approvals.sign(
            proposal_uid=self._uid(token, pending.request),
            nonce=pending.nonce,
            expires_at=pending.expires_at,
            owner_id=pending.owner_id,
            secret=approvals.signing_secret(self.settings),
        )
        if (
            len(presented) < SIGNATURE_PREFIX_CHARS
            or not hmac.compare_digest(expected[: len(presented)], presented)
            or not hmac.compare_digest(expected, pending.signature)
        ):
            raise PromotionGateRefused("this promotion confirmation does not verify.")
        self._pending.pop(token, None)
        return pending.request
