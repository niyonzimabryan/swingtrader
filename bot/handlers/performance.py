"""
Performance command handlers: /performance, /history, /memo
"""

from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy.orm import joinedload
from bot.auth import authorized
from bot.handlers._blocking_utils import run_blocking, BlockingCallTimeout
from bot.formatters import escape_md
from database.db import get_session
from database.models import Trade, Memo, Ticker
from tracking.attribution import get_signal_attribution
from tracking.position_reconciliation import ACTIVE_TRADE_STATUSES, reconcile_broker_positions
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

    # --- ACTIVE TRADES section (DB trades enriched with live broker P&L) ---
    positions = pipeline.broker.get_positions_detail() if pipeline.broker else []
    active_broker = getattr(getattr(pipeline, "broker", None), "active", getattr(pipeline, "broker", None))
    broker_name = getattr(active_broker, "name", "alpaca")
    broker_account_id = getattr(active_broker, "account_number", "") or None
    execution_mode = str(getattr(getattr(pipeline, "settings", None), "execution_mode", "paper")).lower()
    reconcile_broker_positions(
        positions,
        broker_name=broker_name,
        broker_account_id=broker_account_id,
        execution_mode=execution_mode,
        source="performance_command",
    )
    position_map = {
        str(p.get("ticker", "")).upper(): p
        for p in positions
        if p.get("ticker")
    }
    with get_session() as session:
        active_trades = (
            session.query(Trade)
            .options(joinedload(Trade.ticker))
            .filter(Trade.status.in_(ACTIVE_TRADE_STATUSES))
            .order_by(Trade.entry_date.desc().nullslast(), Trade.created_at.desc())
            .all()
        )

        active_lines = []
        matched_symbols = set()
        for trade in active_trades:
            ticker = trade.ticker.symbol if trade.ticker else "?"
            pos = position_map.get(ticker.upper())
            if pos:
                matched_symbols.add(ticker.upper())
            active_lines.append(_format_active_trade_line(trade, ticker, pos))

    for pos in positions:
        ticker = str(pos.get("ticker", "")).upper()
        if ticker and ticker not in matched_symbols:
            active_lines.append(_format_broker_only_position_line(pos))

    if active_lines:
        open_pnl = sum(_position_pnl_values(p)[0] or 0 for p in positions)
        open_emoji = "🟢" if open_pnl >= 0 else "🔴"
        pos_lines = [f"\n*ACTIVE TRADES \\({len(active_lines)}\\)*"]
        pos_lines.extend(active_lines)
        if positions:
            pos_lines.append(
                f"  Broker Open P&L: {open_emoji} `${open_pnl:+,.2f}` "
                f"across `{len(positions)}` positions"
            )
        sections.append("\n".join(pos_lines))
    else:
        sections.append("\n*ACTIVE TRADES \\(0\\)*\n  No active trades\\.")

    # --- CLOSED TRADES section (from DB) ---
    with get_session() as session:
        closed = (
            session.query(Trade)
            .options(joinedload(Trade.ticker))
            .filter(Trade.status == "closed")
            .all()
        )

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


def _format_active_trade_line(trade: Trade, ticker: str, position: dict | None) -> str:
    status = (trade.status or "unknown").replace("_", " ")
    direction = trade.direction or "long"
    shares = _float_value(trade.shares or (position or {}).get("qty", 0)) or 0.0
    entry = _float_value(trade.entry_price or (position or {}).get("entry_price", 0)) or 0.0
    current = _float_value((position or {}).get("current_price"))
    current_str = f" → `${current:,.2f}`" if current else ""
    pnl_str = _format_position_pnl(position)
    stop_str = f" \\| Stop: `${trade.stop_loss:,.2f}`" if trade.stop_loss and trade.stop_loss > 0 else ""
    broker = trade.broker or "unknown"
    return (
        f"  `{escape_md(ticker)}` `{escape_md(status)}` `{escape_md(direction)}`: "
        f"{shares:g} shares @ `${entry:,.2f}`{current_str} "
        f"\\| {pnl_str}{stop_str} \\| `{escape_md(broker)}`"
    )


def _format_broker_only_position_line(position: dict) -> str:
    ticker = str(position.get("ticker", "?"))
    pnl_str = _format_position_pnl(position)
    shares = _float_value(position.get("qty", 0)) or 0.0
    entry = _float_value(position.get("entry_price", 0)) or 0.0
    current = _float_value(position.get("current_price", 0)) or 0.0
    return (
        f"  `{escape_md(ticker)}` `broker only`: {shares:g} shares "
        f"@ `${entry:,.2f}` → `${current:,.2f}` "
        f"\\| {pnl_str} \\| not tracked in DB"
    )


def _format_position_pnl(position: dict | None) -> str:
    if not position:
        return "P&L: `n/a`"
    pnl_abs, pnl_pct = _position_pnl_values(position)
    if pnl_abs is None and pnl_pct is None:
        return "P&L: `n/a`"
    basis = pnl_abs if pnl_abs is not None else pnl_pct
    emoji = "🟢" if basis >= 0 else "🔴"
    abs_part = f"`${pnl_abs:+,.2f}`" if pnl_abs is not None else "`n/a`"
    pct_part = f"`{pnl_pct:+.1f}%`" if pnl_pct is not None else "`n/a`"
    return f"{emoji} {abs_part} \\({pct_part}\\)"


def _position_pnl_values(position: dict) -> tuple[float | None, float | None]:
    pnl_abs = _float_value(position.get("pnl_abs"))
    pnl_pct = _float_value(position.get("pnl_pct"))
    if pnl_abs is None and pnl_pct is None:
        pnl_abs, pnl_pct = _calculate_position_pnl(position)
    return pnl_abs, pnl_pct


def _calculate_position_pnl(position: dict) -> tuple[float | None, float | None]:
    qty = _float_value(position.get("qty"))
    entry = _float_value(position.get("entry_price"))
    current = _float_value(position.get("current_price"))
    if not qty or not entry or current is None:
        return None, None
    side = str(position.get("side") or "long").lower()
    pnl_abs = (entry - current) * qty if side == "short" else (current - entry) * qty
    cost_basis = abs(entry * qty)
    pnl_pct = pnl_abs / cost_basis * 100 if cost_basis > 0 else None
    return pnl_abs, pnl_pct


def _float_value(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
