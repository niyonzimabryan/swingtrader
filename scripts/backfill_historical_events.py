"""Backfill historical events for ticker/setup pairs outside the memo hot path.

Exposes an importable core (`run_backfill`) and a queue consumer (`drain_queue`)
so the scheduled job in `orchestrator/scheduler.py` can drain the cold-ticker
backfill queue without duplicating this logic.
"""

from __future__ import annotations

import argparse
import json

from agents.pattern_agent import LEGACY_TO_ANALOG_SETUP_TYPE
from config.peers import get_peer_resolution
from config.settings import Settings, resolve_backfill_queue_path
from data.event_discovery import EventDiscoveryEngine
from data.event_outcomes import EventOutcomeEngine, HistoricalMarketCapUnavailable
from database.db import get_session, init_db
from database.models import CompanyProfile
from utils.logger import get_logger
from utils.perplexity_search_client import PerplexitySearchClient
from utils.web_search_client import WebSearchClient

log = get_logger("backfill_historical_events")

DEFAULT_EXAMPLES = [
    ("DFTX", "general_positive_catalyst"),
    ("OSCR", "analyst_upgrade_cluster"),
    ("HNGE", "product_launch"),
    ("AAPL", "product_launch"),
]


def _normalize_request(item: dict) -> dict:
    ticker = (item.get("ticker") or item.get("target_ticker") or "").upper()
    setup_type = item.get("setup_type") or ""
    return {
        "ticker": ticker,
        "setup_type": setup_type,
        "catalyst_summary": item.get("catalyst_summary") or setup_type.replace("_", " "),
        "company_name": item.get("company_name", ""),
        "direction": item.get("direction", "neutral"),
    }


def run_backfill(requests: list[dict], settings: Settings | None = None, max_tickers: int | None = None) -> dict:
    """Discover, store, and compute outcomes/context for ticker/setup requests.

    Assumes ``init_db`` has already been called. Returns a summary dict.
    Outcomes are always computed (the analog-ranking critical path); PIT context
    is best-effort — an event whose historical market cap is unavailable keeps its
    outcome and is simply skipped for context (never backfilled with current-as-of
    values).
    """
    settings = settings or Settings()
    normalized = [
        _normalize_request(item)
        for item in requests
        if (item.get("ticker") or item.get("target_ticker"))
    ]
    if max_tickers is not None:
        normalized = normalized[:max_tickers]

    summary = {"tickers": 0, "events_stored": 0, "outcomes_computed": 0}
    if not normalized:
        return summary

    with get_session() as session:
        web_client = WebSearchClient("gemini", None, settings) if settings.gemini_api_key else None
        perplexity = PerplexitySearchClient(settings, session=session) if settings.perplexity_api_key else None
        discovery = EventDiscoveryEngine(settings, web_search_client=web_client, perplexity_client=perplexity)
        outcomes = EventOutcomeEngine(settings)

        for item in normalized:
            ticker = item["ticker"]
            setup_type = LEGACY_TO_ANALOG_SETUP_TYPE.get(item["setup_type"], item["setup_type"])
            summary["tickers"] += 1
            peer_resolution = get_peer_resolution(ticker, settings, session=session, allow_network=True)
            request = {
                "target_ticker": ticker,
                "company_name": item["company_name"],
                "setup_type": setup_type,
                "catalyst_summary": item["catalyst_summary"],
                "direction": item["direction"],
                "peers": peer_resolution.get("peers", []),
            }
            result = discovery.discover_and_store(session, request, run_id=f"backfill-{ticker}-{setup_type}")
            for event in result.get("events", []):
                outcomes.compute_outcome(event, session=session)
                summary["events_stored"] += 1
                summary["outcomes_computed"] += 1
                profile = session.query(CompanyProfile).filter_by(ticker=event.ticker).first()
                sector = profile.sector if profile else ""
                try:
                    outcomes.compute_context(event, session=session, sector=sector)
                except HistoricalMarketCapUnavailable as exc:
                    log.warning("backfill_context_skipped", ticker=event.ticker, reason=str(exc))
            # Commit per request: an interrupted run (ssh drop / lock / crash)
            # keeps completed tickers instead of rolling everything back, and the
            # write lock never spans multi-minute provider calls (BRY-300).
            session.commit()
    return summary


def drain_queue(settings: Settings | None = None, max_tickers: int | None = None) -> dict:
    """Drain the pattern backfill queue file on the DB volume, up to the per-run cap."""
    settings = settings or Settings()
    path = resolve_backfill_queue_path(settings)
    summary = {"tickers": 0, "events_stored": 0, "outcomes_computed": 0, "queue_path": str(path)}
    if not path.exists():
        return summary
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        return summary

    cap = max_tickers if max_tickers is not None else int(getattr(settings, "pattern_backfill_max_tickers_per_run", 20))

    # Dedupe by (ticker, setup_type): cold tickers re-enqueue on every scan, so
    # without this the per-run cap can be eaten by copies of the same request.
    # First occurrence wins; duplicates are dropped from the queue entirely.
    unique: list[tuple[tuple[str, str], str, dict]] = []
    seen: set[tuple[str, str]] = set()
    for line in lines:
        item = json.loads(line)
        key = ((item.get("ticker") or item.get("target_ticker") or "").upper(), item.get("setup_type") or "")
        if key in seen:
            continue
        seen.add(key)
        unique.append((key, line, item))

    batch = unique[: max(0, cap)]
    requests = [item for _, _, item in batch]
    result = run_backfill(requests, settings)
    summary.update({key: result[key] for key in ("tickers", "events_stored", "outcomes_computed")})

    remaining = [line for _, line, _ in unique[len(batch):]]
    if remaining:
        path.write_text("\n".join(remaining) + "\n", encoding="utf-8")
    else:
        path.unlink(missing_ok=True)
    return summary


def run(args) -> dict:
    settings = Settings()
    init_db(settings.database_url)
    if args.queue:
        return drain_queue(settings)

    requests: list[dict] = []
    if args.ticker and args.setup_type:
        requests.append(
            {
                "ticker": args.ticker.upper(),
                "setup_type": args.setup_type,
                "catalyst_summary": args.catalyst_summary or args.setup_type.replace("_", " "),
            }
        )
    if not requests:
        requests = [
            {"ticker": ticker, "setup_type": setup, "catalyst_summary": setup.replace("_", " ")}
            for ticker, setup in DEFAULT_EXAMPLES
        ]
    return run_backfill(requests, settings)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker")
    parser.add_argument("--setup-type")
    parser.add_argument("--catalyst-summary", default="")
    parser.add_argument(
        "--queue",
        action="store_true",
        help="Drain the pattern backfill queue on the DB volume instead of running the default set",
    )
    args = parser.parse_args()
    summary = run(args)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
