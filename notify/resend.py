"""The Resend email channel.

Resend's whole API surface for this is one call::

    POST https://api.resend.com/emails
    Authorization: Bearer re_...
    {"from": ..., "to": [...], "subject": ..., "html": ..., "text": ...}

which is why there is no SDK here. ``httpx`` is already a dependency and the
request is four keys; an SDK would add a package, a version pin, and a second
retry policy to reason about, in exchange for nothing.

The transport is injected (``transport=``) so the tests can assert on the exact
request body without a network. The default posts with ``httpx``.

**Fail closed, and never raise.** Every failure path — no key, no recipient, a
4xx, a timeout, a malformed response — logs, writes a ``failed`` row to
``notifications_sent``, and returns ``False``. Nothing in here can unwind the
proposal, page, or report it was delivering.
"""

from __future__ import annotations

import base64
import os

from notify import store
from notify.channel import Notification
from utils.logger import get_logger

log = get_logger("notify_resend")

RESEND_API_URL = "https://api.resend.com/emails"

DEFAULT_TIMEOUT_SECONDS = 10.0

#: The largest file this channel will base64 into a send, in bytes. Resend's own
#: documented ceiling is 40 MB for the whole request, and base64 costs a third
#: on top — but the real reason for a much smaller number is that this is the
#: *only* channel a headless deployment has. A 30 MB attachment that 413s takes
#: the message down with it, so an oversized file is dropped and the body says
#: where it is instead. Losing the attachment beats losing the email.
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024


def parse_recipients(value) -> list[str]:
    """``"a@b, c@d"`` or ``["a@b"]`` -> ``["a@b", "c@d"]``. Order preserved."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        candidates = list(value)
    else:
        candidates = str(value).replace(";", ",").split(",")
    seen: list[str] = []
    for candidate in candidates:
        address = str(candidate).strip()
        if address and address not in seen:
            seen.append(address)
    return seen


def _httpx_transport(url: str, headers: dict, payload: dict, timeout: float):
    """The default transport. Returns ``(status_code, parsed_body_or_text)``."""
    import httpx

    response = httpx.post(url, headers=headers, json=payload, timeout=timeout)
    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text[:400]}
    return response.status_code, body


class ResendChannel:
    """Sends a :class:`~notify.channel.Notification` as an HTML email.

    Constructed by :func:`notify.registry.configured_channels` when
    ``NOTIFY_EMAIL_ENABLED`` is on and a key, a sender and at least one
    recipient are configured. With any of those missing the channel is simply
    not built, so ``send`` is never reached in a half-configured state.
    """

    name = "email"

    def __init__(
        self,
        *,
        api_key: str,
        sender: str,
        recipients,
        transport=None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        session_factory=None,
        api_url: str = RESEND_API_URL,
    ):
        self.api_key = str(api_key or "")
        self.sender = str(sender or "")
        self.recipients = parse_recipients(recipients)
        self.transport = transport or _httpx_transport
        self.timeout = float(timeout)
        self.session_factory = session_factory
        self.api_url = api_url

    # -- the request ------------------------------------------------------- #

    def build_payload(self, notification: Notification) -> dict:
        """The exact JSON body posted to Resend. Four keys, deliberately.

        ``text`` travels alongside ``html`` always: a text part is what a client
        that refuses HTML shows, and its absence is a well-known spam signal.

        **No ``tags``.** Resend takes a ``tags`` array, and an earlier draft sent
        the card kind and the row reference in it. It was removed: a tag value
        Resend rejects fails the *whole* send with a 422, and the thing it would
        have bought — "which kind, about which row, delivered when" — is already
        in ``notifications_sent``, which this package writes on every attempt and
        which is authoritative in a way a vendor console is not. Risking the only
        channel for a duplicate of a log we own is a bad trade.
        """
        payload = {
            "from": self.sender,
            "to": list(self.recipients),
            "subject": notification.subject,
            "text": notification.text,
        }
        if notification.html:
            payload["html"] = notification.html
        attachments = self.build_attachments(notification)
        if attachments:
            payload["attachments"] = attachments
        return payload

    def build_attachments(self, notification: Notification) -> list[dict]:
        """``detail["attachments"]`` — ``[{"path", "filename"}]`` — read off disk.

        Added by the headless runtime: with Telegram off there is no
        ``send_document``, so the deep-research PDF has to travel as an email
        attachment or not at all. It is a ``detail`` key rather than a field on
        :class:`~notify.channel.Notification` because it is channel-specific in
        exactly the way ``detail["telegram"]`` is — Telegram ignores it and
        sends the document through its own queue.

        A file that is missing, unreadable, or over
        :data:`MAX_ATTACHMENT_BYTES` is **skipped with a log line**, never
        raised: the message it was attached to is worth sending without it.
        """
        requested = (notification.detail or {}).get("attachments")
        if not requested:
            return []
        attachments: list[dict] = []
        for entry in requested:
            entry = entry if isinstance(entry, dict) else {"path": entry}
            path = str(entry.get("path") or "")
            if not path:
                continue
            filename = str(entry.get("filename") or "") or os.path.basename(path)
            try:
                size = os.path.getsize(path)
                if size > MAX_ATTACHMENT_BYTES:
                    log.warning(
                        "notify_email_attachment_too_large",
                        kind=notification.kind,
                        ref=notification.ref,
                        filename=filename,
                        bytes=size,
                        limit=MAX_ATTACHMENT_BYTES,
                    )
                    continue
                with open(path, "rb") as handle:
                    content = base64.b64encode(handle.read()).decode("ascii")
            except Exception as exc:
                log.warning(
                    "notify_email_attachment_unreadable",
                    kind=notification.kind,
                    ref=notification.ref,
                    filename=filename,
                    error=str(exc),
                )
                continue
            attachments.append({"filename": filename, "content": content})
        return attachments

    # -- delivery ---------------------------------------------------------- #

    def send(self, notification: Notification) -> bool:
        if not (self.api_key and self.sender and self.recipients):
            self._record(notification, store.SKIPPED, error="resend channel not configured")
            log.warning("notify_email_unconfigured", kind=notification.kind, ref=notification.ref)
            return False

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            status_code, body = self.transport(
                self.api_url, headers, self.build_payload(notification), self.timeout
            )
        except Exception as exc:
            self._record(notification, store.FAILED, error=f"{type(exc).__name__}: {exc}")
            log.error(
                "notify_email_send_error",
                kind=notification.kind,
                ref=notification.ref,
                error=str(exc),
            )
            return False

        body = body if isinstance(body, dict) else {"raw": str(body)[:400]}
        if int(status_code) >= 400:
            detail = str(body.get("message") or body)[:400]
            self._record(notification, store.FAILED, error=f"HTTP {status_code}: {detail}")
            log.error(
                "notify_email_send_failed",
                kind=notification.kind,
                ref=notification.ref,
                status=int(status_code),
                detail=detail,
            )
            return False

        provider_id = str(body.get("id") or "")
        self._record(notification, store.SENT, provider_id=provider_id)
        log.info(
            "notify_email_sent",
            kind=notification.kind,
            ref=notification.ref,
            provider_id=provider_id,
            recipients=len(self.recipients),
        )
        return True

    def _record(self, notification: Notification, status: str, *, provider_id: str = "", error: str = "") -> None:
        store.record_send(
            kind=notification.kind,
            ref=notification.ref,
            channel=self.name,
            status=status,
            provider_id=provider_id,
            error=error,
            card_uid=notification.card_uid,
            session_factory=self.session_factory,
        )
