"""
IC Memo template for Telegram delivery.
Uses Telegram MarkdownV2 formatting.
"""


def format_memo_telegram(memo_data: dict) -> str:
    """Format a memo for Telegram MarkdownV2 delivery."""
    d = memo_data
    score = d.get("composite_score", 0)
    classification = d.get("classification", "unknown")
    direction_emoji = "📈" if d.get("direction") == "long" else "📉"

    # Signal agreement emojis
    breakdown = d.get("signal_breakdown", {})
    primary_dir = d.get("direction_raw", "bullish")

    def signal_emoji(agent_data):
        direction = agent_data.get("direction", "neutral")
        if direction == primary_dir:
            return "✅"
        elif direction == "neutral":
            return "➖"
        return "⚠️"

    # Escape special chars for MarkdownV2 (ONLY for text outside backtick code spans)
    def esc(text):
        if not text:
            return ""
        special = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        text = str(text)
        for c in special:
            text = text.replace(c, f'\\{c}')
        return text

    # Helper: format a number safely, returning a string.
    # Use this to pre-format numbers before placing them in backtick code spans
    # or passing through esc() for text outside code spans.
    def fmt(value, spec):
        """Format a numeric value safely. Returns formatted string or '?' on failure."""
        try:
            return format(float(value), spec)
        except (ValueError, TypeError):
            return "?"

    lines = []
    lines.append(f"{'🔴' if classification == 'high_conviction' else '🟡'} *TRADE IDEA: {esc(d.get('ticker', '?'))} — {esc(d.get('direction', '?').upper())}*")
    lines.append(f"Score: `{fmt(score, '.2f')}` — {esc(classification.replace('_', ' ').title())}")
    lines.append(f"{esc(d.get('generated_at', ''))}")
    lines.append("")
    lines.append(esc("─" * 30))
    lines.append("")

    # Thesis
    lines.append("*THESIS*")
    lines.append(esc(d.get("thesis", "N/A")))
    lines.append("")

    # Catalyst
    cat = d.get("catalyst", {})
    lines.append("*CATALYST*")
    lines.append(f"Type: `{cat.get('catalyst_type', 'N/A')}`")
    lines.append(f"Summary: {esc(cat.get('catalyst_summary', 'N/A'))}")
    cat_conf = cat.get('confidence', None)
    cat_conf_str = f"{cat_conf:.0%}" if isinstance(cat_conf, (int, float)) else "?"
    lines.append(f"Magnitude: `{cat.get('magnitude', '?')}/5` \\| Confidence: `{cat_conf_str}`")
    impact = cat.get("expected_impact_pct", {})
    if impact:
        lines.append(f"Expected impact: `{impact.get('low', '?')}%` to `{impact.get('high', '?')}%`")
    lines.append(f"Time horizon: `{cat.get('time_horizon_days', '?')}` trading days")
    lines.append("")

    # Fundamentals
    fund = d.get("fundamental", {})
    lines.append("*FUNDAMENTALS*")
    qs = fund.get('quality_score', 0)
    vs = fund.get('valuation_score', 0)
    gs = fund.get('growth_score', 0)
    bs = fund.get('balance_sheet_score', 0)
    lines.append(
        f"Quality: `{qs if isinstance(qs, str) else fmt(qs, '.2f')}` \\| "
        f"Valuation: `{vs if isinstance(vs, str) else fmt(vs, '.2f')}` \\| "
        f"Growth: `{gs if isinstance(gs, str) else fmt(gs, '.2f')}` \\| "
        f"Balance Sheet: `{bs if isinstance(bs, str) else fmt(bs, '.2f')}`"
    )
    if fund.get("flags"):
        lines.append(f"Flags: {esc(', '.join(fund['flags']))}")
    if fund.get("peer_comparison"):
        lines.append(f"Peers: {esc(fund['peer_comparison'][:200])}")
    lines.append("")

    # Historical
    pattern = d.get("pattern", {})
    lines.append("*HISTORICAL PRECEDENT*")
    if pattern.get("status") == "stub":
        lines.append(esc("Insufficient historical data — will improve with time."))
    elif pattern.get("status") == "no_data":
        lines.append(esc(pattern.get("reasoning", "No historical data available for this setup type.")))
    elif pattern.get("status") == "active":
        setup_type = pattern.get("setup_type", "N/A")
        total = pattern.get("total_instances", 0)
        same = pattern.get("same_ticker_instances", 0)
        peer = pattern.get("peer_instances", 0)
        win_rate = pattern.get("win_rate_t10", 0)
        median_ret = pattern.get("median_return_t10", 0)
        avg_winner = pattern.get("avg_winner_t10", 0)
        avg_loser = pattern.get("avg_loser_t10", 0)
        dd_median = pattern.get("max_drawdown_median", 0)
        dd_worst = pattern.get("max_drawdown_worst", 0)

        lines.append(f"Setup: `{setup_type.replace('_', ' ').title()}`")
        lines.append(f"Instances: `{total}` \\(self: `{same}`, peers: `{peer}`\\)")
        win_rate_str = f"{win_rate:.0%}" if isinstance(win_rate, (int, float)) else "?"
        lines.append(f"Win rate \\(T\\+10\\): `{win_rate_str}`")
        lines.append(f"Median return: `{fmt(median_ret, '+.1f')}%`")
        lines.append(f"Avg winner: `{fmt(avg_winner, '+.1f')}%` \\| Avg loser: `{fmt(avg_loser, '.1f')}%`")
        lines.append(f"Max drawdown: `{fmt(dd_median, '.1f')}%` median \\| `{fmt(dd_worst, '.1f')}%` worst")
        if pattern.get("sample_size_warning"):
            lines.append(esc("⚠️ Small sample size — interpret with caution"))
        lines.append("")
        lines.append(esc(pattern.get("reasoning", "")))
    else:
        lines.append(esc(pattern.get("reasoning", "N/A")))
    lines.append("")

    # Sentiment
    sent = d.get("sentiment", {})
    lines.append("*SENTIMENT*")
    if sent.get("status") == "stub":
        lines.append(esc("Reddit sentiment data initializing."))
    else:
        lines.append(esc(sent.get("reasoning", "N/A")))
    lines.append("")

    # Macro
    regime = d.get("regime", {})
    lines.append("*MACRO CONTEXT*")
    lines.append(f"Regime: `{regime.get('regime', '?')}` \\| Multiplier: `{regime.get('position_size_multiplier', '?')}x`")
    lines.append("")

    lines.append(esc("─" * 30))
    lines.append("")

    # Trade Parameters
    params = d.get("trade_params", {})
    lines.append("*TRADE PARAMETERS*")
    lines.append(f"Direction: `{d.get('direction', '?').upper()}`")
    lines.append(f"Entry: `${fmt(params.get('entry_price', 0), ',.2f')}`")
    lines.append(f"Stop\\-loss: `${fmt(params.get('stop_loss', 0), ',.2f')}` \\(`{fmt(params.get('stop_pct', 0), '.1f')}%`\\)")
    lines.append(f"Target 1: `${fmt(params.get('target_1', 0), ',.2f')}` \\(`{fmt(params.get('target_1_pct', 0), '.1f')}%`\\)")
    lines.append(f"Target 2: `${fmt(params.get('target_2', 0), ',.2f')}` \\(`{fmt(params.get('target_2_pct', 0), '.1f')}%`\\)")
    lines.append(f"Position: `{fmt(params.get('position_pct', 0), '.1f')}%` of portfolio \\(`${fmt(params.get('dollar_amount', 0), ',.0f')}`\\)")
    lines.append(f"Shares: `{params.get('shares', '?')}`")
    lines.append(f"Risk/Reward: `{fmt(params.get('risk_reward', 0), '.1f')}:1`")
    lines.append(f"Max hold: `{params.get('max_hold_days', 20)}` trading days")
    lines.append("")

    lines.append(esc("─" * 30))
    lines.append("")

    # Signal Agreement
    lines.append("*SIGNAL AGREEMENT*")
    cat_sig = breakdown.get("catalyst", {})
    fund_sig = breakdown.get("fundamental", {})
    pat_sig = breakdown.get("pattern", {})
    sent_sig = breakdown.get("sentiment", {})
    lines.append(
        f"{signal_emoji(cat_sig)} Catalyst \\| "
        f"{signal_emoji(fund_sig)} Fundamental \\| "
        f"{signal_emoji(pat_sig)} Pattern \\| "
        f"{signal_emoji(sent_sig)} Sentiment"
    )
    lines.append("")

    # Bear Case
    lines.append("*BEAR CASE*")
    lines.append(esc(d.get("bear_case", "N/A")))
    lines.append("")

    # Opus Critique
    opus = d.get("opus_evaluation", {})
    if opus.get("stress_test"):
        lines.append("*OPUS STRESS TEST*")
        lines.append(esc(opus["stress_test"]))

    return "\n".join(lines)


