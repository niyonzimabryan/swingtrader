"""
Inline keyboard callback handlers.
Handles: approve, reject, modify, watchlist, deep research actions on IC memos.
"""

import asyncio
import json
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes
from bot.auth import is_authorized
from bot.handlers._memo_delivery import send_memo_markdown_or_plain
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
    elif data.startswith("deep_research_"):
        await handle_deep_research(query, context, int(data.split("_")[2]))
    elif data.startswith("back_"):
        await handle_back(query, context, int(data.split("_")[1]))
    elif data.startswith("override_"):
        await handle_override(query, context, int(data.split("_")[1]))
    elif data.startswith("dismiss_"):
        await handle_dismiss(query, context, int(data.split("_")[1]))
    elif data.startswith("viewmemo_"):
        await handle_view_memo(query, context, int(data.split("_")[1]))
    elif data.startswith("wl_remove_"):
        await handle_wl_remove(query, context, data.split("wl_remove_")[1])
    elif data.startswith("close_confirm_"):
        await handle_close_confirm(query, context, data.split("close_confirm_")[1])
    elif data == "close_cancel":
        await query.edit_message_text("Position close cancelled.")
    elif data.startswith("confirm_approve_"):
        await execute_trade(query, context, int(data.split("_")[2]))
    elif data.startswith("confirm_place_"):
        await execute_trade(query, context, int(data.split("_")[2]), force_place=True)
    elif data.startswith("cancel_"):
        await query.edit_message_text("Action cancelled.")
    # Position monitor callbacks
    elif data.startswith("pos_close_"):
        await handle_pos_close(query, context, data.split("pos_close_")[1])
    elif data.startswith("pos_sell50_"):
        await handle_pos_sell50(query, context, data.split("pos_sell50_")[1])
    elif data.startswith("pos_t1exit_"):
        await handle_pos_t1exit(query, context, data.split("pos_t1exit_")[1])
    elif data.startswith("pos_hold_"):
        await handle_pos_hold(query, context, int(data.split("pos_hold_")[1]))
    elif data.startswith("pos_extend_"):
        await handle_pos_extend(query, context, int(data.split("pos_extend_")[1]))


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
                if result.get("requires_confirmation"):
                    from bot.keyboards import confirm_keyboard
                    warnings = "\n".join(f"- {w}" for w in result.get("warnings", [])) or "- Broker returned warnings."
                    await query.message.reply_text(
                        f"Broker review completed for {ticker}, but needs final confirmation.\n"
                        f"Broker: {result.get('broker')}\n"
                        f"Estimated notional: ${result.get('estimated_notional') or 0:,.2f}\n\n"
                        f"Warnings:\n{warnings}",
                        parse_mode=None,
                        reply_markup=confirm_keyboard("place", memo_id),
                    )
                elif result.get("review_only"):
                    await query.message.reply_text(
                        f"Reviewed only: {ticker} is approved in Swing Trader and broker review is stored. "
                        f"Switch /mode paper or /mode live to place orders.",
                        parse_mode=None,
                    )
                else:
                    shares = result.get("shares", trade_params.get("shares", 0))
                    entry = result.get("entry_price", trade_params.get("entry_price", 0))
                    notional = result.get("requested_notional")
                    stop = result.get("stop_loss", trade_params.get("stop_loss", 0))
                    size_text = f"${notional:,.2f} notional" if notional else f"{shares} shares"
                    await query.message.reply_text(
                        f"Approved: submitted {result.get('broker', 'broker')} order for {size_text} {ticker} "
                        f"@ ${entry:,.2f}. Stop plan: ${stop:,.2f}. Will monitor status.",
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
    """Add to watchlist — V2: actually persists to WatchlistTicker table."""
    log.info("memo_watchlisted", memo_id=memo_id)

    ticker_symbol = None
    sector = ""

    with get_session() as session:
        memo = session.query(Memo).filter_by(id=memo_id).first()
        if memo:
            memo.status = "watchlisted"
            memo.responded_at = datetime.utcnow()
            ticker_symbol = memo.ticker.symbol if memo.ticker else None
            sector = memo.ticker.sector if memo.ticker else ""

    # V2: Actually add to watchlist table for lower-threshold re-scanning
    status_msg = "Added to watchlist"
    if ticker_symbol:
        from orchestrator.universe import add_to_watchlist
        added = add_to_watchlist(
            ticker_symbol,
            reason=f"Operator watchlisted from memo #{memo_id}",
            source="operator",
            sector=sector,
        )
        if not added:
            status_msg = "Already on watchlist"
    else:
        status_msg = "Could not determine ticker"

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        f"👀 {status_msg}. Will re-scan with lower threshold on next scan cycle.",
        parse_mode=None,
    )


