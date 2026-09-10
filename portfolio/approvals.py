"""The signed, expiring, single-use owner approval — and the channel it goes out on.

Spec L §6.3: **approval is per-order, single-use, expiring, and owner-bound.** A
thesis approval, a strategy promotion, and a prior approval are none of them an
approval of this order. Spec Q §13 adds the shape: the callback carries a signed,
expiring reference, and duplicate, stale, mismatched-owner, non-proposed or
already-used callbacks are rejected.

Four independent controls, because each one fails differently:

``signature``
    HMAC-SHA256 over ``proposal_uid | nonce | expiry | owner``. Binds the
    callback to *this* proposal and *this* owner. The key is
    ``EXECUTION_APPROVAL_SECRET``; with it unset no card can be minted, which is
    the correct failure — a card nobody can forge is worth less than no card at
    all only if it also cannot be *approved*, and an unsigned one can.
``expiry``
    An approval that has been sitting in a chat for a day is not an approval of
    a position sized against yesterday's book.
``single use``
    Enforced in the database by ``proposals.approval_consumed_at``, not in
    memory: the replay to defend against is the one that arrives after a
    restart.
``owner binding``
    The out-of-band channel's own identity check, on top of the signature.

**Why the callback carries a truncated signature.** Telegram caps
``callback_data`` at 64 bytes, and a full base64 HMAC plus the proposal id and a
prefix does not fit. So the card carries the first
:data:`SIGNATURE_PREFIX_CHARS` characters — 72 bits of the digest — and the full
signature stays on the row and is recomputed at verification. That is a
deliberate trade with the reasoning written down: 72 bits is far beyond
brute-force in the seconds-to-minutes an approval lives, and it is the *third*
of four controls rather than the only one. If the approval channel later becomes
a signed ``/admin`` page (Spec K §7), :func:`verify` accepts the full signature
unchanged and the truncation goes away with the channel that forced it.

**The card channel is an injected callable**, exactly like
:mod:`portfolio.paging`. ``propose_order`` is reachable from the MCP surface and
that surface may never import ``bot`` (Spec L §6.1), so this module holds a
registry and ``main.py`` wires the Telegram sender into it. The default writes a
structured log line, which is what a Railway deploy scrapes anyway — and a
deployment that forgot to wire the channel therefore *loses the card*, which is
visible, rather than placing anything, which would not be.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from utils.logger import get_logger

log = get_logger("execution_approval")

#: Characters of the hex digest carried in the out-of-band callback.
SIGNATURE_PREFIX_CHARS = 18

#: The callback prefixes. Short because they share the 64-byte budget.
APPROVE_PREFIX = "p6ok"
REJECT_PREFIX = "p6no"

DEFAULT_TTL_SECONDS = 1800


class ApprovalRefused(Exception):
    """A refusal a human can act on, rather than a stack trace they cannot."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def signing_secret(settings) -> str:
    """The HMAC key, or a refusal.

    No fallback and no derived default. A secret that can be reconstructed from
    something else in the environment is not a secret, and a signature anyone
    can forge is worse than an obviously missing one because it *looks* like a
    control.
    """
    secret = (getattr(settings, "execution_approval_secret", "") or "").strip()
    if not secret:
        raise ApprovalRefused(
            "approval_secret_missing",
            "EXECUTION_APPROVAL_SECRET is not set, so no approval reference can "
            "be signed or verified. Set it (see docs/ENV_SETUP.md §9) before "
            "enabling PHASE6_EXECUTION_ENABLED; until then nothing can be "
            "approved, which is the intended failure.",
        )
    return secret


def ttl_seconds(settings) -> int:
    try:
        value = int(getattr(settings, "approval_ttl_seconds", DEFAULT_TTL_SECONDS) or DEFAULT_TTL_SECONDS)
    except (TypeError, ValueError):
        return DEFAULT_TTL_SECONDS
    return max(60, value)


