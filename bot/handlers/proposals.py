"""The out-of-band approval path, in the bot process (Spec L §6, Spec Q §13).

This is the half of the approval loop that an agent can never reach. The bot is
the only process polling Telegram, so the callback the owner taps arrives here,
and here is where — and *only* where — an approval turns into a placement, by
calling the injected :class:`execution.lifecycle.ExecutionService`. No MCP tool,
token scope, or REST route reaches this module;
``tests/test_execution_lifecycle_isolation.py`` and the import-graph tests hold
that.

Three things live here:

``BotCardSender``
    the in-process card channel, for a deployment where the bot itself hosts the
    proposal path (and for tests). It renders the same signed callback the
    workspace's HTTPS sender does, through the existing message queue, so there
    is one card format rather than two.
``handle_proposal_approve`` / ``handle_proposal_reject``
    the callback handlers. They verify authorisation, parse the signed
    reference, and hand off to the execution service — which re-runs every risk
    check from fresh state before it places anything. The bot does no risk
    logic of its own; that would be a second copy of the guards to keep in sync.
``live_kill_command``
    ``/live_kill on|off``: the persistent kill switch (Spec L §6.5). Owner-only,
    survives restart because it is a database row, and it blocks
    approval-to-placement rather than cancelling anything already at the broker.
"""

from __future__ import annotations

import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.auth import authorized, is_authorized
from database.db import get_session
from portfolio import approvals as approvals_mod
from portfolio import killswitch
from utils.logger import get_logger

log = get_logger("bot_proposals")

APPROVE_TIMEOUT_S = 90.0


# --------------------------------------------------------------------------- #
# The in-process card channel
# --------------------------------------------------------------------------- #


class BotCardSender:
    """Sends an :class:`~portfolio.approvals.ApprovalCard` through the message queue.

    ``portfolio.approvals.send_card`` calls this synchronously, from whatever
    thread ``create_proposal`` runs on. The message queue is async and lives on
    the bot loop, so the send is scheduled onto that loop rather than awaited
    here — the same pattern the paging bridge uses. Delivery failures are
    swallowed by ``send_card``'s own guard, so a card that cannot be sent leaves
    the proposal ``proposed`` and unapprovable, never rolls it back.
    """

    def __init__(self, message_queue, chat_id: str, loop):
        self.mq = message_queue
        self.chat_id = str(chat_id)
        self.loop = loop

    def __call__(self, card: approvals_mod.ApprovalCard) -> None:
        markup = None
        if card.approvable:
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Approve & place", callback_data=card.approve_callback),
                        InlineKeyboardButton("❌ Reject", callback_data=card.reject_callback),
                    ]
                ]
            )
        coro = self.mq.send_plain(self.chat_id, card.body_md, reply_markup=markup)
        try:
            asyncio.run_coroutine_threadsafe(coro, self.loop)
        except Exception as exc:  # pragma: no cover - loop-specific
            log.error("proposal_card_schedule_failed", proposal_id=card.proposal_id, error=str(exc))


def register_bot_card_sender(message_queue, chat_id: str, loop) -> None:
    """Wire :class:`BotCardSender` into the approval registry. ``main.py`` calls this."""
    approvals_mod.register_card_sender(BotCardSender(message_queue, chat_id, loop))


# --------------------------------------------------------------------------- #
# The callback handlers
# --------------------------------------------------------------------------- #


