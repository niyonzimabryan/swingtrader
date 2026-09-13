"""The Telegram channel, wrapping what already sends Telegram messages.

Two shapes of Telegram sender existed before this package, and both stay:

``TelegramChannel``
    posts to the Bot API over HTTPS, which is what the *workspace* process has
    to do — it may not import ``bot/`` (Spec L §6.1). This is the same request
    ``workspace/proposal_card.py`` has always made: same bot, same chat, same
    ``Markdown`` parse mode, same inline keyboard. Behaviour is unchanged when
    ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID`` are set.

``CallableChannel``
    wraps any ``callable(text, reply_markup) -> bool``, which is how the *bot*
    process plugs its in-process message queue in. The bot is the one holding
    the queue and the event loop; this package must not know about either.

Both write to ``notifications_sent`` so a Telegram delivery is as visible in the
log as an email one, and both swallow every exception.

**The inline keyboard.** ``Notification.detail["telegram"]`` is merged over the
payload, so an approvable card carries its ``reply_markup`` there while a page
or a digest carries none. That is what lets one registry deliver both without
the approval callback leaking into the generic path.
"""

from __future__ import annotations

from notify import store
from notify.channel import Notification
from utils.logger import get_logger

log = get_logger("notify_telegram")

TELEGRAM_API = "https://api.telegram.org"

#: Telegram's hard cap on `sendMessage.text`.
TELEGRAM_MSG_LIMIT = 4096

DEFAULT_TIMEOUT_SECONDS = 10.0


def telegram_overrides(notification: Notification) -> dict:
    """The channel-specific block, or ``{}``."""
    block = (notification.detail or {}).get("telegram")
    return dict(block) if isinstance(block, dict) else {}


def telegram_text(notification: Notification) -> str:
    """The body Telegram should show, truncated to the API's own limit.

    Truncation is marked. A silently cut message is a message whose last line
    might have been the one that mattered.
    """
    text = str(telegram_overrides(notification).get("text") or notification.text or "")
    if len(text) <= TELEGRAM_MSG_LIMIT:
        return text
    marker = "\n… (truncated; open the card page for the full version)"
    return text[: TELEGRAM_MSG_LIMIT - len(marker)] + marker


def _httpx_transport(url: str, payload: dict, timeout: float):
    import httpx

    response = httpx.post(url, json=payload, timeout=timeout)
    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text[:400]}
    return response.status_code, body


class TelegramChannel:
    """Posts to the Telegram Bot API over HTTPS. Never raises."""

    name = "telegram"

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        transport=None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        session_factory=None,
        api_base: str = TELEGRAM_API,
    ):
        self.bot_token = str(bot_token or "")
        self.chat_id = str(chat_id or "")
        self.transport = transport or _httpx_transport
        self.timeout = float(timeout)
        self.session_factory = session_factory
        self.api_base = api_base

    def build_payload(self, notification: Notification) -> dict:
        overrides = telegram_overrides(notification)
        payload = {
            "chat_id": self.chat_id,
            "text": telegram_text(notification),
            "parse_mode": overrides.get("parse_mode", "Markdown"),
        }
        if overrides.get("reply_markup") is not None:
            payload["reply_markup"] = overrides["reply_markup"]
        if payload.get("parse_mode") is None:
            payload.pop("parse_mode")
        return payload

    def send(self, notification: Notification) -> bool:
        if not (self.bot_token and self.chat_id):
            self._record(notification, store.SKIPPED, error="telegram channel not configured")
            log.warning("notify_telegram_unconfigured", kind=notification.kind, ref=notification.ref)
            return False
        try:
            status_code, body = self.transport(
                f"{self.api_base}/bot{self.bot_token}/sendMessage",
                self.build_payload(notification),
                self.timeout,
            )
        except Exception as exc:
            self._record(notification, store.FAILED, error=f"{type(exc).__name__}: {exc}")
            log.error(
                "notify_telegram_send_error",
                kind=notification.kind,
                ref=notification.ref,
                error=str(exc),
            )
            return False

        body = body if isinstance(body, dict) else {"raw": str(body)[:400]}
        if int(status_code) >= 400 or not body.get("ok", True):
            detail = str(body.get("description") or body)[:400]
            self._record(notification, store.FAILED, error=f"HTTP {status_code}: {detail}")
            log.error(
                "notify_telegram_send_failed",
                kind=notification.kind,
                ref=notification.ref,
                status=int(status_code),
                detail=detail,
            )
            return False

        result = body.get("result") if isinstance(body.get("result"), dict) else {}
        provider_id = str(result.get("message_id") or "")
        self._record(notification, store.SENT, provider_id=provider_id)
        log.info("notify_telegram_sent", kind=notification.kind, ref=notification.ref)
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


class CallableChannel:
    """Wraps an existing sender so the registry can treat it like any channel.

    ``sender(text, reply_markup)`` may return anything; only an exception counts
    as failure. The bot's queue-backed sender schedules a coroutine and returns
    immediately, so "it did not raise" is genuinely all this layer can know —
    and claiming more would be a lie in the delivery log.
    """

    def __init__(self, sender, *, name: str = "telegram", session_factory=None):
        self.sender = sender
        self.name = str(name)
        self.session_factory = session_factory

    def send(self, notification: Notification) -> bool:
        overrides = telegram_overrides(notification)
        try:
            self.sender(telegram_text(notification), overrides.get("reply_markup"))
        except Exception as exc:
            store.record_send(
                kind=notification.kind,
                ref=notification.ref,
                channel=self.name,
                status=store.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                card_uid=notification.card_uid,
                session_factory=self.session_factory,
            )
            log.error(
                "notify_channel_send_error",
                channel=self.name,
                kind=notification.kind,
                ref=notification.ref,
                error=str(exc),
            )
            return False
        store.record_send(
            kind=notification.kind,
            ref=notification.ref,
            channel=self.name,
            status=store.SENT,
            card_uid=notification.card_uid,
            session_factory=self.session_factory,
        )
        return True
