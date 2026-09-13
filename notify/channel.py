"""What a channel is handed, and what a channel must do with it.

One dataclass and one protocol. Deliberately small: the thing every channel
needs is a subject, a plain-text body, an HTML body, and a stable reference for
the row this is about — everything else is channel-specific and lives in
``detail``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

#: The card kinds. Stable strings — they are written to `notifications_sent.kind`
#: and to `cards.kind`, and an alert rule or a query matches on them.
KIND_PROPOSAL = "proposal"
KIND_SCAN_MEMO = "scan_memo"
KIND_SCORECARD = "scorecard"
KIND_PAGE = "page"
KIND_DIGEST = "digest"
#: A routine operational notice — an order filled, a target hit, a regime
#: change. Distinct from ``KIND_PAGE``, which means capital is exposed in a way
#: nobody chose and somebody has to act now. The two would be indistinguishable
#: in `notifications_sent` if they shared a kind, and "was I paged last week"
#: is exactly the question that log exists to answer.
KIND_ALERT = "alert"

KINDS = (
    KIND_PROPOSAL,
    KIND_SCAN_MEMO,
    KIND_SCORECARD,
    KIND_PAGE,
    KIND_DIGEST,
    KIND_ALERT,
)


@dataclass(frozen=True)
class Notification:
    """One message to a human, in whatever forms the channels can use.

    ``ref``
        a stable id for the underlying row — a proposal uid, a memo id, a page
        event name. It is what makes the delivery log queryable ("was I ever
        told about this proposal, and on which channel"), so it is worth a
        stable value even when nothing needs it at send time.
    ``text``
        the plain-text body. Every channel can render this; it is the fallback
        an email client that refuses HTML shows, and it is what a channel with
        no HTML at all sends.
    ``html``
        the email body. Empty when there is none, which is not an error.
    ``detail``
        channel-specific extras and the card's identity. Recognised keys:

        ``telegram``
            a dict merged over the Telegram payload — ``text``, ``parse_mode``,
            ``reply_markup``. This is how an *approvable* card keeps its inline
            keyboard while travelling the same path as everything else: the
            generic ``text`` has no buttons, and the approval card's does.
        ``card_uid`` / ``card_url``
            the stored card this notification carries, when there is one.
        ``attachments``
            ``[{"path", "filename"}]``, for a channel that can carry a file.
            The email channel base64s each one into the send; Telegram ignores
            the key, because the bot process sends a document through its own
            queue. Read at send time, so a channel that never sends never
            touches the disk.
    """

    kind: str
    subject: str
    text: str
    html: str = ""
    ref: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def card_uid(self) -> str:
        return str((self.detail or {}).get("card_uid") or "")

    @property
    def card_url(self) -> str:
        return str((self.detail or {}).get("card_url") or "")


@runtime_checkable
class Channel(Protocol):
    """A delivery channel. ``send`` never raises; it returns whether it sent.

    ``name`` is written to ``notifications_sent.channel`` and is the string the
    owner sees in a log line, so it is short and stable: ``"email"``,
    ``"telegram"``.
    """

    name: str

    def send(self, notification: Notification) -> bool:  # pragma: no cover - protocol
        ...
