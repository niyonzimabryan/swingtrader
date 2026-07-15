"""Exit-engine simulator (Spec J1).

Replays the bot's *actual* exit semantics over daily OHLC bars — pure math, no
network, no DB. This is the honest, replayable core of the backtester: given an
entry signal and the ATR-derived stop/targets the live path would set, it
reproduces how `execution/order_monitor.py` would have managed the position.

Semantics mirrored from order_monitor (read it, don't guess):
  * Entry fills at the OPEN of the bar AFTER the signal (T+1 open) — never the
    signal-day close. This is the honest executable assumption.
  * Stop: crossing the stop exits at the stop price, BUT if a bar opens through
    the stop the fill is at the (worse) open — a gap-through never pretends the
    stop price was available.
  * Target 1: half the position exits at T1 (favorable-gap fills at the open),
    the remaining half rides with the stop unchanged (order_monitor keeps the
    OCO stop on the remainder after a T1 partial).
  * Target 2: the remaining half exits at T2.
  * Time exit: whatever is still open is closed at the close of the first bar on
    or after entry_date + max_holding_days (order_monitor counts calendar days
    from the fill date).
  * Same-bar stop+target conflict resolves PESSIMISTICALLY — stop first: a bar
    whose range spans both is assumed to have stopped the whole remaining
    position out before any target filled.

Slippage (default 10 bps) is applied adversely to every fill (entry and each
exit), so results are conservative rather than frictionless.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Sequence


@dataclass
class TradeResult:
    """Outcome of replaying one trade's exits over daily bars."""

    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float          # position-weighted (blended) exit fill
    pnl_pct: float
    rule_fired: str            # stop | t1_then_stop | t1_then_t2 | t1_then_time | time
    holding_days: int          # calendar days, entry fill bar -> final exit bar
    mfe: float                 # max favorable excursion, % from entry (>= 0)
    mae: float                 # max adverse excursion, % from entry (<= 0)
    direction: str = "long"
    legs: list[tuple[float, float, str]] = field(default_factory=list)  # (fraction, price, reason)


# rule_fired constants
RULE_STOP = "stop"
RULE_T1_THEN_STOP = "t1_then_stop"
RULE_T1_THEN_T2 = "t1_then_t2"
RULE_T1_THEN_TIME = "t1_then_time"
RULE_TIME = "time"