def sign(*, proposal_uid: str, nonce: str, expires_at: datetime, owner_id: str, secret: str) -> str:
    """The full hex HMAC over the four bound fields.

    The payload is joined with a character that cannot appear in any of the
    fields, so ``("ab", "c")`` and ``("a", "bc")`` cannot produce one digest.
    """
    payload = "\x1f".join(
        (
            str(proposal_uid),
            str(nonce),
            _naive_utc(expires_at).replace(microsecond=0).isoformat(),
            str(owner_id or ""),
        )
    )
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class ApprovalReference:
    """What is written to the proposal row and what the card carries."""

    proposal_id: int
    proposal_uid: str
    nonce: str
    expires_at: datetime
    owner_id: str
    signature: str

    @property
    def prefix(self) -> str:
        return self.signature[:SIGNATURE_PREFIX_CHARS]

    @property
    def approve_callback(self) -> str:
        return f"{APPROVE_PREFIX}:{self.proposal_id}:{self.prefix}"

    @property
    def reject_callback(self) -> str:
        return f"{REJECT_PREFIX}:{self.proposal_id}:{self.prefix}"


def mint(
    *,
    proposal_id: int,
    proposal_uid: str,
    owner_id: str,
    now: datetime,
    settings,
) -> ApprovalReference:
    """Create the reference for one proposal. One per proposal, ever."""
    secret = signing_secret(settings)
    nonce = secrets.token_hex(16)
    expires_at = _naive_utc(now) + timedelta(seconds=ttl_seconds(settings))
    expires_at = expires_at.replace(microsecond=0)
    signature = sign(
        proposal_uid=proposal_uid,
        nonce=nonce,
        expires_at=expires_at,
        owner_id=owner_id,
        secret=secret,
    )
    return ApprovalReference(
        proposal_id=int(proposal_id),
        proposal_uid=str(proposal_uid),
        nonce=nonce,
        expires_at=expires_at,
        owner_id=str(owner_id or ""),
        signature=signature,
    )


def parse_callback(data: str) -> tuple[str, int, str]:
    """``"p6ok:12:9f2c..."`` -> ``("approve", 12, "9f2c...")``.

    Raises :class:`ApprovalRefused` on anything else. The out-of-band channel is
    the only caller, and it hands this whatever a chat client sent.
    """
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] not in (APPROVE_PREFIX, REJECT_PREFIX):
        raise ApprovalRefused("malformed_callback", f"unrecognised approval callback {data!r}.")
    try:
        proposal_id = int(parts[1])
    except ValueError:
        raise ApprovalRefused(
            "malformed_callback", f"approval callback {data!r} carries no proposal id."
        ) from None
    action = "approve" if parts[0] == APPROVE_PREFIX else "reject"
    return action, proposal_id, parts[2]


def verify(
    proposal,
    *,
    presented_signature: str,
    owner_id: str,
    now: datetime,
    settings,
) -> None:
    """Raise :class:`ApprovalRefused` unless this callback may release ``proposal``.

    Checks, in the order that leaks least: the proposal has an unconsumed
    reference at all, the owner matches, the signature verifies, and it has not
    expired. Consuming the reference is the caller's job and belongs in the same
    transaction as the status change, which is why it is not done here.
    """
    if proposal is None:
        raise ApprovalRefused("unknown_proposal", "no proposal matches this approval.")

    stored_signature = getattr(proposal, "approval_signature", "") or ""
    nonce = getattr(proposal, "approval_nonce", "") or ""
    expires_at = _naive_utc(getattr(proposal, "approval_expires_at", None))
    if not (stored_signature and nonce and expires_at):
        raise ApprovalRefused(
            "no_approval_reference",
            "this proposal carries no approval reference; it was never sent for "
            "approval, or it was created risk_rejected.",
        )

    if getattr(proposal, "approval_consumed_at", None) is not None:
        raise ApprovalRefused(
            "approval_already_used",
            "this approval has already been used. An approval is single-use: "
            "a replayed callback places nothing (Spec L §6.3). Propose again "
            "if you want another entry.",
        )

    expected_owner = str(getattr(proposal, "approval_owner_id", "") or "")
    if expected_owner and str(owner_id or "") != expected_owner:
        raise ApprovalRefused(
            "owner_mismatch",
            "this approval is bound to a different owner. Approval is "
            "owner-bound (Spec L §6.3).",
        )

    secret = signing_secret(settings)
    expected = sign(
        proposal_uid=str(getattr(proposal, "proposal_uid", "")),
        nonce=nonce,
        expires_at=expires_at,
        owner_id=expected_owner,
        secret=secret,
    )
    presented = (presented_signature or "").strip()
    if not hmac.compare_digest(expected, stored_signature):
        raise ApprovalRefused(
            "signature_mismatch",
            "the stored approval signature does not verify against the current "
            "secret. The signing key changed after this card was sent; propose "
            "again rather than approving a card that cannot be authenticated.",
        )
    if not presented or not hmac.compare_digest(expected[: len(presented)], presented):
        raise ApprovalRefused(
            "signature_mismatch", "this approval callback does not verify."
        )
    if len(presented) < SIGNATURE_PREFIX_CHARS:
        raise ApprovalRefused(
            "signature_too_short",
            f"an approval callback carries at least {SIGNATURE_PREFIX_CHARS} "
            "characters of the signature.",
        )

    if _naive_utc(now) > expires_at:
        raise ApprovalRefused(
            "approval_expired",
            f"this approval expired at {expires_at.isoformat()}Z. The position "
            "was sized against the book as it stood when the card was sent; "
            "propose again against the current one.",
        )


