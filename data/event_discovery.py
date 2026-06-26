"""Search-backed historical event discovery with strict live-path budgets."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from data.event_extractor import EventExtractor
from database.models import PatternSearchRun
from utils.logger import get_logger
from utils.redaction import redact_payload

log = get_logger("event_discovery")

BANNED_OUTCOME_PATTERNS = [
    r"\brose\b",
    r"\bjump(?:ed)?\b",
    r"\bsurge(?:d)?\b",
    r"\bsoar(?:ed)?\b",
    r"\brall(?:y|ied)\b",
    r"\bpopp?ed\b",
    r"\bplung(?:e|ed)\b",
    r"\bplummeted\b",
    r"\bsank\b",
    r"\bfell\b",
    r"\bdropped\b",
    r"\bcrashed\b",
    r"\btanked\b",
    r"\bgained\b",
    r"\bgainer\b",
    r"\bloser\b",
    r"\bwinner\b",
    r"\bbest\s+performing\b",
    r"\bworst\s+performing\b",
    r"\bshares\s+rise\b",
    r"\bshares\s+fall\b",
    r"\bstock\s+up\b",
    r"\bstock\s+down\b",
    r"\bbest[/\s-]?worst\s+performing\b",
    r"\bafter\s+beating\b",
    r"\bstock\s+reaction\b",
]
BANNED_RE = re.compile("|".join(BANNED_OUTCOME_PATTERNS), re.IGNORECASE)

TIER_A_TYPES = {
    "earnings_beat_guide_up",
    "earnings_beat_guide_flat",
    "earnings_beat_guide_down",
    "earnings_miss",
    "revenue_acceleration",
    "analyst_upgrade_cluster",
    "analyst_downgrade_cluster",
    "insider_cluster_buy",
    "buyback_announcement",
    "dividend_initiation_or_raise",
    "m_and_a_confirmed",
    "fda_or_regulatory_approval",
}
TIER_B_TYPES = {
    "product_launch",
    "major_contract_win",
    "partnership_announcement",
    "management_change",
    "pricing_change",
    "strategic_pivot",
    "litigation_resolution",
    "sector_catalyst_positive",
    "sector_catalyst_negative",
    "ai_or_platform_narrative_shift",
    "capital_raise_or_debt_refi",
    "guidance_or_preannouncement",
}
TIER_C_TYPES = {
    "general_positive_catalyst",
    "general_negative_catalyst",
    "momentum_without_identified_catalyst",
    "rumor_unconfirmed",
}
EVENT_SUPPORTED_TYPES = TIER_A_TYPES | TIER_B_TYPES

QUERY_TEMPLATES = {
    "product_launch": [
        '"{peer}" product launch announcement {year}',
        '"{peer}" unveils new product press release {year}',
        '"{peer}" earnings call transcript product roadmap {year}',
    ],
    "analyst_upgrade_cluster": [
        '"{peer}" analyst rating change {year}',
        '"{peer}" analyst price target revision {year}',
    ],
    "analyst_downgrade_cluster": [
        '"{peer}" analyst rating change {year}',
        '"{peer}" analyst price target revision {year}',
    ],
    "management_change": [
        '"{peer}" names new CEO {year}',
        '"{peer}" CEO transition announcement {year}',
    ],
    "fda_or_regulatory_approval": [
        '"{peer}" FDA approval announcement {year}',
        '"{peer}" regulatory decision press release {year}',
    ],
    "sector_catalyst_positive": [
        '"{industry}" sector regulation announcement {year}',
        '"{sector}" policy change {year}',
    ],
    "sector_catalyst_negative": [
        '"{industry}" sector regulation announcement {year}',
        '"{sector}" policy change {year}',
    ],
    "major_contract_win": [
        '"{peer}" contract award announcement {year}',
        '"{peer}" customer win press release {year}',
    ],
    "partnership_announcement": [
        '"{peer}" partnership announcement {year}',
        '"{peer}" strategic collaboration press release {year}',
    ],
    "buyback_announcement": [
        '"{peer}" share repurchase authorization announcement {year}',
    ],
    "dividend_initiation_or_raise": [
        '"{peer}" dividend increase announcement {year}',
        '"{peer}" initiates dividend press release {year}',
    ],
}


def assert_outcome_neutral(query: str) -> str:
    """Reject generated discovery queries that condition on price outcomes."""
    if BANNED_RE.search(query or ""):
        raise ValueError(f"Outcome-conditioned discovery query rejected: {query}")
    return query


@dataclass
class DiscoveryBudget:
    max_requests: int
    wallclock_s: int
    started: float = field(default_factory=time.monotonic)
    requests_used: int = 0

    def can_request(self) -> bool:
        return self.requests_used < self.max_requests and not self.expired()

    def mark_request(self) -> None:
        self.requests_used += 1

    def expired(self) -> bool:
        return (time.monotonic() - self.started) >= self.wallclock_s

    def elapsed_s(self) -> float:
        return time.monotonic() - self.started


class EventDiscoveryEngine:
    """Discover, extract, and store historical events within request/time budgets."""

    def __init__(
        self,
        settings=None,
        web_search_client=None,
        perplexity_client=None,
        extractor: EventExtractor | None = None,
    ):
        self.settings = settings
        self.web_search_client = web_search_client
        self.perplexity_client = perplexity_client
        self.extractor = extractor or EventExtractor()

    def generate_queries(self, request: dict, years: list[int] | None = None, llm_expanded: list[str] | None = None) -> list[str]:
        setup_type = request.get("setup_type") or ""
        years = years or list(range(datetime.utcnow().year - 1, datetime.utcnow().year - 7, -1))
        peers = [request.get("target_ticker")] + [p.get("ticker") if isinstance(p, dict) else p for p in request.get("peers", [])]
        peers = [p for p in peers if p]
        context = {
            "industry": request.get("industry") or request.get("sector") or "industry",
            "sector": request.get("sector") or "sector",
        }
        templates = QUERY_TEMPLATES.get(setup_type) or [f'"{{peer}}" {setup_type.replace("_", " ")} announcement {{year}}']
        max_queries = int(getattr(self.settings, "pattern_max_search_queries_per_catalyst", 8) if self.settings else 8)
        queries = []
        for peer in peers:
            for year in years:
                for template in templates:
                    query = template.format(peer=peer, year=year, **context)
                    queries.append(assert_outcome_neutral(query))
                    if len(queries) >= max_queries:
                        return queries
        for query in llm_expanded or []:
            queries.append(assert_outcome_neutral(query))
            if len(queries) >= max_queries:
                break
        return queries

    def discover_and_store(self, session, request: dict, run_id: str) -> dict:
        budget = DiscoveryBudget(
            max_requests=int(getattr(self.settings, "pattern_max_search_queries_per_catalyst", 8) if self.settings else 8),
            wallclock_s=int(getattr(self.settings, "pattern_stage_wallclock_budget_s", 45) if self.settings else 45),
        )
        setup_type = request.get("setup_type") or ""
        if setup_type in TIER_C_TYPES:
            return self._record_run(
                session, run_id, request, "unsupported", [], [], budget, {"reason": "unsupported vague catalyst"},
            )
        if setup_type not in EVENT_SUPPORTED_TYPES:
            return self._record_run(
                session, run_id, request, "unsupported", [], [], budget, {"reason": "unknown catalyst type"},
            )

        queries = self.generate_queries(request)
        stored = []
        rejected = []
        provider_usage = {"gemini": 0, "perplexity": 0}
        min_total = int(getattr(self.settings, "pattern_min_total_matches", 10) if self.settings else 10)
        max_events_per_query = int(getattr(self.settings, "pattern_max_events_per_query", 10) if self.settings else 10)

        log.info("pattern_event_search_start", ticker=request.get("target_ticker"), setup_type=setup_type, queries=len(queries))
        for query in queries:
            if not budget.can_request() or len(stored) >= min_total:
                break
            candidates = self._gemini_events(query, request, max_events_per_query) if self.web_search_client else []
            provider_usage["gemini"] += 1 if self.web_search_client else 0
            budget.mark_request()
            if len(candidates) < 2 and self.perplexity_client and budget.can_request():
                candidates.extend(self._perplexity_events(query, request, max_events_per_query))
                provider_usage["perplexity"] += 1
                budget.mark_request()

            for item in candidates[:max_events_per_query]:
                provider = item.pop("_provider", "gemini")
                provider_result = item.pop("_provider_result", None)
                event, status = self.extractor.upsert_candidate(
                    session, item, provider=provider, provider_query=query, provider_result=provider_result
                )
                if event:
                    stored.append(event)
                else:
                    rejected.append(status)

        if stored:
            status = "active"
        elif budget.expired() or budget.requests_used >= budget.max_requests:
            status = "no_matches"
        else:
            status = "no_matches"

        return self._record_run(
            session,
            run_id,
            request,
            status,
            queries[: budget.requests_used],
            stored,
            budget,
            {"provider_usage": provider_usage, "rejected": len(rejected), "rejected_reasons": rejected[:10]},
        )

    def _gemini_events(self, query: str, request: dict, max_events: int) -> list[dict]:
        prompt = self._event_prompt(query, request, max_events)
        try:
            result = self.web_search_client.search_and_analyze_json_with_grounding(
                "You extract historical public-company catalyst events into strict JSON.",
                prompt,
                model=getattr(self.settings, "gemini_discovery_model", None),
                max_searches=2,
                max_tokens=4096,
            )
        except AttributeError:
            result = self.web_search_client.search_and_analyze_json(
                "You extract historical public-company catalyst events into strict JSON.",
                prompt,
                model=getattr(self.settings, "gemini_discovery_model", None),
                max_searches=2,
                max_tokens=4096,
            )
        except Exception as exc:
            log.warning("gemini_event_discovery_failed", query=query, error=str(exc))
            return []
        events = result.get("events", []) if isinstance(result, dict) else []
        for event in events:
            event["_provider"] = "gemini"
            event["_provider_result"] = result.get("_grounding", {}) if isinstance(result, dict) else {}
        return events if isinstance(events, list) else []

    def _perplexity_events(self, query: str, request: dict, max_events: int) -> list[dict]:
        try:
            result = self.perplexity_client.search(query, max_results=max_events)
        except Exception as exc:
            log.warning("perplexity_event_search_failed", query=query, error=str(exc))
            return []
        events = []
        for row in result.get("results", []) if isinstance(result, dict) else []:
            snippet = row.get("snippet") or row.get("title") or ""
            events.append(
                {
                    "ticker": request.get("target_ticker"),
                    "company_name": request.get("company_name", ""),
                    "event_type": request.get("setup_type"),
                    "event_subtype": "",
                    "event_date": "",
                    "event_date_source": "missing",
                    "event_timing": "unknown",
                    "polarity": request.get("direction") or "neutral",
                    "magnitude": None,
                    "headline": row.get("title", ""),
                    "summary": snippet,
                    "evidence": snippet,
                    "source_url": row.get("url", ""),
                    "source_type": "news",
                    "confidence": 0.56,
                    "_provider": "perplexity",
                    "_provider_result": row,
                }
            )
        return events

    def _event_prompt(self, query: str, request: dict, max_events: int) -> str:
        return json.dumps(
            {
                "task": "Search the web and extract historical event candidates. The event_date must be the public catalyst date derived from source content, not the search result publication/crawl date.",
                "query": assert_outcome_neutral(query),
                "target_ticker": request.get("target_ticker"),
                "setup_type": request.get("setup_type"),
                "catalyst_summary": request.get("catalyst_summary", ""),
                "max_events": max_events,
                "required_schema": {
                    "events": [
                        {
                            "ticker": "AAPL",
                            "event_type": request.get("setup_type"),
                            "event_subtype": "",
                            "event_date": "YYYY-MM-DD",
                            "event_date_source": "content",
                            "event_timestamp": None,
                            "event_timing": "unknown",
                            "polarity": "bullish|bearish|mixed|neutral",
                            "magnitude": 0.0,
                            "headline": "",
                            "summary": "",
                            "evidence": "Include the content date phrase used for event_date.",
                            "source_url": "",
                            "source_type": "company_ir|sec_filing|earnings_transcript|press_release|regulator|news|analyst_report|recap|other",
                            "confidence": 0.0,
                        }
                    ]
                },
                "rules": [
                    "Do not include events without a concrete content-derived public event date.",
                    "Do not infer event outcomes or price reactions.",
                    "Prefer primary sources.",
                ],
            }
        )

    def _record_run(
        self,
        session,
        run_id: str,
        request: dict,
        status: str,
        queries: list[str],
        events: list[Any],
        budget: DiscoveryBudget,
        metadata: dict,
    ) -> dict:
        catalyst_hash = hashlib.sha256(
            f"{request.get('target_ticker')}|{request.get('setup_type')}|{request.get('catalyst_summary', '')}".encode("utf-8")
        ).hexdigest()
        clean_metadata = redact_payload(metadata)
        run = PatternSearchRun(
            run_id=run_id,
            ticker=request.get("target_ticker") or "",
            setup_type=request.get("setup_type") or "",
            catalyst_hash=catalyst_hash,
            status=status,
            provider_plan_json=json.dumps(clean_metadata.get("provider_usage", {})),
            queries_json=json.dumps(redact_payload(queries)),
            peer_set_json=json.dumps(redact_payload(request.get("peers", []))),
            result_counts_json=json.dumps(
                {
                    "events_stored": len(events),
                    "requests_used": budget.requests_used,
                    **{k: v for k, v in clean_metadata.items() if k != "provider_usage"},
                }
            ),
            cost_estimate=(clean_metadata.get("provider_usage", {}).get("perplexity", 0) * 0.005),
            duration_s=round(budget.elapsed_s(), 3),
            error=clean_metadata.get("error", ""),
        )
        session.add(run)
        session.flush()
        log.info("pattern_event_search_complete", ticker=run.ticker, status=status, stored=len(events))
        return {
            "status": status,
            "search_run_id": run.id,
            "events": events,
            "queries": queries,
            "provider_usage": clean_metadata.get("provider_usage", {}),
            "duration_s": run.duration_s,
            "requests_used": budget.requests_used,
        }
