"""Delivering the approval card from the *workspace* process.

``propose_order`` runs in the workspace service, which is a separate process
from the bot and — by the rule that makes the whole safety argument hold — may
not import ``bot`` (Spec L §6.1). So when the workspace mints a card it cannot
call the bot's message queue; it reaches the channels over HTTPS directly, with
nothing but the credentials that are already in settings.

Two channels now, both optional and independently configured:

**Telegram** is unchanged and is still the only *approvable* surface. The same
bot, the same chat, the same signed single-use callback data, reached over its
HTTP API rather than through the in-process queue. The callback the owner taps
still arrives at the bot process, which is the one polling Telegram, and is
handled there (``bot/handlers/proposals.py``).

**Email** (``NOTIFY_EMAIL_ENABLED``) sends the same card as designed HTML with a
link to the full page on this service. It carries **no** approval affordance and
cannot carry one: an email has no callback, the page under ``/cards`` is
read-only, and approval stays where it is signed, expiring, single-use and
owner-bound. Turning email on therefore adds a way to *see* a proposal, never a
way to release one.

Registered in :func:`workspace.app` lifespan. With nothing configured the
default log-only sender stays in place and a proposal simply cannot be approved,
which is the safe direction.
"""

from __future__ import annotations

import httpx

from portfolio.approvals import ApprovalCard
from utils.logger import get_logger
from utils.timeutils import utcnow_naive

log = get_logger("workspace_proposal_card")

TELEGRAM_API = "https://api.telegram.org"


def _keyboard(card: ApprovalCard) -> dict | None:
    """The inline keyboard, or ``None`` for a risk-rejected card.

    A refused proposal is shown so the owner sees why, and carries no buttons:
    there is nothing to approve, and ``card.approve_callback`` is ``None`` for
    exactly that reason.
    """
    if not card.approvable:
        return None
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve & place", "callback_data": card.approve_callback},
                {"text": "❌ Reject", "callback_data": card.reject_callback},
            ]
        ]
    }


class TelegramCardSender:
    """Posts an :class:`ApprovalCard` to Telegram over HTTPS. Never raises.

    A delivery failure logs and returns; it does not unwind the proposal write,
    because a written proposal that could not be delivered is a proposal that
    cannot be approved — visible and safe — whereas an exception escaping into
    ``create_proposal`` would roll back the row and lose the audit trail.
    """

    def __init__(self, *, bot_token: str, chat_id: str, timeout: float = 10.0):
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.timeout = timeout

    def __call__(self, card: ApprovalCard) -> None:
        if not (self.bot_token and self.chat_id):
            log.warning("proposal_card_not_configured", proposal_id=card.proposal_id)
            return
        payload = {
            "chat_id": self.chat_id,
            "text": card.body_md,
            "parse_mode": "Markdown",
        }
        keyboard = _keyboard(card)
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        try:
            response = httpx.post(
                f"{TELEGRAM_API}/bot{self.bot_token}/sendMessage",
                json=payload,
                timeout=self.timeout,
            )
            if response.status_code >= 400:
                log.error(
                    "proposal_card_send_failed",
                    proposal_id=card.proposal_id,
                    status=response.status_code,
                    body=response.text[:400],
                )
            else:
                log.info(
                    "proposal_card_sent",
                    proposal_id=card.proposal_id,
                    ticker=card.ticker,
                    approvable=card.approvable,
                )
        except Exception as exc:  # pragma: no cover - network-specific
            log.error("proposal_card_send_error", proposal_id=card.proposal_id, error=str(exc))


# --------------------------------------------------------------------------- #
# The email card
# --------------------------------------------------------------------------- #


def chart_for(ticker: str, *, settings, session_factory=None) -> dict | None:
    """The last N daily bars for ``ticker``, captured into the card payload.

    Read **once**, when the card is minted, and stored with the card: the page
    re-renders the chart from these bars forever, so an email and the page it
    links to can never disagree about what the price did (see
    ``notify/cards/chart.py``).

    Returns ``None`` when the price plane has nothing, which is a card without a
    chart, not an error.
    """
    try:
        from database.models import PriceBar

        limit = int(getattr(settings, "card_chart_sessions", 60) or 60)
        factory = session_factory
        if factory is None:
            from database.db import get_session

            factory = get_session
        with factory() as session:
            rows = (
                session.query(PriceBar)
                .filter(PriceBar.ticker == str(ticker).upper())
                .order_by(PriceBar.session_date.desc())
                .limit(limit)
                .all()
            )
            bars = [
                {
                    "date": row.session_date.isoformat(),
                    "close": float(row.split_adjusted_close),
                }
                for row in reversed(rows)
            ]
    except Exception as exc:  # pragma: no cover - database/price-plane specific
        log.warning("card_chart_bars_unavailable", ticker=ticker, error=str(exc))
        return None
    if not bars:
        return None
    return {
        "ticker": str(ticker).upper(),
        "title": f"{str(ticker).upper()} — last {len(bars)} sessions",
        "bars": bars,
        "as_of_note": f"split-adjusted closes through {bars[-1]['date']}, from the stored price plane",
        "caption": f"Split-adjusted closes, {bars[0]['date']} to {bars[-1]['date']}. "
        "Drawn from the bars stored with this card.",
    }


