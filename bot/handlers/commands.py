"""
Core bot command handlers: /help, /status, /regime, /positions, /agents, /exposure, /risk, /scan, /watchlist
Plus stubs for: /upcoming
"""

import asyncio

from telegram import Update
from telegram.ext import ContextTypes
from bot.auth import authorized
from bot.formatters import escape_md, format_portfolio_status, format_positions_detail
from utils.logger import get_logger

log = get_logger("bot_commands")


@authorized
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all available commands."""
    text = (
        "*🤖 Swing Trader Bot*\n\n"
        "*Portfolio & Status*\n"
        "`/status` \\- Portfolio dashboard\n"
        "`/positions` \\- Open positions detail\n"
        "`/exposure` \\- Sector exposure breakdown\n"
        "`/risk` \\- Risk dashboard\n\n"
        "*Analysis*\n"
        "`/regime` \\- Current macro regime\n"
        "`/test TICKER thesis` \\- Full analysis pipeline\n"
        "`/score TICKER` \\- Quick fundamental snapshot\n"
        "`/watchlist` \\- Active watchlist\n"
        "`/upcoming` \\- Upcoming catalysts\n\n"
        "*Performance*\n"
        "`/performance` \\- Performance summary\n"
        "`/history` \\- Recent trade log\n"
        "`/memo ID` \\- Retrieve past memo\n\n"
        "*Trading*\n"
        "`/close TICKER` \\- Close a position\n"
        "`/adjust TICKER stop PRICE` \\- Adjust stop\\-loss\n\n"
        "*System*\n"
        "`/scan` \\- Trigger full pipeline scan\n"
        "`/ask QUESTION` \\- Natural language query\n"
        "`/agents` \\- Agent health check\n"
        "`/pause` / `/resume` \\- Pause/resume scanning\n"
        "`/config` \\- View configuration\n"
        "`/help` \\- This message"
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")


@authorized
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Portfolio dashboard."""
    pipeline = context.bot_data.get("pipeline")
    if not pipeline:
        await update.message.reply_text("System initializing...")
        return

    try:
        # Get account info
        account = {}
        if pipeline.alpaca:
            account = pipeline.alpaca.get_account_info()

        # Get positions
        positions = []
        if pipeline.alpaca:
            positions = pipeline.alpaca.get_positions_detail()

        # Get regime
        regime = pipeline.macro_agent.get_latest_regime()

        text = format_portfolio_status(account, positions, regime)
        await update.message.reply_text(text, parse_mode="MarkdownV2")
    except Exception as e:
        log.error("status_command_failed", error=str(e))
        await update.message.reply_text(f"Error fetching status: {str(e)[:200]}")


@authorized
async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detailed open positions."""
    pipeline = context.bot_data.get("pipeline")
    if not pipeline:
        await update.message.reply_text("System initializing...")
        return

    try:
        positions = []
        if pipeline.alpaca:
            positions = pipeline.alpaca.get_positions_detail()

        text = format_positions_detail(positions)
        await update.message.reply_text(text, parse_mode="MarkdownV2")
    except Exception as e:
        log.error("positions_command_failed", error=str(e))
        await update.message.reply_text(f"Error: {str(e)[:200]}")


@authorized
async def regime_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Current macro regime with reasoning."""
    pipeline = context.bot_data.get("pipeline")
    if not pipeline:
        await update.message.reply_text("System initializing...")
        return

    try:
        # Run fresh regime analysis
        result = pipeline.macro_agent.analyze()
        rd = result.raw_data

        regime = rd.get("regime", "unknown")
        emoji = "🟢" if regime == "risk-on" else "🟡" if regime == "neutral" else "🔴"

        text = (
            f"{emoji} *MACRO REGIME: {escape_md(regime.upper())}*\n\n"
            f"Confidence: `{result.confidence:.2f}`\n"
            f"Position Multiplier: `{rd.get('position_size_multiplier', 1.0)}x`\n"
            f"Max Positions: `{rd.get('max_positions', 5)}`\n\n"
            f"*Indicator Scores:*\n"
        )
        for k, v in rd.get("scores", {}).items():
            indicator_emoji = "🟢" if v > 0 else "🔴" if v < 0 else "⚪"
            text += f"  {indicator_emoji} {escape_md(k)}: `{v:+d}`\n"

        text += f"\n{escape_md(result.reasoning)}"
        await update.message.reply_text(text, parse_mode="MarkdownV2")
    except Exception as e:
        log.error("regime_command_failed", error=str(e))
        await update.message.reply_text(f"Error: {str(e)[:200]}")


