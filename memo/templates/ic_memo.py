"""
IC Memo template for Telegram delivery.
V2: Three-section layout — Sonnet Proposal → Opus Evaluation → Final Trade Parameters.
Uses Telegram MarkdownV2 formatting.
"""


def format_memo_telegram(memo_data: dict) -> str:
    """Format a memo for Telegram MarkdownV2 delivery."""
    d = memo_data
    score = d.get("composite_score", 0)
    classification = d.get("classification", "unknown")

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

    def esc(text):
        if not text:
            return ""
        special = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        text = str(text)
        for c in special:
            text = text.replace(c, f'\\{c}')
        return text

    def fmt(value, spec):
        try:
            return format(float(value), spec)
        except (ValueError, TypeError):
            return "?"

    lines = []

    # === HEADER ===
    cat = d.get("catalyst", {})
    setup_type = cat.get("catalyst_type", "catalyst").replace("_", " ").title()
    lines.append(
        f"{'🔴' if classification == 'high_conviction' else '🟡'} "
        f"*TRADE IDEA: {esc(d.get('ticker', '?'))} — "
        f"{esc(d.get('direction', '?').upper())} — "
        f"{esc(setup_type)}*"
    )
    lines.append(f"Score: `{fmt(score, '.2f')}` \\| {esc(classification.replace('_', ' ').title())} \\| {esc(d.get('generated_at', '')[:16])}")
    lines.append("")

    # ═══ SONNET PROPOSAL ═══
    lines.append("═══ *SONNET PROPOSAL* ═══")
    lines.append("")

    # Thesis
    lines.append("*THESIS*")
    lines.append(esc(d.get("thesis", "N/A")))
    lines.append("")

    # Catalyst
    lines.append("*CATALYST*")
    lines.append(f"Type: `{cat.get('catalyst_type', 'N/A')}`")
    lines.append(f"Summary: {esc(cat.get('catalyst_summary', 'N/A'))}")
    materiality = cat.get('materiality', cat.get('confidence', None))
    dir_conf = cat.get('direction_confidence', cat.get('confidence', None))
    mat_str = f"{materiality:.0%}" if isinstance(materiality, (int, float)) else "?"
    dir_str = f"{dir_conf:.0%}" if isinstance(dir_conf, (int, float)) else "?"
    lines.append(f"Materiality: `{mat_str}` \\| Direction Confidence: `{dir_str}`")
    impact = cat.get("expected_impact_pct", {})
    if impact:
        lines.append(f"Impact: `{impact.get('low', '?')}%` to `{impact.get('high', '?')}%` \\| Horizon: `{cat.get('time_horizon_days', '?')}d`")
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

    # Historical Precedent
    pattern = d.get("pattern", {})
    lines.append("*HISTORICAL PRECEDENT*")
    if pattern.get("status") == "stub":
        lines.append(esc("Insufficient historical data."))
    elif pattern.get("status") == "no_data":
        lines.append(esc(pattern.get("reasoning", "No historical data available.")))
    elif pattern.get("status") == "active":
        total = pattern.get("total_instances", 0)
        same = pattern.get("same_ticker_instances", 0)
        peer = pattern.get("peer_instances", 0)
        win_rate = pattern.get("win_rate_t10", 0)
        median_ret = pattern.get("median_return_t10", 0)
        avg_winner = pattern.get("avg_winner_t10", 0)
        avg_loser = pattern.get("avg_loser_t10", 0)
        dd_median = pattern.get("max_drawdown_median", 0)
        dd_worst = pattern.get("max_drawdown_worst", 0)

        # V2: Similarity-weighted stats
        hs_count = pattern.get("highly_similar_count", 0)
        weighted_wr = pattern.get("weighted_win_rate_t10")
        weighted_med = pattern.get("weighted_median_return_t10")
        most_similar = pattern.get("most_similar", {})

        lines.append(f"Setup: `{pattern.get('setup_type', 'N/A').replace('_', ' ').title()}`")

        # V2: Show highly similar count in instances line
        if hs_count > 0:
            lines.append(f"Instances: `{total}` \\(`{hs_count}` highly similar\\) \\| self: `{same}`, peers: `{peer}`")
        else:
            lines.append(f"Instances: `{total}` \\(self: `{same}`, peers: `{peer}`\\)")

        # V2: Show similarity-weighted stats when available and different from raw
        win_rate_str = f"{win_rate:.0%}" if isinstance(win_rate, (int, float)) else "?"
        if weighted_wr is not None and abs(weighted_wr - win_rate) > 0.02:
            weighted_wr_str = f"{weighted_wr:.0%}" if isinstance(weighted_wr, (int, float)) else "?"
            lines.append(f"Win rate \\(T\\+10\\): `{win_rate_str}` \\| Similarity\\-weighted: `{weighted_wr_str}`")
            lines.append(f"Median: `{fmt(median_ret, '+.1f')}%` \\| Weighted median: `{fmt(weighted_med, '+.1f')}%`")
        else:
            lines.append(f"Win rate \\(T\\+10\\): `{win_rate_str}` \\| Median: `{fmt(median_ret, '+.1f')}%`")

        lines.append(f"Avg winner: `{fmt(avg_winner, '+.1f')}%` \\| Avg loser: `{fmt(avg_loser, '.1f')}%`")
        lines.append(f"Max DD: `{fmt(dd_median, '.1f')}%` median \\| `{fmt(dd_worst, '.1f')}%` worst")

        # V2: Show most similar instance
        if most_similar and most_similar.get("ticker"):
            sim_ticker = most_similar.get("ticker", "?")
            sim_date = most_similar.get("event_date", "?")
            sim_ret = most_similar.get("return_t10", 0)
            sim_score = most_similar.get("similarity", 0)
            lines.append(
                f"Most similar: `{esc(sim_ticker)}` {esc(sim_date)} "
                f"\\(sim: `{fmt(sim_score, '.0%')}`\\) → `{fmt(sim_ret, '+.1f')}%`"
            )

        if pattern.get("sample_size_warning"):
            lines.append(esc("⚠️ Small sample — interpret with caution"))
        if pattern.get("reasoning"):
            lines.append(esc(pattern.get("reasoning", "")))
    else:
        lines.append(esc(pattern.get("reasoning", "N/A")))
    lines.append("")

    # Web Research
    wr = d.get("web_research", {})
    lines.append("*WEB RESEARCH*")
    if wr.get("status") == "stub":
        lines.append(esc("Web research not available."))
    elif wr.get("status") == "error":
        lines.append(esc("Web research failed — see logs."))
    else:
        key_finding = wr.get("key_finding", "")
        if key_finding:
            lines.append(f"Key finding: {esc(key_finding)}")
        for dim in ("catalyst_context", "competitive_dynamics", "management_signals",
                     "bull_bear_debate", "institutional_positioning"):
            val = wr.get(dim, "")
            if val:
                label = dim.replace("_", " ").title()
                lines.append(f"{esc(label)}: {esc(val[:200])}")
    lines.append("")

    # Risk Analysis
    risk_analysis = d.get("risk_analysis", "")
    counter_args = cat.get("counter_arguments", "")
    if risk_analysis or counter_args:
        lines.append("*RISK ANALYSIS*")
        if risk_analysis:
            lines.append(esc(risk_analysis))
        elif counter_args:
            lines.append(esc(counter_args))
        lines.append("")

    # Draft Trade Parameters
    params = d.get("trade_params", {})
    lines.append("*DRAFT TRADE PARAMS* \\(subject to Opus modification\\)")
    lines.append(
        f"Entry: `${fmt(params.get('entry_price', 0), ',.2f')}` \\| "
        f"Stop: `${fmt(params.get('stop_loss', 0), ',.2f')}` \\(`{fmt(params.get('stop_pct', 0), '.1f')}%`\\)"
    )
    lines.append(
        f"T1: `${fmt(params.get('target_1', 0), ',.2f')}` \\| "
        f"T2: `${fmt(params.get('target_2', 0), ',.2f')}` \\| "
        f"R:R `{fmt(params.get('risk_reward', 0), '.1f')}:1`"
    )
    lines.append(
        f"Position: `{fmt(params.get('position_pct', 0), '.1f')}%` "
        f"\\(`${fmt(params.get('dollar_amount', 0), ',.0f')}`\\) \\| "
        f"`{params.get('shares', '?')}` shares"
    )
    lines.append("")

    # Signal Agreement
    lines.append("*SIGNAL AGREEMENT*")
    cat_sig = breakdown.get("catalyst", {})
    fund_sig = breakdown.get("fundamental", {})
    pat_sig = breakdown.get("pattern", {})
    wr_sig = breakdown.get("web_research", {})
    lines.append(
        f"{signal_emoji(cat_sig)} Catalyst \\| "
        f"{signal_emoji(fund_sig)} Fundamental \\| "
        f"{signal_emoji(pat_sig)} Pattern \\| "
        f"{signal_emoji(wr_sig)} Web Research"
    )
    lines.append("")

    # ═══ OPUS EVALUATION ═══
    opus = d.get("opus_evaluation", {})
    if opus and opus.get("conviction"):
        lines.append("═══ *OPUS EVALUATION* ═══")
        lines.append("")

        rec = opus.get("recommendation", "?")
        rec_emoji = {"proceed": "✅", "reduce_size": "⚠️", "watchlist": "👀", "pass": "❌"}.get(rec, "❓")
        lines.append(f"Verdict: {rec_emoji} *{esc(rec.upper())}* \\| Conviction: `{esc(opus.get('conviction', '?'))}`")

        # Score adjustment
        adjusted = d.get("adjusted_score", d.get("composite_score", 0))
        final = d.get("composite_score", 0)
        opus_score = opus.get("final_score", final)
        if opus.get("delta_clamped"):
            delta_note = f" \\(clamped from {fmt(opus.get('original_opus_score', 0), '.2f')}\\)"
        else:
            delta_note = ""
        opus_delta = opus_score - adjusted if isinstance(adjusted, (int, float)) else 0
        lines.append(f"Score: `{fmt(adjusted, '.2f')}` → `{fmt(final, '.2f')}` \\(Opus: `{fmt(opus_delta, '+.2f')}`{delta_note}\\)")

        key_risk = opus.get("key_risk", "")
        if key_risk:
            lines.append(f"Key Risk: {esc(key_risk)}")

        stress = opus.get("stress_test", "")
        if stress:
            lines.append(f"Stress Test: {esc(stress)}")

        reasoning = opus.get("reasoning", "")
        if reasoning:
            lines.append(f"{esc(reasoning[:400])}")

        pos_adj = opus.get("position_size_adjustment", 1.0)
        if isinstance(pos_adj, (int, float)) and abs(pos_adj - 1.0) > 0.01:
            lines.append(f"Position adjustment: `{fmt(pos_adj, '.1f')}x`")

        lines.append("")

    # ═══ FINAL TRADE PARAMETERS ═══
    lines.append("═══ *FINAL TRADE PARAMETERS* ═══")
    lines.append("")
    lines.append(f"Direction: `{d.get('direction', '?').upper()}`")
    lines.append(f"Entry: `${fmt(params.get('entry_price', 0), ',.2f')}`")
    lines.append(f"Stop\\-loss: `${fmt(params.get('stop_loss', 0), ',.2f')}` \\(`{fmt(params.get('stop_pct', 0), '.1f')}%`\\)")
    lines.append(f"Target 1: `${fmt(params.get('target_1', 0), ',.2f')}` \\(`{fmt(params.get('target_1_pct', 0), '.1f')}%`\\)")
    lines.append(f"Target 2: `${fmt(params.get('target_2', 0), ',.2f')}` \\(`{fmt(params.get('target_2_pct', 0), '.1f')}%`\\)")
    lines.append(f"Position: `{fmt(params.get('position_pct', 0), '.1f')}%` \\(`${fmt(params.get('dollar_amount', 0), ',.0f')}`\\)")
    lines.append(f"Shares: `{params.get('shares', '?')}` \\| R:R: `{fmt(params.get('risk_reward', 0), '.1f')}:1`")
    lines.append(f"Max hold: `{params.get('max_hold_days', 20)}` trading days")

    regime = d.get("regime", {})
    lines.append(f"Regime: `{regime.get('regime', '?')}` \\| Multiplier: `{regime.get('position_size_multiplier', '?')}x`")

    return "\n".join(lines)


