"""Event-anchored replay + parameter sweep + shadow-ledger adapter (Spec J2–J4).

Anchors the exit-engine simulator (``backtest.simulator``) to the point-in-time
pattern library (``HistoricalEvent``): each event becomes a historical entry
signal (direction = polarity), entered at T+1 open, exited by the bot's real
ATR-based stop/target/time logic. No LLM calls, no live-path touch — reads
whatever DB ``DATABASE_URL`` points at and the shared price cache.

Trade parameters come from the memo generator's own code path
(``MemoGenerator._compute_trade_params``) so the replay uses the *exact*
stop/target math the bot ships — see ``_trade_params``. The parameter sweep
(J3) rescales those same levels; ``_sweep_levels`` at the baseline combo
(1.0, 1.0) reproduces the memo levels exactly (locked by a test).
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import inspect as sa_inspect

from backtest.simulator import Bar, TradeResult, simulate_trade
from data.event_outcomes import PriceHistoryCache
from database.models import HistoricalEvent
from memo.generator import MemoGenerator
from utils.logger import get_logger

log = get_logger("event_replay")

# Friendly --classes aliases → concrete HistoricalEvent.event_type values.
# Unknown names pass through as literal event_type filters.
CLASS_ALIASES = {
    "earnings": ["earnings_beat_structured", "earnings_miss_structured"],
    "earnings_beat": ["earnings_beat_structured"],
    "earnings_miss": ["earnings_miss_structured"],
    "upgrades": ["analyst_upgrade_cluster"],
    "downgrades": ["analyst_downgrade_cluster"],
    "grades": ["analyst_upgrade_cluster", "analyst_downgrade_cluster"],
    "analyst": ["analyst_upgrade_cluster", "analyst_downgrade_cluster"],
}

# The 2026-07-04 audit flagged >75% win rates as a lookahead/selection smell.
WIN_RATE_BIAS_THRESHOLD = 0.75
# Below this per-class sample count, sweep results are not trustworthy.
SMALL_SAMPLE_N = 30

# Parameter sweep grid (J3). Target scale 1.0 = as-is; 0.75/1.25 = ±25%.
SWEEP_STOP_MULTS = (0.75, 1.0, 1.25)
SWEEP_TARGET_SCALES = (0.75, 1.0, 1.25)
SWEEP_MAX_HOLD_DAYS = (10, 15, 20)


@dataclass
class ReplayRecord:
    ticker: str
    event_type: str
    source_type: str
    magnitude_bucket: str
    direction: str
    result: TradeResult


@dataclass
class ReplayContext:
    """Everything the sweep needs to recompute levels over already-fetched bars."""

    event_type: str
    direction: str
    bars: list[Bar]
    signal_idx: int
    entry_price: float
    base_stop: float
    base_t1: float
    base_t2: float


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested directly)
# --------------------------------------------------------------------------- #

def resolve_classes(classes: list[str] | None) -> list[str] | None:
    """Expand friendly class names to event_type values; None → all classes."""
    if not classes:
        return None
    out: list[str] = []
    for raw in classes:
        name = raw.strip().lower()
        out.extend(CLASS_ALIASES.get(name, [raw.strip()]))
    return sorted(set(out))


def compute_atr(bars: list[Bar], signal_idx: int, period: int = 14) -> float:
    """ATR as of the signal bar — mirrors ``MarketDataAdapter.get_atr`` (mean of
    the last ``period`` true ranges), computed point-in-time from these bars."""
    window = bars[: signal_idx + 1]
    if len(window) < period + 1:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(window)):
        high, low, prev_close = window[i].high, window[i].low, window[i - 1].close
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(trs) < period:
        return 0.0
    return round(sum(trs[-period:]) / period, 2)


def direction_for(polarity: str | None) -> str | None:
    """bullish → long, bearish → short, neutral/unknown → None (skip)."""
    p = (polarity or "").lower()
    if p == "bullish":
        return "long"
    if p == "bearish":
        return "short"
    return None


def magnitude_bucket(magnitude: float | None) -> str:
    """Coarse |magnitude| buckets. Units differ by event type (% surprise vs
    cluster count) so these are directional only, not cross-class comparable."""
    if magnitude is None:
        return "unknown"
    m = abs(magnitude)
    if m < 1:
        return "0-1"
    if m < 3:
        return "1-3"
    if m < 5:
        return "3-5"
    if m < 10:
        return "5-10"
    return "10+"


def signal_index(bars: list[Bar], event_date: date) -> int | None:
    """Index of the last bar with date <= event_date (the signal bar)."""
    idx = None
    for i, bar in enumerate(bars):
        if bar.date <= event_date:
            idx = i
        else:
            break
    return idx


def _sweep_levels(
    entry: float, base_stop: float, base_t1: float, base_t2: float,
    is_long: bool, stop_mult: float, target_scale: float,
) -> tuple[float, float, float]:
    """Rescale the memo's stop/target distances. (1.0, 1.0) reproduces them."""
    stop_d = abs(entry - base_stop) * stop_mult
    t1_d = abs(base_t1 - entry) * target_scale
    t2_d = abs(base_t2 - entry) * target_scale
    if is_long:
        return round(entry - stop_d, 2), round(entry + t1_d, 2), round(entry + t2_d, 2)
    return round(entry + stop_d, 2), round(entry - t1_d, 2), round(entry - t2_d, 2)


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)).date()


