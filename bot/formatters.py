"""
Message formatting utilities for Telegram.
Handles MarkdownV2 escaping and message splitting.
"""

from memo.templates.ic_memo import format_memo_telegram

import re

TELEGRAM_MSG_LIMIT = 4096
SAFE_LIMIT = 3900  # Safety margin for MarkdownV2 escape overhead


def escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    if not text:
        return ""
    special = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    text = str(text)
    for c in special:
        text = text.replace(c, f'\\{c}')
    return text


def strip_markdown(text: str) -> str:
    """Strip MarkdownV2 formatting from a chunk for plain text fallback.

    Removes escape backslashes so a failed MarkdownV2 chunk can be re-sent
    as plain text without re-rendering the entire memo.
    """
    # Remove escape backslashes before special chars
    text = re.sub(r'\\([_*\[\]()~`>#+=|{}.!\-])', r'\1', text)
    return text


def format_memo(memo_data: dict) -> str:
    """Format a memo for Telegram delivery."""
    return format_memo_telegram(memo_data)


def _find_section_boundary(text: str, limit: int) -> int:
    """Find the last section divider (═══) line before the limit.

    Returns the index of the newline BEFORE the section header,
    or -1 if no suitable boundary found.
    """
    search_text = text[:limit]
    # Look for ═══ section dividers (used in formatted memos)
    idx = search_text.rfind('═══')
    if idx == -1:
        # Also check for === (plain text memos)
        idx = search_text.rfind('===')
    if idx != -1:
        # Split at the newline BEFORE the section header
        newline_before = search_text.rfind('\n', 0, idx)
        if newline_before > limit // 4:
            return newline_before
    return -1


def _emergency_split(text: str, limit: int) -> list[str]:
    """Last-resort split at word boundaries for oversized chunks."""
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind(' ', 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    return chunks


def split_message(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """Split a long message into chunks that fit Telegram's limit.

    Split priority: section boundaries > double newlines > single newlines > spaces.
    Every word is delivered — no truncation, no summarization.
    """
    if len(text) <= limit:
        return [text]

    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break

        # Priority 1: Section boundary (═══ divider lines)
        split_at = _find_section_boundary(text, limit)

        # Priority 2: Double newline (paragraph break)
        if split_at == -1:
            split_at = text.rfind('\n\n', 0, limit)

        # Priority 3: Single newline
        if split_at == -1 or split_at < limit // 4:
            split_at = text.rfind('\n', 0, limit)

        # Priority 4: Space (word boundary)
        if split_at == -1 or split_at < limit // 4:
            split_at = text.rfind(' ', 0, limit)

        # Priority 5: Hard cut — avoid breaking backslash escapes
        if split_at == -1 or split_at < limit // 4:
            split_at = limit
            while split_at > 0 and text[split_at - 1] == '\\':
                split_at -= 1

        chunks.append(text[:split_at])
        text = text[split_at:].lstrip('\n')

    # Safety pass: ensure no chunk exceeds Telegram's hard limit
    safe_chunks = []
    for chunk in chunks:
        if len(chunk) <= TELEGRAM_MSG_LIMIT:
            safe_chunks.append(chunk)
        else:
            safe_chunks.extend(_emergency_split(chunk, TELEGRAM_MSG_LIMIT))

    return safe_chunks


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
