#!/usr/bin/env python3
"""Render representative Telegram memo HTML for 390px mobile QA.

This is a local QA helper. It does not call Telegram or the live bot.
It approximates Telegram MarkdownV2 visually enough to catch mobile line-break,
code-span, and section density issues.
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memo.templates.ic_memo import format_memo_telegram  # noqa: E402

OUT_DIR = ROOT / "docs" / "assets" / "mobile-memo-qa"


def _base_memo(ticker: str, recommendation: str) -> dict:
    long_peer = (
        "Peers are repricing after a mixed earnings season: mega-cap AI infrastructure "
        "spenders still get premium multiples, but software-adjacent names are being "
        "punished quickly when forward guide language sounds less certain."
    )
    web_sentence = (
        "Recent coverage clusters around demand durability, capex cadence, margin pressure, "
        "and whether management can convert backlog into revenue without stretching working capital."
    )
    direction = "long" if recommendation != "pass" else "short"
    return {
        "ticker": ticker,
        "direction": direction,
        "direction_raw": "bullish" if direction == "long" else "bearish",
        "composite_score": 0.78 if recommendation == "proceed" else 0.61 if recommendation == "watchlist" else 0.34,
        "adjusted_score": 0.70,
        "classification": "high_conviction" if recommendation == "proceed" else "moderate" if recommendation == "watchlist" else "no_action",
        "generated_at": "2026-05-22T10:32:00Z",
        "thesis": (
            f"{ticker} has a plausible 3–15 day swing setup, but the memo needs to stay readable "
            "on a narrow Telegram phone viewport without horizontal code-span overflow or dense metric rows."
        ),
        "catalyst": {
            "catalyst_type": "earnings_surprise",
            "catalyst_modifiers": ["sector_macro", "management_guidance", "post_earnings_drift"],
            "catalyst_summary": "Guidance commentary and follow-through volume are the near-term drivers.",
            "materiality": 0.82,
            "direction_confidence": 0.68,
            "expected_impact_pct": {"low": -4.5, "high": 12.25},
            "time_horizon_days": 10,
            "counter_arguments": "If rates back up or AI-capex sentiment fades, this setup can fail before target one.",
        },
        "fundamental": {
            "quality_score": 0.86,
            "valuation_score": 0.52,
            "growth_score": 0.74,
            "balance_sheet_score": 0.91,
            "flags": ["accelerating_growth", "premium_multiple", "high_institutional_attention"],
            "peer_comparison": long_peer,
        },
        "pattern": {
            "status": "active",
            "setup_type": "post_earnings_momentum",
            "total_instances": 48,
            "same_ticker_instances": 7,
            "peer_instances": 41,
            "win_rate_t10": 0.61,
            "median_return_t10": 3.4,
            "avg_winner_t10": 8.2,
            "avg_loser_t10": -4.7,
            "max_drawdown_median": -3.8,
            "max_drawdown_worst": -9.9,
            "highly_similar_count": 9,
            "weighted_win_rate_t10": 0.70,
            "weighted_median_return_t10": 4.8,
            "most_similar": {"ticker": "AMD", "event_date": "2025-11-06", "return_t10": 6.4, "similarity": 0.87},
            "sample_size_warning": True,
            "reasoning": "Historical analogs favor continuation when volume confirms, but gap-fill risk is meaningful in the first two sessions.",
        },
        "web_research": {
            "status": "active",
            "key_finding": "The bull case is visible but not consensus; this is a setup-quality memo, not a certainty memo.",
            "catalyst_context": web_sentence,
            "competitive_dynamics": web_sentence,
            "management_signals": web_sentence,
            "bull_bear_debate": web_sentence,
            "institutional_positioning": web_sentence,
        },
        "risk_analysis": {
            "risks": [
                {"probability": "possible", "severity_pct": 5, "risk": "Guidance reversal after analyst Q&A", "trigger": "volume fades below prior close"},
                {"probability": "unlikely", "severity_pct": 8, "risk": "Sector-wide multiple compression", "trigger": "rates spike / peer selloff"},
            ],
            "failure_mode": "Clean break below the stop with no bounce after the first hour confirms thesis failure.",
        },
        "trade_params": {
            "entry_price": 182.34,
            "stop_loss": 173.72 if direction == "long" else 191.08,
            "stop_pct": 4.7,
            "target_1": 196.20 if direction == "long" else 168.40,
            "target_1_pct": 7.6,
            "target_2": 211.80 if direction == "long" else 157.25,
            "target_2_pct": 16.2,
            "position_pct": 5.0,
            "dollar_amount": 5000.0,
            "shares": 27,
            "risk_reward": 2.1,
            "max_hold_days": 20,
        },
        "signal_breakdown": {
            "catalyst": {"direction": "bullish" if direction == "long" else "bearish"},
            "fundamental": {"direction": "bullish"},
            "pattern": {"direction": "neutral"},
            "web_research": {"direction": "bullish" if recommendation != "pass" else "bearish"},
        },
        "regime": {"regime": "neutral", "position_size_multiplier": 0.8},
        "opus_evaluation": {
            "recommendation": recommendation,
            "conviction": "high" if recommendation == "proceed" else "medium" if recommendation == "watchlist" else "low",
            "key_risk": "The setup is crowded enough that weak market tape can overwhelm ticker-specific signal.",
            "stress_test": "Assume the first move is a head fake; only the stop keeps loss bounded.",
            "reasoning": (
                "Opus likes the signal stack but wants execution discipline. The setup is tradable only if "
                "entry, stop, and target lines stay legible enough for quick review on mobile."
            ),
            "position_size_adjustment": 0.7 if recommendation == "reduce_size" else 1.0,
            "final_score": 0.78 if recommendation == "proceed" else 0.50 if recommendation == "watchlist" else 0.22,
        },
    }


def _telegramish_html(md: str) -> str:
    # First protect code spans, then convert TelegramV2-ish marks, then unescape display escapes.
    code_parts: list[str] = []

    def hold_code(match: re.Match[str]) -> str:
        code_parts.append(html.escape(match.group(1).replace("\\", "")))
        return f"@@CODE{len(code_parts)-1}@@"

    s = html.escape(md)
    s = re.sub(r"`([^`]*)`", hold_code, s)
    s = re.sub(r"(?<!\\)\*([^*\n]+)(?<!\\)\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\\)_([^_\n]+)(?<!\\)_", r"<em>\1</em>", s)
    s = re.sub(r"\\([_\*\[\]\(\)~`&gt;#\+\-=\|{}\.!])", r"\1", s)
    for i, code in enumerate(code_parts):
        s = s.replace(f"@@CODE{i}@@", f"<code>{code}</code>")
    return s.replace("\n", "<br>\n")


def render(label: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    memos = [("NVDA", "proceed"), ("AAPL", "watchlist"), ("MSFT", "pass")]
    cards = []
    for ticker, rec in memos:
        md = format_memo_telegram(_base_memo(ticker, rec))
        (OUT_DIR / f"{label}-{ticker}.md").write_text(md, encoding="utf-8")
        cards.append(
            f"<section class='card'><div class='meta'>{ticker} · {rec.upper()}</div>"
            f"<div class='bubble'>{_telegramish_html(md)}</div></section>"
        )
    html_doc = f"""<!doctype html>