# --------------------------------------------------------------------------- #
# The out-of-band card channel
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ApprovalCard:
    """What the owner sees. ``body_md`` is also stored on the proposal row.

    ``callbacks`` are opaque to this module's consumers: the channel renders
    them as whatever it uses for buttons. They are ``None`` for a
    ``risk_rejected`` proposal, which is shown — with its reason — and cannot be
    approved.
    """

    proposal_id: int
    proposal_uid: str
    ticker: str
    status: str
    body_md: str
    approve_callback: str | None = None
    reject_callback: str | None = None
    expires_at: datetime | None = None
    detail: dict = field(default_factory=dict)

    @property
    def approvable(self) -> bool:
        return bool(self.approve_callback)


def log_card_sender(card: ApprovalCard) -> None:
    """The default channel: a structured log line, and nothing approvable.

    A deployment that never wired a real channel still records that a proposal
    was made and still cannot approve it, because the buttons only exist where
    the channel puts them.
    """
    log.info(
        "approval_card_not_delivered",
        proposal_id=card.proposal_id,
        ticker=card.ticker,
        status=card.status,
        approvable=card.approvable,
        note=(
            "no out-of-band approval channel is registered; the card was not "
            "delivered. Wire one with portfolio.approvals.register_card_sender."
        ),
    )


_SENDER = log_card_sender


def register_card_sender(sender) -> None:
    """Register ``callable(card: ApprovalCard) -> None``. ``main.py`` calls this."""
    global _SENDER
    _SENDER = sender or log_card_sender


def clear_card_sender() -> None:
    """Test seam: the registry is process-global, like the pager's."""
    global _SENDER
    _SENDER = log_card_sender


def has_card_sender() -> bool:
    return _SENDER is not log_card_sender


def send_card(card: ApprovalCard) -> bool:
    """Deliver ``card``. Never raises: a channel failure must not lose the row.

    Returns whether delivery was attempted by a registered channel. A proposal
    whose card could not be sent stays ``proposed`` and simply cannot be
    approved, which is the safe direction.
    """
    try:
        _SENDER(card)
        return True
    except Exception as exc:  # pragma: no cover - channel-specific
        log.error(
            "approval_card_send_failed",
            proposal_id=card.proposal_id,
            ticker=card.ticker,
            error=str(exc),
        )
        return False


class RecordingCardSender:
    """A channel that keeps what it was sent. For tests and ``--dry-run``."""

    def __init__(self):
        self.cards: list[ApprovalCard] = []

    def __call__(self, card: ApprovalCard) -> None:
        self.cards.append(card)

    @property
    def last(self) -> ApprovalCard | None:
        return self.cards[-1] if self.cards else None
