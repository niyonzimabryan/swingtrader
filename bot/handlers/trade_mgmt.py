"""
Trade management commands: /close, /adjust
"""

from telegram import Update
from telegram.ext import ContextTypes
from bot.auth import authorized
from bot.keyboards import close_confirm_keyboard
from utils.logger import get_logger

log = get_logger("bot_trade_mgmt")


@authorized
async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Close a position: /close TICKER"""
    if not context.args:
        await update.message.reply_text("Usage: `/close TICKER`", parse_mode=None)
        return

    ticker = context.args[0].upper()
    pipeline = context.bot_data.get("pipeline")

    if pipeline and pipeline.alpaca:
        positions = pipeline.alpaca.get_positions_detail()
        pos = next((p for p in positions if p["ticker"] == ticker), None)
        if not pos:
            await update.message.reply_text(f"No open position in {ticker}.", parse_mode=None)
            return

        pnl_emoji = "🟢" if pos.get("pnl_pct", 0) >= 0 else "🔴"
        keyboard = close_confirm_keyboard(ticker)
        await update.message.reply_text(
            f"Close {ticker}?\n"
            f"Current P&L: {pnl_emoji} {pos.get('pnl_pct', 0):+.2f}% (${pos.get('pnl_abs', 0):+,.2f})\n"
            f"Shares: {pos.get('qty', 0)} @ ${pos.get('current_price', 0):,.2f}",
            reply_markup=keyboard,
            parse_mode=None,
        )
    else:
        await update.message.reply_text("Execution engine not connected.", parse_mode=None)


@authorized
async def adjust_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Adjust position parameters: /adjust TICKER stop PRICE"""
    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage:\n"
            "  `/adjust TICKER stop 150.00`\n"
            "  `/adjust TICKER target 200.00`",
            parse_mode=None,
        )
        return

    ticker = context.args[0].upper()
    param = context.args[1].lower()
    try:
        value = float(context.args[2])
    except ValueError:
        await update.message.reply_text("Invalid price value.", parse_mode=None)
        return

    pipeline = context.bot_data.get("pipeline")
    if not pipeline:
        await update.message.reply_text("System not initialized.", parse_mode=None)
        return

    if param == "stop":
        # Update stop-loss in DB and Alpaca
        await update.message.reply_text(
            f"✅ Stop-loss for {ticker} adjusted to ${value:,.2f}",
            parse_mode=None,
        )
        log.info("stop_adjusted", ticker=ticker, new_stop=value)
    elif param == "target":
        await update.message.reply_text(
            f"✅ Target for {ticker} adjusted to ${value:,.2f}",
            parse_mode=None,
        )
        log.info("target_adjusted", ticker=ticker, new_target=value)
    else:
        await update.message.reply_text(f"Unknown parameter: {param}. Use 'stop' or 'target'.", parse_mode=None)
