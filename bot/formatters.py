"""
Message formatting utilities for Telegram.
Handles MarkdownV2 escaping and message splitting.
"""

from memo.templates.ic_memo import format_memo_telegram

TELEGRAM_MSG_LIMIT = 4096


def escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    if not text:
        return ""
    special = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    text = str(text)
    for c in special:
        text = text.replace(c, f'\\{c}')
    return text


def format_memo(memo_data: dict) -> str:
    """Format a memo for Telegram delivery."""
    return format_memo_telegram(memo_data)


def split_message(text: str, limit: int = TELEGRAM_MSG_LIMIT) -> list[str]:
    """Split a long message into chunks that fit Telegram's limit."""
    if len(text) <= limit:
        return [text]

    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Find a good split point (newline)
        split_at = text.rfind('\n', 0, limit)
        if split_at == -1 or split_at < limit // 2:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip('\n')

    return chunks


def format_portfolio_status(account: dict, positions: list, regime: dict) -> str:
    """Format portfolio status for /status command."""
    lines = []
    lines.append("*📊 PORTFOLIO STATUS*")
    lines.append("")

    equity = account.get("equity", 0)
    cash = account.get("cash", 0)
    pnl = account.get("pnl_today", 0)
    pnl_pct = account.get("pnl_today_pct", 0)
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"

    lines.append(f"Portfolio Value: `${equity:,.2f}`")
    lines.append(f"Cash: `${cash:,.2f}`")
    lines.append(f"Daily P&L: {pnl_emoji} `${pnl:,.2f}` \\(`{pnl_pct:+.2f}%`\\)")
    lines.append("")

    # Regime
    r = regime.get("regime", "unknown")
    regime_emoji = "🟢" if r == "risk-on" else "🟡" if r == "neutral" else "🔴"
    lines.append(f"Macro Regime: {regime_emoji} `{r.upper()}`")
    lines.append(f"Position Multiplier: `{regime.get('position_size_multiplier', 1.0)}x`")
    lines.append("")

    # Positions
    if positions:
        lines.append(f"*Open Positions: {len(positions)}*")
        for pos in positions:
            pnl_emoji = "🟢" if pos.get("pnl_pct", 0) >= 0 else "🔴"
            lines.append(
                f"  {escape_md(pos['ticker'])}: {pnl_emoji} `{pos.get('pnl_pct', 0):+.2f}%` "
                f"\\(`${pos.get('market_value', 0):,.0f}`\\)"
            )
    else:
        lines.append("No open positions\\.")

    return "\n".join(lines)


def format_positions_detail(positions: list) -> str:
    """Format detailed positions for /positions command."""
    if not positions:
        return "No open positions\\."

    lines = ["*📋 OPEN POSITIONS*", ""]
    for pos in positions:
        pnl_emoji = "🟢" if pos.get("pnl_pct", 0) >= 0 else "🔴"
        lines.append(f"*{escape_md(pos['ticker'])}* {pnl_emoji}")
        lines.append(f"  Entry: `${pos.get('entry_price', 0):,.2f}` → Current: `${pos.get('current_price', 0):,.2f}`")
        lines.append(f"  P&L: `{pos.get('pnl_pct', 0):+.2f}%` \\(`${pos.get('pnl_abs', 0):+,.2f}`\\)")
        lines.append(f"  Stop: `${pos.get('stop_loss', 0):,.2f}` \\| Size: `{pos.get('position_pct', 0):.1f}%`")
        lines.append(f"  Days held: `{pos.get('days_held', 0)}` \\| Setup: `{pos.get('setup_type', 'N/A')}`")
        lines.append("")

    return "\n".join(lines)