def _price_bars_to_bars(price_bars) -> list[Bar]:
    bars = []
    for pb in price_bars:
        if None in (pb.open, pb.high, pb.low, pb.close):
            continue
        bars.append(Bar(date=pb.date, open=pb.open, high=pb.high, low=pb.low, close=pb.close))
    return bars


# --------------------------------------------------------------------------- #
# Stats & aggregation
# --------------------------------------------------------------------------- #

def _stats(results: list[TradeResult]) -> dict:
    pnls = [r.pnl_pct for r in results]
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    win_rate = wins / n if n else 0.0
    return {
        "n": n,
        "win_rate": round(win_rate, 4),
        "median_pnl_pct": round(statistics.median(pnls), 4) if pnls else 0.0,
        "avg_pnl_pct": round(statistics.fmean(pnls), 4) if pnls else 0.0,
        "expectancy_pct": round(statistics.fmean(pnls), 4) if pnls else 0.0,
        "profit_factor": (round(gross_win / gross_loss, 4) if gross_loss else float("inf")),
        "avg_holding_days": round(statistics.fmean([r.holding_days for r in results]), 2) if results else 0.0,
        "rule_dist": dict(Counter(r.rule_fired for r in results)),
        "win_rate_bias_flag": win_rate > WIN_RATE_BIAS_THRESHOLD,
    }


def _group_stats(records: list[ReplayRecord], keyfn) -> dict:
    groups: dict[str, list[TradeResult]] = defaultdict(list)
    for rec in records:
        groups[keyfn(rec)].append(rec.result)
    return {key: _stats(results) for key, results in sorted(groups.items())}


def aggregate(records: list[ReplayRecord]) -> dict:
    """Per event_type / magnitude bucket / source_type stats + overall."""
    return {
        "overall": _stats([r.result for r in records]) if records else {},
        "by_event_type": _group_stats(records, lambda r: r.event_type),
        "by_magnitude": _group_stats(records, lambda r: r.magnitude_bucket),
        "by_source_type": _group_stats(records, lambda r: r.source_type),
    }


# --------------------------------------------------------------------------- #
# J2 — event-anchored replay
# --------------------------------------------------------------------------- #

def _trade_params(memo_gen: MemoGenerator, price: float, atr: float, direction: str) -> dict:
    """Reuse the memo generator's stop/target math verbatim (Spec J requirement:
    import, don't reimplement). Position-sizing inputs are neutral — only the
    price levels matter to the exit simulator."""
    return memo_gen._compute_trade_params(price, atr, {}, 0.0, "moderate", direction=direction)


def _fetch_bars(event: HistoricalEvent, price_cache: PriceHistoryCache, session, max_hold: int) -> list[Bar]:
    ed = _as_date(event.event_date)
    start = ed - timedelta(days=90)  # ~60 trading bars of ATR warmup
    end = ed + timedelta(days=max_hold * 2 + 21)  # forward window for the hold
    return _price_bars_to_bars(price_cache.get_bars(event.ticker, start, end, session=session))


