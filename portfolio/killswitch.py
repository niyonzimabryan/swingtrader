"""The persistent kill switch, and the other thing that blocks a new entry.

Spec L §6.5: ``/live_kill on`` blocks approval-to-placement **and survives
restart**. Spec Q §12 invariant 9 says the same thing from the other side: a
persistent kill switch blocks new orders even after a process restart. So the
state is a database row, not a module variable and not an environment variable —
the restart it has to survive is the one that follows the incident that made
someone reach for it.

Two things block a new entry, and they are separate on purpose:

:func:`engaged`
    the owner pulled the switch. Deliberate, and cleared deliberately.
:func:`blocking_proposals`
    a filled position whose protective stop could not be read back
    (``unprotected``), or a proposal whose broker outcome is unknown
    (``reconciliation_required``). Spec L §5.1 and Spec Q §12 invariant 3: a new
    live entry is refused while any existing one is unprotected. This one is not
    a switch anybody threw; it is the system refusing to open a second position
    while the first is uninsured.

Neither cancels anything already at the broker. Cancelling a live order is a
human decision made in the broker's own app, and code that "helpfully" flattened
a book during an outage would be the worst failure available here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from database.models import PROPOSAL_BLOCKING_STATUSES, ExecutionKillSwitch, Proposal
from utils.timeutils import utcnow_naive

#: The single row's primary key. There is one switch.
SWITCH_ID = 1

KILL_SWITCH_ENGAGED = "kill_switch_engaged"
UNPROTECTED_POSITION = "unprotected_position_blocks_entries"


@dataclass(frozen=True)
class SwitchState:
    engaged: bool
    reason: str = ""
    changed_by: str = ""
    engaged_at: datetime | None = None
    released_at: datetime | None = None

    def as_dict(self) -> dict:
        return {
            "engaged": self.engaged,
            "reason": self.reason,
            "changed_by": self.changed_by,
            "engaged_at": self.engaged_at.isoformat() if self.engaged_at else None,
            "released_at": self.released_at.isoformat() if self.released_at else None,
        }


def _row(session):
    return session.get(ExecutionKillSwitch, SWITCH_ID)


def state(session) -> SwitchState:
    """The current switch state. A missing row reads as *not engaged*.

    Absence is the only reading a missing row can honestly be given — the table
    is created empty by the migration — and the surrounding gates (the Phase 6
    flag, ``ALLOW_LIVE_TRADING``, ``EXECUTION_MODE``, a recorded approval) all
    default closed, so a missing row cannot by itself let anything through.
    """
    row = _row(session)
    if row is None:
        return SwitchState(engaged=False)
    return SwitchState(
        engaged=bool(row.engaged),
        reason=row.reason or "",
        changed_by=row.changed_by or "",
        engaged_at=row.engaged_at,
        released_at=row.released_at,
    )


def engaged(session) -> bool:
    return state(session).engaged


def set_switch(session, *, on: bool, changed_by: str = "", reason: str = "", now: datetime | None = None) -> SwitchState:
    """Engage or release the switch, and record who and when."""
    moment = now or utcnow_naive()
    row = _row(session)
    if row is None:
        row = ExecutionKillSwitch(id=SWITCH_ID, engaged=False)
        session.add(row)
    row.engaged = bool(on)
    row.reason = reason or ""
    row.changed_by = changed_by or ""
    if on:
        row.engaged_at = moment
    else:
        row.released_at = moment
    row.updated_at = moment
    session.flush()
    return state(session)


def blocking_proposals(session) -> list:
    """Proposals whose state blocks every further entry, newest first."""
    return (
        session.query(Proposal)
        .filter(Proposal.status.in_(PROPOSAL_BLOCKING_STATUSES))
        .order_by(Proposal.id.desc())
        .all()
    )


def entry_block(session) -> tuple[str, str] | None:
    """``(code, reason)`` when a new entry may not proceed, else ``None``.

    Checked twice on purpose: once at proposal time, so the agent is told why
    rather than being handed a card that cannot be approved, and once at
    approval-to-placement, which is the check that actually holds. The first is
    a courtesy; only the second is a control.
    """
    switch = state(session)
    if switch.engaged:
        detail = f" ({switch.reason})" if switch.reason else ""
        return (
            KILL_SWITCH_ENGAGED,
            f"the live kill switch is engaged{detail}. No approval may reach a "
            "placement while it is on; release it with /live_kill off.",
        )

    blocked = blocking_proposals(session)
    if blocked:
        first = blocked[0]
        return (
            UNPROTECTED_POSITION,
            f"proposal {first.id} ({first.ticker}) is {first.status}: its "
            "protective stop could not be verified at the broker, so every "
            "further entry is blocked until it is resolved (Spec L §5.1, "
            "Spec Q §12 invariant 3).",
        )
    return None