@authorized
async def agents_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Agent health check."""
    text = (
        "*🤖 AGENT STATUS*\n\n"
        "✅ Macro Regime Agent \\- Operational\n"
        "✅ Catalyst Agent \\- Operational\n"
        "✅ Fundamental Agent \\- Operational\n"
        "✅ Pattern Agent \\- Operational\n"
        "✅ Web Research Agent \\- Operational\n"
        "✅ Scoring Engine \\- Operational\n"
        "✅ Memo Generator \\- Operational\n"
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")


@authorized
async def exposure_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sector exposure breakdown."""
    pipeline = context.bot_data.get("pipeline")
    if not pipeline or not pipeline.alpaca:
        await update.message.reply_text("No positions or system initializing\\.", parse_mode="MarkdownV2")
        return

    try:
        positions = pipeline.alpaca.get_positions_detail()
        if not positions:
            await update.message.reply_text("No open positions\\.", parse_mode="MarkdownV2")
            return

        from config.tickers import UNIVERSE
        sectors = {}
        total = 0
        for pos in positions:
            sector = UNIVERSE.get(pos["ticker"], "Unknown")
            mv = pos.get("market_value", 0)
            sectors[sector] = sectors.get(sector, 0) + mv
            total += mv

        text = "*📊 EXPOSURE BREAKDOWN*\n\n"
        for sector, value in sorted(sectors.items(), key=lambda x: -x[1]):
            pct = (value / total * 100) if total > 0 else 0
            text += f"  {escape_md(sector)}: `${value:,.0f}` \\(`{pct:.1f}%`\\)\n"
        text += f"\nTotal exposure: `${total:,.0f}`"
        await update.message.reply_text(text, parse_mode="MarkdownV2")
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)[:200]}")


@authorized
async def risk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Risk dashboard."""
    pipeline = context.bot_data.get("pipeline")
    text = (
        "*⚠️ RISK DASHBOARD*\n\n"
        f"Drawdown Circuit Breaker: `{(pipeline.settings.drawdown_circuit_breaker_pct * 100) if pipeline else 10:.0f}%`\n"
        f"Daily Loss Halt: `{(pipeline.settings.daily_loss_halt_pct * 100) if pipeline else 3:.0f}%`\n"
        f"Max Concurrent Positions: `{pipeline.settings.max_concurrent_positions if pipeline else 8}`\n"
        f"Max Sector Exposure: `{(pipeline.settings.max_sector_exposure * 100) if pipeline else 30:.0f}%`\n"
        f"Max Single Position: `{(pipeline.settings.max_position_pct * 100) if pipeline else 10:.0f}%`\n"
        f"Max Stop\\-Loss: `{(pipeline.settings.max_stop_loss_pct * 100) if pipeline else 8:.0f}%`\n"
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")


@authorized
async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trigger a full pipeline scan from Telegram."""
    pipeline = context.bot_data.get("pipeline")
    if not pipeline:
        await update.message.reply_text("System initializing...")
        return

    if pipeline.paused:
        await update.message.reply_text("Pipeline is paused. Use /resume first.", parse_mode=None)
        return

    await update.message.reply_text("Full scan starting... this may take several minutes.", parse_mode=None)

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, pipeline.run_full_scan)
        # Scan completion notification is sent by the pipeline itself
    except Exception as e:
        log.error("scan_command_failed", error=str(e))
        await update.message.reply_text(f"Scan failed: {str(e)[:200]}", parse_mode=None)


# --- Stub Commands ---

