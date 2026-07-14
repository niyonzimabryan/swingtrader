"""Exit-engine simulator (Spec J1).

Replays the bot's *actual* exit semantics over daily OHLC bars — pure math,
deterministic, no network. The rules mirror ``execution/order_monitor.py``:

  * Entry fills at the **open of the bar after** the signal bar (T+1 open). The
    honest executable assumption — never T+0 close.
  * A protective stop and two profit targets (T1/T2) sit as an OCO bracket.
    The first half of the position exits at T1; the remaining half rides with
    the stop unchanged until T2, a stop, or the time exit.
  * Gap-through fills at the OPEN, not the stop/target price — if a bar opens
    beyond the stop (or beyond the active target) the fill happens at that
    open, never at the level (you never pretend the level was available).
  * Same-bar stop+target conflict resolves PESSIMISTICALLY: the stop is booked
    first. A bar that touches both a target and the stop is recorded as a
    stop-out with no partial target fill on that bar.
  * Time exit at the close of the ``max_holding_days``-th bar after entry.

Slippage (``slippage_bps``, default 10) is applied adversely to market-style
fills — entry, stop, and time exits. Target fills are limit orders and fill at
the target level (or better, on a favorable gap), with no slippage.

The module intentionally depends on nothing heavy: bars are a light ``Bar``
dataclass so unit tests can hand-construct sequences.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# The position splits in half at T1, mirroring order_monitor's
# ``t1_shares = shares // 2`` / remainder plan. In fractional terms that is a
# clean 0.5 / 0.5 split.
HALF = 0.5


@dataclass
class Bar:
    """A single daily OHLC bar. ``volume`` is unused by the exit engine."""

    date: date
    open: float
    high: float
    low: float
    close: float


@dataclass
class TradeResult:
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float  # share-weighted blended fill across all exit legs
    pnl_pct: float
    rule_fired: str  # stop | t1_then_stop | t1_then_t2 | t1_then_time | time
    holding_days: int  # trading bars from entry bar to final exit bar
    mfe_pct: float  # max favorable excursion over the hold, % of entry (>= 0)
    mae_pct: float  # max adverse excursion over the hold, % of entry (<= 0)


def simulate_trade(
    bars: list[Bar],
    entry_idx: int,
    direction: str,
    stop: float,
    target_1: float,
    target_2: float,
    max_holding_days: int,
    slippage_bps: float = 10,
) -> TradeResult | None:
    """Replay one trade's exit path.

    ``entry_idx`` is the index of the **signal** bar; the position is entered at
    the open of ``entry_idx + 1`` (T+1). Returns ``None`` when there is no T+1
    bar to enter on (caller filters events without sufficient forward bars).
    """
    is_long = direction == "long"
    fill_idx = entry_idx + 1
    if fill_idx >= len(bars) or fill_idx < 0:
        return None

    slip = slippage_bps / 10_000.0
    entry_bar = bars[fill_idx]
    # Entry is a market-style fill at the T+1 open, slipped adversely.
    entry_price = entry_bar.open * (1 + slip) if is_long else entry_bar.open * (1 - slip)

    # The last bar we may still be holding on: max_holding_days bars after entry.
    end_idx = min(fill_idx + max_holding_days, len(bars) - 1)

    exits: list[tuple[float, float]] = []  # (fraction, fill_price)
    remaining = 1.0
    t1_done = False
    rule = ""
    exit_idx = fill_idx
    mfe_pct = 0.0
    mae_pct = 0.0

    def stop_fill(price: float) -> float:
        # Sell-side (long) receives less; buy-to-cover (short) pays more.
        return price * (1 - slip) if is_long else price * (1 + slip)

    def target_fill(open_price: float, level: float) -> float:
        # Limit order at ``level``; a favorable gap fills at the open instead.
        if is_long:
            return open_price if open_price >= level else level
        return open_price if open_price <= level else level

    for i in range(fill_idx, end_idx + 1):
        bar = bars[i]

        # Unrealized excursion experienced while holding this bar's full range.
        if is_long:
            fav = (bar.high - entry_price) / entry_price * 100
            adv = (bar.low - entry_price) / entry_price * 100
        else:
            fav = (entry_price - bar.low) / entry_price * 100
            adv = (entry_price - bar.high) / entry_price * 100
        mfe_pct = max(mfe_pct, fav)
        mae_pct = min(mae_pct, adv)

        active_target = target_1 if not t1_done else target_2

        # 1. Gap-through the stop at the open → fill the remainder at the open.
        gapped_stop = (is_long and bar.open <= stop) or ((not is_long) and bar.open >= stop)
        if gapped_stop:
            fill = bar.open * (1 - slip) if is_long else bar.open * (1 + slip)
            exits.append((remaining, fill))
            remaining = 0.0
            rule = "t1_then_stop" if t1_done else "stop"
            exit_idx = i
            break

        # 2. Stop intrabar — pessimistic: resolves before any same-bar target.
        stop_hit = (is_long and bar.low <= stop) or ((not is_long) and bar.high >= stop)
        if stop_hit:
            exits.append((remaining, stop_fill(stop)))
            remaining = 0.0
            rule = "t1_then_stop" if t1_done else "stop"
            exit_idx = i
            break

        # 3. Target(s).
        target_hit = (is_long and bar.high >= active_target) or (
            (not is_long) and bar.low <= active_target
        )
        if not target_hit:
            continue

        if not t1_done:
            exits.append((HALF, target_fill(bar.open, target_1)))
            remaining -= HALF
            t1_done = True
            exit_idx = i
            # Same bar may also reach T2.
            t2_hit = (is_long and bar.high >= target_2) or ((not is_long) and bar.low <= target_2)
            if t2_hit:
                exits.append((remaining, target_fill(bar.open, target_2)))
                remaining = 0.0
                rule = "t1_then_t2"
                break
        else:
            exits.append((remaining, target_fill(bar.open, target_2)))
            remaining = 0.0
            rule = "t1_then_t2"
            exit_idx = i
            break

    # Anything still open after the hold window exits at the final bar's close.
    if remaining > 0:
        end_bar = bars[end_idx]
        close_fill = end_bar.close * (1 - slip) if is_long else end_bar.close * (1 + slip)
        exits.append((remaining, close_fill))
        rule = "t1_then_time" if t1_done else "time"
        exit_idx = end_idx

    blended_exit = sum(frac * price for frac, price in exits)  # weights sum to 1.0
    pnl_pct = (
        (blended_exit - entry_price) / entry_price * 100
        if is_long
        else (entry_price - blended_exit) / entry_price * 100
    )

    return TradeResult(
        entry_date=entry_bar.date,
        entry_price=round(entry_price, 4),
        exit_date=bars[exit_idx].date,
        exit_price=round(blended_exit, 4),
        pnl_pct=round(pnl_pct, 4),
        rule_fired=rule,
        holding_days=exit_idx - fill_idx,
        mfe_pct=round(mfe_pct, 4),
        mae_pct=round(mae_pct, 4),
    )