def format_memo_plain(memo_data: dict) -> str:
    """Format a memo as plain text (for DB storage / email)."""
    d = memo_data
    lines = []
    lines.append(f"TRADE IDEA: {d.get('ticker', '?')} — {d.get('direction', '?').upper()}")
    lines.append(f"Score: {d.get('composite_score', 0):.2f} — {d.get('classification', '?')}")
    lines.append(f"Generated: {d.get('generated_at', '')}")
    lines.append("=" * 50)
    lines.append(f"\nTHESIS\n{d.get('thesis', 'N/A')}")

    cat = d.get("catalyst", {})
    lines.append(f"\nCATALYST\nType: {cat.get('catalyst_type', 'N/A')}")
    lines.append(f"Summary: {cat.get('catalyst_summary', 'N/A')}")
    cat_conf = cat.get('confidence', None)
    cat_conf_str = f"{cat_conf:.0%}" if isinstance(cat_conf, (int, float)) else "?"
    lines.append(f"Magnitude: {cat.get('magnitude', '?')}/5 | Confidence: {cat_conf_str}")

    fund = d.get("fundamental", {})
    lines.append(f"\nFUNDAMENTALS")
    qs = fund.get('quality_score', 0)
    vs = fund.get('valuation_score', 0)
    gs = fund.get('growth_score', 0)
    bs = fund.get('balance_sheet_score', 0)
    lines.append(f"Quality: {qs if isinstance(qs, str) else f'{qs:.2f}'} | Valuation: {vs if isinstance(vs, str) else f'{vs:.2f}'}")
    lines.append(f"Growth: {gs if isinstance(gs, str) else f'{gs:.2f}'} | Balance Sheet: {bs if isinstance(bs, str) else f'{bs:.2f}'}")

    pattern = d.get("pattern", {})
    lines.append(f"\nHISTORICAL PRECEDENT")
    if pattern.get("status") == "active":
        total = pattern.get("total_instances", 0)
        win_rate = pattern.get("win_rate_t10", 0)
        median_ret = pattern.get("median_return_t10", 0)
        lines.append(f"Setup: {pattern.get('setup_type', 'N/A')}")
        lines.append(f"Instances: {total} | Win rate (T+10): {win_rate:.0%} | Median return: {median_ret:+.1f}%")
    elif pattern.get("status") == "stub":
        lines.append("Insufficient historical data.")
    else:
        lines.append(pattern.get("reasoning", "N/A"))

    params = d.get("trade_params", {})
    lines.append(f"\nTRADE PARAMETERS")
    lines.append(f"Entry: ${params.get('entry_price', 0):,.2f}")
    lines.append(f"Stop-loss: ${params.get('stop_loss', 0):,.2f} ({params.get('stop_pct', 0):.1f}%)")
    lines.append(f"Target 1: ${params.get('target_1', 0):,.2f}")
    lines.append(f"Target 2: ${params.get('target_2', 0):,.2f}")
    lines.append(f"Position: {params.get('position_pct', 0):.1f}% (${params.get('dollar_amount', 0):,.0f})")

    lines.append(f"\nBEAR CASE\n{d.get('bear_case', 'N/A')}")

    return "\n".join(lines)