async def handle_back(query, context, memo_id: int):
    """Go back to main approval keyboard, preserving opus recommendation."""
    opus_rec = "proceed"
    with get_session() as session:
        memo = session.query(Memo).filter_by(id=memo_id).first()
        if memo and memo.opus_critique:
            try:
                opus_data = json.loads(memo.opus_critique)
                opus_rec = opus_data.get("recommendation", "proceed")
            except (json.JSONDecodeError, TypeError):
                pass
    keyboard = memo_approval_keyboard(memo_id, opus_recommendation=opus_rec)
    await query.edit_message_reply_markup(reply_markup=keyboard)


async def handle_wl_remove(query, context, ticker: str):
    """Remove a ticker from the watchlist via inline button."""
    from orchestrator.universe import remove_from_watchlist
    removed = remove_from_watchlist(ticker)
    if removed:
        await query.message.reply_text(f"✅ {ticker} removed from watchlist.", parse_mode=None)
    else:
        await query.message.reply_text(f"⚠️ {ticker} not on watchlist.", parse_mode=None)
    # Remove the buttons from the watchlist message
    await query.edit_message_reply_markup(reply_markup=None)


async def handle_close_confirm(query, context, ticker: str):
    """Execute position close."""
    pipeline = context.bot_data.get("pipeline")
    if pipeline and pipeline.broker:
        try:
            result = pipeline.broker.close_position(ticker)
            await query.edit_message_text(f"✅ Position in {ticker} closed.", parse_mode=None)
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to close {ticker}: {str(e)[:200]}", parse_mode=None)
    else:
        await query.edit_message_text(f"✅ {ticker} close acknowledged (execution engine not connected).", parse_mode=None)


async def handle_deep_research(query, context, memo_id: int):
    """
    Operator-triggered deep research from Telegram button.
    Allows deep research on /eval results where auto-trigger is disabled.
    """
    log.info("deep_research_requested", memo_id=memo_id)

    pipeline = context.bot_data.get("pipeline")
    if not pipeline or not pipeline.deep_research_agent:
        await query.message.reply_text(
            "⚠️ Deep research not available (Gemini API key not configured).",
            parse_mode=None,
        )
        return

    # Get memo details from DB
    ticker_symbol = None
    scoring_result = {}
    catalyst_reasoning = ""
    web_research_reasoning = ""

    with get_session() as session:
        memo = session.query(Memo).filter_by(id=memo_id).first()
        if not memo:
            await query.message.reply_text("Memo not found.", parse_mode=None)
            return

        ticker_symbol = memo.ticker.symbol if memo.ticker else None
        if not ticker_symbol:
            await query.message.reply_text("Could not determine ticker.", parse_mode=None)
            return

        # Reconstruct scoring result from memo data
        scoring_result = {
            "final_score": memo.composite_score,
            "direction": memo.direction,
            "classification": memo.classification,
            "opus_evaluation": json.loads(memo.opus_critique) if memo.opus_critique and memo.opus_critique.startswith("{") else {},
        }
        catalyst_reasoning = memo.thesis or ""
        web_research_reasoning = ""

    # Check if deep research client is available
    if not pipeline.deep_research_agent.dr_client.is_available:
        await query.message.reply_text(
            "⚠️ Deep research client not initialized (check Gemini API key).",
            parse_mode=None,
        )
        return

    await query.message.reply_text(
        f"🔬 Deep research starting for {ticker_symbol}...\n"
        f"This will take 5-20 minutes. You'll be notified when it's complete.",
        parse_mode=None,
    )

    # Build notification callback
    async def _notify(msg: str):
        if pipeline.notification_manager:
            await pipeline.notification_manager.deep_research_update(ticker_symbol, msg)

    # Run deep research in background
    asyncio.create_task(
        pipeline._run_deep_research_async(
            ticker=ticker_symbol,
            memo_id=memo_id,
            scoring_result=scoring_result,
            catalyst_reasoning=catalyst_reasoning,
            web_research_reasoning=web_research_reasoning,
            notify=_notify,
        )
    )


