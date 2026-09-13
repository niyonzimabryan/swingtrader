"""The owner-decision queue: what the MCP tools write and the runtime acts on.

Spec K §10 / Spec L §10, owner ruling 2026-09-13. Bryan approves in his
coding-agent chat rather than in Telegram, and the property that makes that safe
is a division of labour, not a promise in a prompt:

**A tool records a decision. The runtime acts on one.**

Nothing in the workspace's import closure can place an order — there is still no
execute scope, no route, and no path from ``workspace/`` to ``execution/``,
``bot/`` or ``orchestrator/`` (``tests/test_no_execute_scope.py``). What changed
is that the owner may now say yes through a tool instead of a button. The yes is
a row in this table; ``orchestrator/approval_poller.py``, running in the bot
container, is the only thing that reads it and the only thing that calls
``execution/lifecycle.py::on_approval`` — which re-verifies the signed,
single-use, expiring, owner-bound reference and re-runs every risk check from
fresh state before anything reaches a broker.

This module lives in ``portfolio/`` because both sides need it and ``portfolio/``
is the one package both sides may import. It holds the store and nothing else:
no rendering, no plan computation, no execution.

**The claim is the concurrency control.** :func:`claim` is a single conditional
``UPDATE ... WHERE claimed_at IS NULL`` whose row count decides the winner. That
is atomic on SQLite and on Postgres alike, needs no dialect branch, and is why
two pollers — or a poller racing the Telegram callback — cannot both act on one
decision. On Postgres :func:`claimable` additionally selects ``FOR UPDATE SKIP
LOCKED`` so a second poller walks past a row the first is already claiming
rather than waiting on it; that is a throughput choice, and correctness rests on
the conditional update either way.

For an order approval the claim sits *in front of* the real single-use lock and
does not replace it: ``proposals.approval_consumed_at`` is still consumed inside
``on_approval``'s own transaction, so even a claim that somehow double-fired
would place once.
"""

from __future__ import annotations

import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

from database.models import (
    OWNER_ACTION_KINDS,
    OWNER_ACTION_PENDING_STATUSES,
    OWNER_ACTION_STATUSES,
    OWNER_ACTION_SUBJECTS,
    OWNER_ACTION_TERMINAL_STATUSES,
    OwnerAction,
)
from portfolio import approvals as approvals_mod
from utils.logger import get_logger
from utils.timeutils import utcnow_naive

log = get_logger("owner_actions")

#: The two kinds that need the runtime to build a plan before there is anything
#: to confirm. Everything else is recorded as ``confirmed`` in one call.
TWO_STEP_KINDS: tuple[str, ...] = ("promote_arm", "demote_arm")

DEFAULT_TTL_SECONDS = 900