def format_memo_plain(memo_data: dict) -> str:
    """Format a memo as plain text (for DB storage / email)."""
    d = memo_data
    cat = d.get("catalyst", {})
    setup_type = cat.get("catalyst_type", "catalyst").replace("_", " ").title()
    lines = []

    lines.append(f"TRADE IDEA: {d.get('ticker', '?')} — {d.get('direction', '?').upper()} — {setup_type}")
    lines.append(f"Score: {d.get('composite_score', 0):.2f} — {d.get('classification', '?')}")
    lines.append(f"Generated: {d.get('generated_at', '')}")
    lines.append("=" * 50)

    lines.append(f"\n{'=' * 20} SONNET PROPOSAL {'=' * 20}")
    lines.append(f"\nTHESIS\n{d.get('thesis', 'N/A')}")

    lines.append(f"\nCATALYST\nType: {cat.get('catalyst_type', 'N/A')}")
    lines.append(f"Summary: {cat.get('catalyst_summary', 'N/A')}")
    materiality = cat.get('materiality', cat.get('confidence', None))
    dir_conf = cat.get('direction_confidence', cat.get('confidence', None))
    mat_str = f"{materiality:.0%}" if isinstance(materiality, (int, float)) else "?"
    dir_str = f"{dir_conf:.0%}" if isinstance(dir_conf, (int, float)) else "?"
    lines.append(f"Materiality: {mat_str} | Direction Confidence: {dir_str}")

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
        hs_count = pattern.get("highly_similar_count", 0)
        weighted_wr = pattern.get("weighted_win_rate_t10")
        weighted_med = pattern.get("weighted_median_return_t10")
        most_similar = pattern.get("most_similar", {})

        lines.append(f"Setup: {pattern.get('setup_type', 'N/A')}")
        if hs_count > 0:
            lines.append(f"Instances: {total} ({hs_count} highly similar) | Win rate (T+10): {win_rate:.0%} | Median return: {median_ret:+.1f}%")
        else:
            lines.append(f"Instances: {total} | Win rate (T+10): {win_rate:.0%} | Median return: {median_ret:+.1f}%")

        if weighted_wr is not None and abs(weighted_wr - win_rate) > 0.02:
            lines.append(f"Similarity-weighted win rate: {weighted_wr:.0%} | Weighted median: {weighted_med:+.1f}%")

        if most_similar and most_similar.get("ticker"):
            sim_ret = most_similar.get("return_t10", 0)
            sim_score = most_similar.get("similarity", 0)
            lines.append(f"Most similar: {most_similar['ticker']} {most_similar.get('event_date', '?')} (sim: {sim_score:.0%}) → {sim_ret:+.1f}%")
    elif pattern.get("status") == "stub":
        lines.append("Insufficient historical data.")
    else:
        lines.append(pattern.get("reasoning", "N/A"))

    wr = d.get("web_research", {})
    lines.append(f"\nWEB RESEARCH")
    if wr.get("status") == "stub":
        lines.append("Web research not available.")
    elif wr.get("status") == "error":
        lines.append("Web research failed.")
    else:
        key_finding = wr.get("key_finding", "")
        if key_finding:
            lines.append(f"Key finding: {key_finding}")
        for dim in ("catalyst_context", "competitive_dynamics", "management_signals",
                     "bull_bear_debate", "institutional_positioning"):
            val = wr.get(dim, "")
            if val:
                label = dim.replace("_", " ").title()
                lines.append(f"{label}: {val[:200]}")

    risk = d.get("risk_analysis", "") or cat.get("counter_arguments", "")
    if risk:
        lines.append(f"\nRISK ANALYSIS\n{risk}")

    opus = d.get("opus_evaluation", {})
    if opus and opus.get("conviction"):
        lines.append(f"\n{'=' * 20} OPUS EVALUATION {'=' * 20}")
        lines.append(f"Verdict: {opus.get('recommendation', '?').upper()} | Conviction: {opus.get('conviction', '?')}")
        lines.append(f"Key Risk: {opus.get('key_risk', 'N/A')}")
        lines.append(f"Stress Test: {opus.get('stress_test', 'N/A')}")
        lines.append(f"Reasoning: {opus.get('reasoning', 'N/A')}")

    params = d.get("trade_params", {})
    lines.append(f"\n{'=' * 20} FINAL TRADE PARAMETERS {'=' * 20}")
    lines.append(f"Direction: {d.get('direction', '?').upper()}")
    lines.append(f"Entry: ${params.get('entry_price', 0):,.2f}")
    lines.append(f"Stop-loss: ${params.get('stop_loss', 0):,.2f} ({params.get('stop_pct', 0):.1f}%)")
    lines.append(f"Target 1: ${params.get('target_1', 0):,.2f} ({params.get('target_1_pct', 0):.1f}%)")
    lines.append(f"Target 2: ${params.get('target_2', 0):,.2f} ({params.get('target_2_pct', 0):.1f}%)")
    lines.append(f"Position: {params.get('position_pct', 0):.1f}% (${params.get('dollar_amount', 0):,.0f})")
    lines.append(f"Shares: {params.get('shares', '?')} | R:R: {params.get('risk_reward', 0):.1f}:1")
    lines.append(f"Max hold: {params.get('max_hold_days', 20)} trading days")

    return "\n".join(lines)