class EmailCardSender:
    """Renders an :class:`ApprovalCard` as an HTML email and a linked page.

    The card's ``detail`` is ``portfolio.proposals.proposal_payload(row)``, which
    is the same dict the MCP response carries — so the email, the page and the
    tool answer print the same numbers because they read the same object, not
    because three renderers were kept in step by hand.
    """

    def __init__(self, settings, *, session_factory=None, channels=None):
        self.settings = settings
        self.session_factory = session_factory
        self.channels = channels

    def __call__(self, card: ApprovalCard) -> None:
        try:
            from notify.cards import deliver
            from notify.cards.proposal import build_payload

            payload = build_payload(
                proposal=dict(card.detail or {}),
                approvable=card.approvable,
                created_at_utc=utcnow_naive().isoformat(),
                chart=chart_for(
                    card.ticker, settings=self.settings, session_factory=self.session_factory
                ),
            )
            deliver(
                payload,
                settings=self.settings,
                channels=self.channels,
                session_factory=self.session_factory,
            )
        except Exception as exc:  # pragma: no cover - channel-specific
            log.error(
                "proposal_card_email_failed",
                proposal_id=card.proposal_id,
                error=str(exc),
            )


class FanOutCardSender:
    """Calls every registered card sender. One failure never stops the others.

    ``portfolio.approvals.send_card`` already guards the outer call, but it
    guards it *once*: without this, a Telegram timeout would swallow the email
    that was meant to follow it. A card that reached one channel and not the
    other is a much better outcome than one that reached neither.
    """

    def __init__(self, senders):
        self.senders = list(senders)

    def __call__(self, card: ApprovalCard) -> None:
        for sender in self.senders:
            try:
                sender(card)
            except Exception as exc:  # pragma: no cover - sender-specific
                log.error(
                    "proposal_card_sender_failed",
                    proposal_id=card.proposal_id,
                    sender=type(sender).__name__,
                    error=str(exc),
                )


def register_if_configured(settings, *, session_factory=None) -> list[str]:
    """Wire the configured card channels in. Returns their names, in order.

    Idempotent and safe to call at every workspace startup. With the Phase 6
    flag off, or nothing configured, it leaves the log-only default in place and
    returns ``[]`` — and a proposal then cannot be approved, which is the safe
    direction.
    """
    from portfolio import approvals

    if not bool(getattr(settings, "phase6_execution_enabled", False)):
        return []

    senders = []
    names: list[str] = []

    token = (getattr(settings, "telegram_bot_token", "") or "").strip()
    chat_id = (getattr(settings, "telegram_chat_id", "") or "").strip()
    if token and chat_id:
        senders.append(TelegramCardSender(bot_token=token, chat_id=chat_id))
        names.append("telegram")

    from notify.registry import email_configured

    email_usable, email_missing = email_configured(settings)
    if email_usable:
        senders.append(EmailCardSender(settings, session_factory=session_factory))
        names.append("email")

    if not senders:
        missing = []
        if not token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not chat_id:
            missing.append("TELEGRAM_CHAT_ID")
        missing.extend(email_missing)
        log.warning(
            "proposal_card_channel_unconfigured",
            missing=",".join(missing),
            note=(
                "PHASE6_EXECUTION_ENABLED is on but no approval-card channel is "
                "configured, so cards will only be logged and no proposal can be "
                "approved. Unset: " + ", ".join(missing) + ". Telegram is the only "
                "channel that can carry an approvable card; email is a second way "
                "to see one, never a second way to release one."
            ),
        )
        return []

    if not (token and chat_id):
        log.warning(
            "proposal_card_not_approvable",
            note=(
                "email is configured but Telegram is not, so cards are delivered "
                "and nothing can be approved: the approval callback arrives on "
                "Telegram, in the bot process. Set TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_CHAT_ID to make a proposal approvable."
            ),
        )

    approvals.register_card_sender(
        senders[0] if len(senders) == 1 else FanOutCardSender(senders)
    )
    return names
