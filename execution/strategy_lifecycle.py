"""Strategy Lab arms on Phase 6's execution service (Spec Q §12).

This is the wiring, and it is deliberately thin. Everything that decides
*whether* an execution may proceed lives in :mod:`strategy_lab.execution` — the
§12 state machine, the reservation, the mode-to-venue binding — and everything
that talks to a broker lives in :class:`execution.lifecycle.ExecutionService`.
Neither of those is duplicated here. What is here is the join:

* :meth:`StrategyExecutionService.propose` runs the gates that must happen
  **before** an approval card exists — the adapter binding, the kill switch and
  its new execution-block reasons, the broker's declared capabilities, the live
  promotion — and only then asks Phase 6's ``create_proposal`` for a card.
* :meth:`StrategyExecutionService.on_approval` is Phase 6's own ``on_approval``
  with an observer attached; the observer maps each Phase 6 state change onto
  the §12 machine, so the row a restart resumes from is written as the
  placement happens rather than inferred afterwards.
* :meth:`StrategyExecutionService.resume` and :meth:`reconcile` are what make
  the persistence worth having: after a crash, every non-terminal row is
  resolved from the broker's own answer, and **no new entry order is ever
  placed** by either — protection may be re-placed, an entry may not.

Every ``strategy_trades`` write goes through ``strategy_lab/registry.py``, the
package's service boundary and PR 3's writer for the same table. Nothing here
opens a second one: ``registry.open_execution`` and
``registry.advance_execution`` are the only two functions this module uses to
change a row, and the rules they enforce come from ``strategy_lab/execution.py``,
which is pure and cannot reach a session or a setting at all.

Three properties are worth stating because they are easy to lose:

**The reservation is persisted before the external order call.** Phase 6
notifies ``approved`` after the transaction that recorded the fresh size and
before ``_place_and_protect`` runs; the observer turns that into
``owner_approved -> risk_reserved`` in its own committed transaction. So the
notional is on disk, visible to a concurrent worker, before anything leaves the
process (Spec Q §12 invariant 6).

**An unknown outcome is never guessed.** Phase 6 hands ``placement_unknown``
straight through; the §12 machine moves ``risk_reserved -> submitted ->
placement_unknown`` (we did submit — the *response* was ambiguous) and stops
there. The only way out is ``reconciliation_required``, and the only thing that
resolves that is the broker's answer to "does an order with this ref_id exist".

**Shadow cannot reach here at all.** :func:`strategy_lab.execution.bind_adapter`
raises before an adapter is selected, and :meth:`propose` binds before it does
anything else. There is no code path in this module that reaches a broker
without having bound one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from database.models import Proposal
from execution import lifecycle as p6
from portfolio import killswitch
from portfolio import proposals as proposals_mod
from portfolio.capabilities import CapabilityRefused, OrderIntent, gate_intent
from portfolio.paging import log_pager
from strategy_lab import execution as slx
from strategy_lab import registry
from strategy_lab.domain import (
    TERMINAL_EXECUTION_STATES,
    ExecutionMode,
    ExecutionState,
    InvalidTransition,
)
from tracking import position_reconciliation as recon
from utils.logger import get_logger
from utils.timeutils import utcnow_naive

log = get_logger("strategy_lifecycle")

#: Page events. Stable strings, matching the Spec Q §17 vocabulary.
ARM_EXECUTION_BLOCKED = "strategy_execution_blocked"
ARM_PLACEMENT_UNKNOWN = "strategy_placement_unknown"
ARM_PROTECTION_FAILED = "live_protection_failed"
ARM_RECONCILIATION_MISMATCH = "broker_reconciliation_mismatch"
ARM_RECONCILIATION_RESOLVED = "broker_reconciliation_resolved"
ARM_UNSUPPORTED_FLOW = "strategy_execution_unsupported_flow"

#: Phase 6 event -> the §12 states it implies, in order. A tuple because two of
#: them are two hops: Phase 6 goes straight from "approved" to placing, and from
#: "submitted" to a fill, while §12 names the intermediate states that a restart
#: has to be able to resume from.
_OBSERVER_STATES: Mapping[str, tuple[ExecutionState, ...]] = {
    p6.ON_RISK_REJECTED: (ExecutionState.RISK_REJECTED,),
    p6.ON_APPROVED: (ExecutionState.OWNER_APPROVED, ExecutionState.RISK_RESERVED),
    p6.ON_REVIEW_REJECTED: (ExecutionState.REVIEW_REJECTED,),
    p6.ON_SUBMITTED: (ExecutionState.SUBMITTED,),
    # An outright placement failure is the verified-no-order case: the broker
    # said no and returned nothing to reconcile. Terminal, releases (§12 inv 12).
    p6.ON_PLACEMENT_FAILED: (ExecutionState.FAILED_NO_ORDER,),
    # An ambiguous one is not. We *did* submit; the response was unreadable.
    p6.ON_PLACEMENT_UNKNOWN: (ExecutionState.SUBMITTED, ExecutionState.PLACEMENT_UNKNOWN),
    p6.ON_ACCEPTED: (ExecutionState.ACCEPTED,),
    p6.ON_PARTIALLY_FILLED: (ExecutionState.ACCEPTED, ExecutionState.PARTIALLY_FILLED),
    p6.ON_FILLED: (ExecutionState.ACCEPTED, ExecutionState.FILLED),
    p6.ON_PROTECTION_PENDING: (ExecutionState.PROTECTION_PENDING,),
    p6.ON_PROTECTED: (ExecutionState.PROTECTED,),
    p6.ON_PROTECTION_FAILED: (ExecutionState.PROTECTION_FAILED,),
    p6.ON_STOP_REPLACED: (ExecutionState.PROTECTED,),
}


def _execution_or_none(session, execution_id: str):
    """The row for ``execution_id``, or ``None``. A read, so it stays local."""
    if not execution_id:
        return None
    try:
        return registry.execution_row(session, execution_id)
    except registry.NotFound:
        return None


class ArmExecutionRefused(Exception):
    """A refusal with a stable code, raised before anything external happens."""

    def __init__(self, code: str, message: str, execution_id: str = ""):
        self.code = code
        self.message = message
        self.execution_id = execution_id
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ArmExecutionRequest:
    """What an arm wants executed. The mode is carried, never inferred.

    ``entry``, ``stop`` and ``risk_fraction`` come from the strategy's decision;
    the *quantity* does not exist here and cannot (Spec L §6.6) — code computes
    it from these three at approval, against fresh state.
    """

    arm_id: int
    decision_id: int
    mode: ExecutionMode
    ticker: str
    entry: float
    stop: float
    risk_fraction: float
    cohort_answer_id: str = ""
    expected_hold_sessions: int | None = None
    target1_price: float | None = None
    target2_price: float | None = None


@dataclass
class ArmExecutionCard:
    """The result of :meth:`StrategyExecutionService.propose`."""

    execution_id: str
    status: str
    proposal_id: int | None = None
    approval_signature: str = ""
    blocked_reason: str = ""
    message: str = ""

    def as_dict(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "status": self.status,
            "proposal_id": self.proposal_id,
            "blocked_reason": self.blocked_reason,
            "message": self.message,
        }


@dataclass
class ResumeAction:
    execution_id: str
    was: str
    now: str
    action: str
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "was": self.was,
            "now": self.now,
            "action": self.action,
            **slx.redact(self.detail),
        }


class StrategyExecutionService:
    """The explicit-mode entry point for Strategy Lab arms (Spec Q §12 inv 11).

    ``adapters`` maps a **venue label** to a broker, not a mode to a broker: the
    mode chooses the venue (:data:`strategy_lab.execution.MODE_VENUES`) and the
    venue chooses the adapter, so registering the live Robinhood adapter under
    ``alpaca_paper`` is a wiring error that fails loudly at bind time rather
    than a paper arm quietly reaching live capital.
    """

    def __init__(
        self,
        *,
        session_factory,
        settings,
        adapters: Mapping[str, Any],
        pager=None,
        resolver=None,
        owner_id: str = "",
    ):
        self.session_factory = session_factory
        self.settings = settings
        self.adapters = dict(adapters or {})
        self.pager = pager or log_pager
        self.resolver = resolver
        self.owner_id = owner_id

    # -- adapter binding ----------------------------------------------------

    def _bind(self, arm, mode) -> slx.AdapterBinding:
        """Bind the one adapter this arm's mode may reach. Shadow raises."""
        mode = ExecutionMode(mode)
        venue = slx.MODE_VENUES.get(mode)
        if venue is None:
            raise slx.ShadowReachedExecution(f"arm {getattr(arm, 'id', '?')}")
        return slx.bind_adapter(
            arm_mode=arm.mode,
            requested_mode=mode,
            venue=venue,
            adapter=self.adapters.get(venue),
        )

    def _service(self, binding: slx.AdapterBinding) -> p6.ExecutionService:
        return p6.ExecutionService(
            session_factory=self.session_factory,
            broker=binding.adapter,
            settings=self.settings,
            pager=self.pager,
            resolver=self.resolver,
            observer=self._observer,
        )

    # -- the gates that must happen before a card exists --------------------

    def _live_authorization(self, session, arm) -> tuple[str, str] | None:
        """Everything §12 invariant 2 requires of a *live* arm, or a refusal.

        The promotion event itself was validated when it was recorded (Spec Q §8
        binds source arm, evidence snapshot, target arm, shared version, mode and
        budget, and refuses anything else); what is checked here is that one
        exists for *this* arm and that this arm is still the single globally
        active live champion. A promotion is not a standing licence: an arm that
        has since been paused, retired, or replaced as champion is not live.
        """
        refusal = p6.live_gate_refusal(self.settings)
        if refusal is not None:
            return refusal
        if (arm.status or "") != "active":
            return (
                "live_arm_not_active",
                f"arm {arm.id} is {arm.status!r}, not 'active'. Only the active "
                "live champion may place a live entry (Spec Q §12 invariant 2).",
            )
        champion = registry.active_live_arm(session)
        if champion is None or champion.id != arm.id:
            return (
                "not_the_live_champion",
                f"arm {arm.id} is not the single globally active live arm"
                + (f" (that is arm {champion.id})" if champion else "")
                + ". Live capital runs one champion (Spec Q §3, §12 invariant 2).",
            )
        if not registry.promotions_for(session, arm.id):
            return (
                "no_promotion_event",
                f"arm {arm.id} carries no owner promotion_event. A live arm is "
                "authorized by an audited owner decision that binds its "
                "evidence, never by a flag (Spec Q §8, §12 invariant 2).",
            )
        return None

    def _capability_refusal(self, binding: slx.AdapterBinding) -> tuple[str, str] | None:
        """Run :func:`portfolio.capabilities.gate_intent` on the *adapter*.

        ``propose_order`` already gates on the capabilities recorded for the
        account at the daily probe. This gate is the other half and it is not
        redundant: it asks the adapter the arm will actually reach whether it
        can enforce the exit policy, and it asks **before** an order is formed
        (Spec L §5, Spec Q §12 invariant 4). If Robinhood's declaration ever
        loses ``can_place_standalone_gtc_stop``, every Strategy Lab live entry
        becomes impossible here, with no flag to flip and no manual-exit
        fallback — which is exactly what Spec Q §12's closing paragraph requires.
        """
        capabilities = None
        getter = getattr(binding.adapter, "capabilities", None)
        if callable(getter):
            capabilities = getter()
        if capabilities is None:
            return (
                "capabilities_undeclared",
                f"the {binding.venue!r} adapter declares no capabilities. An "
                "adapter that cannot say whether it can protect a position is "
                "treated as one that cannot (Spec L §5).",
            )
        try:
            gate_intent(
                capabilities,
                OrderIntent(
                    symbol="",
                    side="buy",
                    order_type=self._entry_order_type(),
                    requires_protective_exit=True,
                    extended_hours=False,
                ),
            )
        except CapabilityRefused as exc:
            return ("capability_refused", exc.reason)
        return None

    def _entry_order_type(self) -> str:
        raw = str(getattr(self.settings, "robinhood_order_type", "limit") or "limit").lower()
        return "market" if raw == "market" else "limit"

    # -- propose ------------------------------------------------------------

    def propose(self, request: ArmExecutionRequest, *, now: datetime | None = None) -> ArmExecutionCard:
        """Open the execution, run every pre-placement gate, and mint the card.

        The order matters. The ``execution_id`` is committed *first*, before any
        gate and long before any reservation, so that a crash anywhere below
        leaves a row the resume pass can resolve. A gate that then refuses moves
        that row to a terminal state, which releases nothing because nothing was
        reserved yet — and the row remains as the audit trail for why the arm
        did not trade.
        """
        now = now or utcnow_naive()
        if not bool(getattr(self.settings, "phase6_execution_enabled", False)):
            raise ArmExecutionRefused(
                "phase6_disabled",
                "PHASE6_EXECUTION_ENABLED is false; no execution path exists.",
            )

        with self.session_factory() as session:
            arm = registry.require_arm(session, request.arm_id)
            binding = self._bind(arm, request.mode)

            # The portfolio context is read *before* the row is opened, so the
            # attempt is identified by the book it was judged against — which is
            # the idempotency key `registry.record_execution` uses, and what
            # makes "the same decision, re-proposed against an unchanged book"
            # resolve to one execution rather than two (Spec Q §8).
            context_hash = proposals_mod.read_context(
                session, now=now, symbol=request.ticker
            ).context_hash
            trade = registry.open_execution(
                session,
                request.arm_id,
                request.decision_id,
                mode=binding.mode,
                portfolio_context_hash=context_hash,
                intended_entry_price=request.entry,
                stop_price=request.stop,
                target1_price=request.target1_price,
                target2_price=request.target2_price,
            )
            session.commit()

            if trade.status != ExecutionState.PROPOSED.value:
                # An earlier attempt resolved to this row. Idempotent by
                # construction: hand it back rather than starting a second.
                # Two shapes, and the message says which — a non-terminal row is
                # in flight and holds the decision's one open slot; a terminal
                # one is `registry.record_execution`'s
                # `(decision, portfolio_context_hash)` rule, which means this
                # decision was already settled against *this* book. Re-proposing
                # it needs a genuinely different portfolio context, which is what
                # makes "the owner cancelled this and it did not come straight
                # back" true (Spec Q §8).
                settled = trade.status in {s.value for s in TERMINAL_EXECUTION_STATES}
                return ArmExecutionCard(
                    execution_id=trade.execution_id,
                    status=trade.status,
                    proposal_id=self._proposal_id_for(session, trade.execution_id),
                    blocked_reason=(trade.blocked_reason or "") if settled else "",
                    message=(
                        f"this decision already has a settled execution ({trade.status}) "
                        "against this portfolio context; nothing was placed."
                        if settled
                        else "an execution for this decision is already in flight."
                    ),
                )

            refusal = killswitch.entry_block(session)
            if refusal is None:
                refusal = self._capability_refusal(binding)
            if refusal is None and binding.is_live:
                refusal = self._live_authorization(session, arm)
            if refusal is not None:
                return self._refuse(session, trade, refusal, now=now)

            row = proposals_mod.create_proposal(
                session,
                ticker=request.ticker,
                entry=request.entry,
                stop=request.stop,
                risk_fraction=request.risk_fraction,
                cohort_answer_id=request.cohort_answer_id,
                expected_hold_sessions=request.expected_hold_sessions,
                settings=self.settings,
                requester_token_label=f"arm:{arm.id}",
                owner_id=self.owner_id,
                now=now,
                resolver=self.resolver,
            )
            row.execution_id = trade.execution_id
            # The arm's mode is the record, not the global setting the proposal
            # writer defaulted to (Spec Q §12 invariant 11).
            row.execution_mode = binding.mode.value
            trade.quantity = float(row.quantity or 0)
            trade.notional = float(row.notional or 0.0)
            # `portfolio_context_hash` is deliberately **not** rewritten from the
            # proposal. It is the row's identity key (PR 3: an attempt is
            # `(decision, portfolio_context_hash)`), and rewriting it after the
            # fact would silently change what a retry resolves to. The two are
            # read from the same session at the same instant for the same symbol
            # and should agree; a disagreement is a defect worth seeing rather
            # than a value worth adopting.
            if (row.portfolio_context_hash or "") != trade.portfolio_context_hash:
                log.warning(
                    "strategy_execution_context_hash_drift",
                    execution_id=trade.execution_id,
                    proposal_id=row.id,
                )
            session.flush()

            if row.status == "risk_rejected":
                card = self._refuse(
                    session,
                    trade,
                    (row.rejection_code or "risk_rejected", row.rejection_reason or ""),
                    now=now,
                    proposal_id=row.id,
                )
                session.commit()
                return card

            session.commit()
            return ArmExecutionCard(
                execution_id=trade.execution_id,
                status=trade.status,
                proposal_id=row.id,
                approval_signature=row.approval_signature or "",
                message="proposed; awaiting the owner's single-use approval.",
            )

    def _refuse(self, session, trade, refusal, *, now, proposal_id: int | None = None) -> ArmExecutionCard:
        code, reason = refusal
        registry.advance_execution(
            session, trade.execution_id, ExecutionState.RISK_REJECTED, reason=code, now=now
        )
        session.commit()
        self.pager(
            ARM_EXECUTION_BLOCKED,
            slx.redact({
                "execution_id": trade.execution_id,
                "arm_id": trade.arm_id,
                "reason_code": code,
                "reason": reason,
                "recovery": (
                    "No order was placed and nothing is reserved. Clear the "
                    "condition named above — release the kill switch, resolve "
                    "the blocking execution, or record the promotion — then let "
                    "the arm propose again on its next decision."
                ),
            }),
        )
        return ArmExecutionCard(
            execution_id=trade.execution_id,
            status=trade.status,
            proposal_id=proposal_id,
            blocked_reason=code,
            message=reason,
        )

    @staticmethod
    def _proposal_id_for(session, execution_id: str) -> int | None:
        row = (
            session.query(Proposal)
            .filter(Proposal.execution_id == execution_id)
            .order_by(Proposal.id.desc())
            .first()
        )
        return row.id if row else None

    # -- approval -----------------------------------------------------------

    def on_approval(self, *, execution_id: str, presented_signature: str, owner_id: str, now=None):
        """Phase 6's ``on_approval``, with the §12 machine attached.

        Nothing about the placement is reimplemented here: the approval check,
        the fresh risk re-evaluation, the review, the entry, the fill poll, the
        stop and its read-back are all Phase 6's. What this adds is the binding
        (so the mode selects the adapter rather than the global setting) and the
        observer (so every hop lands on ``strategy_trades`` as it happens).
        """
        with self.session_factory() as session:
            trade = registry.execution_row(session, execution_id)
            arm = registry.require_arm(session, trade.arm_id)
            binding = self._bind(arm, trade.mode)
            proposal_id = self._proposal_id_for(session, execution_id)
            if proposal_id is None:
                raise ArmExecutionRefused(
                    "no_proposal",
                    f"execution {execution_id} has no proposal to approve.",
                    execution_id,
                )
        return self._service(binding).on_approval(
            proposal_id=proposal_id,
            presented_signature=presented_signature,
            owner_id=owner_id,
            now=now,
        )

    # -- the observer -------------------------------------------------------

    def _observer(self, event: str, proposal: Proposal, detail: dict) -> None:
        """Map one Phase 6 state change onto the §12 machine.

        Runs in its own session and commits, because the point of the write is
        that it is durable *before* the next broker call. A proposal with no
        ``execution_id`` is not a Strategy Lab execution and is ignored, which
        is what lets one ``ExecutionService`` serve both paths.
        """
        execution_id = getattr(proposal, "execution_id", "") or ""
        if not execution_id:
            return
        states = _OBSERVER_STATES.get(event)
        if not states:
            return
        with self.session_factory() as session:
            trade = _execution_or_none(session, execution_id)
            if trade is None:
                return
            fields = self._observer_fields(event, proposal, detail)
            for state in states:
                if (
                    state is ExecutionState.PROTECTION_PENDING
                    and trade.status == ExecutionState.PROTECTION_FAILED.value
                ):
                    # Re-protecting a failed position announces "pending" on the
                    # way, but §12 draws no edge back from `protection_failed` to
                    # `protection_pending` — only forward to `protected`. The
                    # announcement is skipped rather than logged as a defect;
                    # the `protected` notification that follows is the one that
                    # carries the outcome.
                    continue
                try:
                    trade = registry.advance_execution(
                        session, execution_id, state,
                        reason=self._reason(event, detail), **fields
                    )
                except InvalidTransition as exc:
                    # A hop the machine forbids is a real defect, not something
                    # to paper over: it is logged with both states and the row
                    # is left where it was, so the resume pass sees the truth.
                    log.error(
                        "strategy_execution_illegal_transition",
                        execution_id=execution_id,
                        event=event,
                        error=str(exc),
                    )
                    break
                fields = {}
            session.commit()

            if trade.status == ExecutionState.PLACEMENT_UNKNOWN.value:
                self.pager(
                    ARM_PLACEMENT_UNKNOWN,
                    slx.redact({
                        "execution_id": execution_id,
                        "entry_ref_id": proposal.entry_ref_id or "",
                        "ticker": proposal.ticker,
                        "recovery": (
                            "The placement response was unreadable. The notional "
                            "stays reserved and every new entry is blocked until "
                            "reconciliation answers whether an order carrying "
                            f"ref_id {proposal.entry_ref_id!r} exists. Run the "
                            "resume pass, or look the ref_id up in the broker's "
                            "own app; do not re-place the entry by hand."
                        ),
                    }),
                )
            elif trade.status == ExecutionState.PROTECTION_FAILED.value:
                self.pager(
                    ARM_PROTECTION_FAILED,
                    slx.redact({
                        "execution_id": execution_id,
                        "ticker": proposal.ticker,
                        "quantity": detail.get("quantity"),
                        "stop_price": proposal.stop,
                        "recovery": (
                            "The entry filled and its gtc stop_market could not "
                            "be read back. The position is UNPROTECTED, every "
                            "new live entry is now blocked, and no in-process "
                            "watcher stands in for the missing stop. Place a "
                            "protective stop in the broker's app, confirm it in "
                            "the open-orders read, then re-run the resume pass."
                        ),
                    }),
                )

    @staticmethod
    def _reason(event: str, detail: dict) -> str:
        if event in (p6.ON_RISK_REJECTED, p6.ON_REVIEW_REJECTED, p6.ON_PLACEMENT_FAILED):
            return str(detail.get("reason_code") or event)
        if event in (p6.ON_PLACEMENT_UNKNOWN, p6.ON_PROTECTION_FAILED):
            return event
        return ""

    @staticmethod
    def _observer_fields(event: str, proposal: Proposal, detail: dict) -> dict:
        """Columns to carry across with a transition. Numbers only, no payloads."""
        fields: dict[str, Any] = {}
        if event == p6.ON_APPROVED:
            fields["quantity"] = float(proposal.quantity or 0)
            fields["notional"] = float(proposal.notional or 0.0)
            fields["portfolio_context_hash"] = proposal.portfolio_context_hash or ""
        elif event in (p6.ON_PARTIALLY_FILLED, p6.ON_FILLED):
            fields["quantity"] = float(proposal.filled_quantity or proposal.quantity or 0)
            fields["filled_entry_price"] = proposal.average_fill_price
        elif event == p6.ON_PLACEMENT_UNKNOWN:
            fields["reconciliation_state"] = "unknown_placement"
        elif event == p6.ON_PROTECTED:
            fields["reconciliation_state"] = "none"
        return fields

    # -- owner-side terminal paths -----------------------------------------

    def cancel(self, *, execution_id: str, by: str = "owner", reason: str = "", now=None) -> str:
        """Owner cancellation, before anything has been placed.

        Legal only from ``proposed``: once an entry is at the broker, "cancel"
        is a broker action a human takes in the broker's own app, and code that
        cheerfully flattened a book during an outage would be the worst failure
        available here (the same reasoning as :mod:`portfolio.killswitch`).
        """
        now = now or utcnow_naive()
        with self.session_factory() as session:
            trade = registry.execution_row(session, execution_id)
            registry.advance_execution(
                session, trade.execution_id, ExecutionState.CANCELLED,
                reason=reason or f"cancelled_by:{by}", now=now,
            )
            self._void_proposal(session, execution_id, status="cancelled", reason=reason or f"cancelled by {by}")
            session.commit()
            return trade.status

    def expire_stale(self, *, now=None) -> list[str]:
        """Expire every ``proposed`` execution whose approval reference has lapsed.

        An approval that has expired cannot be used (Phase 6 refuses it), so the
        row would otherwise sit non-terminal forever holding the decision's one
        open-execution slot. Expiry is terminal and releases nothing, because a
        ``proposed`` row never reserved anything.
        """
        now = now or utcnow_naive()
        expired: list[str] = []
        with self.session_factory() as session:
            rows = (
                session.query(Proposal)
                .filter(Proposal.status == "proposed")
                .filter(Proposal.execution_id.isnot(None))
                .filter(Proposal.approval_expires_at.isnot(None))
                .filter(Proposal.approval_expires_at < now)
                .all()
            )
            for proposal in rows:
                trade = _execution_or_none(session, proposal.execution_id or "")
                if trade is None or trade.status != ExecutionState.PROPOSED.value:
                    continue
                registry.advance_execution(
                    session, trade.execution_id, ExecutionState.EXPIRED,
                    reason="approval_expired", now=now,
                )
                proposal.status = "expired"
                proposal.updated_at = now
                expired.append(trade.execution_id)
            session.commit()
        return expired

    def close(self, *, execution_id: str, exit_price: float | None = None, exit_reason: str = "", now=None) -> str:
        """Walk a protected position out: ``protected -> closing -> closed``.

        The exit itself is the protective stop triggering, or a sell the owner
        placed; this records the outcome and releases the reservation, which is
        the last thing the §12 machine owes the daily notional counter.
        """
        now = now or utcnow_naive()
        with self.session_factory() as session:
            trade = registry.execution_row(session, execution_id)
            if trade.status not in (ExecutionState.CLOSING.value, ExecutionState.CLOSED.value):
                registry.advance_execution(session, execution_id, ExecutionState.CLOSING, now=now)
            trade = registry.advance_execution(
                session,
                execution_id,
                ExecutionState.CLOSED,
                now=now,
                exit_price=exit_price,
                exit_reason=(exit_reason or "closed")[:40],
            )
            session.commit()
            return trade.status

    def _void_proposal(self, session, execution_id: str, *, status: str, reason: str) -> None:
        proposal_id = self._proposal_id_for(session, execution_id)
        if proposal_id is None:
            return
        proposal = session.get(Proposal, proposal_id)
        if proposal is None or proposal.status != "proposed":
            return
        proposal.status = status
        proposal.rejection_reason = reason
        proposal.updated_at = utcnow_naive()

    # -- restart recovery ---------------------------------------------------

    def resume(self, *, now=None) -> list[ResumeAction]:
        """Resolve every non-terminal execution from the broker's own answer.

        This is what makes the persistence worth having: after a restart there
        is no in-memory state at all, so each row is re-derived from what the
        broker says right now. Two rules hold throughout, and the safety tests
        assert both:

        * **No entry order is ever placed here.** A protective stop may be
          re-placed — that is the whole point of surviving a restart with an
          unprotected fill — but an entry never is. An execution whose entry
          cannot be found is resolved to a terminal no-order state or to
          ``reconciliation_required``, never re-submitted.
        * **A read that fails is not an answer.** Any exception from the broker
          leaves the row where it was, or moves it to
          ``reconciliation_required``; it never resolves one optimistically.
        """
        now = now or utcnow_naive()
        actions: list[ResumeAction] = []
        with self.session_factory() as session:
            # Shadow executions are deliberately excluded, and not as an
            # optimisation. PR 3's shadow executor walks the same §12 machine and
            # leaves rows sitting at `protected` until the simulated position
            # matures, so they are non-terminal — but there is no adapter to
            # resolve them against, and asking for one is
            # `ShadowReachedExecution` by design. A simulated position is
            # resumed by re-running the shadow executor, never from a broker.
            rows = [
                row
                for mode in slx.MODE_VENUES
                for row in registry.resumable_executions(session, mode=mode)
            ]
            rows.sort(key=lambda row: row.id)
            plan = [(row.execution_id, row.status, row.arm_id) for row in rows]
        for execution_id, status, arm_id in plan:
            try:
                action = self._resume_one(execution_id, status, arm_id, now)
            except slx.ShadowReachedExecution:
                raise
            except Exception as exc:  # pragma: no cover - adapter-specific
                log.error("strategy_resume_failed", execution_id=execution_id, error=str(exc))
                action = ResumeAction(execution_id, status, status, "unresolved", {"reason": "read_failed"})
            if action is not None:
                actions.append(action)
        return actions

    def _load_proposal(self, proposal_id: int | None) -> Proposal | None:
        """A detached snapshot of a proposal, safe to read outside its session.

        The resume pass hops between short transactions on purpose — each write
        it makes has to be durable before the next broker call — so it reads
        rows out and lets the session close. Expunging before the close is what
        keeps the loaded attributes readable rather than raising
        ``DetachedInstanceError`` on the first access, which would be caught by
        the broad handler in :meth:`resume` and reported as "the broker could
        not be read". A bookkeeping mistake must not disguise itself as an
        unresolvable broker state.
        """
        if not proposal_id:
            return None
        with self.session_factory() as session:
            row = session.get(Proposal, int(proposal_id))
            if row is None:
                return None
            session.expunge(row)
            return row

    def _resume_one(self, execution_id: str, status: str, arm_id: int, now) -> ResumeAction | None:
        with self.session_factory() as session:
            arm = registry.require_arm(session, arm_id)
            binding = self._bind(arm, arm.mode)
            proposal_id = self._proposal_id_for(session, execution_id)
        proposal = self._load_proposal(proposal_id)

        state = ExecutionState(status)
        if state is ExecutionState.PROPOSED:
            return None  # nothing to resume: it is waiting on the owner.
        if proposal is None:
            return self._to_reconciliation(execution_id, status, "no_linked_proposal", now)

        service = self._service(binding)
        broker = binding.adapter

        if state in (ExecutionState.OWNER_APPROVED, ExecutionState.RISK_RESERVED):
            # Crashed between approval and a confirmed submission. The ref_id is
            # the only durable link to an order that may or may not exist.
            return self._resolve_by_ref_id(service, execution_id, status, proposal, now)

        if state in (ExecutionState.SUBMITTED, ExecutionState.ACCEPTED):
            return self._resume_working_entry(service, execution_id, status, proposal, now)

        if state in (
            ExecutionState.PARTIALLY_FILLED,
            ExecutionState.FILLED,
            ExecutionState.PROTECTION_PENDING,
        ):
            return self._resume_protection(service, execution_id, status, proposal, now)

        if state in (ExecutionState.PROTECTED, ExecutionState.PROTECTION_FAILED):
            grew = self._refresh_fill(service, proposal, now)
            if not grew and service._stop_is_readable(proposal):
                self._set(execution_id, ExecutionState.PROTECTED, now=now)
                return ResumeAction(
                    execution_id, status, ExecutionState.PROTECTED.value, "protection_verified"
                )
            return self._resume_protection(
                service, execution_id, status, self._load_proposal(proposal.id), now
            )

        if state is ExecutionState.PLACEMENT_UNKNOWN:
            return self._resolve_unknown(service, execution_id, status, proposal, now)

        if state is ExecutionState.RECONCILIATION_REQUIRED:
            return self._resolve_unknown(service, execution_id, status, proposal, now)

        return None  # `closing` is walked out by `close`, not by a restart.

    def _resolve_by_ref_id(self, service, execution_id, status, proposal, now) -> ResumeAction:
        """Did the entry land? Terminal only when the broker says *no*."""
        found = None
        finder = getattr(service.broker, "find_order_by_ref_id", None)
        if callable(finder):
            found = finder(proposal.entry_ref_id or "")
        elif proposal.entry_broker_order_id:
            found = service.broker.get_order_status(proposal.entry_broker_order_id) or None
        else:
            # An adapter that cannot answer the question cannot clear the row.
            return self._to_reconciliation(execution_id, status, "adapter_cannot_resolve_ref_id", now)

        if not found:
            # Verified no order: terminal, and the reservation releases exactly
            # once because reaching a terminal state *is* the release.
            self._set(execution_id, ExecutionState.FAILED_NO_ORDER, now=now, reason="verified_no_order")
            self._mark_proposal(proposal.id, "failed", "resume: no order exists for this ref_id.")
            return ResumeAction(execution_id, status, ExecutionState.FAILED_NO_ORDER.value, "verified_no_order")

        order_id = str(found.get("id") or found.get("order_id") or "")
        self._mark_proposal(proposal.id, "submitted", "", entry_broker_order_id=order_id or None)
        # §12 draws no edge from `owner_approved` straight to `submitted`: the
        # reservation is taken in between, and skipping it would submit an order
        # whose notional nothing had counted. A crash in that gap is vanishingly
        # narrow — the observer commits both hops in one transaction — but a
        # resume pass that could not resolve the row would leave it stuck, so the
        # missing hop is walked rather than jumped.
        if status == ExecutionState.OWNER_APPROVED.value:
            self._set(
                execution_id, ExecutionState.RISK_RESERVED, now=now,
                notional=float(proposal.notional or 0.0),
                quantity=float(proposal.quantity or 0),
            )
        self._set(execution_id, ExecutionState.SUBMITTED, now=now)
        return self._resume_working_entry(
            service,
            execution_id,
            ExecutionState.SUBMITTED.value,
            self._load_proposal(proposal.id),
            now,
        )

    def _resume_working_entry(self, service, execution_id, status, proposal, now) -> ResumeAction:
        info = service.broker.get_order_status(proposal.entry_broker_order_id or "") or {}
        state = (info.get("status") or "").strip().lower()
        filled = float(info.get("filled_qty") or 0.0)
        if state in ("rejected", "failed"):
            self._set(execution_id, ExecutionState.ORDER_REJECTED, now=now, reason="broker_rejected")
            self._mark_proposal(proposal.id, "rejected", "resume: the broker rejected this entry.")
            return ResumeAction(execution_id, status, ExecutionState.ORDER_REJECTED.value, "order_rejected")
        if state in ("cancelled", "canceled"):
            self._set(execution_id, ExecutionState.CANCELLED, now=now, reason="broker_cancelled")
            self._mark_proposal(proposal.id, "cancelled", "resume: this entry was cancelled at the broker.")
            return ResumeAction(execution_id, status, ExecutionState.CANCELLED.value, "cancelled")
        if state == "expired":
            # `submitted -> expired` is not an edge; `accepted -> expired` is.
            # An order the broker held long enough to expire was one it accepted.
            self._set(execution_id, ExecutionState.ACCEPTED, now=now)
            self._set(execution_id, ExecutionState.EXPIRED, now=now, reason="broker_expired")
            self._mark_proposal(proposal.id, "expired", "resume: this entry expired at the broker.")
            return ResumeAction(execution_id, status, ExecutionState.EXPIRED.value, "expired")
        if not info:
            return self._to_reconciliation(execution_id, status, "entry_order_not_found", now)
        if filled <= 0:
            self._set(execution_id, ExecutionState.ACCEPTED, now=now)
            return ResumeAction(execution_id, status, ExecutionState.ACCEPTED.value, "still_working")

        requested = float(info.get("quantity") or proposal.quantity or 0.0)
        self._mark_proposal(
            proposal.id,
            "filled",
            "",
            filled_quantity=filled,
            average_fill_price=info.get("filled_avg_price") or proposal.average_fill_price,
            filled_at=proposal.filled_at or now,
        )
        self._set(execution_id, ExecutionState.ACCEPTED, now=now)
        self._set(
            execution_id,
            ExecutionState.PARTIALLY_FILLED if 0 < filled < requested else ExecutionState.FILLED,
            now=now,
            quantity=filled,
            filled_entry_price=info.get("filled_avg_price") or proposal.average_fill_price,
        )
        return self._resume_protection(
            service, execution_id, status, self._load_proposal(proposal.id), now
        )

    def _refresh_fill(self, service, proposal, now) -> bool:
        """Re-read the entry and record a fill that has grown since last time.

        Returns whether the filled quantity increased. This is the case a
        protected position can silently rot in: a partial fill was protected,
        the remainder arrived later, and the stop now covers less than the
        position. Nothing detects that without asking the broker again.
        """
        order_id = proposal.entry_broker_order_id or ""
        if not order_id:
            return False
        info = service.broker.get_order_status(order_id) or {}
        filled = float(info.get("filled_qty") or 0.0)
        known = float(proposal.filled_quantity or 0.0)
        if filled <= known:
            return False
        self._mark_proposal(
            proposal.id,
            proposal.status,
            "",
            filled_quantity=filled,
            average_fill_price=info.get("filled_avg_price") or proposal.average_fill_price,
        )
        log.info(
            "strategy_fill_grew",
            proposal_id=proposal.id,
            was=known,
            now=filled,
        )
        return True

    def _resume_protection(self, service, execution_id, status, proposal, now) -> ResumeAction:
        """Place or re-place the protective stop for whatever actually filled.

        Idempotent twice over: the ``ref_id`` is derived from the quantity being
        protected, so re-running with the same fill reuses one stop while a
        *larger* fill deliberately asks for a new one; and ``_protect`` itself
        reads the stop back before calling the position protected.

        A resize cancels the old, undersized stop **first**. Leaving it would
        put two sell orders against one position, and a stop that can sell
        shares twice is not protection, it is a short.

        The route back through the machine is deliberate. §12 draws no edge from
        ``protected`` to ``protection_pending``, and it should not: a protected
        position whose stop no longer covers it is precisely the case the spec
        describes as "broker and local state disagree", so it goes through
        ``reconciliation_required`` — blocking new entries while it is
        undersized — and re-enters the lifecycle at ``filled``.
        """
        with self.session_factory() as session:
            row = session.get(Proposal, proposal.id)
            trade = _execution_or_none(session, execution_id)
            current = ExecutionState(trade.status) if trade else ExecutionState(status)
            qty = int(float(row.filled_quantity or row.quantity or 0))
            if qty <= 0:
                return self._to_reconciliation(execution_id, status, "nothing_filled_to_protect", now)

            target_ref = self._stop_ref_id(row, qty)
            resizing = bool(row.stop_ref_id) and row.stop_ref_id != target_ref
            if resizing and row.stop_broker_order_id:
                canceller = getattr(service.broker, "cancel_order", None)
                if callable(canceller):
                    canceller(row.stop_broker_order_id)
            if resizing:
                row.stop_broker_order_id = None
            row.stop_ref_id = target_ref
            session.commit()

        if current is ExecutionState.PROTECTED and resizing:
            self._set(
                execution_id, ExecutionState.RECONCILIATION_REQUIRED, now=now,
                reason="protection_undersized",
            )
            self._set(execution_id, ExecutionState.FILLED, now=now, quantity=float(qty))
        if current in (
            ExecutionState.FILLED,
            ExecutionState.PARTIALLY_FILLED,
            ExecutionState.PROTECTION_PENDING,
        ) or (current is ExecutionState.PROTECTED and resizing):
            self._set(execution_id, ExecutionState.PROTECTION_PENDING, now=now, quantity=float(qty))

        with self.session_factory() as session:
            row = session.get(Proposal, proposal.id)
            result = service._protect(session, row, now)
            session.commit()

        final = (
            ExecutionState.PROTECTED
            if result.status == "protected"
            else ExecutionState.PROTECTION_FAILED
        )
        self._set(
            execution_id, final, now=now,
            reason="" if final is ExecutionState.PROTECTED else "protection_failed",
        )
        return ResumeAction(
            execution_id, status, final.value, "protection_placed", {"quantity": qty}
        )

    @staticmethod
    def _stop_ref_id(proposal: Proposal, quantity: int) -> str:
        """The protective stop's idempotency key, keyed to what it protects.

        A stop is only a protective exit if it covers the whole filled position,
        so the quantity belongs in the key: re-protecting the same fill reuses
        one order (the broker deduplicates), and protecting a *grown* position
        after the remainder of a partial fill arrives asks for a new one rather
        than silently leaving half the shares uninsured.
        """
        base = proposal.stop_ref_id or f"p6-stop-{proposal.proposal_uid}"
        base = base.split("-q")[0]
        return f"{base}-q{int(quantity)}"

    def _resolve_unknown(self, service, execution_id, status, proposal, now) -> ResumeAction:
        """The only exit from ``placement_unknown``, and it goes through §12.

        The diagram is explicit: ``placement_unknown -> reconciliation_required``
        and nothing else, and the reservation is held throughout. From
        ``reconciliation_required`` the broker's answer resolves it either into
        the normal lifecycle or terminally.
        """
        if status == ExecutionState.PLACEMENT_UNKNOWN.value:
            self._set(execution_id, ExecutionState.RECONCILIATION_REQUIRED, now=now, reason="placement_unknown")
        action = self._resolve_by_ref_id(service, execution_id, ExecutionState.RECONCILIATION_REQUIRED.value, proposal, now)
        if action.now not in (ExecutionState.RECONCILIATION_REQUIRED.value,):
            self.pager(
                ARM_RECONCILIATION_RESOLVED,
                slx.redact({
                    "execution_id": execution_id,
                    "state": action.now,
                    "recovery": "Reconciliation resolved this execution; entries are unblocked once no other execution is blocking.",
                }),
            )
        return action

    def _to_reconciliation(self, execution_id, status, reason, now) -> ResumeAction:
        self._set(execution_id, ExecutionState.RECONCILIATION_REQUIRED, now=now, reason=reason)
        self.pager(
            ARM_RECONCILIATION_MISMATCH,
            slx.redact({
                "execution_id": execution_id,
                "reason_code": reason,
                "recovery": (
                    "Local and broker state disagree, or the broker could not be "
                    "read. The reservation is held and every new entry is "
                    "blocked. Check the broker's own app for this execution's "
                    "ref_id, resolve it there, and re-run the resume pass."
                ),
            }),
        )
        return ResumeAction(execution_id, status, ExecutionState.RECONCILIATION_REQUIRED.value, "reconciliation_required", {"reason_code": reason})

    def _set(self, execution_id: str, state: ExecutionState, *, now, reason: str = "", **fields) -> None:
        with self.session_factory() as session:
            trade = _execution_or_none(session, execution_id)
            if trade is None:
                return
            try:
                registry.advance_execution(
                    session, execution_id, state, reason=reason, now=now, **fields
                )
            except InvalidTransition as exc:
                log.error("strategy_resume_illegal_transition", execution_id=execution_id, error=str(exc))
                session.rollback()
                return
            session.commit()

    def _mark_proposal(self, proposal_id: int, status: str, reason: str, **fields) -> None:
        with self.session_factory() as session:
            row = session.get(Proposal, proposal_id)
            if row is None:
                return
            row.status = status
            if reason:
                row.rejection_reason = reason
            for key, value in fields.items():
                if value is not None:
                    setattr(row, key, value)
            row.updated_at = utcnow_naive()
            session.commit()

    # -- reconciliation -----------------------------------------------------

    def reconcile(self, *, mode, now=None) -> recon.ExecutionReconciliation:
        """Compare the execution ledger against the broker, and fail closed.

        Every mismatch moves its execution to ``reconciliation_required``, which
        blocks new entries through the *existing* kill-switch gate rather than a
        second switch, and pages with a recovery instruction rather than an
        error string (Spec Q §12 invariant 10).
        """
        now = now or utcnow_naive()
        mode = ExecutionMode(mode)
        with self.session_factory() as session:
            venue = slx.MODE_VENUES.get(mode)
            adapter = self.adapters.get(venue) if venue else None
            if adapter is None:
                raise ArmExecutionRefused(
                    "no_adapter",
                    f"no adapter is registered for the {venue!r} venue, so "
                    f"{mode.value} executions cannot be reconciled. A flow that "
                    "cannot be checked fails closed rather than passing.",
                )
            positions = adapter.get_positions_detail()
            report = recon.reconcile_executions(
                session,
                positions=positions,
                mode=mode.value,
                known_tickers=recon.open_execution_tickers(session, mode=mode.value),
            )
            session.commit()

        for finding in report.mismatches:
            if finding.execution_id:
                self._set(
                    finding.execution_id,
                    ExecutionState.RECONCILIATION_REQUIRED,
                    now=now,
                    reason=finding.kind,
                    reconciliation_state=finding.kind,
                )
            self.pager(
                ARM_RECONCILIATION_MISMATCH,
                slx.redact({
                    "execution_id": finding.execution_id,
                    "ticker": finding.ticker,
                    "reason_code": finding.kind,
                    "expected_quantity": finding.expected_quantity,
                    "broker_quantity": finding.broker_quantity,
                    "recovery": (
                        f"{finding.detail} New entries are blocked until this is "
                        "resolved. Compare the position in the broker's own app, "
                        "correct the side that is wrong, then re-run the "
                        "reconciliation pass. Do not place a compensating order "
                        "from here."
                    ),
                }),
            )
        return report
