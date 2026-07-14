"""Event-anchored replay + parameter sweep + shadow-ledger adapter (Spec J2-J4).

Anchors the exit-engine simulator (J1) on real, PIT-guarded entry signals:

  * J2 — every ``HistoricalEvent`` (the pattern library from search + FMP
    structured backfill) becomes a candidate trade: direction = polarity, entry
    at T+1 open, stop/targets from the bot's ACTUAL ATR-based logic (reused from
    memo generation — imported, not reimplemented), replayed over daily bars
    from the outcome-engine price cache. Aggregated per event_type, magnitude
    bucket, and source_type, with the audit's >75%-win-rate bias sanity flag.
  * J3 — a small stop/target/hold grid recomputes expectancy per class over the
    already-cached bars (report only; nothing is auto-applied).
  * J4 — the same replay over Spec I's ``scored_candidates`` shadow ledger, if
    that table exists (clean skip otherwise).

No LLM calls anywhere — LLM-stage replay is permanently out of scope (models
know the future relative to past dates; that path would be lookahead-
contaminated garbage). See the spec's scope decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import fmean, median
from typing import Callable, Sequence

from sqlalchemy import inspect as sa_inspect

from backtest.simulator import TradeResult, simulate_trade
from data.event_outcomes import PriceBar, PriceHistoryCache
from database.models import HistoricalEvent
from memo.generator import MemoGenerator
from utils.logger import get_logger

log = get_logger("event_replay")

ATR_PERIOD = 14
DEFAULT_SLIPPAGE_BPS = 10
# Calendar-day windows around the event for bar fetches.
PRE_WINDOW_DAYS = 45          # enough trailing bars for a 14-bar ATR
FORWARD_BUFFER_DAYS = 15      # beyond max_holding_days so the time-exit bar exists

# J3 parameter grid.
STOP_WIDTH_MULTS = (0.75, 1.0, 1.25)
TARGET_MULTS = (0.75, 1.0, 1.25)     # target distance {-25%, as-is, +25%}
HOLD_DAYS_GRID = (10, 15, 20)
SMALL_SAMPLE_N = 30                  # below this, sweep results carry a caution

# Spec I score buckets (J4).
SCORE_BUCKETS = ((0.0, 0.3), (0.3, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 1.01))

BarProvider = Callable[[str, date, date, object], Sequence[PriceBar]]


# --------------------------------------------------------------------------- #
# Plans and results
# --------------------------------------------------------------------------- #


@dataclass
class EventPlan:
    """Everything needed to (re)simulate one anchored trade, bars cached once."""

    key: str                 # event_type / ledger cohort etc. (primary grouping key)
    ticker: str
    direction: str
    magnitude: float | None
    source_type: str
    bars: list
    signal_idx: int
    price: float             # signal-day close (the memo's "current price")
    atr: float
    ref_id: int | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class ReplayTrade:
    key: str
    ticker: str
    direction: str
    magnitude: float | None
    source_type: str
    result: TradeResult
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# PIT ATR — mirrors data.market_data.MarketDataAdapter.get_atr over given bars.
# --------------------------------------------------------------------------- #


def compute_atr(bars: Sequence, signal_idx: int, period: int = ATR_PERIOD) -> float:
    """14-period ATR from the bars up to and including ``signal_idx`` (no lookahead).

    Same true-range definition and simple mean of the last ``period`` TRs as the
    live get_atr; returns 0.0 when there is not enough trailing history (the
    live path then falls back to default_stop_loss_pct — replicated here).
    """
    if signal_idx < period:
        return 0.0
    trs = []
    for i in range(signal_idx - period + 1, signal_idx + 1):
        high, low, prev_close = bars[i].high, bars[i].low, bars[i - 1].close
        if high is None or low is None or prev_close is None:
            return 0.0
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return round(fmean(trs), 2) if trs else 0.0


def _first_idx_on_or_after(bars: Sequence, target: date) -> int | None:
    for idx, bar in enumerate(bars):
        if bar.date >= target:
            return idx
    return None


# --------------------------------------------------------------------------- #
# Trade-params reuse (imported from memo generation, not reimplemented).
# --------------------------------------------------------------------------- #


class TradeParams:
    """Thin adapter over MemoGenerator._compute_trade_params (the live code path)."""

    def __init__(self, settings, memo_generator: MemoGenerator | None = None):
        self.settings = settings
        self.gen = memo_generator or MemoGenerator(settings, anthropic_client=None)

    def base(self, price: float, atr: float, direction: str) -> dict:
        # regime {} -> position_size_multiplier defaults to 1.0; score/classification
        # only affect sizing (unused here). This IS the memo path for stop/targets.
        return self.gen._compute_trade_params(price, atr, {}, 0.0, "moderate", direction=direction)

    @staticmethod
    def scaled_levels(entry: float, stop_pct_frac: float, direction: str,
                      stop_mult: float, target_mult: float) -> tuple[float, float, float]:
        """Recompute (stop, target_1, target_2) for a sweep combo.

        stop_mult scales the stop *width*; target_mult scales the target distance
        (the memo's 2x/3x-of-stop multiples). At (1.0, 1.0) this reproduces the
        memo's own stop/targets.
        """
        if direction == "short":
            stop = entry * (1 + stop_mult * stop_pct_frac)
            t1 = entry * (1 - target_mult * 2 * stop_pct_frac)
            t2 = entry * (1 - target_mult * 3 * stop_pct_frac)
        else:
            stop = entry * (1 - stop_mult * stop_pct_frac)
            t1 = entry * (1 + target_mult * 2 * stop_pct_frac)
            t2 = entry * (1 + target_mult * 3 * stop_pct_frac)
        return round(stop, 2), round(t1, 2), round(t2, 2)


def direction_from_polarity(polarity: str | None) -> str | None:
    p = (polarity or "").lower()
    if p in ("bullish", "long"):
        return "long"
    if p in ("bearish", "short"):
        return "short"
    return None


# --------------------------------------------------------------------------- #
# Building plans from HistoricalEvent rows (J2).
# --------------------------------------------------------------------------- #


def _class_matches(event_type: str, classes: Sequence[str] | None) -> bool:
    if not classes:
        return True
    et = (event_type or "").lower()
    for token in classes:
        tok = token.strip().lower().rstrip("s")  # upgrades -> upgrade, earnings -> earning
        if tok and tok in et:
            return True
    return False


def load_events(session, classes: Sequence[str] | None, years: int | None,
                today: date | None = None) -> list[HistoricalEvent]:
    today = today or date.today()
    query = session.query(HistoricalEvent)
    if years:
        cutoff = today - timedelta(days=365 * years)
        query = query.filter(HistoricalEvent.event_date >= cutoff)
    events = query.order_by(HistoricalEvent.event_date).all()
    return [e for e in events if _class_matches(e.event_type, classes)]


@dataclass
class BuildStats:
    considered: int = 0
    skipped_neutral: int = 0
    skipped_no_bars: int = 0
    skipped_no_entry: int = 0
    skipped_insufficient_forward: int = 0
    built: int = 0


def build_plans(
    session,
    events: Sequence[HistoricalEvent],
    price_provider: BarProvider,
    max_holding_days: int,
) -> tuple[list[EventPlan], BuildStats]:
    stats = BuildStats()
    plans: list[EventPlan] = []
    for event in events:
        stats.considered += 1
        direction = direction_from_polarity(event.polarity)
        if direction is None:
            stats.skipped_neutral += 1
            continue
        event_date = _as_date(event.event_date)
        start = event_date - timedelta(days=PRE_WINDOW_DAYS)
        end = event_date + timedelta(days=max_holding_days + FORWARD_BUFFER_DAYS)
        bars = list(price_provider(event.ticker, start, end, session))
        signal_idx = _first_idx_on_or_after(bars, event_date)
        if signal_idx is None:
            stats.skipped_no_bars += 1
            continue
        if signal_idx + 1 >= len(bars):
            stats.skipped_no_entry += 1
            continue
        # Require a full forward window so the trade can reach a genuine time exit
        # (avoids right-censoring recent, unmatured events).
        entry_date = bars[signal_idx + 1].date
        if bars[-1].date < entry_date + timedelta(days=max_holding_days):
            stats.skipped_insufficient_forward += 1
            continue
        signal_close = bars[signal_idx].close or 0
        if signal_close <= 0:
            stats.skipped_no_bars += 1
            continue
        plans.append(EventPlan(
            key=event.event_type,
            ticker=event.ticker,
            direction=direction,
            magnitude=event.magnitude,
            source_type=event.source_type or "other",
            bars=bars,
            signal_idx=signal_idx,
            price=signal_close,
            atr=compute_atr(bars, signal_idx),
            ref_id=event.id,
        ))
        stats.built += 1
    return plans, stats


def simulate_plan(
    plan: EventPlan,
    params: TradeParams,
    max_holding_days: int,
    stop_mult: float = 1.0,
    target_mult: float = 1.0,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> TradeResult:
    base = params.base(plan.price, plan.atr, plan.direction)
    if stop_mult == 1.0 and target_mult == 1.0:
        stop, t1, t2 = base["stop_loss"], base["target_1"], base["target_2"]
    else:
        stop, t1, t2 = TradeParams.scaled_levels(
            base["entry_price"], base["stop_pct"] / 100.0, plan.direction, stop_mult, target_mult
        )
    return simulate_trade(
        plan.bars, plan.signal_idx, plan.direction, stop, t1, t2, max_holding_days, slippage_bps
    )


def replay_plans(
    plans: Sequence[EventPlan],
    params: TradeParams,
    max_holding_days: int,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> list[ReplayTrade]:
    trades = []
    for plan in plans:
        result = simulate_plan(plan, params, max_holding_days, slippage_bps=slippage_bps)
        trades.append(ReplayTrade(
            key=plan.key, ticker=plan.ticker, direction=plan.direction,
            magnitude=plan.magnitude, source_type=plan.source_type,
            result=result, extra=plan.extra,
        ))
    return trades


# --------------------------------------------------------------------------- #
# Aggregation.
# --------------------------------------------------------------------------- #


def magnitude_bucket(magnitude: float | None) -> str:
    if magnitude is None:
        return "unknown"
    m = abs(magnitude)
    if m < 2:
        return "<2"
    if m < 5:
        return "2-5"
    if m < 10:
        return "5-10"
    return "10+"


def summarize(trades: Sequence[ReplayTrade]) -> dict:
    """Per-group stats over a list of ReplayTrades (all sharing a group key)."""
    pnls = [t.result.pnl_pct for t in trades]
    n = len(pnls)
    if n == 0:
        return {"n": 0}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    win_rate = len(wins) / n
    rule_dist: dict[str, int] = {}
    for t in trades:
        rule_dist[t.result.rule_fired] = rule_dist.get(t.result.rule_fired, 0) + 1
    return {
        "n": n,
        "win_rate": round(win_rate, 3),
        "median_pnl": round(median(pnls), 2),
        "avg_pnl": round(fmean(pnls), 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "avg_holding_days": round(fmean([t.result.holding_days for t in trades]), 1),
        "rule_dist": rule_dist,
        # Audit sanity: a real catalyst edge does not win >75% of the time; a
        # group above that is flagged for a lookahead/leakage recheck.
        "bias_flag": win_rate > 0.75,
    }


def group_by(trades: Sequence[ReplayTrade], key_fn: Callable[[ReplayTrade], str]) -> dict[str, dict]:
    groups: dict[str, list[ReplayTrade]] = {}
    for t in trades:
        groups.setdefault(key_fn(t), []).append(t)
    return {k: summarize(v) for k, v in sorted(groups.items())}


def build_report(trades: Sequence[ReplayTrade]) -> dict:
    return {
        "overall": summarize(trades),
        "by_event_type": group_by(trades, lambda t: t.key),
        "by_source_type": group_by(trades, lambda t: t.source_type),
        "by_type_and_magnitude": group_by(
            trades, lambda t: f"{t.key} | {magnitude_bucket(t.magnitude)}"
        ),
    }


# --------------------------------------------------------------------------- #
# J3 — parameter sweep (report only).
# --------------------------------------------------------------------------- #


def run_sweep(
    plans: Sequence[EventPlan],
    params: TradeParams,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> dict:
    """Expectancy per (stop_mult, target_mult, hold) combo, per class.

    Returns {event_type: {"combos": [...], "best": {...}}}. Pure recompute over
    already-cached bars — no network. Report only; nothing is auto-applied.
    """
    by_class: dict[str, list[EventPlan]] = {}
    for plan in plans:
        by_class.setdefault(plan.key, []).append(plan)

    out: dict[str, dict] = {}
    for event_type, class_plans in sorted(by_class.items()):
        combos = []
        for stop_mult in STOP_WIDTH_MULTS:
            for target_mult in TARGET_MULTS:
                for hold in HOLD_DAYS_GRID:
                    pnls = [
                        simulate_plan(p, params, hold, stop_mult, target_mult, slippage_bps).pnl_pct
                        for p in class_plans
                    ]
                    n = len(pnls)
                    combos.append({
                        "stop_mult": stop_mult,
                        "target_mult": target_mult,
                        "hold_days": hold,
                        "n": n,
                        "expectancy": round(fmean(pnls), 3) if n else 0.0,
                        "win_rate": round(sum(1 for p in pnls if p > 0) / n, 3) if n else 0.0,
                    })
        best = max(combos, key=lambda c: c["expectancy"]) if combos else None
        out[event_type] = {
            "combos": combos,
            "best": best,
            "n": len(class_plans),
            "small_sample": len(class_plans) < SMALL_SAMPLE_N,
        }
    return out


# --------------------------------------------------------------------------- #
# J4 — shadow-ledger adapter (thin; clean skip if Spec I's table is absent).
# --------------------------------------------------------------------------- #


def shadow_table_exists(session) -> bool:
    try:
        return sa_inspect(session.get_bind()).has_table("scored_candidates")
    except Exception:  # pragma: no cover - defensive
        return False


def _score_bucket(score: float | None) -> str:
    s = score or 0.0
    for lo, hi in SCORE_BUCKETS:
        if lo <= s < hi:
            return f"{lo:.2f}-{hi:.2f}"
    return "0.65-1.01"


def replay_shadow_ledger(
    session,
    price_provider: BarProvider,
    settings,
    params: TradeParams,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> dict:
    """Replay Spec I's scored_candidates rows as trades. Skips cleanly if absent."""
    if not shadow_table_exists(session):
        return {"status": "skipped_table_absent"}

    # Import here so a missing/renamed Spec I model can never break J1-J3.
    from database.models import ScoredCandidate

    max_holding_days = settings.max_holding_days
    rows = session.query(ScoredCandidate).order_by(ScoredCandidate.scored_at).all()
    trades: list[ReplayTrade] = []
    stats = BuildStats()
    for row in rows:
        stats.considered += 1
        direction = direction_from_polarity(row.direction)
        if direction is None:
            stats.skipped_neutral += 1
            continue
        scored_on = _as_date(row.scored_at)
        if scored_on is None:
            stats.skipped_no_bars += 1
            continue
        start = scored_on - timedelta(days=PRE_WINDOW_DAYS)
        end = scored_on + timedelta(days=max_holding_days + FORWARD_BUFFER_DAYS)
        bars = list(price_provider(row.ticker, start, end, session))
        signal_idx = _first_idx_on_or_after(bars, scored_on)
        if signal_idx is None:
            stats.skipped_no_bars += 1
            continue
        if signal_idx + 1 >= len(bars):
            stats.skipped_no_entry += 1
            continue
        entry_date = bars[signal_idx + 1].date
        if bars[-1].date < entry_date + timedelta(days=max_holding_days):
            stats.skipped_insufficient_forward += 1
            continue
        # Params from stored stop/targets; only target_1 is on the ledger, so the
        # remainder rides to stop/time (simulator handles target_2=None).
        stop = row.suggested_stop
        t1 = row.target_1
        if not stop and not t1:
            # Fall back to the live ATR logic if the ledger row lacks stored levels.
            base = params.base(bars[signal_idx].close or 0, compute_atr(bars, signal_idx), direction)
            stop, t1 = base["stop_loss"], base["target_1"]
        result = simulate_trade(
            bars, signal_idx, direction, stop, t1, None, max_holding_days, slippage_bps
        )
        cohort = row.cohort or "below"
        trades.append(ReplayTrade(
            key=cohort, ticker=row.ticker, direction=direction,
            magnitude=row.final_score, source_type="shadow_ledger",
            result=result, extra={"cohort": cohort, "score": row.final_score},
        ))
        stats.built += 1

    return {
        "status": "ok",
        "build_stats": stats.__dict__,
        "overall": summarize(trades),
        "by_cohort": group_by(trades, lambda t: t.extra.get("cohort", "below")),
        "by_score_bucket": group_by(trades, lambda t: _score_bucket(t.extra.get("score"))),
    }


# --------------------------------------------------------------------------- #
# Top-level orchestration (used by the CLI).
# --------------------------------------------------------------------------- #


def run_event_replay(
    session,
    settings,
    classes: Sequence[str] | None = None,
    years: int | None = None,
    price_provider: BarProvider | None = None,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    with_sweep: bool = False,
    with_shadow: bool = False,
    today: date | None = None,
) -> dict:
    provider = price_provider or PriceHistoryCache(settings).get_bars
    params = TradeParams(settings)
    max_holding_days = settings.max_holding_days

    events = load_events(session, classes, years, today=today)
    plans, build_stats = build_plans(session, events, provider, max_holding_days)
    trades = replay_plans(plans, params, max_holding_days, slippage_bps)

    result = {
        "params": {
            "classes": list(classes) if classes else "all",
            "years": years,
            "slippage_bps": slippage_bps,
            "max_holding_days": max_holding_days,
            "entry": "T+1 open",
            "same_bar": "pessimistic (stop first)",
            "llm_replay": "excluded (lookahead — out of scope by design)",
        },
        "build_stats": build_stats.__dict__,
        "report": build_report(trades),
    }
    if with_sweep:
        result["sweep"] = run_sweep(plans, params, slippage_bps)
    if with_shadow:
        result["shadow"] = replay_shadow_ledger(session, provider, settings, params, slippage_bps)
    return result


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")[:10]).date()
    except ValueError:
        return None
