"""Backfill historical events for ticker/setup pairs outside the memo hot path."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agents.pattern_agent import LEGACY_TO_ANALOG_SETUP_TYPE
from config.peers import get_peer_resolution
from config.settings import Settings
from data.event_discovery import EventDiscoveryEngine
from data.event_outcomes import EventOutcomeEngine, HistoricalMarketCapUnavailable
from database.db import get_session, init_db
from database.models import CompanyProfile
from utils.perplexity_search_client import PerplexitySearchClient
from utils.web_search_client import WebSearchClient


def _requests(args, settings: Settings) -> list[dict]:
    requests = []
    if args.queue:
        path = Path(args.queue)
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    requests.append(json.loads(line))
    if args.ticker and args.setup_type:
        requests.append(
            {
                "ticker": args.ticker.upper(),
                "setup_type": args.setup_type,
                "catalyst_summary": args.catalyst_summary or args.setup_type.replace("_", " "),
            }
        )
    if not requests:
        default_examples = [
            ("DFTX", "general_positive_catalyst"),
            ("OSCR", "analyst_upgrade_cluster"),
            ("HNGE", "product_launch"),
            ("AAPL", "product_launch"),
        ]
        requests = [
            {"ticker": ticker, "setup_type": setup, "catalyst_summary": setup.replace("_", " ")}
            for ticker, setup in default_examples
        ]
    return requests


def run(args) -> int:
    settings = Settings()
    init_db(settings.database_url)
    count = 0

    with get_session() as session:
        web_client = None
        if settings.gemini_api_key:
            web_client = WebSearchClient("gemini", None, settings)
        perplexity = PerplexitySearchClient(settings, session=session) if settings.perplexity_api_key else None
        discovery = EventDiscoveryEngine(settings, web_search_client=web_client, perplexity_client=perplexity)
        outcomes = EventOutcomeEngine(settings)

        for item in _requests(args, settings):
            ticker = (item.get("ticker") or item.get("target_ticker") or "").upper()
            setup_type = LEGACY_TO_ANALOG_SETUP_TYPE.get(item.get("setup_type"), item.get("setup_type"))
            peer_resolution = get_peer_resolution(ticker, settings, session=session, allow_network=True)
            request = {
                "target_ticker": ticker,
                "company_name": item.get("company_name", ""),
                "setup_type": setup_type,
                "catalyst_summary": item.get("catalyst_summary", ""),
                "direction": item.get("direction", "neutral"),
                "peers": peer_resolution.get("peers", []),
            }
            result = discovery.discover_and_store(session, request, run_id=f"backfill-{ticker}-{setup_type}")
            for event in result.get("events", []):
                outcomes.compute_outcome(event, session=session)
                profile = session.query(CompanyProfile).filter_by(ticker=event.ticker).first()
                sector = profile.sector if profile else ""
                try:
                    outcomes.compute_context(event, session=session, sector=sector)
                except HistoricalMarketCapUnavailable:
                    raise
                count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker")
    parser.add_argument("--setup-type")
    parser.add_argument("--catalyst-summary", default="")
    parser.add_argument("--queue", default="")
    args = parser.parse_args()
    count = run(args)
    print(f"Backfilled {count} historical events with outcomes/context")


if __name__ == "__main__":
    main()
