"""
Performance command handlers: /performance, /history, /memo
"""

from telegram import Update
from telegram.ext import ContextTypes
from bot.auth import authorized
from bot.handlers._blocking_utils import run_blocking, BlockingCallTimeout
from bot.formatters import escape_md
from database.db import get_session
from database.models import Trade, Memo, Ticker
from tracking.attribution import get_signal_attribution
from utils.logger import get_logger

log = get_logger("bot_performance")
PERFORMANCE_TIMEOUT_S = 180


@authorized
async def performance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enhanced performance dashboard: live Alpaca + DB closed trades."""
    pipeline = context.bot_data.get("pipeline")
    if not pipeline:
        await update.message.reply_text("System initializing...")
        return

    await update.message.reply_text("Generating performance dashboard...", parse_mode=None)

    try:
        text = await run_blocking(
            operation="performance_command",
            fn=lambda: _build_performance_text(pipeline),
            timeout_s=PERFORMANCE_TIMEOUT_S,
        )
        await update.message.reply_text(text, parse_mode="MarkdownV2")
    except BlockingCallTimeout:
        await update.message.reply_text(
            f"Performance request timed out after {PERFORMANCE_TIMEOUT_S}s. Try again shortly.",
            parse_mode=None,
        )
    except Exception as e:
        log.error("performance_failed", error=str(e))
        await update.message.reply_text(f"Error: {str(e)[:200]}")


def _build_performance_text(pipeline) -> str:
    """Sync helper for /performance so it can run in executor."""
    sections = []

    # --- ACCOUNT section (live from Alpaca) ---
    account = pipeline.broker.get_account_info() if pipeline.broker else {}
    equity = account.get("equity", 0)
    cash = account.get("cash", 0)
    day_pnl = account.get("pnl_today", 0)
    day_pnl_pct = account.get("pnl_today_pct", 0)
    day_emoji = "🟢" if day_pnl >= 0 else "🔴"

    sections.append(
        f"*📈 PERFORMANCE DASHBOARD*\n\n"
        f"*ACCOUNT*\n"
        f"  Equity: `${equity:,.0f}` \\| Cash: `${cash:,.0f}`\n"
        f"  Day P&L: {day_emoji} `${day_pnl:+,.0f}` \\(`{day_pnl_pct:+.2f}%`\\)"
    )

    # --- OPEN POSITIONS section (live from Alpaca) ---
    positions = pipeline.broker.get_positions_detail() if pipeline.broker else []
    if positions:
        open_pnl = sum(p.get("pnl_abs", 0) for p in positions)
        open_emoji = "🟢" if open_pnl >= 0 else "🔴"
        pos_lines = [f"\n*OPEN POSITIONS \\({len(positions)}\\)*"]

        # Match positions with DB trades to get stop-loss info
        with get_session() as session:
            open_trades = session.query(Trade).filter(Trade.status == "open").all()
            stop_map = {}
            for t in open_trades:
                if t.ticker:
                    stop_map[t.ticker.symbol] = t.stop_loss

        for p in positions:
            ticker = p["ticker"]
            pnl_pct = p.get("pnl_pct", 0)
            p_emoji = "🟢" if pnl_pct >= 0 else "🔴"
            stop = stop_map.get(ticker, 0)
            stop_str = f" \\| Stop: `${stop:,.2f}`" if stop > 0 else ""
            pos_lines.append(
                f"  `{escape_md(ticker)}`: {p.get('qty', 0)} shares "
                f"@ `${p.get('entry_price', 0):,.2f}` → `${p.get('current_price', 0):,.2f}` "
                f"\\| {p_emoji} `{pnl_pct:+.1f}%`{stop_str}"
            )
        pos_lines.append(f"  Open P&L: {open_emoji} `${open_pnl:+,.2f}`")
        sections.append("\n".join(pos_lines))
    else:
        sections.append("\n*OPEN POSITIONS \\(0\\)*\n  No open positions\\.")

    # --- CLOSED TRADES section (from DB) ---
    with get_session() as session:
        closed = session.query(Trade).filter(Trade.status == "closed").all()

    if closed:
        total_pnl = sum(t.pnl_absolute or 0 for t in closed)
        wins = [t for t in closed if (t.pnl_absolute or 0) > 0]
        losses = [t for t in closed if (t.pnl_absolute or 0) <= 0]
        win_rate = len(wins) / len(closed) * 100 if closed else 0
        avg_win = sum(t.pnl_pct or 0 for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.pnl_pct or 0 for t in losses) / len(losses) if losses else 0
        gross_wins = sum(t.pnl_absolute or 0 for t in wins)
        gross_losses = abs(sum(t.pnl_absolute or 0 for t in losses))
        profit_factor = gross_wins / gross_losses if gross_losses > 0 else 0

        # Best and worst trades
        best = max(closed, key=lambda t: t.pnl_pct or 0)
        worst = min(closed, key=lambda t: t.pnl_pct or 0)
        best_sym = best.ticker.symbol if best.ticker else "?"
        worst_sym = worst.ticker.symbol if worst.ticker else "?"

        # Average holding period
        hold_days = []
        for t in closed:
            if t.entry_date and t.exit_date:
                hold_days.append((t.exit_date - t.entry_date).days)
        avg_hold = sum(hold_days) / len(hold_days) if hold_days else 0

        pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
        sections.append(
            f"\n*CLOSED TRADES \\({len(closed)}\\)*\n"
            f"  Win Rate: `{win_rate:.0f}%` \\(`{len(wins)}W` / `{len(losses)}L`\\)\n"
            f"  Total P&L: {pnl_emoji} `${total_pnl:+,.2f}`\n"
            f"  Avg Win: `{avg_win:+.1f}%` \\| Avg Loss: `{avg_loss:+.1f}%`\n"
            f"  Profit Factor: `{profit_factor:.1f}`\n"
            f"  Best: `{escape_md(best_sym)}` `{best.pnl_pct or 0:+.1f}%` \\| "
            f"Worst: `{escape_md(worst_sym)}` `{worst.pnl_pct or 0:+.1f}%`\n"
            f"  Avg Hold: `{avg_hold:.1f}` days"
        )
    else:
        sections.append("\n*CLOSED TRADES \\(0\\)*\n  No closed trades yet\\.")

    attr = get_signal_attribution()
    overall = attr.get("overall", {})
    if overall:
        sections.append(
            f"\n*ATTRIBUTION SNAPSHOT*\n"
            f"  Avg R: `{overall.get('avg_r', 0):+.2f}` \\| "
            f"Win Rate: `{overall.get('win_rate', 0):.0f}%`\n"
            f"  {escape_md(attr.get('sample_warning', ''))}"
        )

    return "\n".join(sections)


@authorized
async def attr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Compact attribution dashboard."""
    await update.message.reply_text("Generating attribution dashboard...", parse_mode=None)
    try:
        text = await run_blocking(
            operation="attr_command",
            fn=_build_attr_text,
            timeout_s=PERFORMANCE_TIMEOUT_S,
        )
        await update.message.reply_text(text, parse_mode="MarkdownV2")
    except BlockingCallTimeout:
        await update.message.reply_text(
            f"Attribution request timed out after {PERFORMANCE_TIMEOUT_S}s. Try again shortly.",
            parse_mode=None,
        )
    except Exception as e:
        log.error("attr_failed", error=str(e))
        await update.message.reply_text(f"Error: {str(e)[:200]}", parse_mode=None)


