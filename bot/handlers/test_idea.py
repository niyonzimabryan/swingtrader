"""
/test and /score command handlers.
/test TICKER thesis — run full pipeline, return scored memo
/score TICKER — quick fundamental snapshot
"""

import asyncio

from telegram import Update
from telegram.ext import ContextTypes
from bot.auth import authorized
from bot.handlers._blocking_utils import run_blocking, BlockingCallTimeout
from bot.handlers._memo_delivery import send_memo_markdown_or_plain
from bot.formatters import escape_md, format_memo
from bot.keyboards import memo_approval_keyboard
from utils.logger import get_logger

log = get_logger("bot_test_idea")
SCORE_TIMEOUT_S = 120


@authorized
async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run full analysis pipeline for a ticker + thesis."""
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "Usage: `/test TICKER your thesis here`\n"
            "Example: `/test NVDA Strong AI demand driving datacenter revenue growth`",
            parse_mode=None,
        )
        return

    ticker = context.args[0].upper()
    thesis = " ".join(context.args[1:]) if len(context.args) > 1 else ""

    pipeline = context.bot_data.get("pipeline")
    if not pipeline:
        await update.message.reply_text("System not initialized yet.")
        return

    # Send initial status message (will be edited with progress)
    status_msg = await update.message.reply_text(
        f"Analyzing {ticker}...\n\nStarting pipeline...",
        parse_mode=None,
    )

    # Progress callback — called from sync executor, edits status message
    chat_id = update.effective_chat.id
    msg_id = status_msg.message_id
    bot = context.bot
    bot_loop = asyncio.get_event_loop()

    def progress_cb(stage_text: str):
        try:
            asyncio.run_coroutine_threadsafe(
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=f"Analyzing {ticker}...\n\n{stage_text}",
                ),
                bot_loop,
            )
        except Exception:
            pass

    try:
        memo_data = await pipeline.run_ad_hoc_async(ticker, thesis, progress_cb=progress_cb)

        if not memo_data:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg.message_id,
                text=f"Analysis complete for {ticker} — no actionable opportunity found (score below threshold or insufficient data).",
            )
            return

        # Delete status message
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=status_msg.message_id,
            )
        except Exception:
            pass

        # Send the formatted memo (split if >4096 chars, keyboard on last chunk)
        memo_text = format_memo(memo_data)
        memo_id = memo_data.get("memo_id", 0)
        opus_rec = memo_data.get("opus_evaluation", {}).get("recommendation", "proceed")
        keyboard = memo_approval_keyboard(memo_id, opus_recommendation=opus_rec)
        await send_memo_markdown_or_plain(
            message=update.message,
            bot=context.bot,
            chat_id=update.effective_chat.id,
            memo_text=memo_text,
            keyboard=keyboard,
            source="test_command",
        )

    except Exception as e:
        log.error("test_command_failed", ticker=ticker, error=str(e))
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg.message_id,
                text=f"Analysis failed for {ticker}: {str(e)[:300]}",
            )
        except Exception:
            await update.message.reply_text(f"Analysis failed: {str(e)[:300]}")


@authorized
async def score_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick fundamental snapshot for a ticker."""
    if not context.args:
        await update.message.reply_text("Usage: `/score TICKER`", parse_mode=None)
        return

    ticker = context.args[0].upper()
    pipeline = context.bot_data.get("pipeline")
    if not pipeline:
        await update.message.reply_text("System not initialized yet.")
        return

    await update.message.reply_text(f"Scoring {ticker}...", parse_mode=None)

    try:
        text = await run_blocking(
            operation="score_command",
            fn=lambda: _build_score_text(pipeline, ticker),
            timeout_s=SCORE_TIMEOUT_S,
        )
        await update.message.reply_text(text, parse_mode="MarkdownV2")
    except BlockingCallTimeout:
        await update.message.reply_text(
            f"Score request timed out after {SCORE_TIMEOUT_S}s. Try again shortly.",
            parse_mode=None,
        )
    except Exception as e:
        log.error("score_command_failed", ticker=ticker, error=str(e))
        await update.message.reply_text(f"Error: {str(e)[:300]}")


def _build_score_text(pipeline, ticker: str) -> str:
    """Sync helper for /score so it can run in executor."""
    result = pipeline.fundamental_agent.analyze(ticker=ticker, sector=pipeline.get_sector(ticker))
    rd = result.raw_data

    text = (
        f"*FUNDAMENTAL SNAPSHOT: {escape_md(ticker)}*\n\n"
        f"Quality: `{rd.get('quality_score', 0):.2f}`\n"
        f"Balance Sheet: `{rd.get('balance_sheet_score', 0):.2f}`\n"
        f"Valuation: `{rd.get('valuation_score', 0):.2f}`\n"
        f"Growth: `{rd.get('growth_score', 0):.2f}`\n"
        f"*Composite: `{rd.get('composite_score', 0):.2f}`*\n\n"
    )
    flags = rd.get("flags", [])
    if flags:
        text += f"Flags: {escape_md(', '.join(flags))}\n\n"

    ratios = rd.get("ratios", {})
    if ratios:
        text += (
            f"P/E: `{ratios.get('pe_forward', 'N/A')}`\n"
            f"EV/EBITDA: `{ratios.get('ev_ebitda', 'N/A')}`\n"
            f"PEG: `{ratios.get('peg', 'N/A')}`\n"
        )

    if rd.get("peer_comparison"):
        text += f"\n{escape_md(rd['peer_comparison'][:300])}"
    return text
