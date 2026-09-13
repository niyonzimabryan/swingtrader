"""Card kinds, and the one call that mints, stores and delivers one.

:func:`deliver` is the whole flow in the order it must happen:

1. mint a uid and sign the link,
2. **store the payload** — the page reads this and nothing else, so a link that
   reaches an inbox before the row exists would 404,
3. render the email and the plain-text part from the stored payload,
4. broadcast to every configured channel.

Everything below step 1 is pure given the payload, which is what makes the page
re-renderable months later and makes the golden tests meaningful.
"""

from __future__ import annotations

from notify import store
from notify.cards import alert, base, chart, digest, memo, proposal, scorecard
from notify.cards.base import RenderedCard, render, render_html, render_text
from notify.channel import Notification
from notify.links import CardLinkUnavailable, card_links, new_card_uid
from notify.registry import broadcast
from utils.logger import get_logger

log = get_logger("notify_cards")

__all__ = [
    "RenderedCard",
    "alert",
    "base",
    "broadcast_card",
    "chart",
    "deliver",
    "digest",
    "memo",
    "proposal",
    "render",
    "render_html",
    "render_text",
    "scorecard",
]


def deliver(
    payload: dict,
    *,
    settings,
    channels=None,
    telegram=None,
    session_factory=None,
    store_card: bool = True,
) -> dict:
    """Mint, store, render and send one card. Returns ``{channel: delivered}``.

    ``telegram`` is the channel-specific override block (``text``,
    ``parse_mode``, ``reply_markup``). It is how an approval card keeps its
    inline keyboard and its existing Markdown body while travelling this path:
    Telegram's message is unchanged, and the email is the new thing.

    Never raises. A failure to sign, store, or render logs and falls through to
    delivering what it can — a page with no link still has to reach the owner.
    """
    payload = dict(payload or {})
    uid = str(payload.get("uid") or "") or new_card_uid()
    payload["uid"] = uid

    card_url = chart_url = ""
    try:
        card_url, chart_url = card_links(uid, settings=settings)
    except CardLinkUnavailable as exc:
        log.warning("notify_card_unsigned", kind=payload.get("kind"), reason=str(exc))

    if not (payload.get("chart") or {}).get("bars"):
        chart_url = ""

    if store_card:
        store.store_card(
            uid=uid,
            kind=str(payload.get("kind") or ""),
            ref=str(payload.get("ref") or ""),
            subject=str(payload.get("subject") or ""),
            payload=payload,
            session_factory=session_factory,
        )

    rendered = render(payload, chart_url=chart_url, card_url=card_url)
    detail = {"card_uid": uid, "card_url": card_url}
    if telegram:
        detail["telegram"] = dict(telegram)

    return broadcast(
        Notification(
            kind=str(payload.get("kind") or ""),
            subject=rendered.subject,
            text=rendered.text,
            html=rendered.html_email,
            ref=str(payload.get("ref") or ""),
            detail=detail,
        ),
        settings=settings,
        channels=channels,
    )


def broadcast_card(payload: dict, **kwargs) -> dict:
    """Alias kept for readability at call sites that are not minting a card."""
    return deliver(payload, **kwargs)
