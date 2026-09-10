"""The Spec Q §12 execution state machine, over ``strategy_trades``.

This module is the *persistence and rules* half of Strategy Lab live safety. It
holds the §12 state machine, the explicit-mode entry gate, the notional
reservation, and the redaction allowlist — and it holds **no broker**. The
placement half is Phase 6's :class:`execution.lifecycle.ExecutionService`, and
the two are joined in ``execution/strategy_lifecycle.py``, which is on the other
side of the import boundary this package may not cross (Spec Q §5,
``tests/test_strategy_lab_import_graph.py``).

That split is not bookkeeping. Spec Q §12 invariant 11 says execution mode is an
explicit immutable input, never inferred from global mutable settings — and the
cheapest way to guarantee a module cannot infer a mode from a setting is to make
``import config`` fail in it. So the rules live here, where nothing can reach a
setting, a broker, or a model client; the wiring lives in ``execution/``, where
the import-graph test already forbids the workspace from reaching it at all.

**What this module owns**

*The one non-terminal execution per decision.* :func:`open_execution` commits an
``execution_id`` **before** any reservation or broker call, and the uniqueness is
held by the partial unique index ``uq_strategy_trades_open_execution`` — a
database constraint, not an in-process lock a second worker or a restart would
not see (§12 invariant 6). A concurrent caller that loses the race gets the
*existing* row back, which is what makes a retried approval reuse one placement
rather than create a second.

*Every hop checked.* :func:`transition` runs
``strategy_lab.domain.EXECUTION_TRANSITIONS`` on every move and is a no-op when
the row is already in the requested state, so a retry replays safely.

*The reservation, released exactly once.* A reservation is not an event with a
counter; it is a **predicate on the status column**
(:data:`RESERVING_EXECUTION_STATES`). A row holds its notional from
``risk_reserved`` until it reaches a terminal state, and terminal states have no
outgoing edges — so "released exactly once" is structural rather than asserted,
and no column can drift out of step with the status that governs it.
``placement_unknown`` and ``reconciliation_required`` are deliberately inside the
reserving set: §12 invariant 12 keeps an unknown outcome reserved until
reconciliation proves no order exists.

*Which adapter a mode may reach.* :func:`bind_adapter` is the §12 invariant 11
gate: ``shadow`` raises rather than returning an adapter at all, ``paper``
accepts only the Alpaca paper venue, ``live`` accepts only the promoted live
venue, and the arm's own recorded mode has to agree with the requested one.
Every mismatch is refused here, before a broker review is even formed.

*Redaction.* :func:`redact` is the allowlist §12 invariant 8 and Spec Q §17
require: request ids and named fields survive, everything else is dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping

from sqlalchemy.exc import IntegrityError

from database import models
from strategy_lab.domain import (
    EXECUTION_TRANSITIONS,
    TERMINAL_EXECUTION_STATES,
    ExecutionMode,
    ExecutionState,
    StrategyLabError,
    is_terminal,
    require_transition,
)
from strategy_lab.registry import NotFound, new_execution_id
from utils.logger import get_logger
from utils.timeutils import utcnow_naive

log = get_logger("strategy_lab_execution")


# --------------------------------------------------------------------------- #
# Venues
# --------------------------------------------------------------------------- #

#: The only venue a ``paper`` arm may reach (Spec Q §11, §12 invariant 11).
#: Alpaca paper, whatever the application-wide broker setting happens to be.
PAPER_VENUE = "alpaca_paper"

#: The only venue a ``live`` arm may reach. The promoted live path is the
#: Robinhood Agentic account and nothing else (Spec L §5.1).
LIVE_VENUE = "robinhood_live"

#: ``mode -> the one venue it may select``. ``shadow`` is absent on purpose:
#: there is no venue a shadow arm may reach, and an empty entry would read as
#: "not configured yet" rather than "never".
MODE_VENUES: Mapping[ExecutionMode, str] = {
    ExecutionMode.PAPER: PAPER_VENUE,
    ExecutionMode.LIVE: LIVE_VENUE,
}


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ExecutionRefusedLocally(StrategyLabError):
    """A refusal raised before any adapter is touched. Carries a stable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class ShadowReachedExecution(ExecutionRefusedLocally):
    """A shadow arm reached the execution entry point. This is a bug, loudly.

    Spec Q §12 invariant 11: ``shadow`` cannot reach an order API. The shadow
    executor simulates a fill and writes a row; it never binds an adapter. If
    this is raised, something inferred a mode instead of carrying one.
    """

    def __init__(self, detail: str = ""):
        super().__init__(
            "shadow_cannot_execute",
            "a shadow arm reached the broker execution entry point"
            + (f" ({detail})" if detail else "")
            + ". Shadow records what a strategy would do and sends no order "
            "(Spec Q §12 invariant 11); reaching here means a mode was "
            "inferred rather than carried.",
        )