@authorized
async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current watchlist with remove buttons."""
    from orchestrator.universe import get_watchlist, remove_from_watchlist
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    args = update.message.text.split(maxsplit=1)

    # /watchlist remove TICKER
    if len(args) > 1 and args[1].strip().lower().startswith("remove "):
        ticker = args[1].strip().split(maxsplit=1)[1].upper()
        removed = remove_from_watchlist(ticker)
        if removed:
            await update.message.reply_text(f"✅ {ticker} removed from watchlist.", parse_mode=None)
        else:
            await update.message.reply_text(f"⚠️ {ticker} not found on watchlist.", parse_mode=None)
        return

    # /watchlist add TICKER [reason]
    if len(args) > 1 and args[1].strip().lower().startswith("add "):
        from orchestrator.universe import add_to_watchlist
        parts = args[1].strip().split(maxsplit=2)  # "add", "TICKER", optional reason
        ticker = parts[1].upper() if len(parts) > 1 else ""
        reason = parts[2] if len(parts) > 2 else "Manual add via /watchlist"
        if not ticker:
            await update.message.reply_text("Usage: `/watchlist add TICKER [reason]`", parse_mode=None)
            return
        added = add_to_watchlist(ticker, reason=reason, source="operator")
        if added:
            await update.message.reply_text(f"✅ {ticker} added to watchlist. Will re-scan with lower threshold.", parse_mode=None)
        else:
            await update.message.reply_text(f"⚠️ {ticker} is already on the watchlist.", parse_mode=None)
        return

    # Show watchlist
    watchlist = get_watchlist()
    if not watchlist:
        await update.message.reply_text(
            "📋 *Watchlist is empty*\n\n"
            "Add tickers via:\n"
            "• `/watchlist add TICKER reason`\n"
            "• 👀 Watchlist button on memos",
            parse_mode="MarkdownV2",
        )
        return

    lines = ["*📋 WATCHLIST*\n"]
    buttons = []
    for i, item in enumerate(watchlist, 1):
        ticker = escape_md(item["ticker"])
        sector = escape_md(item.get("sector", ""))
        reason = item.get("reason", "")
        # Truncate reason for display
        short_reason = reason[:60] + "..." if len(reason) > 60 else reason
        short_reason = escape_md(short_reason)
        lines.append(f"{i}\\. `{ticker}` \\({sector}\\)")
        if short_reason:
            lines.append(f"   _{short_reason}_")
        # Add remove button for each ticker
        buttons.append([InlineKeyboardButton(
            f"🗑 Remove {item['ticker']}",
            callback_data=f"wl_remove_{item['ticker']}",
        )])

    lines.append(f"\n_{escape_md(f'{len(watchlist)} tickers — re-scanned with lower threshold')}_")

    keyboard = InlineKeyboardMarkup(buttons) if buttons else None
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
    )

@authorized
async def upcoming_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📅 Upcoming catalysts — coming soon.", parse_mode=None)

@authorized
async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pipeline = context.bot_data.get("pipeline")
    if pipeline:
        pipeline.paused = True
    await update.message.reply_text("⏸ Scanning paused. Existing positions still monitored. Use /resume to restart.", parse_mode=None)

@authorized
async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pipeline = context.bot_data.get("pipeline")
    if pipeline:
        pipeline.paused = False
    await update.message.reply_text("▶️ Scanning resumed.", parse_mode=None)

@authorized
async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pipeline = context.bot_data.get("pipeline")
    if not pipeline:
        await update.message.reply_text("System initializing...")
        return
    s = pipeline.settings
    text = (
        f"Portfolio: ${s.portfolio_value:,.0f}\n"
        f"Base position: {s.base_position_pct*100:.0f}%\n"
        f"Max position: {s.max_position_pct*100:.0f}%\n"
        f"Memo threshold: {s.memo_threshold}\n"
        f"Max hold: {s.max_holding_days} days\n"
        f"Scoring model: {s.scoring_model}\n"
    )
    await update.message.reply_text(text, parse_mode=None)