async def handle_proposal_callback(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatch a ``p6ok:``/``p6no:`` callback. Called from the main router.

    The router already checks authorisation, but this is the one callback that
    can move live capital, so it checks again rather than trusting its caller —
    defence in depth on the single most sensitive path in the system. The
    signature verification inside the execution service is the real control;
    this is the cheap belt to its braces.
    """
    if not is_authorized(query.message.chat_id):
        log.warning("unauthorized_proposal_callback", chat_id=query.message.chat_id)
        return
    data = query.data or ""
    try:
        action, proposal_id, signature = approvals_mod.parse_callback(data)
    except approvals_mod.ApprovalRefused as exc:
        await query.message.reply_text(f"Could not read that approval: {exc.message}", parse_mode=None)
        return

    owner_id = str(query.message.chat_id)
    if action == "reject":
        await _reject(query, context, proposal_id, owner_id)
        return
    await _approve(query, context, proposal_id, signature, owner_id)


def _lab_execution_id(proposal_id: int) -> str:
    """The ``strategy_trades.execution_id`` this proposal belongs to, or ``""``.

    The routing key for both halves below, and it is deliberately read from the
    **row** rather than from the callback: callback data is whatever a chat client
    sent, while ``proposals.execution_id`` was written by
    ``StrategyExecutionService.propose`` inside its own transaction. So a crafted
    callback cannot make a lab execution look like a plain proposal, or the
    reverse.
    """
    from database.models import Proposal

    with get_session() as session:
        proposal = session.get(Proposal, proposal_id)
        return (getattr(proposal, "execution_id", "") or "") if proposal else ""


async def _reject(query, context, proposal_id: int, owner_id: str) -> None:
    from database.models import Proposal
    from utils.timeutils import utcnow_naive

    execution_id = _lab_execution_id(proposal_id)
    if execution_id:
        # A Strategy Lab execution has a second row — the `strategy_trades` one —
        # and leaving it `proposed` would hold its decision's single
        # open-execution slot forever. `cancel` moves both terminally, which
        # releases nothing because a `proposed` row reserved nothing.
        await _reject_lab(query, context, proposal_id, execution_id)
        return

    with get_session() as session:
        proposal = session.get(Proposal, proposal_id)
        if proposal is None:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("Proposal not found.", parse_mode=None)
            return
        if proposal.status == "proposed":
            proposal.status = "rejected"
            proposal.approval_consumed_at = utcnow_naive()
            proposal.updated_at = proposal.approval_consumed_at
            session.commit()
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        f"Rejected proposal {proposal_id}. Nothing was placed.", parse_mode=None
    )


async def _reject_lab(query, context, proposal_id: int, execution_id: str) -> None:
    from bot.handlers._blocking_utils import BlockingCallTimeout, run_blocking

    service = _lab_service(context)
    if service is None:
        await query.message.reply_text(
            "The Strategy Lab execution service is not wired in this deployment, "
            "so this execution cannot be cancelled from here. Nothing was placed.",
            parse_mode=None,
        )
        return

    def work():
        return service.cancel(
            execution_id=execution_id, by="owner", reason="owner_rejected"
        )

    try:
        status = await run_blocking("lab_proposal_reject", work, APPROVE_TIMEOUT_S)
    except BlockingCallTimeout:
        await query.message.reply_text("Rejection is still running.", parse_mode=None)
        return
    except Exception as exc:
        await query.message.reply_text(f"Could not reject: {str(exc)[:300]}", parse_mode=None)
        return
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        f"Rejected proposal {proposal_id} (execution {execution_id[:12]}: "
        f"{status}). Nothing was placed.",
        parse_mode=None,
    )


def _lab_service(context):
    """PR 5's explicit-mode service, as ``main.py`` wired it, or ``None``.

    Separate from ``execution_service`` on purpose. Phase 6's service is bound to
    one broker chosen from the global ``EXECUTION_MODE``; a Strategy Lab execution
    must reach the venue its *arm's* mode allows, which is what
    ``StrategyExecutionService`` binds. Approving a lab execution through the
    global service would be the exact inference Spec Q §12 invariant 11 forbids,
    so a deployment that wired only the global one cannot approve a lab execution
    at all — it says so rather than placing through the wrong venue.
    """
    return context.bot_data.get("strategy_execution_service")


async def _approve(query, context, proposal_id: int, signature: str, owner_id: str) -> None:
    # Which service may place this one is a property of the row, not of the
    # callback: a proposal carrying an `execution_id` belongs to a Strategy Lab
    # arm, and the arm's immutable mode — not the global EXECUTION_MODE — decides
    # the venue. So a lab execution is approved through PR 5's explicit-mode
    # service and a plain proposal through Phase 6's (Spec Q §12 invariant 11).
    execution_id = _lab_execution_id(proposal_id)
    service = (
        _lab_service(context) if execution_id else context.bot_data.get("execution_service")
    )
    if service is None:
        await query.message.reply_text(
            (
                "The Strategy Lab execution service is not wired in this "
                "deployment, so this arm's execution cannot be approved from "
                "here. Nothing was placed and the proposal is unchanged."
            )
            if execution_id
            else (
                "Execution service is not wired in this deployment; cannot place. "
                "The proposal is unchanged."
            ),
            parse_mode=None,
        )
        return

    from bot.handlers._blocking_utils import BlockingCallTimeout, run_blocking

    def work():
        # The service opens its own session, verifies the signed single-use
        # reference, re-runs every risk check from fresh state, checks the kill
        # switch, then places and protects. The bot adds no risk logic. For a lab
        # execution the same call additionally binds the arm's mode to its one
        # allowed adapter and writes every §12 hop as it happens.
        from execution.lifecycle import ExecutionRefused
        from execution.strategy_lifecycle import ArmExecutionRefused

        try:
            if execution_id:
                return ("ok", service.on_approval(
                    execution_id=execution_id,
                    presented_signature=signature,
                    owner_id=owner_id,
                ))
            return ("ok", service.on_approval(
                proposal_id=proposal_id, presented_signature=signature, owner_id=owner_id
            ))
        except (ExecutionRefused, ArmExecutionRefused) as exc:
            return ("refused", f"{exc.code}: {exc.message}")
        except approvals_mod.ApprovalRefused as exc:
            return ("refused", f"{exc.code}: {exc.message}")

    try:
        kind, payload = await run_blocking("proposal_approval", work, APPROVE_TIMEOUT_S)
    except BlockingCallTimeout:
        await query.message.reply_text(
            f"Approval of proposal {proposal_id} is still running past "
            f"{APPROVE_TIMEOUT_S:.0f}s. Check /orders and the proposal's status "
            "before approving again — do not re-tap, the approval is single-use.",
            parse_mode=None,
        )
        return

    await query.edit_message_reply_markup(reply_markup=None)
    if kind == "refused":
        await query.message.reply_text(f"Not placed: {payload}", parse_mode=None)
        return

    result = payload
    await query.message.reply_text(
        f"Proposal {proposal_id}: {result.status}. {result.message}"
        + (f"\nentry {result.entry_order_id}" if result.entry_order_id else "")
        + (f"\nstop {result.stop_order_id}" if result.stop_order_id else ""),
        parse_mode=None,
    )


# --------------------------------------------------------------------------- #
# The kill switch command
# --------------------------------------------------------------------------- #


@authorized
async def live_kill_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/live_kill on|off`` — the persistent kill switch (Spec L §6.5).

    On blocks every approval-to-placement and survives a restart because the
    state is a row, not a process variable. It does not cancel orders already at
    the broker: that is a decision made in the broker's own app.
    """
    args = context.args or []
    who = str(update.effective_chat.id)
    if not args or args[0].lower() not in ("on", "off", "status"):
        with get_session() as session:
            switch = killswitch.state(session)
        state = "ENGAGED" if switch.engaged else "off"
        await update.message.reply_text(
            f"Live kill switch is {state}.\nUsage: /live_kill on | off",
            parse_mode=None,
        )
        return

    verb = args[0].lower()
    if verb == "status":
        with get_session() as session:
            switch = killswitch.state(session)
        await update.message.reply_text(
            f"Live kill switch: {'ENGAGED' if switch.engaged else 'off'}"
            + (f"\nreason: {switch.reason}" if switch.reason else ""),
            parse_mode=None,
        )
        return

    reason = " ".join(args[1:]).strip()
    with get_session() as session:
        switch = killswitch.set_switch(
            session, on=(verb == "on"), changed_by=who, reason=reason
        )
        session.commit()
    if switch.engaged:
        await update.message.reply_text(
            "🛑 Live kill switch ENGAGED. No approval will reach a placement "
            "until it is released with /live_kill off. Orders already at the "
            "broker are unchanged — cancel those in the Robinhood app if needed.",
            parse_mode=None,
        )
    else:
        await update.message.reply_text(
            "Live kill switch released. Approvals can reach placement again, "
            "subject to every other gate.",
            parse_mode=None,
        )
