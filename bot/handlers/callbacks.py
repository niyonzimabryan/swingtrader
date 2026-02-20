"""
Inline keyboard callback handlers.
Handles: approve, reject, modify, watchlist actions on IC memos.
"""

import json
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes
from bot.auth import is_authorized
from bot.keyboards import modify_keyboard, memo_approval_keyboard
from database.db import get_session
from database.models import Memo
from utils.logger import get_logger

log = get_logger("bot_callbacks")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route callback queries to appropriate handlers."""
    query = update.callback_query
    if not query or not query.data:
        return

    if not is_authorized(query.message.chat_id):
        return

    await query.answer()  # Acknowledge callback
    data = query.data

    if data.startswith("approve_"):
        await handle_approve(query, context, int(data.split("_")[1]))
    elif data.startswith("reject_"):
        await handle_reject(query, context, int(data.split("_")[1]))
    elif data.startswith("modify_"):
        await handle_modify(query, context, int(data.split("_")[1]))
    elif data.startswith("watchlist_"):
        await handle_watchlist(query, context, int(data.split("_")[1]))
    elif data.startswith("back_"):
        await handle_back(query, context, int(data.split("_")[1]))
    elif data.startswith("close_confirm_"):
        await handle_close_confirm(query, context, data.split("close_confirm_")[1])
    elif data == "close_cancel":
        await query.edit_message_text("Position close cancelled.")
    elif data.startswith("confirm_approve_"):
        await execute_trade(query, context, int(data.split("_")[2]))
    elif data.startswith("cancel_"):
        await query.edit_message_text("Action cancelled.")


async def handle_approve(query, context, memo_id: int):
    """Approve a trade — triggers execution."""
    log.info("memo_approved", memo_id=memo_id)

    # Update memo status
    with get_session() as session:
        memo = session.query(Memo).filter_by(id=memo_id).first()
        if not memo:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("Memo not found.")
            return

        memo.status = "approved"
        memo.responded_at = datetime.utcnow()
        ticker = memo.ticker.symbol if memo.ticker else "?"
        trade_params = memo.trade_params_dict

    # Execute the trade
    pipeline = context.bot_data.get("pipeline")
    if pipeline and pipeline.order_manager:
        try:
            result = await pipeline.order_manager.execute_approved_trade(memo_id)
            if result.get("success"):
                await query.edit_message_reply_markup(reply_markup=None)
                shares = trade_params.get("shares", 0)
                entry = trade_params.get("entry_price", 0)
                stop = trade_params.get("stop_loss", 0)
                await query.message.reply_text(
                    f"✅ Approved: Submitting limit buy for {shares} shares {ticker} "
                    f"@ ${entry:,.2f}. Stop-loss at ${stop:,.2f}. Will confirm fill.",
                    parse_mode=None,
                )
            else:
                error = result.get("error", "Unknown error")
                await query.message.reply_text(f"❌ Execution failed: {error}", parse_mode=None)
        except Exception as e:
            log.error("trade_execution_failed", memo_id=memo_id, error=str(e))
            await query.message.reply_text(f"❌ Execution error: {str(e)[:300]}", parse_mode=None)
    else:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("✅ Approved (execution engine not connected — paper trade simulated).", parse_mode=None)


async def handle_reject(query, context, memo_id: int):
    """Reject a trade idea."""
    log.info("memo_rejected", memo_id=memo_id)
    with get_session() as session:
        memo = session.query(Memo).filter_by(id=memo_id).first()
        if memo:
            memo.status = "rejected"
            memo.responded_at = datetime.utcnow()

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("❌ Rejected. Logged for signal calibration.", parse_mode=None)


async def handle_modify(query, context, memo_id: int):
    """Show modification options."""
    keyboard = modify_keyboard(memo_id)
    await query.edit_message_reply_markup(reply_markup=keyboard)


async def handle_watchlist(query, context, memo_id: int):
    """Add to watchlist."""
    log.info("memo_watchlisted", memo_id=memo_id)
    with get_session() as session:
        memo = session.query(Memo).filter_by(id=memo_id).first()
        if memo:
            memo.status = "watchlisted"
            memo.responded_at = datetime.utcnow()

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("👀 Added to watchlist. Will alert if score changes materially.", parse_mode=None)


async def handle_back(query, context, memo_id: int):
    """Go back to main approval keyboard."""
    keyboard = memo_approval_keyboard(memo_id)
    await query.edit_message_reply_markup(reply_markup=keyboard)


async def handle_close_confirm(query, context, ticker: str):
    """Execute position close."""
    pipeline = context.bot_data.get("pipeline")
    if pipeline and pipeline.alpaca:
        try:
            result = pipeline.alpaca.close_position(ticker)
            await query.edit_message_text(f"✅ Position in {ticker} closed.", parse_mode=None)
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to close {ticker}: {str(e)[:200]}", parse_mode=None)
    else:
        await query.edit_message_text(f"✅ {ticker} close acknowledged (execution engine not connected).", parse_mode=None)


async def execute_trade(query, context, memo_id: int):
    """Confirm and execute an approved trade."""
    await handle_approve(query, context, memo_id)