class ExecutionConflict(ExecutionRefusedLocally):
    """Another non-terminal execution already exists for this decision."""


# --------------------------------------------------------------------------- #
# Reservations
# --------------------------------------------------------------------------- #

#: The states in which a row **holds** its notional/risk reservation.
#:
#: Spec Q §12: "Every terminal path releases its notional/risk reservation
#: exactly once. An unknown placement outcome is not terminal and cannot release
#: its reservation until reconciliation proves no order exists or resolves the
#: order into the normal lifecycle."
#:
#: Expressed as a predicate on the status rather than as a released-at column, so
#: that release *is* the terminal transition: there is no second write to forget,
#: to repeat, or to disagree with the status. ``proposed`` and ``owner_approved``
#: are outside the set — the reservation is taken at ``risk_reserved``, which is
#: the state whose whole purpose is to exist before the external order call.
RESERVING_EXECUTION_STATES: frozenset[ExecutionState] = frozenset({
    ExecutionState.RISK_RESERVED,
    ExecutionState.SUBMITTED,
    ExecutionState.ACCEPTED,
    ExecutionState.PLACEMENT_UNKNOWN,
    ExecutionState.PARTIALLY_FILLED,
    ExecutionState.FILLED,
    ExecutionState.PROTECTION_PENDING,
    ExecutionState.PROTECTED,
    ExecutionState.PROTECTION_FAILED,
    ExecutionState.CLOSING,
    ExecutionState.RECONCILIATION_REQUIRED,
})

#: States that block every *new* live entry until a human resolves them
#: (Spec Q §12 invariant 3, restated by invariant 6 for the unknown case).
#: ``portfolio.killswitch.entry_block`` reads the same set through
#: ``database.models.STRATEGY_TRADE_BLOCKING_STATUSES``.
BLOCKING_EXECUTION_STATES: frozenset[ExecutionState] = frozenset({
    ExecutionState.PROTECTION_FAILED,
    ExecutionState.PLACEMENT_UNKNOWN,
    ExecutionState.RECONCILIATION_REQUIRED,
})


def holds_reservation(state: ExecutionState | str) -> bool:
    """Whether a row in ``state`` still has its notional reserved."""
    return ExecutionState(state) in RESERVING_EXECUTION_STATES


def reserved_notional(session, *, mode, on_date: date | None = None) -> float:
    """Notional this mode currently holds reserved, optionally for one day.

    Counted from the status column, so an approval that is racing another one
    cannot overspend the cap: the first to reach ``risk_reserved`` is visible to
    the second the moment its transaction commits (Spec Q §12 invariant 6).
    """
    mode = ExecutionMode(mode).value
    query = (
        session.query(models.StrategyTrade)
        .filter(models.StrategyTrade.mode == mode)
        .filter(models.StrategyTrade.status.in_([s.value for s in RESERVING_EXECUTION_STATES]))
    )
    if on_date is not None:
        start = datetime(on_date.year, on_date.month, on_date.day)
        query = query.filter(
            models.StrategyTrade.created_at >= start,
            models.StrategyTrade.created_at < start + timedelta(days=1),
        )
    return sum(float(row.notional or 0.0) for row in query.all())


# --------------------------------------------------------------------------- #
# The explicit-mode entry gate (Spec Q §12 invariant 11)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AdapterBinding:
    """One arm's mode bound to the one adapter that mode is allowed to reach.

    Frozen, and produced only by :func:`bind_adapter`. A caller that wants an
    adapter has to state the arm's recorded mode, the mode it believes it is
    executing, and the venue label of the adapter it brought; disagreement
    between any two of those is a refusal rather than a silent preference.
    """

    mode: ExecutionMode
    venue: str
    adapter: Any

    @property
    def is_live(self) -> bool:
        return self.mode is ExecutionMode.LIVE