def replay_events(
    session,
    settings,
    classes: list[str] | None = None,
    years: int | None = None,
    price_cache: PriceHistoryCache | None = None,
    memo_gen: MemoGenerator | None = None,
    today: date | None = None,
) -> tuple[list[ReplayRecord], list[ReplayContext], dict]:
    """Replay every qualifying HistoricalEvent. Returns (records, sweep-contexts, skip-counts)."""
    event_types = resolve_classes(classes)
    price_cache = price_cache or PriceHistoryCache(settings)
    memo_gen = memo_gen or MemoGenerator(settings)
    slippage = getattr(settings, "backtest_slippage_bps", 10.0)
    max_hold = getattr(settings, "max_holding_days", 20)

    query = session.query(HistoricalEvent)
    if event_types:
        query = query.filter(HistoricalEvent.event_type.in_(event_types))
    if years:
        cutoff = (today or date.today()) - timedelta(days=365 * years)
        query = query.filter(HistoricalEvent.event_date >= cutoff)
    events = query.order_by(HistoricalEvent.event_date).all()

    records: list[ReplayRecord] = []
    contexts: list[ReplayContext] = []
    skipped = Counter()

    for event in events:
        direction = direction_for(event.polarity)
        if direction is None:
            skipped["neutral_polarity"] += 1
            continue
        bars = _fetch_bars(event, price_cache, session, max_hold)
        if len(bars) < 2:
            skipped["no_bars"] += 1
            continue
        sig_idx = signal_index(bars, _as_date(event.event_date))
        if sig_idx is None or sig_idx + 1 >= len(bars):
            skipped["no_forward_bar"] += 1
            continue
        params = _trade_params(memo_gen, bars[sig_idx].close, compute_atr(bars, sig_idx), direction)
        result = simulate_trade(
            bars, sig_idx, direction,
            params["stop_loss"], params["target_1"], params["target_2"],
            params["max_hold_days"], slippage_bps=slippage,
        )
        if result is None:
            skipped["no_forward_bar"] += 1
            continue
        records.append(ReplayRecord(
            ticker=event.ticker,
            event_type=event.event_type,
            source_type=event.source_type or "unknown",
            magnitude_bucket=magnitude_bucket(event.magnitude),
            direction=direction,
            result=result,
        ))
        contexts.append(ReplayContext(
            event_type=event.event_type, direction=direction, bars=bars, signal_idx=sig_idx,
            entry_price=params["entry_price"], base_stop=params["stop_loss"],
            base_t1=params["target_1"], base_t2=params["target_2"],
        ))

    log.info("event_replay_done", events=len(events), replayed=len(records), skipped=dict(skipped))
    return records, contexts, dict(skipped)


# --------------------------------------------------------------------------- #
# J3 — parameter sensitivity sweep
# --------------------------------------------------------------------------- #

def run_sweep(contexts: list[ReplayContext], slippage_bps: float = 10.0) -> dict:
    """Recompute expectancy per class over the parameter grid (report only)."""
    combos = [(s, t, h) for s in SWEEP_STOP_MULTS for t in SWEEP_TARGET_SCALES for h in SWEEP_MAX_HOLD_DAYS]
    per_class: dict[str, dict[tuple, list[float]]] = defaultdict(lambda: defaultdict(list))
    class_n: Counter = Counter()

    for ctx in contexts:
        class_n[ctx.event_type] += 1
        is_long = ctx.direction == "long"
        for combo in combos:
            stop_mult, target_scale, max_hold = combo
            stop, t1, t2 = _sweep_levels(
                ctx.entry_price, ctx.base_stop, ctx.base_t1, ctx.base_t2,
                is_long, stop_mult, target_scale,
            )
            res = simulate_trade(ctx.bars, ctx.signal_idx, ctx.direction, stop, t1, t2, max_hold, slippage_bps=slippage_bps)
            if res is not None:
                per_class[ctx.event_type][combo].append(res.pnl_pct)

    result = {}
    for cls, combo_map in sorted(per_class.items()):
        rows = [
            {"combo": {"stop_mult": c[0], "target_scale": c[1], "max_holding_days": c[2]},
             "n": len(pnls), "expectancy_pct": round(statistics.fmean(pnls), 4)}
            for c, pnls in combo_map.items()
        ]
        best = max(rows, key=lambda r: r["expectancy_pct"]) if rows else None
        result[cls] = {
            "n": class_n[cls],
            "caution_small_sample": class_n[cls] < SMALL_SAMPLE_N,
            "best": best,
            "combos": rows,
        }
    return {
        "grid": {
            "stop_mults": list(SWEEP_STOP_MULTS),
            "target_scales": list(SWEEP_TARGET_SCALES),
            "max_holding_days": list(SWEEP_MAX_HOLD_DAYS),
            "combo_count": len(combos),
        },
        "per_class": result,
    }


