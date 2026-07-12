"""Shadow calibration ledger (Spec I1).

Records EVERY scored candidate with its signal breakdown, then fills forward
returns nightly once each horizon matures. This is the flywheel: ~30 labeled
datapoints/day (vs ~2 approvals) that turn threshold changes into empirical
decisions from decile data instead of guesses.

Design invariants:
- Ledger writes NEVER gate or alter pipeline behavior. `record_scored_candidate`
  swallows every error and logs `shadow_ledger_write_failed`.
- Horizon maturity is trading-day aware (utils/market_hours.is_trading_day).
- The nightly job commits per batch — no giant transactions (BRY-300 lesson).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from database.db import get_session
from database.models import ScoredCandidate
from data.event_outcomes import PriceHistoryCache, _first_bar_idx_on_or_after
from utils.market_hours import is_trading_day
from utils.timeutils import utcnow_naive
from utils.logger import get_logger

log = get_logger("shadow_ledger")

# Forward-return horizons in trading days.
HORIZONS = (1, 3, 5, 10, 20)

# Score buckets for the weekly calibration curve (lower-inclusive, upper-exclusive
# except the last which is open-ended).
SCORE_BUCKETS = (
    ("0.00-0.30", 0.0, 0.30),
    ("0.30-0.45", 0.30, 0.45),
    ("0.45-0.55", 0.45, 0.55),
    ("0.55-0.65", 0.55, 0.65),
    ("0.65+", 0.65, 1.01),
)

# Batch size for per-row commits in the nightly job (no giant transactions).
_RETURN_BATCH_SIZE = 25


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def trading_days_between(start: date, end: date) -> int:
    """Count trading days strictly after `start`, up to and including `end`."""
    if start is None or end is None or end <= start:
        return 0
    count = 0
    day = start + timedelta(days=1)
    while day <= end:
        if is_trading_day(day):
            count += 1
        day += timedelta(days=1)
    return count


def matured_horizons(scored_at: Any, today: date | None = None) -> list[int]:
    """Horizons (in HORIZONS) whose T+N trading day is on or before `today`."""
    scored_date = _coerce_date(scored_at)
    if scored_date is None:
        return []
    today = today or date.today()
    elapsed = trading_days_between(scored_date, today)
    return [n for n in HORIZONS if elapsed >= n]


def _signal_score(scoring_result: dict, name: str) -> float | None:
    entry = (scoring_result.get("signal_breakdown") or {}).get(name) or {}
    value = entry.get("score")
    return float(value) if value is not None else None


def classify_cohort(final_score: float, settings) -> str:
    """Score-band cohort for the ledger: memo | exploration | below."""
    memo_min = float(getattr(settings, "auto_approve_min_score", 0.55))
    expl_min = float(getattr(settings, "exploration_min_score", 0.45))
    if final_score >= memo_min:
        return "memo"
    if final_score >= expl_min:
        return "exploration"
    return "below"


def record_scored_candidate(
    settings,
    *,
    run_id: str,
    ticker: str,
    source: str,
    scoring_result: dict,
    regime: dict | None,
    memo_generated: bool,
    cohort: str | None = None,
    entry_price: float | None = None,
    suggested_stop: float | None = None,
    target_1: float | None = None,
    price_cache: PriceHistoryCache | None = None,
) -> int | None:
    """Write one ledger row for a scored candidate. Never raises.

    Returns the row id, or None on any failure (logged, non-fatal).
    """
    try:
        final_score = float(scoring_result.get("final_score", 0) or 0)
        if cohort is None:
            cohort = classify_cohort(final_score, settings)
        regime_str = ""
        if isinstance(regime, dict):
            regime_str = str(regime.get("regime", "") or "")
        elif regime:
            regime_str = str(regime)
        pattern_entry = (scoring_result.get("signal_breakdown") or {}).get("pattern") or {}
        direction = str(scoring_result.get("direction", "neutral") or "neutral")

        with get_session() as session:
            resolved_entry = entry_price
            if (resolved_entry is None or resolved_entry <= 0):
                resolved_entry = _resolve_entry_price(
                    ticker, price_cache or PriceHistoryCache(settings), session
                )
            row = ScoredCandidate(
                run_id=run_id or "",
                ticker=ticker.upper(),
                scored_at=utcnow_naive(),
                source=source or "",
                final_score=round(final_score, 4),
                catalyst_score=_signal_score(scoring_result, "catalyst"),
                fundamental_score=_signal_score(scoring_result, "fundamental"),
                pattern_score=_signal_score(scoring_result, "pattern"),
                pattern_status=str(pattern_entry.get("status") or ""),
                web_research_score=_signal_score(scoring_result, "web_research"),
                direction=direction,
                regime=regime_str,
                entry_price=resolved_entry,
                suggested_stop=suggested_stop,
                target_1=target_1,
                memo_generated=bool(memo_generated),
                cohort=cohort,
            )
            session.add(row)
            session.flush()
            return row.id
    except Exception as e:  # never let the ledger affect the pipeline
        log.warning("shadow_ledger_write_failed", ticker=ticker, error=str(e))
        return None


def _resolve_entry_price(ticker: str, price_cache: PriceHistoryCache, session) -> float | None:
    """Most recent close on/before today from the outcome-engine price cache."""
    try:
        today = date.today()
        bars = price_cache.get_bars(ticker, today - timedelta(days=10), today, session=session)
        for bar in reversed(bars):
            if bar.close and bar.close > 0:
                return float(bar.close)
    except Exception as e:
        log.warning("shadow_ledger_price_lookup_failed", ticker=ticker, error=str(e))
    return None


def mark_paper_traded(candidate_id: int, cohort: str) -> None:
    """Flag a ledger row as auto-executed in paper (I2). Never raises."""
    if not candidate_id:
        return
    try:
        with get_session() as session:
            row = session.query(ScoredCandidate).filter_by(id=candidate_id).first()
            if row:
                row.paper_traded = True
                row.cohort = cohort
    except Exception as e:
        log.warning("shadow_ledger_mark_paper_failed", candidate_id=candidate_id, error=str(e))


def compute_matured_returns(
    settings,
    today: date | None = None,
    max_per_run: int | None = None,
    price_cache: PriceHistoryCache | None = None,
) -> dict:
    """Nightly job: fill forward returns for rows whose horizons have matured.

    Trading-day aware, idempotent (only fills missing matured horizons), capped
    per run, and commits per batch. Returns a summary dict.
    """
    today = today or date.today()
    if max_per_run is None:
        max_per_run = int(getattr(settings, "shadow_returns_max_per_run", 300))
    price_cache = price_cache or PriceHistoryCache(settings)

    # Candidate rows: have an entry price and are not yet fully matured (t20 null).
    with get_session() as session:
        pending_ids = [
            row_id
            for (row_id,) in session.query(ScoredCandidate.id)
            .filter(
                ScoredCandidate.entry_price.isnot(None),
                ScoredCandidate.entry_price > 0,
                ScoredCandidate.ret_t20.is_(None),
            )
            .order_by(ScoredCandidate.scored_at.asc())
            .all()
        ]

    processed = 0
    rows_updated = 0
    horizons_filled = 0
    index = 0

    while index < len(pending_ids) and processed < max_per_run:
        batch = pending_ids[index:index + _RETURN_BATCH_SIZE]
        index += len(batch)
        p, r, h = _process_return_batch(batch, today, price_cache, max_per_run - processed)
        processed += p
        rows_updated += r
        horizons_filled += h

    summary = {
        "pending": len(pending_ids),
        "processed": processed,
        "rows_updated": rows_updated,
        "horizons_filled": horizons_filled,
        "max_per_run": max_per_run,
    }
    log.info("shadow_returns_run", **summary)
    return summary


def _process_return_batch(row_ids: list[int], today: date, price_cache: PriceHistoryCache, budget: int):
    """Compute matured returns for one batch, committed as a single transaction.

    `budget` caps how many rows with real work this batch may process (the global
    shadow_returns_max_per_run limit); rows with nothing to compute are free.
    """
    processed = 0
    rows_updated = 0
    horizons_filled = 0
    with get_session() as session:
        for row_id in row_ids:
            if processed >= budget:
                break
            row = session.query(ScoredCandidate).filter_by(id=row_id).first()
            if not row or not row.entry_price or row.entry_price <= 0:
                continue
            matured = matured_horizons(row.scored_at, today)
            missing = [n for n in matured if getattr(row, f"ret_t{n}") is None]
            if not missing:
                continue  # waiting for the next horizon to mature — no work yet
            processed += 1
            filled = _fill_row_returns(row, missing, price_cache, session)
            if filled:
                rows_updated += 1
                horizons_filled += filled
                row.returns_computed_at = utcnow_naive()
    return processed, rows_updated, horizons_filled


def _fill_row_returns(row: ScoredCandidate, missing: list[int], price_cache, session) -> int:
    """Fill the given horizons for one row from the price cache. Returns count filled."""
    scored_date = _coerce_date(row.scored_at)
    if scored_date is None:
        return 0
    try:
        bars = price_cache.get_bars(
            row.ticker,
            scored_date - timedelta(days=5),
            today_plus_buffer(scored_date, max(missing)),
            session=session,
        )
    except Exception as e:
        log.warning("shadow_returns_price_failed", ticker=row.ticker, error=str(e))
        return 0
    anchor_idx = _first_bar_idx_on_or_after(bars, scored_date)
    if anchor_idx is None:
        return 0
    entry = float(row.entry_price)
    filled = 0
    for n in missing:
        target_idx = anchor_idx + n
        if target_idx < len(bars) and bars[target_idx].close:
            setattr(row, f"ret_t{n}", round((bars[target_idx].close / entry - 1) * 100, 2))
            filled += 1
    return filled


def today_plus_buffer(scored_date: date, max_horizon: int) -> date:
    """Upper bound for the price window: enough calendar days to cover max horizon."""
    # ~1.5 calendar days per trading day + slack, capped at today's data availability.
    return scored_date + timedelta(days=int(max_horizon * 1.7) + 10)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return round(ordered[mid], 2)
    return round((ordered[mid - 1] + ordered[mid]) / 2, 2)


def calibration_report(session=None) -> dict:
    """Aggregate matured ledger rows into the decile curve for the weekly report.

    Uses ret_t10 as the reference horizon. Pure math, no LLM.
    Returns buckets (count / win_rate / median_t10) and a by-direction split.
    """
    if session is None:
        with get_session() as s:
            return _calibration_from_session(s)
    return _calibration_from_session(session)


def _calibration_from_session(session) -> dict:
    rows = (
        session.query(ScoredCandidate)
        .filter(ScoredCandidate.ret_t10.isnot(None))
        .all()
    )
    total = len(rows)

    def _summarize(subset) -> dict:
        rets = [r.ret_t10 for r in subset if r.ret_t10 is not None]
        count = len(rets)
        wins = sum(1 for x in rets if x > 0)
        return {
            "count": count,
            "win_rate": round(wins / count * 100, 1) if count else 0.0,
            "median_t10": _median(rets),
        }

    buckets = []
    for label, lo, hi in SCORE_BUCKETS:
        subset = [r for r in rows if lo <= (r.final_score or 0) < hi]
        summary = _summarize(subset)
        summary["label"] = label
        buckets.append(summary)

    by_direction = {}
    for direction in ("bullish", "bearish", "neutral"):
        subset = [r for r in rows if (r.direction or "neutral") == direction]
        if subset:
            by_direction[direction] = _summarize(subset)

    return {"total_matured": total, "buckets": buckets, "by_direction": by_direction}
