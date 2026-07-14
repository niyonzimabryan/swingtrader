"""CLI: event-replay backtester (Spec J2/J3/J4).

    python -m backtest.run_event_replay [--classes upgrades,earnings] [--years N]
                                        [--sweep] [--json out.json]

Reads whatever DB ``DATABASE_URL`` points at (ops runs it in the prod container
against the warmed pattern library). Never touches the live scan path. Prints a
markdown summary and optionally writes a JSON artifact. No LLM calls.
"""

from __future__ import annotations

import argparse
import json
import math

from config.settings import Settings
from database.db import get_session, init_db
from backtest.event_replay import (
    WIN_RATE_BIAS_THRESHOLD,
    aggregate,
    replay_events,
    replay_shadow_ledger,
    run_sweep,
)


def _pf(value) -> str:
    return "∞" if value == float("inf") or (isinstance(value, float) and math.isinf(value)) else f"{value:.2f}"


def _rules(rule_dist: dict) -> str:
    return ", ".join(f"{k}:{v}" for k, v in sorted(rule_dist.items())) or "-"


def _stats_table(title: str, groups: dict) -> str:
    lines = [f"### {title}", ""]
    if not groups:
        return "\n".join(lines + ["_(no samples)_", ""])
    lines.append("| group | n | win% | med% | avg% | PF | avg hold | rules | >75%? |")
    lines.append("|---|--:|--:|--:|--:|--:|--:|---|:--:|")
    for name, s in groups.items():
        flag = "⚠️" if s.get("win_rate_bias_flag") else ""
        lines.append(
            f"| {name} | {s['n']} | {s['win_rate'] * 100:.1f} | {s['median_pnl_pct']:.2f} | "
            f"{s['avg_pnl_pct']:.2f} | {_pf(s['profit_factor'])} | {s['avg_holding_days']:.1f} | "
            f"{_rules(s['rule_dist'])} | {flag} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_markdown(agg: dict, skipped: dict, sweep: dict | None, shadow: dict | None) -> str:
    out = ["# Event-replay backtest", ""]
    overall = agg.get("overall") or {}
    if overall:
        out.append(
            f"**Overall:** n={overall['n']}, win={overall['win_rate'] * 100:.1f}%, "
            f"avg={overall['avg_pnl_pct']:.2f}%, median={overall['median_pnl_pct']:.2f}%, "
            f"PF={_pf(overall['profit_factor'])}, avg hold={overall['avg_holding_days']:.1f}d"
        )
    else:
        out.append("_No events replayed._")
    out.append(f"\n_Skipped: {skipped or 'none'}. "
               f"⚠️ marks win rate > {WIN_RATE_BIAS_THRESHOLD * 100:.0f}% (audit lookahead/selection smell)._\n")

    out.append(_stats_table("By event type", agg.get("by_event_type", {})))
    out.append(_stats_table("By magnitude bucket", agg.get("by_magnitude", {})))
    out.append(_stats_table("By source type (search vs fmp_structured)", agg.get("by_source_type", {})))

    if sweep is not None:
        out.append("## Parameter sweep (report only — no auto-application)\n")
        grid = sweep["grid"]
        out.append(
            f"Grid: stop×{grid['stop_mults']} · target×{grid['target_scales']} · "
            f"hold{grid['max_holding_days']} = {grid['combo_count']} combos/class.\n"
        )
        out.append("| class | n | best stop× | best tgt× | best hold | best expectancy% | caution |")
        out.append("|---|--:|--:|--:|--:|--:|:--:|")
        for cls, data in sweep["per_class"].items():
            best = data["best"] or {}
            combo = best.get("combo", {})
            caution = "n<30 ⚠️" if data["caution_small_sample"] else ""
            out.append(
                f"| {cls} | {data['n']} | {combo.get('stop_mult', '-')} | {combo.get('target_scale', '-')} | "
                f"{combo.get('max_holding_days', '-')} | {best.get('expectancy_pct', 0):.2f} | {caution} |"
            )
        out.append("")

    if shadow is not None:
        out.append("## Shadow-ledger replay (Spec I calibration)\n")
        out.append(f"rows={shadow['rows']}, replayed={shadow['replayed']}\n")
        out.append(_stats_table("By cohort", shadow["by_cohort"]))
        out.append(_stats_table("By score bucket", shadow["by_score_bucket"]))
    else:
        out.append("_Shadow-ledger (Spec I `scored_candidates`) not present — J4 skipped._\n")

    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Event-replay backtester (Spec J)")
    parser.add_argument("--classes", default="", help="comma list, e.g. upgrades,earnings (default: all)")
    parser.add_argument("--years", type=int, default=None, help="only events within the last N years")
    parser.add_argument("--sweep", action="store_true", help="run the parameter sensitivity grid (J3)")
    parser.add_argument("--json", dest="json_out", default=None, help="write JSON artifact to this path")
    args = parser.parse_args(argv)

    settings = Settings()
    init_db(settings.database_url)
    classes = [c for c in args.classes.split(",") if c.strip()] or None

    with get_session() as session:
        records, contexts, skipped = replay_events(session, settings, classes=classes, years=args.years)
        agg = aggregate(records)
        sweep = run_sweep(contexts, getattr(settings, "backtest_slippage_bps", 10.0)) if args.sweep else None
        shadow = replay_shadow_ledger(session, settings, years=args.years)

    print(render_markdown(agg, skipped, sweep, shadow))

    if args.json_out:
        artifact = {
            "classes": classes, "years": args.years,
            "skipped": skipped, "aggregate": agg, "sweep": sweep, "shadow_ledger": shadow,
        }
        with open(args.json_out, "w") as fh:
            json.dump(artifact, fh, indent=2, default=str)
        print(f"\n_JSON artifact written to {args.json_out}_")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