def simulate_trade(
    bars: Sequence,
    entry_idx: int,
    direction: str,
    stop: float | None,
    target_1: float | None,
    target_2: float | None,
    max_holding_days: int,
    slippage_bps: float = 10,
) -> TradeResult:
    """Replay the bot's exit engine for one trade.

    ``bars`` is an ordered sequence of daily bars — any object exposing
    ``date``/``open``/``high``/``low``/``close`` (``data.event_outcomes.PriceBar``
    satisfies this). ``entry_idx`` is the index of the SIGNAL bar; the trade
    enters at the open of ``bars[entry_idx + 1]`` (T+1 open).
    """
    is_long = direction == "long"
    entry_bar_idx = entry_idx + 1
    if entry_bar_idx >= len(bars):
        raise ValueError("insufficient bars for T+1 open entry (no bar after the signal)")

    slip = float(slippage_bps) / 10_000.0
    entry_bar = bars[entry_bar_idx]
    entry_fill = _apply_slippage(entry_bar.open, is_long, is_entry=True, slip=slip)

    # Time-exit boundary: first bar on/after entry_date + max_holding_days calendar
    # days (mirrors order_monitor's days_held >= max_holding_days), capped at the
    # last available bar.
    time_exit_date = entry_bar.date + timedelta(days=max_holding_days)
    end_idx = len(bars) - 1
    for i in range(entry_bar_idx, len(bars)):
        if bars[i].date >= time_exit_date:
            end_idx = i
            break

    legs: list[tuple[float, float, str]] = []
    t1_done = False
    remaining = 1.0
    rule = None
    exit_bar = bars[end_idx]
    mfe = 0.0
    mae = 0.0

    for i in range(entry_bar_idx, end_idx + 1):
        bar = bars[i]
        mfe, mae = _update_excursion(bar, entry_fill, is_long, mfe, mae)

        # 1. Stop takes precedence (pessimistic same-bar resolution).
        if _stop_hit(bar, stop, is_long):
            fill = _apply_slippage(_stop_fill(bar, stop, is_long), is_long, is_entry=False, slip=slip)
            legs.append((remaining, fill, "stop"))
            remaining = 0.0
            rule = RULE_T1_THEN_STOP if t1_done else RULE_STOP
            exit_bar = bar
            break

        # 2. Target 1 (first half), then possibly Target 2 on the same bar.
        if not t1_done and _target_hit(bar, target_1, is_long):
            fill1 = _apply_slippage(_target_fill(bar, target_1, is_long), is_long, is_entry=False, slip=slip)
            legs.append((0.5, fill1, "t1"))
            t1_done = True
            remaining = 0.5
            exit_bar = bar
            if _target_hit(bar, target_2, is_long):
                fill2 = _apply_slippage(_target_fill(bar, target_2, is_long), is_long, is_entry=False, slip=slip)
                legs.append((0.5, fill2, "t2"))
                remaining = 0.0
                rule = RULE_T1_THEN_T2
                break
            continue

        # 3. Target 2 for the remaining half (T1 already booked on a prior bar).
        if t1_done and _target_hit(bar, target_2, is_long):
            fill2 = _apply_slippage(_target_fill(bar, target_2, is_long), is_long, is_entry=False, slip=slip)
            legs.append((remaining, fill2, "t2"))
            remaining = 0.0
            rule = RULE_T1_THEN_T2
            exit_bar = bar
            break

    # 4. Anything still open time-exits at the end bar's close.
    if remaining > 0:
        close_fill = _apply_slippage(bars[end_idx].close, is_long, is_entry=False, slip=slip)
        legs.append((remaining, close_fill, "time"))
        exit_bar = bars[end_idx]
        rule = RULE_T1_THEN_TIME if t1_done else RULE_TIME
        remaining = 0.0

    blended_exit = sum(frac * price for frac, price, _ in legs)
    if is_long:
        pnl_pct = (blended_exit - entry_fill) / entry_fill * 100 if entry_fill else 0.0
    else:
        pnl_pct = (entry_fill - blended_exit) / entry_fill * 100 if entry_fill else 0.0

    return TradeResult(
        entry_date=entry_bar.date,
        entry_price=round(entry_fill, 4),
        exit_date=exit_bar.date,
        exit_price=round(blended_exit, 4),
        pnl_pct=round(pnl_pct, 2),
        rule_fired=rule,
        holding_days=(exit_bar.date - entry_bar.date).days,
        mfe=round(mfe * 100, 2),
        mae=round(mae * 100, 2),
        direction=direction,
        legs=[(round(f, 4), round(p, 4), r) for f, p, r in legs],
    )


# --------------------------------------------------------------------------- #
# Per-bar predicates — long/short symmetric.
# --------------------------------------------------------------------------- #


def _stop_hit(bar, stop: float | None, is_long: bool) -> bool:
    if not stop or stop <= 0:
        return False
    return bar.low <= stop if is_long else bar.high >= stop


def _stop_fill(bar, stop: float, is_long: bool) -> float:
    """Gap-through the stop fills at the (worse) open; otherwise at the stop."""
    if is_long:
        return bar.open if bar.open <= stop else stop
    return bar.open if bar.open >= stop else stop


def _target_hit(bar, target: float | None, is_long: bool) -> bool:
    if not target or target <= 0:
        return False
    return bar.high >= target if is_long else bar.low <= target


def _target_fill(bar, target: float, is_long: bool) -> float:
    """Favorable gap through the target fills at the (better) open."""
    if is_long:
        return bar.open if bar.open >= target else target
    return bar.open if bar.open <= target else target


def _apply_slippage(price: float, is_long: bool, is_entry: bool, slip: float) -> float:
    """Adverse slippage: pay up to enter/cover, receive less to sell."""
    if slip <= 0:
        return price
    buy = (is_long and is_entry) or (not is_long and not is_entry)
    return price * (1 + slip) if buy else price * (1 - slip)


def _update_excursion(bar, entry_fill: float, is_long: bool, mfe: float, mae: float) -> tuple[float, float]:
    if not entry_fill:
        return mfe, mae
    if is_long:
        fav = (bar.high - entry_fill) / entry_fill
        adv = (bar.low - entry_fill) / entry_fill
    else:
        fav = (entry_fill - bar.low) / entry_fill
        adv = (entry_fill - bar.high) / entry_fill
    return max(mfe, fav), min(mae, adv)