def _build_attr_text() -> str:
    data = get_signal_attribution()
    overall = data.get("overall", {})
    memo_counts = data.get("memo_counts", {})
    lines = ["*ATTRIBUTION*", ""]
    warning = data.get("sample_warning")
    if warning:
        lines.append(escape_md(warning))
        lines.append("")
    if overall:
        lines.append(
            f"Closed: `{overall.get('trades', 0)}` \\| "
            f"Win rate: `{overall.get('win_rate', 0):.1f}%` \\| "
            f"Avg R: `{overall.get('avg_r', 0):+.2f}` \\| "
            f"P&L: `${overall.get('total_pnl', 0):+,.2f}`"
        )
    else:
        lines.append("No closed trades yet\\.")
    if memo_counts:
        total = memo_counts.get("total", 0)
        approved = memo_counts.get("approved", 0)
        watchlisted = memo_counts.get("watchlisted", 0)
        rejected = memo_counts.get("rejected", 0)
        approval_rate = approved / total * 100 if total else 0
        lines.append(
            f"Memos: `{total}` \\| Approved: `{approved}` \\(`{approval_rate:.1f}%`\\) "
            f"\\| Watchlisted: `{watchlisted}` \\| Rejected: `{rejected}`"
        )
    groups = data.get("groups", {})
    for label, values in (("By setup", groups.get("setup_type", {})), ("By direction", groups.get("direction", {})), ("By score", groups.get("score_bucket", {}))):
        if not values:
            continue
        lines.append("")
        lines.append(f"*{escape_md(label)}*")
        for name, summary in values.items():
            lines.append(
                f"`{escape_md(name)}`: `{summary.get('trades', 0)}` trades, "
                f"`{summary.get('win_rate', 0):.0f}%` win, "
                f"`{summary.get('avg_r', 0):+.2f}R`"
            )
    agents = data.get("agents", {})
    if agents:
        lines.append("")
        lines.append("*Agent score correlation*")
        for agent, summary in agents.items():
            corr = summary.get("correlation")
            corr_text = "n/a" if corr is None else f"{corr:+.2f}"
            lines.append(f"`{escape_md(agent)}`: n=`{summary.get('n', 0)}`, corr=`{corr_text}`")
    return "\n".join(lines)


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
