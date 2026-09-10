"""The Spec Q §12 execution *rules*: which venue, which states, what to log.

This module is pure. It holds no session, imports no broker, and cannot read a
setting — `tests/test_strategy_lab_import_graph.py` lists it among the modules
that reach nothing first-party but `strategy_lab` and `utils`. That is not
tidiness; it is the enforcement point for Spec Q §12 invariant 11.

    "Execution mode is an explicit immutable input, not inferred from global
    mutable settings. `shadow` cannot reach an order API; `paper` can reach only
    the Alpaca paper adapter; `live` can reach only the promoted live broker
    path."

The cheapest way to guarantee a module cannot infer a mode from a setting is to
make `import config` fail in it. So the rule about which adapter a mode may
reach lives here, where nothing can reach a setting, a broker, or a session.

**Where the other halves are.** `strategy_lab/registry.py` is the service
boundary and holds every `strategy_trades` write — PR 3 put `record_execution`,
`set_execution_state` and `open_execution_for` there and PR 5 adds the
reservation reads beside them, rather than opening a second writer.
`execution/strategy_lifecycle.py` does the wiring: it runs these rules, calls
that registry, and drives Phase 6's `ExecutionService`.

**What is here**

:func:`bind_adapter`
    the §12 invariant 11 gate. ``shadow`` raises rather than returning an
    adapter at all; ``paper`` accepts only the Alpaca paper venue; ``live`` only
    the promoted live venue; the arm's own recorded mode must agree with the
    requested one; and an adapter that *declares* a different venue is refused
    on its declaration, which is what catches a mis-registration the venue key
    alone cannot.

:data:`RESERVING_EXECUTION_STATES`
    the reservation, as a predicate on the status column rather than as an event
    with a counter. A row holds its notional from ``risk_reserved`` until it
    reaches a terminal state, and terminal states have no outgoing edge — so
    "every terminal path releases its reservation exactly once" is structural,
    with no second write to forget, repeat, or disagree with the status.
    ``placement_unknown`` and ``reconciliation_required`` are deliberately
    inside the set: §12 invariant 12 keeps an unknown outcome reserved until
    reconciliation proves no order exists.

:func:`redact`
    the allowlist §12 invariant 8 and Spec Q §17 require: request ids and named
    fields survive, every other value is replaced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from strategy_lab.domain import (
    TERMINAL_EXECUTION_STATES,
    ExecutionMode,
    ExecutionState,
    StrategyLabError,
)
from utils.logger import get_logger

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
#: ``database.models.STRATEGY_TRADE_BLOCKING_STATUSES``, which
#: ``tests/test_strategy_lab_safety.py`` asserts agrees with this one.
BLOCKING_EXECUTION_STATES: frozenset[ExecutionState] = frozenset({
    ExecutionState.PROTECTION_FAILED,
    ExecutionState.PLACEMENT_UNKNOWN,
    ExecutionState.RECONCILIATION_REQUIRED,
})


def holds_reservation(state: ExecutionState | str) -> bool:
    """Whether a row in ``state`` still has its notional reserved.

    The counting lives in :func:`strategy_lab.registry.reserved_notional`, which
    is where a session may be held; this is the predicate it counts on, and it
    is here so a caller can ask the question without one.
    """
    return ExecutionState(state) in RESERVING_EXECUTION_STATES


#: Non-terminal states, as a convenience for the same reason: a reader of this
#: module should be able to ask "is this row still in flight" without importing
#: the registry.
NON_TERMINAL_EXECUTION_STATES: frozenset[ExecutionState] = frozenset(
    state for state in ExecutionState if state not in TERMINAL_EXECUTION_STATES
)


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
