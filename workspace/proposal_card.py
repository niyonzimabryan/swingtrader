"""Delivering the approval card from the *workspace* process.

``propose_order`` runs in the workspace service, which is a separate process
from the bot and — by the rule that makes the whole safety argument hold — may
not import ``bot`` (Spec L §6.1). So when the workspace mints a card it cannot
call the bot's message queue; it posts to the Telegram Bot API over HTTPS
directly, with nothing but the bot token and the owner chat id, both of which
are already in settings.

This is not a second approval channel. It is the *same* channel — the same bot,
the same chat, the same signed single-use callback data — reached over its HTTP
API instead of through the in-process queue, because the two live in different
processes. The **callback** the owner taps still arrives at the bot process,
which is the one polling Telegram, and is handled there
(``bot/handlers/proposals.py``). The signature the card carries is what ties the
two processes together without either importing the other.

Registered in :func:`workspace.app` lifespan when ``PHASE6_EXECUTION_ENABLED``
is on and a bot token and chat id are configured. When they are not, the
default log-only sender stays in place and a proposal simply cannot be
approved, which is the safe direction.
"""

from __future__ import annotations

import httpx

from portfolio.approvals import ApprovalCard
from utils.logger import get_logger

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


def register_if_configured(settings) -> bool:
    """Wire the Telegram sender into the approval registry. Returns whether it did.

    Idempotent and safe to call at every workspace startup. With the flag off,
    or a token or chat id missing, it leaves the log-only default in place.
    """
    from portfolio import approvals

    if not bool(getattr(settings, "phase6_execution_enabled", False)):
        return False
    token = (getattr(settings, "telegram_bot_token", "") or "").strip()
    chat_id = (getattr(settings, "telegram_chat_id", "") or "").strip()
    if not (token and chat_id):
        log.warning(
            "proposal_card_channel_unconfigured",
            note=(
                "PHASE6_EXECUTION_ENABLED is on but TELEGRAM_BOT_TOKEN or "
                "TELEGRAM_CHAT_ID is unset; approval cards will only be logged "
                "and no proposal can be approved."
            ),
        )
        return False
    approvals.register_card_sender(TelegramCardSender(bot_token=token, chat_id=chat_id))
    return True