async def handle_override(query, context, memo_id: int):
    """Override Opus watchlist/pass — show Sonnet's draft params for confirmation."""
    log.info("memo_override_requested", memo_id=memo_id)

    with get_session() as session:
        memo = session.query(Memo).filter_by(id=memo_id).first()
        if not memo:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("Memo not found.", parse_mode=None)
            return
        trade_params = memo.trade_params_dict
        ticker = memo.ticker.symbol if memo.ticker else "?"
        opus_rec = "WATCHLIST"
        if memo.opus_critique:
            try:
                opus_data = json.loads(memo.opus_critique)
                opus_rec = opus_data.get("recommendation", "watchlist").upper()
            except (json.JSONDecodeError, TypeError):
                pass

    from bot.keyboards import confirm_keyboard
    params_text = (
        f"⚠️ OVERRIDE: Opus recommended {opus_rec} for {ticker}.\n\n"
        f"Sonnet's draft parameters:\n"
        f"Entry: ${trade_params.get('entry_price', 0):,.2f}\n"
        f"Stop: ${trade_params.get('stop_loss', 0):,.2f} ({trade_params.get('stop_pct', 0):.1f}%)\n"
        f"Target 1: ${trade_params.get('target_1', 0):,.2f}\n"
        f"Target 2: ${trade_params.get('target_2', 0):,.2f}\n"
        f"Position: {trade_params.get('position_pct', 0):.1f}% (${trade_params.get('dollar_amount', 0):,.0f})\n"
        f"Shares: {trade_params.get('shares', '?')}\n\n"
        f"Proceed with these parameters?"
    )
    keyboard = confirm_keyboard("approve", memo_id)
    await query.message.reply_text(params_text, parse_mode=None, reply_markup=keyboard)


async def handle_dismiss(query, context, memo_id: int):
    """Dismiss a watchlisted/passed memo."""
    log.info("memo_dismissed", memo_id=memo_id)
    with get_session() as session:
        memo = session.query(Memo).filter_by(id=memo_id).first()
        if memo:
            memo.status = "dismissed"
            memo.responded_at = datetime.utcnow()

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("Dismissed. Logged for signal calibration.", parse_mode=None)


async def handle_view_memo(query, context, memo_id: int):
    """Deliver a full memo from scan results, with action buttons."""
    log.info("view_memo_requested", memo_id=memo_id)

    with get_session() as session:
        memo = session.query(Memo).filter_by(id=memo_id).first()
        if not memo:
            await query.message.reply_text("Memo not found.", parse_mode=None)
            return

        # Try to load full memo_data from JSON column (v2.1+)
        memo_data = {}
        if hasattr(memo, "memo_data_json") and memo.memo_data_json:
            try:
                memo_data = json.loads(memo.memo_data_json)
            except (json.JSONDecodeError, TypeError):
                pass

        opus_rec = "proceed"
        if memo.opus_critique:
            try:
                opus_rec = json.loads(memo.opus_critique).get("recommendation", "proceed")
            except (json.JSONDecodeError, TypeError):
                pass

        full_text = memo.full_text or ""

    from bot.formatters import format_memo, split_message
    from bot.keyboards import memo_approval_keyboard
    keyboard = memo_approval_keyboard(memo_id, opus_recommendation=opus_rec)

    if memo_data and memo_data.get("ticker"):
        memo_text = format_memo(memo_data)
        await send_memo_markdown_or_plain(
            message=query.message,
            bot=context.bot,
            chat_id=query.message.chat_id,
            memo_text=memo_text,
            keyboard=keyboard,
            source="view_memo",
        )
        return

    # Fallback: send stored plain text (old memos without memo_data_json)
    if full_text:
        chunks = split_message(full_text)
        for i, chunk in enumerate(chunks):
            is_last = i == len(chunks) - 1
            await query.message.reply_text(
                chunk, parse_mode=None,
                reply_markup=keyboard if is_last else None,
            )
    else:
        await query.message.reply_text("Memo data not available.", parse_mode=None)


