"""Which channels are on, and sending to all of them.

``configured_channels(settings)`` is the whole policy in one function:

============================  ===================  ==========================
``NOTIFY_EMAIL_ENABLED``      ``TELEGRAM_*`` set   channels
============================  ===================  ==========================
false                         no                   none
false                         yes                  telegram
true (key/from/to set)        no                   email
true (key/from/to set)        yes                  email, telegram
true (anything missing)       either               telegram if set, else none
============================  ===================  ==========================

Email is first in the list because it is the channel Bryan actually reads;
``broadcast`` sends to every channel regardless of order, so this is
presentation, not precedence.

A missing-but-enabled email configuration logs
``notify_email_channel_unconfigured`` naming exactly which variable is unset.
"Enabled but silently not sending" is the failure that costs a real message, so
it gets a named log line rather than a bare absence.
"""

from __future__ import annotations

from notify.channel import Notification
from notify.resend import ResendChannel, parse_recipients
from notify.telegram import TelegramChannel
from utils.logger import get_logger

log = get_logger("notify_registry")


def email_configured(settings) -> tuple[bool, list[str]]:
    """``(usable, missing_variable_names)`` for the email channel.

    Separate from :func:`configured_channels` so a caller — the workspace's
    startup warning, a health field, a test — can ask *what* is missing without
    building a channel it is not going to use.
    """
    if not bool(getattr(settings, "notify_email_enabled", False)):
        return False, ["NOTIFY_EMAIL_ENABLED"]
    missing = []
    if not (getattr(settings, "resend_api_key", "") or "").strip():
        missing.append("RESEND_API_KEY")
    if not (getattr(settings, "pager_email_from", "") or "").strip():
        missing.append("PAGER_EMAIL_FROM")
    if not parse_recipients(getattr(settings, "pager_email_to", "")):
        missing.append("PAGER_EMAIL_TO")
    return (not missing), missing


def telegram_configured(settings) -> bool:
    return bool(
        (getattr(settings, "telegram_bot_token", "") or "").strip()
        and (getattr(settings, "telegram_chat_id", "") or "").strip()
    )


def configured_channels(settings, *, session_factory=None) -> list:
    """Every channel whose flag is on and whose configuration is complete."""
    channels: list = []

    usable, missing = email_configured(settings)
    if usable:
        channels.append(
            ResendChannel(
                api_key=(settings.resend_api_key or "").strip(),
                sender=(settings.pager_email_from or "").strip(),
                recipients=settings.pager_email_to,
                session_factory=session_factory,
            )
        )
    elif bool(getattr(settings, "notify_email_enabled", False)):
        log.warning(
            "notify_email_channel_unconfigured",
            missing=",".join(missing),
            note=(
                "NOTIFY_EMAIL_ENABLED is on but the channel cannot be built; no "
                "email will be sent. See docs/NOTIFICATIONS.md."
            ),
        )

    if telegram_configured(settings):
        channels.append(
            TelegramChannel(
                bot_token=(settings.telegram_bot_token or "").strip(),
                chat_id=(settings.telegram_chat_id or "").strip(),
                session_factory=session_factory,
            )
        )

    return channels


def email_channels(settings, *, session_factory=None) -> list:
    """Only the email channel(s), for a caller that already sent to Telegram.

    The bot delivers the digest, the weekly report and the scan summary through
    its own message queue and must keep doing so (removing Telegram is the
    headless-runtime change's job). Handing those callers
    :func:`configured_channels` would send every one of them to Telegram twice.
    """
    return [
        channel
        for channel in configured_channels(settings, session_factory=session_factory)
        if getattr(channel, "name", "") == "email"
    ]


def broadcast(notification: Notification, *, settings=None, channels=None) -> dict:
    """Send to every channel. Returns ``{channel_name: delivered}``.

    Never raises, even if a channel does something a channel is not supposed to
    do: a broken channel must not take down the caller, which is usually a
    scheduled job or a ``propose_order`` that has already written its row.

    With no channel configured this logs ``notify_no_channel`` and returns
    ``{}``. That is the visible-and-safe direction the whole delivery layer is
    built around — a message that could not be delivered is a message that was
    not delivered, and pretending otherwise is what makes an outage silent.
    """
    if channels is None:
        if settings is None:
            from config.settings import Settings

            settings = Settings()
        channels = configured_channels(settings)

    if not channels:
        log.warning(
            "notify_no_channel",
            kind=notification.kind,
            ref=notification.ref,
            subject=notification.subject,
        )
        return {}

    results: dict = {}
    for channel in channels:
        name = getattr(channel, "name", channel.__class__.__name__)
        try:
            results[name] = bool(channel.send(notification))
        except Exception as exc:  # pragma: no cover - a channel that broke its contract
            results[name] = False
            log.error(
                "notify_channel_raised",
                channel=name,
                kind=notification.kind,
                ref=notification.ref,
                error=str(exc),
            )
    return results
