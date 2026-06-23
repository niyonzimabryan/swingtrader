"""Deterministic bakeoff for the pattern analog engine.

This script reads stored HistoricalEvent/EventOutcome rows only. It does not run
live Gemini or Perplexity discovery, so repeated bakeoffs are reproducible.
"""

from __future__ import annotations

import argparse
import json

from config.peers import get_peer_resolution
from config.settings import Settings
from data.analog_ranker import AnalogRanker
from database.db import get_session, init_db


DEFAULT_CASES = [
    {"ticker": "DFTX", "setup_type": "general_positive_catalyst", "expected": "unsupported_or_decomposed"},
    {"ticker": "OSCR", "setup_type": "analyst_upgrade_cluster", "expected": "active_or_partial"},
    {"ticker": "HNGE", "setup_type": "product_launch", "expected": "active_or_partial"},
    {"ticker": "AAPL", "setup_type": "product_launch", "expected": "active_or_partial"},
    {"ticker": "MSFT", "setup_type": "ai_or_platform_narrative_shift", "expected": "active_or_partial"},
    {"ticker": "LLY", "setup_type": "fda_or_regulatory_approval", "expected": "active_or_partial"},
]


def run(cases: list[dict] | None = None) -> dict:
    settings = Settings()
    init_db(settings.database_url)
    ranker = AnalogRanker(settings)
    rows = []
    broad_returns = []

    with get_session() as session:
        for case in cases or DEFAULT_CASES:
            peer_resolution = get_peer_resolution(case["ticker"], settings, session=session, allow_network=False)
            request = {
                "target_ticker": case["ticker"],
                "setup_type": case["setup_type"],
                "catalyst_summary": case.get("catalyst_summary", case["setup_type"].replace("_", " ")),
                "peers": peer_resolution.get("peers", []),
                "current_context": {},
            }
            ranked = ranker.rank(session, request, peer_resolution)
            stats = ranked.get("summary_stats", {})
            win_rate = stats.get("win_rate_t10", 0.5)
            broad_returns.append(win_rate)
            rows.append(
                {
                    "ticker": case["ticker"],
                    "setup_type": case["setup_type"],
                    "new_status": ranked.get("status"),
                    "analog_count": stats.get("total_instances", 0),
                    "direct_peer_broad_mix": {
                        key: len(value)
                        for key, value in ranked.get("evidence_tiers", {}).items()
                    },
                    "provider_calls": 0,
                    "cost_estimate": 0.0,
                    "win_rate_t10": win_rate,
                    "median_return_t10": stats.get("median_return_t10", 0.0),
                    "warnings": ranked.get("warnings", []),
                }
            )

    broad_average = sum(broad_returns) / len(broad_returns) if broad_returns else 0.5
    return {
        "cases": rows,
        "bias_sanity": {
            "average_win_rate_t10": round(broad_average, 3),
            "flag": broad_average > 0.75,
            "note": "Win rate above 75% is a survivorship-bias red flag; inspect discovery queries and sources."
            if broad_average > 0.75
            else "Win-rate sanity check passed.",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases-json", default="")
    args = parser.parse_args()
    cases = json.loads(args.cases_json) if args.cases_json else None
    print(json.dumps(run(cases), indent=2))


if __name__ == "__main__":
    main()