def bind_adapter(*, arm_mode, requested_mode, venue: str, adapter) -> AdapterBinding:
    """Bind ``adapter`` to ``requested_mode``, or refuse before any review.

    Four refusals, in the order they can be checked without touching anything:

    1. ``shadow`` raises :class:`ShadowReachedExecution` — there is no adapter.
    2. the arm's recorded mode and the requested mode must be the same object;
       an arm's mode is its identity, not a setting (Spec Q §8).
    3. the venue must be the single one that mode may select.
    4. an adapter must actually have been passed.
    """
    arm_mode = ExecutionMode(arm_mode)
    requested_mode = ExecutionMode(requested_mode)

    if requested_mode is ExecutionMode.SHADOW or arm_mode is ExecutionMode.SHADOW:
        raise ShadowReachedExecution(
            f"arm_mode={arm_mode.value}, requested_mode={requested_mode.value}"
        )
    if arm_mode is not requested_mode:
        raise ExecutionRefusedLocally(
            "mode_mismatch",
            f"the arm runs in {arm_mode.value!r} but execution was requested in "
            f"{requested_mode.value!r}. An arm's mode is immutable and is "
            "carried by the arm, never inferred (Spec Q §12 invariant 11).",
        )
    expected = MODE_VENUES[requested_mode]
    if (venue or "").strip().lower() != expected:
        raise ExecutionRefusedLocally(
            "venue_mismatch",
            f"a {requested_mode.value!r} arm may reach only the {expected!r} "
            f"venue; {venue!r} was offered. A paper arm reaches Alpaca paper "
            "whatever the application-wide broker is, and a live arm reaches "
            "only the promoted live path (Spec Q §11, §12 invariant 11).",
        )
    if adapter is None:
        raise ExecutionRefusedLocally(
            "no_adapter",
            f"no adapter was supplied for the {expected!r} venue.",
        )

    # The adapter's own declaration, when it makes one. This is what catches the
    # wiring error the venue label alone cannot: registering the live Robinhood
    # adapter under the paper venue key would otherwise route a paper arm to
    # live capital, and nothing above would notice. An adapter that declares
    # nothing (a test fake, a future venue) is accepted; a *contradicting*
    # declaration never is.
    declared = str(getattr(adapter, "venue", "") or "").strip().lower()
    if declared and declared != expected:
        raise ExecutionRefusedLocally(
            "adapter_venue_mismatch",
            f"the adapter registered at {expected!r} declares itself "
            f"{declared!r}. A {requested_mode.value!r} arm may reach only "
            f"{expected!r}, and an adapter that says it is something else is a "
            "wiring error, not a preference (Spec Q §11, §12 invariant 11).",
        )
    return AdapterBinding(mode=requested_mode, venue=expected, adapter=adapter)


# --------------------------------------------------------------------------- #
# Persistence: one execution_id, committed before anything external
# --------------------------------------------------------------------------- #


def get_execution(session, execution_id: str):
    """The row carrying ``execution_id``, or ``None``."""
    if not execution_id:
        return None
    return (
        session.query(models.StrategyTrade)
        .filter(models.StrategyTrade.execution_id == execution_id)
        .first()
    )


def require_execution(session, execution_id: str) -> models.StrategyTrade:
    row = get_execution(session, execution_id)
    if row is None:
        raise NotFound(f"no strategy_trade carries execution_id {execution_id!r}")
    return row


def open_execution_for(session, decision_id: int):
    """The one non-terminal execution for ``decision_id``, or ``None``."""
    return (
        session.query(models.StrategyTrade)
        .filter(models.StrategyTrade.decision_id == int(decision_id))
        .filter(
            models.StrategyTrade.status.notin_([s.value for s in TERMINAL_EXECUTION_STATES])
        )
        .order_by(models.StrategyTrade.id.asc())
        .first()
    )


