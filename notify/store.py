"""Writing the card and the delivery log. Every function here fails soft.

A notification is a *report about* work that already happened. If recording the
report fails — the database is down, the table is not migrated yet — the work it
reports on must not be lost, and neither must the delivery. So every function in
this module catches, logs, and returns a falsy value rather than raising into
``send_card`` or into a scheduled job.

The session factory is injected so the workspace and the bot can each hand in
their own, and so a test can hand in one bound to a scratch database.
"""

from __future__ import annotations

import json
from datetime import datetime

from utils.logger import get_logger
from utils.timeutils import utcnow_naive

log = get_logger("notify_store")

#: Statuses written to `notifications_sent.status`.
SENT = "sent"
FAILED = "failed"
SKIPPED = "skipped"


def _default_session_factory():
    from database.db import get_session

    return get_session


def store_card(
    *,
    uid: str,
    kind: str,
    ref: str,
    subject: str,
    payload: dict,
    session_factory=None,
    now: datetime | None = None,
) -> bool:
    """Persist a card's source data. Returns whether the row was written.

    The payload is the page's only input (``database.models.Card``), so it is
    written **before** the notification goes out: a link in an inbox that 404s
    because the row lost a race is worse than an email that was never sent.
    """
    factory = session_factory or _default_session_factory()
    try:
        from database.models import Card

        with factory() as session:
            session.add(
                Card(
                    card_uid=str(uid),
                    kind=str(kind or ""),
                    ref=str(ref or "")[:120],
                    subject=str(subject or ""),
                    payload_json=json.dumps(payload or {}, sort_keys=True, default=str),
                    created_at=now or utcnow_naive(),
                )
            )
        return True
    except Exception as exc:  # pragma: no cover - database-specific
        log.error("notify_card_store_failed", card_uid=str(uid), kind=kind, error=str(exc))
        return False


def load_card(uid: str, *, session_factory=None) -> dict | None:
    """The stored payload for ``uid``, or ``None``.

    ``None`` for an unknown uid **and** for an unreachable database, because the
    caller's only correct response to either is the same 404: a card page that
    said "temporarily unavailable" would be a channel for probing which uids
    exist.
    """
    factory = session_factory or _default_session_factory()
    try:
        from database.models import Card

        with factory() as session:
            row = session.query(Card).filter(Card.card_uid == str(uid)).one_or_none()
            if row is None:
                return None
            payload = row.payload
            payload.setdefault("kind", row.kind)
            payload.setdefault("subject", row.subject)
            return payload
    except Exception as exc:  # pragma: no cover - database-specific
        log.error("notify_card_load_failed", card_uid=str(uid), error=str(exc))
        return None


def record_send(
    *,
    kind: str,
    ref: str,
    channel: str,
    status: str,
    provider_id: str = "",
    error: str = "",
    card_uid: str = "",
    session_factory=None,
    now: datetime | None = None,
) -> bool:
    """Append one delivery attempt. Returns whether the row was written."""
    factory = session_factory or _default_session_factory()
    try:
        from database.models import NotificationSend

        with factory() as session:
            session.add(
                NotificationSend(
                    kind=str(kind or "")[:32],
                    ref=str(ref or "")[:120],
                    channel=str(channel or "")[:32],
                    status=str(status or "")[:24],
                    provider_id=str(provider_id or "")[:120],
                    error=str(error or "")[:4000],
                    card_uid=str(card_uid or "")[:64],
                    created_at=now or utcnow_naive(),
                )
            )
        return True
    except Exception as exc:  # pragma: no cover - database-specific
        log.error(
            "notify_send_record_failed",
            kind=kind,
            ref=ref,
            channel=channel,
            error=str(exc),
        )
        return False