async def execute_trade(query, context, memo_id: int, force_place: bool = False):
    """Confirm and execute an approved trade."""
    if not force_place:
        await handle_approve(query, context, memo_id)
        return

    pipeline = context.bot_data.get("pipeline")
    if not pipeline or not pipeline.order_manager:
        await query.message.reply_text("Execution engine not connected.", parse_mode=None)
        return
    try:
        result = await pipeline.order_manager.execute_approved_trade(memo_id, force_place=True)
        await query.edit_message_reply_markup(reply_markup=None)
        if result.get("success"):
            await query.message.reply_text(
                f"Final confirmation accepted. Submitted {result.get('broker', 'broker')} order "
                f"for {result.get('ticker', '?')} (order {result.get('entry_order_id', '')[-8:]}).",
                parse_mode=None,
            )
        else:
            await query.message.reply_text(f"Execution failed: {result.get('error', 'unknown')}", parse_mode=None)
    except Exception as e:
        await query.message.reply_text(f"Execution error: {str(e)[:300]}", parse_mode=None)


# ── Position Monitor Callback Handlers ──

async def handle_pos_close(query, context, ticker: str):
    """Close an entire position from position monitor alert."""
    pipeline = context.bot_data.get("pipeline")
    if pipeline and pipeline.broker:
        try:
            result = pipeline.broker.close_position(ticker)
            if result.get("success"):
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_text(f"✅ {ticker} position closed.", parse_mode=None)
            else:
                await query.message.reply_text(
                    f"❌ Failed to close {ticker}: {result.get('error', 'unknown')}", parse_mode=None,
                )
        except Exception as e:
            await query.message.reply_text(f"❌ Error closing {ticker}: {str(e)[:200]}", parse_mode=None)
    else:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"✅ {ticker} close acknowledged (no execution engine).", parse_mode=None)


async def handle_pos_sell50(query, context, ticker: str):
    """Sell/cover 50% of a position. Direction-aware."""
    pipeline = context.bot_data.get("pipeline")
    if pipeline and pipeline.broker:
        try:
            positions = pipeline.broker.get_positions_detail()
            pos = _position_for_ticker(positions, ticker)
            if not pos:
                await query.message.reply_text(f"⚠️ No open position in {ticker}.", parse_mode=None)
                return
            qty = _position_qty(pos)
            current_price = _position_price(pos)
            if current_price <= 0:
                await query.message.reply_text(f"⚠️ Missing current price for {ticker}.", parse_mode=None)
                return
            reduce_qty = qty // 2
            if reduce_qty <= 0:
                await query.message.reply_text(f"⚠️ Position too small to split (only {qty} shares).", parse_mode=None)
                return
            is_short = pos.get("side") == "short"
            if is_short:
                pipeline.broker.submit_limit_cover(ticker, reduce_qty, current_price)
                action = "Covering"
            else:
                pipeline.broker.submit_limit_sell(ticker, reduce_qty, current_price)
                action = "Selling"
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                f"✅ {action} {reduce_qty} of {qty} shares of {ticker} @ ${current_price:,.2f}.",
                parse_mode=None,
            )
        except Exception as e:
            await query.message.reply_text(f"❌ Failed: {str(e)[:200]}", parse_mode=None)
    else:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"✅ 50% reduce for {ticker} acknowledged (no execution engine).", parse_mode=None)