class OwnerActionRefused(Exception):
    """A refusal the owner can act on, rather than a stack trace they cannot."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _naive(value: datetime | None) -> datetime | None:
    """Naive UTC, so a stored column and an injected ``now`` compare."""
    if value is None:
        return None
    return (
        value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    )


def ttl_seconds(settings) -> int:
    try:
        value = int(
            getattr(settings, "owner_action_ttl_seconds", DEFAULT_TTL_SECONDS)
            or DEFAULT_TTL_SECONDS
        )
    except (TypeError, ValueError):
        return DEFAULT_TTL_SECONDS
    return max(60, value)


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def open_action_for(session, subject_kind: str, subject_ref: str):
    """The decision already outstanding on this subject, or ``None``.

    "Outstanding" is any non-terminal status. A second decision on a subject
    that already has one is refused rather than queued: two approvals of one
    proposal is exactly the replay the single-use rule exists to stop, and
    telling the owner "you already approved this" is a better answer than
    silently making it a no-op later.
    """
    return (
        session.query(OwnerAction)
        .filter(OwnerAction.subject_kind == str(subject_kind))
        .filter(OwnerAction.subject_ref == str(subject_ref))
        .filter(OwnerAction.status.notin_(OWNER_ACTION_TERMINAL_STATUSES))
        .order_by(OwnerAction.id.desc())
        .first()
    )


def get_by_uid(session, action_uid: str):
    return (
        session.query(OwnerAction)
        .filter(OwnerAction.action_uid == str(action_uid or ""))
        .one_or_none()
    )


def record(
    session,
    *,
    kind: str,
    subject_kind: str,
    subject_ref: str,
    status: str,
    owner_id: str,
    token_label: str = "",
    payload: dict | None = None,
    card_md: str = "",
    reason: str = "",
    now: datetime | None = None,
) -> OwnerAction:
    """Write one decision. Validates the vocabulary here rather than at the CHECK.

    A CHECK-constraint violation surfaces as an opaque ``IntegrityError`` from
    whichever engine you happen to be on; these refusals name the value.
    """
    if kind not in OWNER_ACTION_KINDS:
        raise OwnerActionRefused(
            "unknown_kind", f"{kind!r} is not one of {list(OWNER_ACTION_KINDS)}."
        )
    if subject_kind not in OWNER_ACTION_SUBJECTS:
        raise OwnerActionRefused(
            "unknown_subject",
            f"{subject_kind!r} is not one of {list(OWNER_ACTION_SUBJECTS)}.",
        )
    if status not in OWNER_ACTION_STATUSES:
        raise OwnerActionRefused(
            "unknown_status", f"{status!r} is not one of {list(OWNER_ACTION_STATUSES)}."
        )
    moment = now or utcnow_naive()
    row = OwnerAction(
        action_uid=str(uuid.uuid4()),
        kind=kind,
        status=status,
        subject_kind=subject_kind,
        subject_ref=str(subject_ref),
        payload_json=json.dumps(payload or {}, sort_keys=True, default=str),
        card_md=card_md or "",
        reason=reason or "",
        requested_by=str(owner_id or ""),
        requested_token_label=str(token_label or "")[:100],
        requested_at=moment,
        confirmed_at=(moment if status == "confirmed" else None),
        created_at=moment,
        updated_at=moment,
    )
    session.add(row)
    session.flush()
    log.info(
        "owner_action_recorded",
        action_uid=row.action_uid,
        kind=kind,
        subject=f"{subject_kind}:{subject_ref}",
        status=status,
        token_label=row.requested_token_label,
    )
    return row


def set_payload(row: OwnerAction, payload: dict) -> None:
    row.payload_json = json.dumps(payload or {}, sort_keys=True, default=str)


def finish(
    session,
    row: OwnerAction,
    *,
    status: str,
    outcome_code: str = "",
    outcome_detail: str = "",
    now: datetime | None = None,
) -> OwnerAction:
    """Move a row to a terminal status and record what happened.

    A refusal from the execution service is an *outcome*, not a reason to leave
    the row claimable: retrying an approval that was refused for a risk breach
    would be the system arguing with its own guards.
    """
    moment = now or utcnow_naive()
    row.status = status
    row.outcome_code = (outcome_code or "")[:60]
    row.outcome_detail = outcome_detail or ""
    if status == "executed":
        row.executed_at = moment
    row.updated_at = moment
    session.flush()
    log.info(
        "owner_action_finished",
        action_uid=row.action_uid,
        kind=row.kind,
        status=status,
        outcome_code=row.outcome_code,
    )
    return row


# --------------------------------------------------------------------------- #
# Claiming
# --------------------------------------------------------------------------- #


def claimable(session, *, kinds=None, statuses=None, limit: int = 25) -> list[int]:
    """Ids of unclaimed rows the poller may work on, oldest first.

    Returns ids rather than rows on purpose: the claim below is a separate,
    conditional statement, and handing back a loaded ORM object would invite
    acting on a row somebody else claimed between the select and the update.
    """
    query = (
        session.query(OwnerAction.id)
        .filter(OwnerAction.claimed_at.is_(None))
        .filter(
            OwnerAction.status.in_(tuple(statuses or OWNER_ACTION_PENDING_STATUSES))
        )
    )
    if kinds:
        query = query.filter(OwnerAction.kind.in_(tuple(kinds)))
    query = query.order_by(OwnerAction.id.asc()).limit(max(1, int(limit)))
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        # Throughput, not correctness: a second poller walks past a row the
        # first is mid-claim on instead of blocking behind it. SQLite has no
        # row-level lock to skip, and the conditional UPDATE below is what
        # actually decides the winner on both engines.
        query = query.with_for_update(skip_locked=True)
    return [row_id for (row_id,) in query.all()]


def claim(session, action_id: int, *, claimed_by: str, now: datetime | None = None) -> bool:
    """Take the row, or report that somebody else already has it.

    One conditional ``UPDATE``. The engine evaluates the predicate and applies
    the change under the same row lock, so exactly one caller can see a row
    count of 1 — which is the whole of the mutual exclusion, on either engine.
    """
    moment = now or utcnow_naive()
    updated = (
        session.query(OwnerAction)
        .filter(OwnerAction.id == int(action_id))
        .filter(OwnerAction.claimed_at.is_(None))
        .update(
            {
                OwnerAction.claimed_at: moment,
                OwnerAction.claimed_by: str(claimed_by or "")[:64],
                OwnerAction.updated_at: moment,
            },
            synchronize_session=False,
        )
    )
    return bool(updated)


def release(session, row: OwnerAction, *, now: datetime | None = None) -> None:
    """Hand a claimed row back, for a failure that is worth retrying.

    Used only where the *attempt* could not be made at all — the execution
    service is not wired in this deployment, say. A refusal that the guards
    produced is terminal and goes through :func:`finish`.
    """
    row.claimed_at = None
    row.claimed_by = ""
    row.updated_at = now or utcnow_naive()
    session.flush()


# --------------------------------------------------------------------------- #
# The confirmation a tier change carries (Spec Q §13)
# --------------------------------------------------------------------------- #
#
# A proposal already has a signed reference, minted at `propose_order`. A tier
# change has none, because nothing mints one for it, so this is where its four
# controls come from. It is the same HMAC — `portfolio.approvals.sign` — over the
# same four bound fields, so there is one signature scheme in the codebase and
# one place to reason about it.


def _confirmation_uid(row: OwnerAction) -> str:
    """Everything the confirmation binds, in one string.

    The kind, the subject and the prepared plan are all in here, so a
    confirmation signed against one plan cannot confirm another: the signature
    fails before any arm row is read.
    """
    payload = row.payload if isinstance(row.payload, dict) else {}
    return "\x1f".join(
        (
            "owner_action",
            str(row.action_uid),
            str(row.kind),
            f"{row.subject_kind}:{row.subject_ref}",
            str(payload.get("target_arm_id", "")),
            str(payload.get("evidence_metric_snapshot_id", "")),
            str(payload.get("to_mode", "")),
            f"{float(payload.get('requested_risk_budget') or 0.0):.10f}",
        )
    )


def mint_confirmation(
    row: OwnerAction, *, owner_id: str, settings, now: datetime | None = None
) -> OwnerAction:
    """Sign this prepared action. The runtime calls it; one per action, ever."""
    import secrets

    secret = approvals_mod.signing_secret(settings)
    moment = now or utcnow_naive()
    row.confirm_nonce = secrets.token_hex(16)
    row.confirm_expires_at = (moment + timedelta(seconds=ttl_seconds(settings))).replace(
        microsecond=0
    )
    row.confirm_owner_id = str(owner_id or "")
    row.confirm_signature = approvals_mod.sign(
        proposal_uid=_confirmation_uid(row),
        nonce=row.confirm_nonce,
        expires_at=row.confirm_expires_at,
        owner_id=row.confirm_owner_id,
        secret=secret,
    )
    row.updated_at = moment
    return row


def verify_confirmation(
    row: OwnerAction,
    *,
    presented_signature: str,
    owner_id: str,
    settings,
    now: datetime | None = None,
) -> None:
    """Raise :class:`OwnerActionRefused` unless this confirmation may release ``row``.

    The same four controls as an execution approval, checked in the order that
    leaks least: there is a reference at all, the owner matches, the signature
    verifies, it has not expired, and it has not already been used. Consuming it
    is the caller's job and belongs in the same transaction as the status
    change, which is why it is not done here.
    """
    moment = now or utcnow_naive()
    if not (row.confirm_signature and row.confirm_nonce and row.confirm_expires_at):
        raise OwnerActionRefused(
            "no_confirmation_reference",
            "this action carries no confirmation reference: it has not been "
            "prepared yet, or it was refused before preparation. There is "
            "nothing to confirm.",
        )
    if row.confirm_consumed_at is not None:
        raise OwnerActionRefused(
            "confirmation_already_used",
            "this confirmation has already been used. It is single-use: a "
            "replay changes nothing. Ask again if you want another tier change.",
        )
    expected_owner = str(row.confirm_owner_id or "")
    if expected_owner and str(owner_id or "") != expected_owner:
        raise OwnerActionRefused(
            "owner_mismatch",
            "this confirmation is bound to a different owner (Spec Q §13).",
        )
    expected = approvals_mod.sign(
        proposal_uid=_confirmation_uid(row),
        nonce=row.confirm_nonce,
        expires_at=row.confirm_expires_at,
        owner_id=expected_owner,
        secret=approvals_mod.signing_secret(settings),
    )
    if not hmac.compare_digest(expected, row.confirm_signature or ""):
        raise OwnerActionRefused(
            "signature_mismatch",
            "the stored confirmation signature does not verify against the "
            "current secret, or the prepared plan has changed since it was "
            "signed. Ask again rather than confirming a card that cannot be "
            "authenticated.",
        )
    presented = (presented_signature or "").strip()
    if len(presented) < approvals_mod.SIGNATURE_PREFIX_CHARS:
        raise OwnerActionRefused(
            "signature_too_short",
            "a confirmation carries at least "
            f"{approvals_mod.SIGNATURE_PREFIX_CHARS} characters of the signature.",
        )
    if not hmac.compare_digest(expected[: len(presented)], presented):
        raise OwnerActionRefused(
            "signature_mismatch", "this confirmation does not verify."
        )
    if _naive(moment) > _naive(row.confirm_expires_at):
        raise OwnerActionRefused(
            "confirmation_expired",
            f"this confirmation expired at {row.confirm_expires_at.isoformat()}Z. "
            "The card described the world as it was; ask again against the "
            "world as it now is.",
        )


def consume_confirmation(
    session,
    row: OwnerAction,
    *,
    presented_signature: str,
    owner_id: str,
    settings,
    now: datetime | None = None,
) -> OwnerAction:
    """Verify and consume, in one transaction. The workspace calls this."""
    moment = now or utcnow_naive()
    verify_confirmation(
        row,
        presented_signature=presented_signature,
        owner_id=owner_id,
        settings=settings,
        now=moment,
    )
    row.confirm_consumed_at = moment
    row.confirmed_at = moment
    row.status = "confirmed"
    row.updated_at = moment
    session.flush()
    return row


def expire_stale(session, *, now: datetime | None = None) -> int:
    """Move prepared-but-unconfirmed actions past their TTL to ``expired``.

    Housekeeping the poller does on each pass. Without it a prepared action sits
    in ``proposals_pending`` forever looking actionable, and a stale card that
    still *looks* approvable is the failure mode the TTL exists to prevent.
    """
    moment = now or utcnow_naive()
    stale = (
        session.query(OwnerAction)
        .filter(OwnerAction.status == "prepared")
        .filter(OwnerAction.confirm_expires_at.isnot(None))
        .filter(OwnerAction.confirm_expires_at < moment)
        .all()
    )
    for row in stale:
        finish(
            session,
            row,
            status="expired",
            outcome_code="confirmation_expired",
            outcome_detail=(
                "the confirmation expired unconfirmed; nothing was changed."
            ),
            now=moment,
        )
    return len(stale)


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def payload_of(row: OwnerAction) -> dict:
    """One action as an MCP response. Never the signature — only its prefix.

    The prefix is what a confirming call quotes back, exactly as the Telegram
    callback carries a prefix rather than the whole digest (Spec L §6.3). The
    full signature stays on the row and is recomputed at verification.
    """
    prefix = (row.confirm_signature or "")[: approvals_mod.SIGNATURE_PREFIX_CHARS]
    return {
        "action_uid": row.action_uid,
        "kind": row.kind,
        "status": row.status,
        "subject": {"kind": row.subject_kind, "ref": row.subject_ref},
        "payload": row.payload,
        "card_md": row.card_md,
        "reason": row.reason or None,
        "requested_by": row.requested_by,
        "requested_token_label": row.requested_token_label,
        "requested_at": row.requested_at.isoformat() if row.requested_at else None,
        "confirmation_signature": prefix or None,
        "confirmation_expires_at": (
            row.confirm_expires_at.isoformat() if row.confirm_expires_at else None
        ),
        "claimed_at": row.claimed_at.isoformat() if row.claimed_at else None,
        "executed_at": row.executed_at.isoformat() if row.executed_at else None,
        "outcome_code": row.outcome_code or None,
        "outcome_detail": row.outcome_detail or None,
        "placed": False,
    }
