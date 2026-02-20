"""
Performance command handlers: /performance, /history, /memo
"""

from telegram import Update
from telegram.ext import ContextTypes
from bot.auth import authorized
from bot.formatters import escape_md
from database.db import get_session
from database.models import Trade, Memo, Ticker
from utils.logger import get_logger

log = get_logger("bot_performance")


@authorized
async def performance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Performance summary."""
    try:
        with get_session() as session:
            closed = session.query(Trade).filter(Trade.status == "closed").all()

            if not closed:
                await update.message.reply_text("No closed trades yet. Performance tracking begins after first trade.", parse_mode=None)
                return

            total_pnl = sum(t.pnl_absolute or 0 for t in closed)
            wins = [t for t in closed if (t.pnl_absolute or 0) > 0]
            losses = [t for t in closed if (t.pnl_absolute or 0) <= 0]
            win_rate = len(wins) / len(closed) * 100 if closed else 0
            avg_win = sum(t.pnl_pct or 0 for t in wins) / len(wins) if wins else 0
            avg_loss = sum(t.pnl_pct or 0 for t in losses) / len(losses) if losses else 0
            profit_factor = abs(sum(t.pnl_absolute or 0 for t in wins) / sum(t.pnl_absolute or 0 for t in losses)) if losses and sum(t.pnl_absolute or 0 for t in losses) != 0 else 0

            pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"

            text = (
                f"*📈 PERFORMANCE SUMMARY*\n\n"
                f"Total Trades: `{len(closed)}`\n"
                f"Total P&L: {pnl_emoji} `${total_pnl:+,.2f}`\n"
                f"Win Rate: `{win_rate:.1f}%`\n"
                f"Avg Winner: `{avg_win:+.2f}%`\n"
                f"Avg Loser: `{avg_loss:+.2f}%`\n"
                f"Profit Factor: `{profit_factor:.2f}`\n"
                f"Wins: `{len(wins)}` \\| Losses: `{len(losses)}`\n"
            )
            await update.message.reply_text(text, parse_mode="MarkdownV2")
    except Exception as e:
        log.error("performance_failed", error=str(e))
        await update.message.reply_text(f"Error: {str(e)[:200]}")


@authorized
async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recent trade log."""
    try:
        ticker_filter = context.args[0].upper() if context.args else None

        with get_session() as session:
            query = session.query(Trade).filter(Trade.status == "closed").order_by(Trade.exit_date.desc())
            if ticker_filter:
                ticker_obj = session.query(Ticker).filter_by(symbol=ticker_filter).first()
                if ticker_obj:
                    query = query.filter(Trade.ticker_id == ticker_obj.id)
            trades = query.limit(10).all()

            if not trades:
                await update.message.reply_text("No trade history yet.", parse_mode=None)
                return

            text = "*📜 RECENT TRADES*\n\n"
            for t in trades:
                symbol = t.ticker.symbol if t.ticker else "?"
                pnl_emoji = "🟢" if (t.pnl_pct or 0) >= 0 else "🔴"
                text += (
                    f"{pnl_emoji} `{escape_md(symbol)}` \\| "
                    f"`{t.pnl_pct or 0:+.2f}%` \\| "
                    f"`{escape_md(t.exit_reason or 'N/A')}` \\| "
                    f"`{escape_md(t.setup_type or 'N/A')}`\n"
                )
            await update.message.reply_text(text, parse_mode="MarkdownV2")
    except Exception as e:
        log.error("history_failed", error=str(e))
        await update.message.reply_text(f"Error: {str(e)[:200]}")


@authorized
async def memo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Retrieve a past memo by ID."""
    if not context.args:
        await update.message.reply_text("Usage: `/memo ID`", parse_mode=None)
        return

    try:
        memo_id = int(context.args[0])
        with get_session() as session:
            memo = session.query(Memo).filter_by(id=memo_id).first()
            if not memo:
                await update.message.reply_text(f"Memo #{memo_id} not found.", parse_mode=None)
                return

            ticker = memo.ticker.symbol if memo.ticker else "?"
            text = (
                f"*MEMO \\#{memo_id}: {escape_md(ticker)}*\n"
                f"Score: `{memo.composite_score:.2f}` \\| Status: `{escape_md(memo.status)}`\n"
                f"Created: {escape_md(str(memo.created_at))}\n\n"
                f"{escape_md(memo.full_text[:2000])}"
            )
            await update.message.reply_text(text, parse_mode="MarkdownV2")
    except ValueError:
        await update.message.reply_text("Invalid memo ID. Use a number.", parse_mode=None)
    except Exception as e:
        log.error("memo_command_failed", error=str(e))
        await update.message.reply_text(f"Error: {str(e)[:200]}")