# --------------------------------------------------------------------------- #
# J4 — shadow-ledger adapter (thin; skips cleanly if Spec I unmerged)
# --------------------------------------------------------------------------- #

def shadow_ledger_available(session) -> bool:
    """True only if Spec I's ``scored_candidates`` table exists in this DB."""
    try:
        return "scored_candidates" in sa_inspect(session.get_bind()).get_table_names()
    except Exception as exc:  # pragma: no cover - inspection failure = treat as absent
        log.warning("shadow_ledger_inspect_failed", error=str(exc))
        return False


def _first(mapping: dict, *names):
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


def replay_shadow_ledger(
    session,
    settings,
    price_cache: PriceHistoryCache | None = None,
    years: int | None = None,
    today: date | None = None,
) -> dict | None:
    """Replay Spec I shadow-ledger rows as trades (entry T+1 open after scored_at,
    stored stop/target). Returns None when the table is absent — never imports
    Spec I code paths that may not exist. Grouped by cohort and score bucket.

    Spec I's ``scored_candidates`` stores a single ``suggested_stop`` + ``target_1``
    (no T2); the T2 leg falls back to T1 so the whole position exits at the one
    target. Rows without a tradeable direction (default ``neutral``) are skipped."""
    if not shadow_ledger_available(session):
        log.info("shadow_ledger_absent_skipping")
        return None

    from sqlalchemy import MetaData, Table, select

    meta = MetaData()
    table = Table("scored_candidates", meta, autoload_with=session.get_bind())
    price_cache = price_cache or PriceHistoryCache(settings)
    slippage = getattr(settings, "backtest_slippage_bps", 10.0)
    max_hold = getattr(settings, "max_holding_days", 20)
    cutoff = ((today or date.today()) - timedelta(days=365 * years)) if years else None

    by_cohort: dict[str, list[TradeResult]] = defaultdict(list)
    by_bucket: dict[str, list[TradeResult]] = defaultdict(list)
    n_rows = n_replayed = 0

    for row in session.execute(select(table)).mappings():
        n_rows += 1
        row = dict(row)
        scored_raw = _first(row, "scored_at", "scored_date", "created_at")
        ticker = _first(row, "ticker", "symbol")
        stop = _first(row, "suggested_stop", "stop_loss", "stop")
        t1 = _first(row, "target_1", "target1")
        t2 = _first(row, "target_2", "target2") or t1  # single-target ledger → T2=T1
        if not (scored_raw and ticker and stop and t1):
            continue
        direction = (_first(row, "direction", "side") or "").lower()
        if direction not in ("long", "short"):
            continue  # neutral / unscored → no tradeable direction
        scored_at = _as_date(scored_raw)
        if cutoff and scored_at < cutoff:
            continue
        cohort = str(_first(row, "cohort") or "unknown")
        bucket = str(_first(row, "score_bucket", "score_band") or _score_bucket(_first(row, "score", "final_score")))

        start = scored_at - timedelta(days=15)
        end = scored_at + timedelta(days=max_hold * 2 + 21)
        bars = _price_bars_to_bars(price_cache.get_bars(str(ticker), start, end, session=session))
        sig_idx = signal_index(bars, scored_at)
        if sig_idx is None or sig_idx + 1 >= len(bars):
            continue
        result = simulate_trade(bars, sig_idx, direction, float(stop), float(t1), float(t2), max_hold, slippage_bps=slippage)
        if result is None:
            continue
        n_replayed += 1
        by_cohort[cohort].append(result)
        by_bucket[bucket].append(result)

    return {
        "rows": n_rows,
        "replayed": n_replayed,
        "by_cohort": {k: _stats(v) for k, v in sorted(by_cohort.items())},
        "by_score_bucket": {k: _stats(v) for k, v in sorted(by_bucket.items())},
    }


def _score_bucket(score) -> str:
    if score is None:
        return "unknown"
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "unknown"
    if s < 0.4:
        return "0.0-0.4"
    if s < 0.6:
        return "0.4-0.6"
    if s < 0.8:
        return "0.6-0.8"
    return "0.8-1.0"
