"""Rank stored HistoricalEvent rows as analog evidence for a current catalyst."""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from statistics import median
from typing import Any

import numpy as np

from database.models import EventContext, EventOutcome, HistoricalEvent

ANALOG_ACTIVE_STATUSES = {"complete", "partial", "insufficient_forward_returns"}
SOURCE_QUALITY = {
    "company_ir": 1.0,
    "sec_filing": 0.95,
    "earnings_transcript": 0.9,
    "press_release": 0.85,
    "regulator": 0.8,
    "news": 0.65,
    "analyst_report": 0.6,
    "recap": 0.35,
    "other": 0.3,
}


class AnalogRanker:
    def __init__(self, settings=None):
        self.settings = settings
        self.max_candidates = 120

    def rank(self, session, request: dict, peer_resolution: dict | None = None) -> dict:
        session.flush()
        ticker = (request.get("target_ticker") or request.get("ticker") or "").upper()
        setup_type = request.get("setup_type") or ""
        peers = peer_resolution.get("peers", []) if peer_resolution else []
        peer_scores = {
            (peer.get("ticker") or "").upper(): float(peer.get("score") or 0)
            for peer in peers
            if isinstance(peer, dict)
        }
        peer_set = set(peer_scores)
        candidates = self._candidate_events(session, ticker, setup_type, peer_set)
        if not candidates:
            return {
                "status": "no_matches",
                "setup_type": setup_type,
                "evidence_tiers": {"same_ticker": [], "close_peer": [], "sector_peer": [], "broad_base_rate": []},
                "summary_stats": {},
                "top_analogs": [],
                "warnings": ["No stored historical events for this catalyst type."],
            }

        current_context = request.get("current_context") or {}
        scored = []
        for event, outcome, context in candidates:
            score = self._analog_score(event, outcome, context, request, ticker, peer_scores, current_context)
            tier = self._tier(event.ticker, ticker, peer_scores)
            scored.append((score, tier, event, outcome, context))
        scored.sort(key=lambda item: item[0], reverse=True)
        top = scored[:30]

        evidence_tiers = {"same_ticker": [], "close_peer": [], "sector_peer": [], "broad_base_rate": []}
        for score, tier, event, outcome, context in top:
            evidence_tiers[tier].append(self._serialize_analog(score, event, outcome, tier))

        top_analogs = [self._serialize_analog(score, event, outcome, tier) for score, tier, event, outcome, _ in top[:10]]
        stats = self._summary_stats([item[3] for item in top])
        warnings = []
        if len(evidence_tiers["broad_base_rate"]) > len(evidence_tiers["same_ticker"]) + len(evidence_tiers["close_peer"]):
            warnings.append("Broad base-rate evidence dominates; direct/close-peer analog support is limited.")
        partial_count = sum(1 for _, _, _, outcome, _ in top if outcome.status != "complete")
        if partial_count and partial_count == len(top):
            warnings.append("Stored analogs exist, but forward-return horizons are still immature or partial.")

        return {
            "status": "active" if stats.get("matured_t10_count", 0) else "insufficient_forward_returns",
            "setup_type": setup_type,
            "evidence_tiers": evidence_tiers,
            "summary_stats": stats,
            "top_analogs": top_analogs,
            "warnings": warnings,
        }

    def _candidate_events(self, session, ticker: str, setup_type: str, peer_set: set[str]) -> list[tuple]:
        tickers = {ticker, *peer_set}
        direct = (
            session.query(HistoricalEvent, EventOutcome, EventContext)
            .join(EventOutcome, EventOutcome.event_id == HistoricalEvent.id)
            .outerjoin(EventContext, EventContext.event_id == HistoricalEvent.id)
            .filter(HistoricalEvent.event_type == setup_type)
            .filter(HistoricalEvent.ticker.in_(tickers))
            .order_by(HistoricalEvent.event_date.desc())
            .limit(self.max_candidates)
            .all()
        )
        if len(direct) >= self.max_candidates // 2:
            return direct
        broad = (
            session.query(HistoricalEvent, EventOutcome, EventContext)
            .join(EventOutcome, EventOutcome.event_id == HistoricalEvent.id)
            .outerjoin(EventContext, EventContext.event_id == HistoricalEvent.id)
            .filter(HistoricalEvent.event_type == setup_type)
            .filter(~HistoricalEvent.ticker.in_(tickers))
            .order_by(HistoricalEvent.event_date.desc())
            .limit(max(0, self.max_candidates - len(direct)))
            .all()
        )
        return [*direct, *broad]

    def _analog_score(
        self,
        event: HistoricalEvent,
        outcome: EventOutcome,
        context: EventContext | None,
        request: dict,
        target_ticker: str,
        peer_scores: dict[str, float],
        current_context: dict,
    ) -> float:
        parts = {
            "event_semantic_similarity": self._event_similarity(event, request),
            "peer_similarity": self._peer_similarity(event.ticker, target_ticker, peer_scores),
            "context_similarity": self._context_similarity(context, current_context),
            "magnitude_similarity": self._magnitude_similarity(event.magnitude, request.get("magnitude")),
            "source_quality_confidence": self._source_quality(event),
            "recency_score": self._recency_score(event.event_date),
            "outcome_completeness": self._outcome_completeness(outcome),
        }
        weights = {
            "event_semantic_similarity": 0.35,
            "peer_similarity": 0.20,
            "context_similarity": 0.15,
            "magnitude_similarity": 0.10,
            "source_quality_confidence": 0.10,
            "recency_score": 0.05,
            "outcome_completeness": 0.05,
        }
        return round(_weighted_renormalized(parts, weights), 4)

    def _event_similarity(self, event: HistoricalEvent, request: dict) -> float | None:
        query_embedding = request.get("embedding")
        event_embedding = _json_list(event.embedding_json)
        if query_embedding and event_embedding:
            return _cosine(query_embedding, event_embedding)

        summary = " ".join(
            str(request.get(key, "") or "")
            for key in ("catalyst_summary", "setup_type", "event_subtype")
        )
        event_text = " ".join([event.event_type or "", event.event_subtype or "", event.headline or "", event.summary or ""])
        token_overlap = _token_overlap(summary, event_text)
        subtype_match = 1.0 if request.get("event_subtype") and request.get("event_subtype") == event.event_subtype else 0.5
        type_match = 1.0 if request.get("setup_type") == event.event_type else 0.0
        return round(0.5 * type_match + 0.35 * token_overlap + 0.15 * subtype_match, 4)

    def _peer_similarity(self, ticker: str, target_ticker: str, peer_scores: dict[str, float]) -> float:
        if ticker == target_ticker:
            return 1.0
        if ticker in peer_scores:
            return max(0.0, min(1.0, peer_scores[ticker]))
        return 0.25

    def _context_similarity(self, context: EventContext | None, current_context: dict) -> float | None:
        if not context:
            return None
        parts = {
            "macro": 1.0 if current_context.get("macro_regime") and current_context.get("macro_regime") == context.macro_regime else None,
            "vix": _proximity(current_context.get("vix_level"), context.vix_level, 25),
            "momentum": _proximity(current_context.get("ticker_momentum_20d"), context.ticker_momentum_20d, 30),
            "volatility": _proximity(current_context.get("ticker_volatility_20d"), context.ticker_volatility_20d, 80),
            "market_cap": _ratio_similarity(current_context.get("market_cap"), context.market_cap),
            "trailing_pe": _ratio_similarity(current_context.get("trailing_pe_ratio"), context.trailing_pe_ratio),
        }
        vals = [v for v in parts.values() if v is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    def _magnitude_similarity(self, left: Any, right: Any) -> float | None:
        try:
            if left is None or right is None:
                return None
            left_f = float(left)
            right_f = float(right)
            denom = max(abs(left_f), abs(right_f), 1)
            return max(0.0, 1 - abs(left_f - right_f) / denom)
        except (TypeError, ValueError):
            return None

    def _source_quality(self, event: HistoricalEvent) -> float:
        return round((SOURCE_QUALITY.get(event.source_type or "other", 0.3) + (event.confidence or 0)) / 2, 4)

    def _recency_score(self, event_date: date) -> float:
        days = max(0, (date.today() - event_date).days)
        return round(max(0.2, math.exp(-days / (365 * 8))), 4)

    def _outcome_completeness(self, outcome: EventOutcome) -> float:
        try:
            matured = json.loads(outcome.matured_horizons_json or "[]")
        except json.JSONDecodeError:
            matured = []
        return min(1.0, len(matured) / 6)

    def _tier(self, ticker: str, target_ticker: str, peer_scores: dict[str, float]) -> str:
        if ticker == target_ticker:
            return "same_ticker"
        if peer_scores.get(ticker, 0) >= 0.65:
            return "close_peer"
        if peer_scores.get(ticker, 0) >= 0.45:
            return "sector_peer"
        return "broad_base_rate"

    def _serialize_analog(self, score: float, event: HistoricalEvent, outcome: EventOutcome, tier: str) -> dict:
        return {
            "ticker": event.ticker,
            "date": event.event_date.isoformat() if event.event_date else "",
            "event_type": event.event_type,
            "headline": event.headline,
            "summary": event.summary,
            "source_domain": event.source_domain,
            "source_url": event.source_url,
            "source_type": event.source_type,
            "similarity_score": round(score, 3),
            "evidence_tier": tier,
            "return_t10": outcome.return_t10,
            "return_t20": outcome.return_t20,
            "outcome_status": outcome.status,
        }

    def _summary_stats(self, outcomes: list[EventOutcome]) -> dict:
        vals_t10 = [o.return_t10 for o in outcomes if o.return_t10 is not None]
        vals_t20 = [o.return_t20 for o in outcomes if o.return_t20 is not None]
        return {
            "total_instances": len(outcomes),
            "matured_t10_count": len(vals_t10),
            "matured_t20_count": len(vals_t20),
            "win_rate_t10": round(sum(1 for v in vals_t10 if v > 0) / len(vals_t10), 3) if vals_t10 else 0.5,
            "median_return_t10": round(float(median(vals_t10)), 2) if vals_t10 else 0.0,
            "median_return_t20": round(float(median(vals_t20)), 2) if vals_t20 else 0.0,
            "avg_winner_t10": round(float(np.mean([v for v in vals_t10 if v > 0])), 2) if any(v > 0 for v in vals_t10) else 0.0,
            "avg_loser_t10": round(float(np.mean([v for v in vals_t10 if v <= 0])), 2) if any(v <= 0 for v in vals_t10) else 0.0,
        }


def _weighted_renormalized(parts: dict[str, float | None], weights: dict[str, float]) -> float:
    total = 0.0
    weight = 0.0
    for key, value in parts.items():
        if value is None:
            continue
        total += value * weights[key]
        weight += weights[key]
    return total / weight if weight else 0.5


def _json_list(value: str | None) -> list[float] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [float(v) for v in parsed]
    except Exception:
        return None
    return None


def _cosine(left: list[float], right: list[float]) -> float | None:
    n = min(len(left), len(right))
    if not n:
        return None
    a = np.array(left[:n])
    b = np.array(right[:n])
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return None
    return round((float(np.dot(a, b)) / denom + 1) / 2, 4)


def _token_overlap(left: str, right: str) -> float:
    a = {t for t in re.split(r"\W+", (left or "").lower()) if len(t) > 2}
    b = {t for t in re.split(r"\W+", (right or "").lower()) if len(t) > 2}
    if not a or not b:
        return 0.5
    return len(a & b) / len(a | b)


def _proximity(left: Any, right: Any, scale: float) -> float | None:
    try:
        if left is None or right is None:
            return None
        return max(0.0, 1 - abs(float(left) - float(right)) / scale)
    except (TypeError, ValueError):
        return None


def _ratio_similarity(left: Any, right: Any) -> float | None:
    try:
        if left is None or right is None or float(left) <= 0 or float(right) <= 0:
            return None
        return min(float(left), float(right)) / max(float(left), float(right))
    except (TypeError, ValueError):
        return None
