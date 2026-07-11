"""Bulk-load historical catalyst events from FMP structured data (Spec H).

Offline/ops tool. Warms the pattern-analog library from two high-frequency
catalyst classes that have exact-dated structured API sources, so the engine
does not depend solely on grounded Gemini search (which yields ~0-2 events per
ticker/pass):

  * Earnings surprises  -> FMP /stable/earnings  (verified against FMP stable
    docs and the repo's existing FMP usage in data/event_outcomes.py).
  * Analyst upgrades/downgrades -> FMP /stable/grades. Same-direction actions
    inside a short window are clustered into one analyst_(up|down)grade_cluster
    event; isolated singles are skipped.

NO LLM calls anywhere in this path. Events are stored through the same
EventExtractor validation/dedupe path discovery uses, outcomes via the existing
EventOutcomeEngine, and PIT context best-effort (skipped, never faked, when
historical market cap is unavailable). Idempotent: re-running does not duplicate.

Usage (offline / ops only — never against the live Telegram process):
    python -m scripts.bulk_load_structured_events --tickers AAPL,MSFT --years 2
    python -m scripts.bulk_load_structured_events --universe --years 3
    python -m scripts.bulk_load_structured_events --tickers AAPL --dry-run
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from config.settings import Settings
from config.tickers import UNIVERSE  # canonical ~503-ticker universe (orchestrator/universe seeds from this)
from data.event_discovery import EARNINGS_BEAT_STRUCTURED, EARNINGS_MISS_STRUCTURED
from data.event_extractor import EventExtractor
from data.event_outcomes import (
    FMP_BASE,
    EventOutcomeEngine,
    HistoricalMarketCapUnavailable,
    is_event_mature,
)
from database.db import get_session, init_db
from database.models import CompanyProfile, HistoricalEvent
from utils.logger import get_logger
from utils.rate_limiter import rate_limiter
from utils.redaction import redact_text

log = get_logger("bulk_load_structured_events")

SOURCE_TYPE = "fmp_structured"
PROVIDER = "fmp_structured"
CONFIDENCE = 0.9
# Same-direction analyst actions within this window collapse into one cluster.
# ~5 trading days ≈ 7 calendar days.
CLUSTER_WINDOW_DAYS = 7
MIN_CLUSTER_SIZE = 2
# Clamp surprise % so a near-zero estimate can't produce an absurd magnitude.
MAGNITUDE_CLAMP = 100.0
GRADES_LIMIT = 1000
DEFAULT_YEARS = 3
DEFAULT_CLASSES = ("earnings", "upgrades")
# Dedupe against existing rows by (ticker, event_type, event_date ± this many days).
DEDUPE_WINDOW_DAYS = 2
# Offline tool: outcomes are cheap relative to a night of grounded search, so the
# per-run cap is generous. Override with --outcomes-per-run when FMP-throttled.
DEFAULT_OUTCOMES_PER_RUN = 100_000


# --------------------------------------------------------------------------- #
# Pure candidate builders (no network, no DB) — unit-tested directly.
# --------------------------------------------------------------------------- #


def build_earnings_candidate(
    ticker: str, row: dict, today: date, years: int, company_name: str = ""
) -> dict | None:
    """Map one FMP /earnings row to a HistoricalEvent candidate, or None to skip."""
    event_date = _to_date(row.get("date"))
    if not _in_window(event_date, today, years):
        return None
    actual = _to_float(row.get("epsActual"))
    estimate = _to_float(row.get("epsEstimated"))
    if actual is None or estimate is None or estimate == 0:
        return None  # no surprise computable without both sides and a non-zero base

    surprise = (actual - estimate) / abs(estimate) * 100
    surprise = max(-MAGNITUDE_CLAMP, min(MAGNITUDE_CLAMP, surprise))
    beat = actual >= estimate
    event_type = EARNINGS_BEAT_STRUCTURED if beat else EARNINGS_MISS_STRUCTURED
    polarity = "bullish" if beat else "bearish"
    date_iso = event_date.isoformat()

    headline = (
        f"{ticker} earnings {date_iso}: EPS {_fmt(actual)} vs {_fmt(estimate)} est "
        f"({surprise:+.1f}% surprise)"
    )
    summary = (
        f"Reported {date_iso}: EPS {_fmt(actual)} vs {_fmt(estimate)} estimate, "
        f"{surprise:+.1f}% surprise."
    )
    rev_actual = _to_float(row.get("revenueActual"))
    rev_estimate = _to_float(row.get("revenueEstimated"))
    if rev_actual and rev_estimate:
        summary += f" Revenue {_fmt_int(rev_actual)} vs {_fmt_int(rev_estimate)} est."
    evidence = (
        f"FMP structured earnings surprise for {ticker} reported {date_iso}: "
        f"actual EPS {_fmt(actual)}, estimate {_fmt(estimate)}."
    )
    return {
        "ticker": ticker,
        "company_name": company_name,
        "event_type": event_type,
        "event_subtype": "earnings_surprise",
        "event_date": date_iso,
        "event_date_source": "content",
        "event_timing": "unknown",
        "polarity": polarity,
        "magnitude": round(surprise, 2),
        "headline": headline,
        "summary": summary,
        "evidence": evidence,
        "source_url": _earnings_source_url(ticker),
        "source_type": SOURCE_TYPE,
        "confidence": CONFIDENCE,
    }


def cluster_grade_candidates(
    ticker: str, rows: list[dict], today: date, years: int, company_name: str = ""
) -> list[dict]:
    """Cluster same-direction analyst actions into upgrade/downgrade cluster events."""
    ups, downs = _grade_actions(rows, today, years)
    candidates: list[dict] = []
    for group in _cluster(ups):
        candidates.append(_build_grade_cluster_candidate(ticker, group, "upgrade", company_name))
    for group in _cluster(downs):
        candidates.append(_build_grade_cluster_candidate(ticker, group, "downgrade", company_name))
    return candidates


def _grade_actions(rows: list[dict], today: date, years: int) -> tuple[list[dict], list[dict]]:
    ups: list[dict] = []
    downs: list[dict] = []
    for row in rows or []:
        event_date = _to_date(row.get("date") or row.get("publishedDate"))
        if not _in_window(event_date, today, years):
            continue
        action = str(row.get("action") or "").lower()
        record = {
            "date": event_date,
            "firm": (row.get("gradingCompany") or "").strip(),
            "new": (row.get("newGrade") or "").strip(),
            "prev": (row.get("previousGrade") or "").strip(),
        }
        if "upgrade" in action:
            ups.append(record)
        elif "downgrade" in action:
            downs.append(record)
    return ups, downs


def _cluster(actions: list[dict]) -> list[list[dict]]:
    """Greedy same-direction clustering: group actions within CLUSTER_WINDOW_DAYS of
    the group's first action; keep only groups with >= MIN_CLUSTER_SIZE members."""
    ordered = sorted(actions, key=lambda a: a["date"])
    clusters: list[list[dict]] = []
    i = 0
    while i < len(ordered):
        start = ordered[i]["date"]
        group = [ordered[i]]
        j = i + 1
        while j < len(ordered) and (ordered[j]["date"] - start).days <= CLUSTER_WINDOW_DAYS:
            group.append(ordered[j])
            j += 1
        if len(group) >= MIN_CLUSTER_SIZE:
            clusters.append(group)
        i = j
    return clusters


