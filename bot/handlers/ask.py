"""
/ask command — natural language query handler.
Uses Sonnet with full portfolio context.
"""

from telegram import Update
from telegram.ext import ContextTypes
from bot.auth import authorized
from bot.handlers._blocking_utils import run_blocking, BlockingCallTimeout
from utils.model_selector import get_model
from utils.logger import get_logger

log = get_logger("bot_ask")
ASK_TIMEOUT_S = 210


@authorized
async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Natural language query about portfolio, market, or past decisions."""
    if not context.args:
        await update.message.reply_text(
            "Usage: `/ask your question here`\n\n"
            "Examples:\n"
            "  /ask what's my risk if semiconductors sell off 10%?\n"
            "  /ask why did we pass on CRWD?\n"
            "  /ask how are my positions doing?",
            parse_mode=None,
        )
        return

    question = " ".join(context.args)
    pipeline = context.bot_data.get("pipeline")

    if not pipeline or not pipeline.anthropic_client:
        await update.message.reply_text("AI engine not connected.", parse_mode=None)
        return

    await update.message.reply_text("🤔 Thinking...", parse_mode=None)

    try:
        response = await run_blocking(
            operation="ask_command",
            fn=lambda: _run_ask_sync(pipeline, question),
            timeout_s=ASK_TIMEOUT_S,
        )
        await update.message.reply_text(response, parse_mode=None)
    except BlockingCallTimeout:
        await update.message.reply_text(
            f"Request timed out after {ASK_TIMEOUT_S}s. Please retry with a shorter question.",
            parse_mode=None,
        )
    except Exception as e:
        log.error("ask_failed", question=question, error=str(e))
        await update.message.reply_text(f"Error processing question: {str(e)[:300]}", parse_mode=None)


def _run_ask_sync(pipeline, question: str) -> str:
    """Build context + call Sonnet in a sync function for executor use."""
    portfolio_context = _build_portfolio_context(pipeline)

    model = get_model("ask_query", pipeline.settings)
    system = (
        "You are an AI assistant for a swing trading system. You have access to the full portfolio state, "
        "recent trades, and system configuration. Answer the operator's question concisely and accurately. "
        "Use specific numbers, prices, and dates when available. Keep responses under 300 words."
    )
    prompt = f"PORTFOLIO STATE:\n{portfolio_context}\n\nOPERATOR QUESTION: {question}"
    return pipeline.anthropic_client.analyze(model, system, prompt, max_tokens=1000)


def _build_portfolio_context(pipeline) -> str:
    """Serialize portfolio state for the /ask prompt."""
    parts = []

    # Account info
    if pipeline.alpaca:
        try:
            account = pipeline.alpaca.get_account_info()
            parts.append(f"Account: equity=${account.get('equity', 0):,.2f}, cash=${account.get('cash', 0):,.2f}")
        except Exception:
            pass

        # Positions
        try:
            positions = pipeline.alpaca.get_positions_detail()
            if positions:
                pos_lines = []
                for p in positions:
                    pos_lines.append(f"  {p['ticker']}: {p.get('qty', 0)} shares, entry ${p.get('entry_price', 0):,.2f}, "
                                    f"current ${p.get('current_price', 0):,.2f}, P&L {p.get('pnl_pct', 0):+.2f}%")
                parts.append("Positions:\n" + "\n".join(pos_lines))
            else:
                parts.append("No open positions.")
        except Exception:
            pass

    # Regime
    try:
        regime = pipeline.macro_agent.get_latest_regime()
        parts.append(f"Macro regime: {regime.get('regime', 'unknown')}, multiplier: {regime.get('position_size_multiplier', 1.0)}")
    except Exception:
        pass

    # Recent trades
    try:
        from database.db import get_session
        from database.models import Trade
        with get_session() as session:
            recent = session.query(Trade).filter(Trade.status == "closed").order_by(Trade.exit_date.desc()).limit(5).all()
            if recent:
                trade_lines = []
                for t in recent:
                    symbol = t.ticker.symbol if t.ticker else "?"
                    trade_lines.append(f"  {symbol}: {t.pnl_pct or 0:+.2f}%, exit: {t.exit_reason or '?'}")
                parts.append("Recent closed trades:\n" + "\n".join(trade_lines))
    except Exception:
        pass

    return "\n\n".join(parts) if parts else "No portfolio data available."
