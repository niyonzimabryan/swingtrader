"""CLI for the event-replay backtester (Spec J2-J4).

    python -m backtest.run_event_replay [--classes upgrades,earnings] [--years N]
                                        [--json out.json] [--sweep] [--shadow]

Reads whatever DB ``DATABASE_URL`` points at (ops runs it in the prod container
against the warmed pattern library). NEVER touches the live scan path — it only
reads events/prices and simulates. Prints a markdown summary and, with --json,
writes the full structured artifact.
"""

from __future__ import annotations

import argparse
import json

from backtest.event_replay import DEFAULT_SLIPPAGE_BPS, run_event_replay
from config.settings import Settings
from database.db import get_session, init_db


# --------------------------------------------------------------------------- #
# Markdown rendering.
# --------------------------------------------------------------------------- #


def _fmt_pf(pf) -> str:
    return "∞" if pf is None else f"{pf:.2f}"


def _flag(stats: dict) -> str:
    return " ⚠️>75%" if stats.get("bias_flag") else ""


def _stats_row(label: str, s: dict) -> str:
    if s.get("n", 0) == 0:
        return f"| {label} | 0 | — | — | — | — | — |"
    return (
        f"| {label} | {s['n']} | {s['win_rate'] * 100:.1f}%{_flag(s)} | "
        f"{s['median_pnl']:+.2f} | {s['avg_pnl']:+.2f} | {_fmt_pf(s['profit_factor'])} | "
        f"{s['avg_holding_days']:.1f} |"
    )


def _table(title: str, groups: dict) -> list[str]:
    lines = [f"### {title}", "",
             "| Group | n | Win rate | Median % | Avg % | Profit factor | Avg hold (d) |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for label, s in groups.items():
        lines.append(_stats_row(label, s))
    lines.append("")
    return lines


def render_markdown(result: dict) -> str:
    p = result["params"]
    bs = result["build_stats"]
    lines = [
        "# Event-replay backtest",
        "",
        f"- Classes: `{p['classes']}` · Years: `{p['years']}` · Slippage: `{p['slippage_bps']} bps`",
        f"- Entry: **{p['entry']}** · Same-bar: **{p['same_bar']}** · Max hold: `{p['max_holding_days']}d`",
        f"- LLM-stage replay: **{p['llm_replay']}**",
        "",
        (f"Built **{bs['built']}** trades from {bs['considered']} events "
         f"(skipped: neutral {bs['skipped_neutral']}, no-bars {bs['skipped_no_bars']}, "
         f"no-entry {bs['skipped_no_entry']}, insufficient-forward {bs['skipped_insufficient_forward']})."),
        "",
    ]
    report = result["report"]
    lines += _table("Per class (event_type) — expectancy", report["by_event_type"])
    lines += _table("By source type (search vs fmp_structured)", report["by_source_type"])
    lines += _table("By event_type × magnitude bucket", report["by_type_and_magnitude"])
    lines += ["**Overall**", "", *_table("All trades", {"all": report["overall"]})]

    if "sweep" in result:
        lines += render_sweep(result["sweep"])
    if "shadow" in result:
        lines += render_shadow(result["shadow"])
    return "\n".join(lines)


def render_sweep(sweep: dict) -> list[str]:
    lines = ["## J3 — parameter sensitivity (best combo per class)", "",
             "Report only; nothing is auto-applied.", "",
             "| Class | n | Best stop× | Best target× | Best hold | Expectancy % | Win rate | Caution |",
             "|---|---:|---:|---:|---:|---:|---:|---|"]
    for event_type, data in sweep.items():
        best = data.get("best")
        caution = "n<30 — low confidence" if data.get("small_sample") else ""
        if not best:
            lines.append(f"| {event_type} | {data['n']} | — | — | — | — | — | {caution} |")
            continue
        lines.append(
            f"| {event_type} | {data['n']} | {best['stop_mult']} | {best['target_mult']} | "
            f"{best['hold_days']} | {best['expectancy']:+.3f} | {best['win_rate'] * 100:.1f}% | {caution} |"
        )
    lines.append("")
    return lines


def render_shadow(shadow: dict) -> list[str]:
    lines = ["## J4 — shadow calibration ledger", ""]
    if shadow.get("status") != "ok":
        lines += [f"_Skipped: {shadow.get('status')}_ (Spec I `scored_candidates` table absent).", ""]
        return lines
    lines += _table("By cohort", shadow["by_cohort"])
    lines += _table("By score bucket", shadow["by_score_bucket"])
    return lines


# --------------------------------------------------------------------------- #
# Entry point.
# --------------------------------------------------------------------------- #


def run(args) -> dict:
    settings = Settings()
    # Default target is whatever DATABASE_URL/settings points at (prod ops path);
    # --db is a convenience override for local scratch runs, since settings.py
    # calls load_dotenv(override=True) and would otherwise clobber an exported
    # DATABASE_URL with the repo .env value.
    init_db(args.db or settings.database_url)
    classes = [c.strip() for c in args.classes.split(",") if c.strip()] if args.classes else None
    with get_session() as session:
        return run_event_replay(
            session,
            settings,
            classes=classes,
            years=args.years,
            slippage_bps=args.slippage_bps,
            with_sweep=args.sweep,
            with_shadow=args.shadow,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="", help="DB URL override (default: DATABASE_URL / settings)")
    parser.add_argument("--classes", default="", help="Comma-separated class tokens, e.g. upgrades,earnings (default: all)")
    parser.add_argument("--years", type=int, default=None, help="Lookback window in years (default: all)")
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    parser.add_argument("--sweep", action="store_true", help="Also run the J3 parameter grid")
    parser.add_argument("--shadow", action="store_true", help="Also replay the J4 shadow ledger (skips if absent)")
    parser.add_argument("--json", dest="json_out", default="", help="Write the full structured artifact to this path")
    args = parser.parse_args()

    result = run(args)
    print(render_markdown(result))
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(result, fh, indent=2, default=str)
        print(f"\n[wrote JSON artifact: {args.json_out}]")


if __name__ == "__main__":
    main()
