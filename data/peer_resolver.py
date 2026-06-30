"""Cached peer resolution for historical analog analysis."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable

import httpx

from database.models import CompanyProfile, PeerEdge
from utils.logger import get_logger
from utils.rate_limiter import rate_limiter
from utils.redaction import redact_payload, redact_text

log = get_logger("peer_resolver")

FMP_BASE = "https://financialmodelingprep.com/stable"
TICKER_RE = re.compile(r"\b[A-Z]{1,5}\b")
COMMON_FALSE_TICKERS = {
    "CEO", "CFO", "ETF", "FDA", "IPO", "IR", "LLC", "NYSE", "NASDAQ", "SEC", "USA", "US",
}


@dataclass
class PeerCandidate:
    ticker: str
    score: float
    source: str
    reasons: list[str]
    rank: int = 0

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "score": round(float(self.score), 3),
            "rank": self.rank,
            "source": self.source,
            "reasons": self.reasons,
        }


class PeerResolver:
    """Resolve peers from manual overrides, DB cache, FMP, correlation, and search."""

    def __init__(
        self,
        settings=None,
        manual_peers: dict[str, list[str]] | None = None,
        session_factory: Callable | None = None,
        fmp_client: Any | None = None,
        price_client: Any | None = None,
        perplexity_client: Any | None = None,
        gemini_client: Any | None = None,
    ):
        self.settings = settings
        self.manual_peers = {k.upper(): [p.upper() for p in v] for k, v in (manual_peers or {}).items()}
        self.session_factory = session_factory
        self.fmp_client = fmp_client
        self.price_client = price_client
        self.perplexity_client = perplexity_client
        self.gemini_client = gemini_client
        self.fmp_key = getattr(settings, "fmp_api_key", "") if settings else ""
        self.max_peer_count = int(getattr(settings, "pattern_max_peer_count", 20) if settings else 20)
        self.cache_ttl_days = int(getattr(settings, "pattern_peer_cache_ttl_days", 30) if settings else 30)

    def resolve(self, ticker: str, session=None, allow_network: bool = True) -> dict:
        ticker = (ticker or "").upper().strip()
        started = datetime.utcnow()
        if not ticker:
            return self._result("", [], "low_confidence_peers", 0.0, started, ["missing ticker"])

        manual = self.manual_peers.get(ticker, [])
        if manual:
            peers = [
                PeerCandidate(peer, 1.0 - idx * 0.02, "manual", ["manual peer override"], idx + 1)
                for idx, peer in enumerate(manual[: self.max_peer_count])
            ]
            self._persist_edges(ticker, peers, session)
            return self._result(ticker, peers, "active", 0.95, started, [])

        cached = self._cached_edges(ticker, session)
        if cached:
            confidence = min(0.9, max(c.score for c in cached))
            return self._result(ticker, cached[: self.max_peer_count], "cache_hit", confidence, started, [])

        candidates: list[PeerCandidate] = []
        profile = None

        if allow_network:
            fmp_peers = self._fmp_stock_peers(ticker)
            candidates.extend(fmp_peers)
            profile = self._get_profile(ticker)
            if profile:
                self._persist_profile(ticker, profile, session)
                candidates.extend(self._fmp_screener_peers(ticker, profile))

            candidates = self._merge_candidates(candidates)
            if len(candidates) < max(3, self.max_peer_count // 2):
                candidates.extend(self._correlation_candidates(ticker, candidates, profile))
                candidates = self._merge_candidates(candidates)

            confidence = self._confidence(candidates)
            if len(candidates) < max(3, self.max_peer_count // 2) or confidence < 0.55:
                candidates.extend(self._search_peer_candidates(ticker, profile, provider="perplexity"))
                candidates = self._merge_candidates(candidates)
                confidence = self._confidence(candidates)
            if len(candidates) < max(3, self.max_peer_count // 2) or confidence < 0.55:
                candidates.extend(self._search_peer_candidates(ticker, profile, provider="gemini"))

        ranked = self._merge_candidates(candidates)[: self.max_peer_count]
        self._persist_edges(ticker, ranked, session)
        confidence = self._confidence(ranked)
        status = "active" if ranked and confidence >= 0.45 else "low_confidence_peers"
        warnings = [] if ranked else ["no peer candidates found from cache or structured fallbacks"]
        return self._result(ticker, ranked, status, confidence, started, warnings)

    def _result(
        self,
        ticker: str,
        peers: list[PeerCandidate],
        status: str,
        confidence: float,
        started: datetime,
        warnings: list[str],
    ) -> dict:
        return {
            "ticker": ticker,
            "peers": [p.as_dict() for p in peers],
            "status": status,
            "confidence": round(float(confidence), 3),
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "duration_s": round((datetime.utcnow() - started).total_seconds(), 3),
            "warnings": warnings,
        }

    def _cached_edges(self, ticker: str, session=None) -> list[PeerCandidate]:
        if session is None and not self.session_factory:
            return []
        now = datetime.utcnow()
        try:
            if session is not None:
                rows = (
                    session.query(PeerEdge)
                    .filter(PeerEdge.target_ticker == ticker)
                    .filter((PeerEdge.expires_at.is_(None)) | (PeerEdge.expires_at > now))
                    .order_by(PeerEdge.rank.asc(), PeerEdge.score.desc())
                    .all()
                )
                return [self._edge_to_candidate(row) for row in rows]
            with self.session_factory() as sess:
                return self._cached_edges(ticker, sess)
        except Exception as exc:
            log.warning("peer_cache_read_failed", ticker=ticker, error=str(exc))
            return []

    def _edge_to_candidate(self, row: PeerEdge) -> PeerCandidate:
        try:
            reasons = json.loads(row.reasons_json or "[]")
        except (TypeError, json.JSONDecodeError):
            reasons = []
        return PeerCandidate(
            ticker=row.peer_ticker,
            score=row.score or 0,
            source=row.source or "cache",
            reasons=reasons,
            rank=row.rank or 0,
        )

    def _persist_edges(self, ticker: str, peers: list[PeerCandidate], session=None) -> None:
        if not peers or (session is None and not self.session_factory):
            return
        expires = datetime.utcnow() + timedelta(days=self.cache_ttl_days)
        as_of = date.today()
        try:
            if session is not None:
                for idx, peer in enumerate(peers, start=1):
                    existing = (
                        session.query(PeerEdge)
                        .filter_by(
                            target_ticker=ticker,
                            peer_ticker=peer.ticker,
                            source=peer.source,
                            as_of_date=as_of,
                        )
                        .first()
                    )
                    if existing:
                        existing.rank = idx
                        existing.score = peer.score
                        existing.reasons_json = json.dumps(peer.reasons)
                        existing.expires_at = expires
                    else:
                        session.add(
                            PeerEdge(
                                target_ticker=ticker,
                                peer_ticker=peer.ticker,
                                rank=idx,
                                score=peer.score,
                                source=peer.source,
                                reasons_json=json.dumps(peer.reasons),
                                as_of_date=as_of,
                                expires_at=expires,
                            )
                        )
                return
            with self.session_factory() as sess:
                self._persist_edges(ticker, peers, sess)
        except Exception as exc:
            log.warning("peer_cache_write_failed", ticker=ticker, error=str(exc))

    def _persist_profile(self, ticker: str, profile: dict, session=None) -> None:
        if session is None and not self.session_factory:
            return
        expires = datetime.utcnow() + timedelta(days=self.cache_ttl_days)
        try:
            if session is not None:
                existing = session.query(CompanyProfile).filter_by(ticker=ticker).first()
                values = {
                    "name": profile.get("companyName") or profile.get("company_name") or profile.get("name") or "",
                    "exchange": profile.get("exchangeShortName") or profile.get("exchange") or "",
                    "sector": profile.get("sector") or "",
                    "industry": profile.get("industry") or "",
                    "market_cap": _to_float(profile.get("mktCap") or profile.get("marketCap")),
                    "beta": _to_float(profile.get("beta")),
                    "description": profile.get("description") or "",
                    "country": profile.get("country") or "",
                    "currency": profile.get("currency") or "",
                    "raw_json": json.dumps(redact_payload(profile)),
                    "profile_source": "fmp_profile",
                    "expires_at": expires,
                }
                if existing:
                    for key, val in values.items():
                        setattr(existing, key, val)
                else:
                    session.add(CompanyProfile(ticker=ticker, **values))
                return
            with self.session_factory() as sess:
                self._persist_profile(ticker, profile, sess)
        except Exception as exc:
            log.warning("company_profile_cache_failed", ticker=ticker, error=str(exc))

    def _fmp_stock_peers(self, ticker: str) -> list[PeerCandidate]:
        data = None
        if self.fmp_client and hasattr(self.fmp_client, "get_stock_peers"):
            data = self.fmp_client.get_stock_peers(ticker)
        else:
            data = self._fmp_request("/stock-peers", {"symbol": ticker})
        peers = []
        for idx, peer in enumerate(_extract_peer_symbols(data), start=1):
            if peer == ticker:
                continue
            peers.append(
                PeerCandidate(
                    ticker=peer,
                    score=max(0.55, 0.9 - idx * 0.03),
                    source="fmp_stock_peers",
                    reasons=["FMP stock-peers endpoint"],
                    rank=idx,
                )
            )
        return peers

    def _get_profile(self, ticker: str) -> dict | None:
        if self.fmp_client and hasattr(self.fmp_client, "get_profile"):
            return _first(self.fmp_client.get_profile(ticker))
        return _first(self._fmp_request("/profile", {"symbol": ticker}))

    def _fmp_screener_peers(self, ticker: str, profile: dict | None) -> list[PeerCandidate]:
        if not profile:
            return []
        sector = profile.get("sector") or ""
        industry = profile.get("industry") or ""
        exchange = profile.get("exchangeShortName") or profile.get("exchange") or ""
        market_cap = _to_float(profile.get("mktCap") or profile.get("marketCap"))
        params = {"limit": 100, "isActivelyTrading": "true"}
        if sector:
            params["sector"] = sector
        if exchange:
            params["exchange"] = exchange

        if self.fmp_client and hasattr(self.fmp_client, "screen"):
            rows = self.fmp_client.screen(params)
        else:
            rows = self._fmp_request("/stock-screener", params) or []

        peers = []
        for row in rows if isinstance(rows, list) else []:
            symbol = (row.get("symbol") or row.get("ticker") or "").upper()
            if not symbol or symbol == ticker:
                continue
            peer_cap = _to_float(row.get("marketCap") or row.get("mktCap"))
            same_industry = _text_score(industry, row.get("industry") or "")
            cap_score = _market_cap_score(market_cap, peer_cap)
            sector_score = 1.0 if sector and sector == row.get("sector") else 0.4
            exchange_score = 1.0 if exchange and exchange == row.get("exchangeShortName") else 0.8
            score = (
                0.25 * same_industry
                + 0.20 * cap_score
                + 0.15 * 0.75
                + 0.15 * 0.5
                + 0.10 * 0.6
                + 0.10 * sector_score
                + 0.05 * exchange_score
            )
            if score < 0.35:
                continue
            reasons = []
            if same_industry >= 0.7:
                reasons.append(f"same/related industry: {row.get('industry') or industry}")
            if cap_score >= 0.7 and market_cap and peer_cap:
                reasons.append(f"market cap {peer_cap / market_cap:.1f}x")
            if sector:
                reasons.append(f"same sector: {sector}")
            peers.append(PeerCandidate(symbol, score, "fmp_screener", reasons or ["FMP screener similarity"]))
        return peers

    def _correlation_candidates(
        self,
        ticker: str,
        candidates: list[PeerCandidate],
        profile: dict | None,
    ) -> list[PeerCandidate]:
        if not self.price_client or not hasattr(self.price_client, "returns"):
            return []
        peers = []
        try:
            base_returns = self.price_client.returns(ticker, days=180)
            if not base_returns:
                return []
            for candidate in candidates[: self.max_peer_count * 2]:
                peer_returns = self.price_client.returns(candidate.ticker, days=180)
                corr = _correlation(base_returns, peer_returns)
                if corr is None or corr < 0.35:
                    continue
                peers.append(
                    PeerCandidate(
                        candidate.ticker,
                        min(0.9, candidate.score + corr * 0.15),
                        f"{candidate.source}+correlation",
                        [*candidate.reasons, f"180d return correlation {corr:.2f}"],
                    )
                )
        except Exception as exc:
            log.warning("peer_correlation_failed", ticker=ticker, error=str(exc))
        return peers

    def _search_peer_candidates(
        self,
        ticker: str,
        profile: dict | None,
        provider: str,
    ) -> list[PeerCandidate]:
        name = (profile or {}).get("companyName") or (profile or {}).get("company_name") or ticker
        queries = [
            f"{ticker} public company closest competitors peers sector market cap",
            f"{name} competitors publicly traded peers",
        ]
        rows: list[dict] = []
        try:
            if provider == "perplexity" and self.perplexity_client:
                for query in queries:
                    result = self.perplexity_client.search(query, max_results=5)
                    rows.extend(result.get("results", []) if isinstance(result, dict) else result or [])
            elif provider == "gemini" and self.gemini_client:
                prompt = (
                    f"Find publicly traded US-listed competitors/peers for {ticker} ({name}). "
                    "Return JSON only: {\"peers\":[{\"ticker\":\"...\",\"reason\":\"...\"}]}"
                )
                result = self.gemini_client.search_and_analyze_json(
                    "You identify public-company peer groups from source-backed search.",
                    prompt,
                    max_searches=3,
                    max_tokens=1024,
                )
                rows.extend(result.get("peers", []))
        except Exception as exc:
            log.warning("peer_search_failed", ticker=ticker, provider=provider, error=str(exc))
            return []

        candidates = []
        for row in rows:
            text = " ".join(
                str(row.get(key, ""))
                for key in ("ticker", "title", "snippet", "reason", "summary")
                if isinstance(row, dict)
            )
            for symbol in TICKER_RE.findall(text.upper()):
                if symbol == ticker or symbol in COMMON_FALSE_TICKERS:
                    continue
                candidates.append(
                    PeerCandidate(
                        symbol,
                        0.45 if provider == "perplexity" else 0.42,
                        f"{provider}_search",
                        [f"{provider} peer-search mention"],
                    )
                )
        return candidates

    def _merge_candidates(self, candidates: list[PeerCandidate]) -> list[PeerCandidate]:
        by_ticker: dict[str, PeerCandidate] = {}
        for candidate in candidates:
            symbol = (candidate.ticker or "").upper()
            if not symbol or symbol in COMMON_FALSE_TICKERS:
                continue
            candidate.ticker = symbol
            existing = by_ticker.get(symbol)
            if not existing:
                by_ticker[symbol] = candidate
                continue
            existing.score = max(existing.score, candidate.score)
            source_parts = set(existing.source.split("+")) | set(candidate.source.split("+"))
            existing.source = "+".join(sorted(source_parts))
            for reason in candidate.reasons:
                if reason not in existing.reasons:
                    existing.reasons.append(reason)
        ranked = sorted(by_ticker.values(), key=lambda p: p.score, reverse=True)
        for idx, candidate in enumerate(ranked, start=1):
            candidate.rank = idx
        return ranked

    def _confidence(self, candidates: list[PeerCandidate]) -> float:
        if not candidates:
            return 0.0
        top = candidates[: min(5, len(candidates))]
        score = sum(c.score for c in top) / len(top)
        coverage = min(1.0, len(candidates) / max(5, self.max_peer_count / 2))
        return max(0.0, min(1.0, score * 0.75 + coverage * 0.25))

    def _fmp_request(self, endpoint: str, params: dict | None = None) -> list | dict | None:
        if not self.fmp_key:
            return None
        rate_limiter.acquire("fmp")
        try:
            query = {"apikey": self.fmp_key}
            if params:
                query.update(params)
            response = httpx.get(f"{FMP_BASE}{endpoint}", params=query, timeout=20)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            log.warning("peer_fmp_request_failed", endpoint=endpoint, error=redact_text(str(exc)))
            return None


def _first(value: Any) -> dict | None:
    if isinstance(value, list):
        return value[0] if value and isinstance(value[0], dict) else None
    return value if isinstance(value, dict) else None


def _extract_peer_symbols(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, dict):
        for key in ("peersList", "peerList", "peers", "symbols"):
            if key in value:
                return _extract_peer_symbols(value[key])
        return []
    if isinstance(value, list):
        symbols = []
        for item in value:
            if isinstance(item, str):
                symbols.extend(part.strip().upper() for part in item.split(",") if part.strip())
            elif isinstance(item, dict):
                symbols.extend(_extract_peer_symbols(item))
        return [s for s in symbols if s and s not in COMMON_FALSE_TICKERS]
    return []


def _to_float(value: Any) -> float | None:
    try:
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    except (TypeError, ValueError):
        return None


def _text_score(left: str, right: str) -> float:
    left_tokens = {t for t in re.split(r"\W+", (left or "").lower()) if len(t) > 2}
    right_tokens = {t for t in re.split(r"\W+", (right or "").lower()) if len(t) > 2}
    if not left_tokens or not right_tokens:
        return 0.4
    overlap = len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))
    return max(0.3, min(1.0, 0.4 + overlap * 1.4))


def _market_cap_score(base: float | None, peer: float | None) -> float:
    if not base or not peer or base <= 0 or peer <= 0:
        return 0.5
    ratio = peer / base
    if 0.5 <= ratio <= 2.0:
        return 1.0
    if 0.25 <= ratio <= 4.0:
        return 0.7
    return max(0.1, 1 - abs(math.log(ratio)) / math.log(20))


def _correlation(left: list[float], right: list[float]) -> float | None:
    try:
        import numpy as np

        n = min(len(left), len(right))
        if n < 30:
            return None
        return float(np.corrcoef(np.array(left[-n:]), np.array(right[-n:]))[0, 1])
    except Exception:
        return None