def _build_grade_cluster_candidate(
    ticker: str, group: list[dict], direction: str, company_name: str
) -> dict:
    start = group[0]["date"]
    span = (group[-1]["date"] - start).days
    date_iso = start.isoformat()
    count = len(group)
    verb = "upgrades" if direction == "upgrade" else "downgrades"
    event_type = "analyst_upgrade_cluster" if direction == "upgrade" else "analyst_downgrade_cluster"
    polarity = "bullish" if direction == "upgrade" else "bearish"
    firms = ", ".join(sorted({a["firm"] for a in group if a["firm"]}))
    moves = "; ".join(
        f"{a['firm']} {a['prev']}→{a['new']}".strip() for a in group if a["firm"]
    )
    headline = f"{ticker}: {count} analyst {verb} in {span}d from {date_iso}"
    summary = f"Cluster of {count} analyst {verb} starting {date_iso}"
    summary += f" ({firms})." if firms else "."
    evidence = f"FMP structured analyst grade cluster for {ticker} starting {date_iso}"
    evidence += f": {moves}." if moves else "."
    return {
        "ticker": ticker,
        "company_name": company_name,
        "event_type": event_type,
        "event_subtype": f"{direction}_cluster",
        "event_date": date_iso,
        "event_date_source": "content",
        "event_timing": "unknown",
        "polarity": polarity,
        "magnitude": float(count),
        "headline": headline,
        "summary": summary,
        "evidence": evidence,
        "source_url": _grades_source_url(ticker),
        "source_type": SOURCE_TYPE,
        "confidence": CONFIDENCE,
    }


# --------------------------------------------------------------------------- #
# Loader (network + DB)
# --------------------------------------------------------------------------- #