async def handle_pos_t1exit(query, context, ticker: str):
    """Reduce 50% and move stop to breakeven after T1 hit. Direction-aware."""
    from database.models import Trade, Ticker as TickerModel

    pipeline = context.bot_data.get("pipeline")
    if not pipeline or not pipeline.broker:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"✅ T1 exit for {ticker} acknowledged (no execution engine).", parse_mode=None)
        return

    try:
        positions = pipeline.broker.get_positions_detail()
        pos = _position_for_ticker(positions, ticker)
        if not pos:
            await query.message.reply_text(f"⚠️ No open position in {ticker}.", parse_mode=None)
            return

        qty = _position_qty(pos)
        current_price = _position_price(pos)
        entry_price = _position_entry_price(pos) or current_price
        if current_price <= 0:
            await query.message.reply_text(f"⚠️ Missing current price for {ticker}.", parse_mode=None)
            return
        reduce_qty = qty // 2
        if reduce_qty <= 0:
            await query.message.reply_text(f"⚠️ Position too small to split.", parse_mode=None)
            return

        is_short = pos.get("side") == "short"

        # Reduce 50% (sell for long, cover for short)
        if is_short:
            pipeline.broker.submit_limit_cover(ticker, reduce_qty, current_price)
        else:
            pipeline.broker.submit_limit_sell(ticker, reduce_qty, current_price)

        # Move stop to breakeven
        with get_session() as session:
            trade = session.query(Trade).join(TickerModel).filter(
                Trade.status == "open",
                TickerModel.symbol == ticker,
            ).first()
            stop_order_id = (trade.broker_stop_order_id or trade.alpaca_stop_order_id) if trade else None
            if trade and stop_order_id:
                direction = trade.direction or "long"
                # Cancel old stop
                pipeline.broker.cancel_order(stop_order_id)
                # Place new stop at entry price (breakeven)
                remaining = qty - reduce_qty
                if not hasattr(pipeline.broker, "submit_stop_loss"):
                    new_stop_id = ""
                else:
                    new_stop_id = pipeline.broker.submit_stop_loss(
                        ticker, remaining, trade.entry_price, direction=direction,
                    )
                trade.alpaca_stop_order_id = new_stop_id
                trade.broker_stop_order_id = new_stop_id
                trade.stop_loss = trade.entry_price

        action = "Covered" if is_short else "Sold"
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            f"✅ {ticker}: {action} {reduce_qty} shares, stop moved to breakeven (${entry_price:,.2f}).",
            parse_mode=None,
        )
    except Exception as e:
        await query.message.reply_text(f"❌ T1 exit failed: {str(e)[:200]}", parse_mode=None)


async def handle_pos_hold(query, context, trade_id: int):
    """Acknowledge hold — dismiss the alert."""
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("✅ Holding. Alert dismissed.", parse_mode=None)


async def handle_pos_extend(query, context, trade_id: int):
    """Extend max holding period by 5 days."""
    from database.models import Trade
    with get_session() as session:
        trade = session.query(Trade).filter_by(id=trade_id).first()
        if trade and trade.entry_date:
            # Reset the time warning so it can fire again
            trade.time_warning_sent = False
            # Shift entry date forward by 5 days to effectively extend hold
            from datetime import timedelta
            trade.entry_date = trade.entry_date + timedelta(days=5)
            ticker = trade.ticker.symbol if trade.ticker else "?"
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                f"✅ {ticker} extended by 5 days. New expiry in ~{(trade.entry_date + timedelta(days=20) - datetime.utcnow()).days} days.",
                parse_mode=None,
            )
        else:
            await query.message.reply_text("⚠️ Trade not found.", parse_mode=None)


def _position_for_ticker(positions: list[dict], ticker: str) -> dict | None:
    target = (ticker or "").upper()
    return next((p for p in positions if str(p.get("ticker") or p.get("symbol") or "").upper() == target), None)


def _position_qty(position: dict) -> int:
    raw = position.get("qty", position.get("quantity", position.get("shares", 0)))
    try:
        return int(float(raw or 0))
    except (TypeError, ValueError):
        return 0


def _position_price(position: dict) -> float:
    raw = position.get("current_price", position.get("last_trade_price", position.get("price", 0)))
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def _position_entry_price(position: dict) -> float:
    raw = position.get("entry_price", position.get("avg_entry_price", position.get("average_price", 0)))
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0