<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>{label.title()} mobile memo QA</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ margin: 0; background: #0f172a; font: 15px/1.38 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }}
.phone {{ width: 390px; margin: 0 auto; padding: 12px 10px 28px; box-sizing: border-box; background: #e7eef5; }}
.header {{ color: #334155; font-weight: 700; letter-spacing: .02em; margin: 2px 0 10px; }}
.card {{ margin: 0 0 18px; }}
.meta {{ color: #64748b; font-size: 12px; font-weight: 700; margin: 0 0 5px 8px; }}
.bubble {{ background: #fff; color: #111827; border-radius: 18px 18px 18px 4px; padding: 10px 12px; box-shadow: 0 1px 2px rgb(15 23 42 / .14); overflow-wrap: anywhere; }}
strong {{ font-weight: 750; }}
em {{ color: #334155; font-style: italic; }}
code {{ display: inline; background: #eef2f7; border: 1px solid #dbe4ef; border-radius: 5px; padding: 0 3px; font: 13px/1.25 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: nowrap; }}
@media (prefers-color-scheme: dark) {{
  .phone {{ background: #111827; }} .bubble {{ background: #1f2937; color: #f9fafb; }}
  .header, .meta, em {{ color: #cbd5e1; }} code {{ background: #111827; border-color: #334155; color: #f8fafc; }}
}}
</style></head><body><main class='phone'><div class='header'>{label.upper()} · 390px Telegram-style preview</div>{''.join(cards)}</main></body></html>"""
    html_path = OUT_DIR / f"{label}.html"
    html_path.write_text(html_doc, encoding="utf-8")
    print(html_path)


if __name__ == "__main__":
    render(sys.argv[1] if len(sys.argv) > 1 else "after")