class StructuredEventLoader:
    """Fetch FMP structured data, store events, compute outcomes/context."""

    def __init__(self, settings=None, extractor: EventExtractor | None = None,
                 outcome_engine: EventOutcomeEngine | None = None, fetch=None):
        self.settings = settings
        self.fmp_key = getattr(settings, "fmp_api_key", "") if settings else ""
        self.extractor = extractor or EventExtractor()
        self._outcome_engine = outcome_engine
        self._fetch = fetch  # test hook: fn(endpoint, params) -> list | dict | None

    def candidates_for(
        self, ticker: str, classes, years: int, today: date, company_name: str = ""
    ) -> list[dict]:
        candidates: list[dict] = []
        if "earnings" in classes:
            for row in self._earnings_rows(ticker, years):
                candidate = build_earnings_candidate(ticker, row, today, years, company_name)
                if candidate:
                    candidates.append(candidate)
        if "upgrades" in classes:
            candidates.extend(
                cluster_grade_candidates(ticker, self._grade_rows(ticker), today, years, company_name)
            )
        return candidates

    def load(
        self,
        session,
        tickers,
        classes=DEFAULT_CLASSES,
        years: int = DEFAULT_YEARS,
        dry_run: bool = False,
        outcomes_per_run: int = DEFAULT_OUTCOMES_PER_RUN,
        today: date | None = None,
        progress_every: int = 25,
    ) -> dict:
        today = today or date.today()
        summary = {
            "tickers": 0,
            "classes": list(classes),
            "years": years,
            "dry_run": dry_run,
            "earnings_candidates": 0,
            "upgrade_candidates": 0,
            "downgrade_candidates": 0,
            "would_store": 0,
            "events_stored": 0,
            "skipped_dupes": 0,
            "skipped_invalid": 0,
            "outcomes_computed": 0,
            "contexts_computed": 0,
            "llm_calls": 0,
        }
        outcome_engine = None if dry_run else self._get_outcome_engine()

        for idx, raw_ticker in enumerate(tickers, start=1):
            ticker = (raw_ticker or "").upper().strip()
            if not ticker:
                continue
            summary["tickers"] += 1
            per_ticker = {"events_stored": 0, "outcomes_computed": 0, "skipped_dupes": 0}

            for candidate in self.candidates_for(ticker, classes, years, today):
                _tally_candidate(summary, candidate["event_type"])
                if dry_run:
                    summary["would_store"] += 1
                    continue
                event_date = _to_date(candidate["event_date"])
                if self._existing_within(session, ticker, candidate["event_type"], event_date):
                    summary["skipped_dupes"] += 1
                    per_ticker["skipped_dupes"] += 1
                    continue
                event, status = self.extractor.upsert_candidate(
                    session, candidate, provider=PROVIDER, provider_query=candidate["source_url"]
                )
                if event is None:
                    summary["skipped_invalid"] += 1
                    continue
                if status != "created":
                    summary["skipped_dupes"] += 1
                    per_ticker["skipped_dupes"] += 1
                    continue
                summary["events_stored"] += 1
                per_ticker["events_stored"] += 1
                if (
                    outcome_engine is not None
                    and summary["outcomes_computed"] < outcomes_per_run
                    and is_event_mature(event.event_date)
                ):
                    if self._compute_outcome_and_context(outcome_engine, event, session, summary):
                        per_ticker["outcomes_computed"] += 1

            if idx % max(1, progress_every) == 0:
                log.info(
                    "bulk_load_progress",
                    ticker=ticker,
                    processed=idx,
                    events_stored=summary["events_stored"],
                    outcomes_computed=summary["outcomes_computed"],
                    skipped_dupes=summary["skipped_dupes"],
                )

        log.info("bulk_load_complete", **{k: v for k, v in summary.items() if k != "classes"})
        return summary

    def _compute_outcome_and_context(self, engine, event, session, summary) -> bool:
        try:
            engine.compute_outcome(event, session=session)
            summary["outcomes_computed"] += 1
        except Exception as exc:  # pragma: no cover - defensive; price provider hiccups
            log.warning("bulk_outcome_failed", ticker=event.ticker, error=redact_text(str(exc)))
            return False
        profile = session.query(CompanyProfile).filter_by(ticker=event.ticker).first()
        sector = profile.sector if profile else ""
        try:
            engine.compute_context(event, session=session, sector=sector)
            summary["contexts_computed"] += 1
        except HistoricalMarketCapUnavailable as exc:
            log.warning("bulk_context_skipped", ticker=event.ticker, reason=str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("bulk_context_failed", ticker=event.ticker, error=redact_text(str(exc)))
        return True

    def _existing_within(self, session, ticker: str, event_type: str, event_date: date) -> bool:
        lo = event_date - timedelta(days=DEDUPE_WINDOW_DAYS)
        hi = event_date + timedelta(days=DEDUPE_WINDOW_DAYS)
        return (
            session.query(HistoricalEvent.id)
            .filter(
                HistoricalEvent.ticker == ticker,
                HistoricalEvent.event_type == event_type,
                HistoricalEvent.event_date >= lo,
                HistoricalEvent.event_date <= hi,
            )
            .first()
            is not None
        )

    def _get_outcome_engine(self) -> EventOutcomeEngine:
        if self._outcome_engine is None:
            self._outcome_engine = EventOutcomeEngine(self.settings)
        return self._outcome_engine

    def _earnings_rows(self, ticker: str, years: int) -> list[dict]:
        limit = max(years * 4 + 8, 12)
        data = self._fmp_request("/earnings", {"symbol": ticker, "limit": limit})
        return data if isinstance(data, list) else []

    def _grade_rows(self, ticker: str) -> list[dict]:
        data = self._fmp_request("/grades", {"symbol": ticker, "limit": GRADES_LIMIT})
        return data if isinstance(data, list) else []

    def _fmp_request(self, endpoint: str, params: dict) -> list | dict | None:
        if self._fetch is not None:
            return self._fetch(endpoint, params)
        if not self.fmp_key:
            return None
        rate_limiter.acquire("fmp")
        try:
            query = {"apikey": self.fmp_key, **params}
            response = httpx.get(f"{FMP_BASE}{endpoint}", params=query, timeout=25)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            log.warning("bulk_fmp_request_failed", endpoint=endpoint, error=redact_text(str(exc)))
            return None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _tally_candidate(summary: dict, event_type: str) -> None:
    if event_type in (EARNINGS_BEAT_STRUCTURED, EARNINGS_MISS_STRUCTURED):
        summary["earnings_candidates"] += 1
    elif event_type == "analyst_upgrade_cluster":
        summary["upgrade_candidates"] += 1
    elif event_type == "analyst_downgrade_cluster":
        summary["downgrade_candidates"] += 1


def _in_window(event_date: date | None, today: date, years: int) -> bool:
    """PIT: strictly-past dates within the lookback window (future/today rejected)."""
    if event_date is None:
        return False
    return today - timedelta(days=365 * years) <= event_date < today


def _earnings_source_url(ticker: str) -> str:
    return f"{FMP_BASE}/earnings?symbol={ticker}"


def _grades_source_url(ticker: str) -> str:
    return f"{FMP_BASE}/grades?symbol={ticker}"


def _to_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")[:10]).date()
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if numeric == numeric else None  # drop NaN


def _fmt(value: float) -> str:
    return f"{value:.2f}"


def _fmt_int(value: float) -> str:
    return f"{value:,.0f}"


def resolve_tickers(args) -> list[str]:
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    elif args.universe:
        tickers = list(UNIVERSE.keys())
    else:
        tickers = []
    if args.limit_tickers:
        tickers = tickers[: args.limit_tickers]
    return tickers


def run(args) -> dict:
    settings = Settings()
    init_db(settings.database_url)
    if args.fmp_daily_cap:
        # Offline process only — raise the FMP ceiling so a large run isn't
        # throttled to the prod-safe 250/day default. Does NOT touch the live
        # scan process (separate interpreter).
        rate_limiter.register("fmp", args.fmp_daily_cap, 86400)

    classes = tuple(c.strip() for c in args.classes.split(",") if c.strip())
    tickers = resolve_tickers(args)
    if not tickers:
        raise SystemExit("No tickers: pass --tickers AAPL,MSFT or --universe")

    loader = StructuredEventLoader(settings)
    with get_session() as session:
        return loader.load(
            session,
            tickers,
            classes=classes,
            years=args.years,
            dry_run=args.dry_run,
            outcomes_per_run=args.outcomes_per_run,
            progress_every=args.progress_every,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--tickers", default="", help="Comma-separated tickers, e.g. AAPL,MSFT")
    group.add_argument("--universe", action="store_true", help="Load the full ~503-ticker universe")
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS, help="Lookback window in years")
    parser.add_argument("--classes", default=",".join(DEFAULT_CLASSES), help="earnings,upgrades")
    parser.add_argument("--limit-tickers", type=int, default=0, help="Cap tickers processed")
    parser.add_argument("--outcomes-per-run", type=int, default=DEFAULT_OUTCOMES_PER_RUN)
    parser.add_argument("--fmp-daily-cap", type=int, default=0,
                        help="Raise the in-process FMP/day cap for large offline runs (0=leave default)")
    parser.add_argument("--progress-every", type=int, default=25, help="Log progress every N tickers")
    parser.add_argument("--dry-run", action="store_true", help="Compute candidates but store nothing")
    args = parser.parse_args()
    print(json.dumps(run(args), indent=2))


if __name__ == "__main__":
    main()