def open_execution(
    session,
    *,
    arm_id: int,
    decision_id: int,
    mode,
    intended_entry_price: float | None = None,
    stop_price: float | None = None,
    target1_price: float | None = None,
    target2_price: float | None = None,
    portfolio_context_hash: str = "",
    now: datetime | None = None,
) -> models.StrategyTrade:
    """Create — or re-find — the one non-terminal execution for a decision.

    The ``execution_id`` is generated and flushed **before** any reservation and
    before any broker call (Spec Q §12 invariant 6), so a process that dies
    between here and a placement leaves a row a restart can resolve rather than
    an order nobody knows about.

    Idempotent three ways, which is the point:

    * an existing non-terminal row for the decision is returned unchanged;
    * a concurrent worker that loses the ``uq_strategy_trades_open_execution``
      race gets that worker's row back rather than an ``IntegrityError``;
    * the arm's mode is checked against the requested one, so a retry cannot
      quietly re-open the same decision in a different mode.

    The insert runs inside a SAVEPOINT so that losing the race rolls back only
    the failed insert, never the caller's transaction.
    """
    now = now or utcnow_naive()
    mode = ExecutionMode(mode)

    arm = session.get(models.ExperimentArm, int(arm_id))
    if arm is None:
        raise NotFound(f"no experiment arm {arm_id}")
    if ExecutionMode(arm.mode) is not mode:
        raise ExecutionRefusedLocally(
            "mode_mismatch",
            f"arm {arm_id} runs in {arm.mode!r}; an execution was requested in "
            f"{mode.value!r}. The arm carries the mode (Spec Q §8, §12 "
            "invariant 11).",
        )
    if session.get(models.StrategyDecision, int(decision_id)) is None:
        raise NotFound(f"no strategy decision {decision_id}")

    existing = open_execution_for(session, decision_id)
    if existing is not None:
        if ExecutionMode(existing.mode) is not mode:
            raise ExecutionConflict(
                "open_execution_mode_conflict",
                f"decision {decision_id} already has a non-terminal execution "
                f"{existing.execution_id} in {existing.mode!r}; it cannot also "
                f"run in {mode.value!r}.",
            )
        return existing

    row = models.StrategyTrade(
        execution_id=new_execution_id(),
        arm_id=int(arm_id),
        decision_id=int(decision_id),
        mode=mode.value,
        status=ExecutionState.PROPOSED.value,
        portfolio_context_hash=portfolio_context_hash or "",
        intended_entry_price=intended_entry_price,
        stop_price=stop_price,
        target1_price=target1_price,
        target2_price=target2_price,
        proposed_at=now,
        created_at=now,
        updated_at=now,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        # Someone else got there first. The constraint is the arbiter, and the
        # row it protected is the answer — never a second placement.
        existing = open_execution_for(session, decision_id)
        if existing is None:
            raise
        log.info(
            "strategy_execution_race_lost",
            decision_id=int(decision_id),
            winner=existing.execution_id,
        )
        return existing

    log.info(
        "strategy_execution_opened",
        execution_id=row.execution_id,
        arm_id=int(arm_id),
        decision_id=int(decision_id),
        mode=mode.value,
    )
    return row


def transition(
    session,
    trade: models.StrategyTrade,
    to_state,
    *,
    reason: str = "",
    now: datetime | None = None,
    **fields,
) -> models.StrategyTrade:
    """Move one execution along the §12 machine, or refuse the hop.

    Idempotent: re-asserting the state a row is already in updates the supplied
    fields and returns, which is how a retry behaves. Illegal hops raise
    ``strategy_lab.domain.InvalidTransition`` with the legal moves listed.

    The reservation needs no bookkeeping here — see
    :data:`RESERVING_EXECUTION_STATES`. Reaching a terminal state *is* the
    release, and a terminal state has no outgoing edge, so it happens once.
    """
    now = now or utcnow_naive()
    current = ExecutionState(trade.status)
    target = require_transition(
        EXECUTION_TRANSITIONS, current, to_state, label=f"execution {trade.execution_id}"
    )

    for key, value in fields.items():
        if not hasattr(trade, key):
            raise AttributeError(f"strategy_trades has no column {key!r}")
        setattr(trade, key, value)
    if reason:
        trade.blocked_reason = reason[:80]

    if target is not current:
        trade.status = target.value
        if target is ExecutionState.OWNER_APPROVED:
            trade.approved_at = trade.approved_at or now
        elif target is ExecutionState.SUBMITTED:
            trade.submitted_at = trade.submitted_at or now
        elif target in (ExecutionState.FILLED, ExecutionState.PARTIALLY_FILLED):
            trade.filled_at = trade.filled_at or now
        elif target is ExecutionState.CLOSED:
            trade.closed_at = trade.closed_at or now
    trade.updated_at = now
    session.flush()

    if target is not current:
        log.info(
            "live_order_state_changed",
            execution_id=trade.execution_id,
            mode=trade.mode,
            **redact({"from": current.value, "to": target.value, "reason": reason}),
        )
        if is_terminal(target) and holds_reservation(current):
            log.info(
                "strategy_reservation_released",
                execution_id=trade.execution_id,
                notional=float(trade.notional or 0.0),
                terminal_state=target.value,
            )
    return trade


def blocking_executions(session) -> list[models.StrategyTrade]:
    """Executions whose state blocks every further live entry, newest first."""
    return (
        session.query(models.StrategyTrade)
        .filter(models.StrategyTrade.status.in_([s.value for s in BLOCKING_EXECUTION_STATES]))
        .order_by(models.StrategyTrade.id.desc())
        .all()
    )


def resumable_executions(session, *, mode=None) -> list[models.StrategyTrade]:
    """Non-terminal executions, oldest first — what a restart has to resolve."""
    query = session.query(models.StrategyTrade).filter(
        models.StrategyTrade.status.notin_([s.value for s in TERMINAL_EXECUTION_STATES])
    )
    if mode is not None:
        query = query.filter(models.StrategyTrade.mode == ExecutionMode(mode).value)
    return query.order_by(models.StrategyTrade.id.asc()).all()


# --------------------------------------------------------------------------- #
# Redaction (Spec Q §12 invariant 8, §17)
# --------------------------------------------------------------------------- #

#: The only keys that survive :func:`redact`. Spec Q §17: "Never log raw
#: brokerage payloads, account numbers, tokens, or sensitive research-provider
#: responses. Persist only the allowlisted fields required for audit and
#: reconciliation."
#:
#: An allowlist rather than a denylist, because the failure mode of a denylist
#: is a field nobody thought of, arriving from a broker whose payload shape we
#: do not control.
REDACTION_ALLOWLIST: frozenset[str] = frozenset({
    # Identity and idempotency — the whole point of keeping anything.
    "execution_id", "decision_id", "arm_id", "proposal_id", "request_id",
    "ref_id", "entry_ref_id", "stop_ref_id", "order_id", "entry_order_id",
    "stop_order_id", "broker_order_id",
    # What happened, in words a state machine already constrains.
    "from", "to", "state", "status", "reason", "reason_code", "error_code",
    "event", "mode", "venue", "broker", "symbol", "ticker", "side",
    "order_type", "time_in_force", "market_hours",
    # Numbers reconciliation needs.
    "quantity", "filled_quantity", "requested_quantity", "stop_price",
    "limit_price", "notional", "filled_notional", "average_fill_price",
    "expected_quantity", "broker_quantity",
    # Operator affordances.
    "recovery", "blocked_reason", "reconciliation_state", "attempt",
})

#: What replaces a dropped value, so a reader can see that something was there.
REDACTED = "[redacted]"


def redact(payload: Mapping[str, Any] | None, *, extra_allowed: Iterable[str] = ()) -> dict:
    """An allowlisted copy of ``payload``, safe to log or persist.

    Keys outside the allowlist are kept **as keys** with their values replaced
    by :data:`REDACTED`: knowing that a broker sent an ``account_number`` is
    useful for diagnosis, and knowing its value is a liability. Nested mappings
    are redacted recursively; anything else non-scalar is dropped to its type
    name, because a nested broker payload is exactly what must not survive.
    """
    allowed = REDACTION_ALLOWLIST | {str(k) for k in extra_allowed}
    out: dict[str, Any] = {}
    for key, value in (payload or {}).items():
        name = str(key)
        if name not in allowed:
            out[name] = REDACTED
            continue
        if isinstance(value, Mapping):
            out[name] = redact(value, extra_allowed=extra_allowed)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            out[name] = value
        else:
            out[name] = f"<{type(value).__name__}>"
    return out
