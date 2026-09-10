"""Indicator formulas, stated once (Spec Q §6, §7).

Pure stdlib and pure functions of the numbers handed in: no bars type, no
snapshot, no session. Everything a strategy computes from prices lives here so
that the implementation manifest can hash *the formula* — Spec Q §6 lists
"indicator formulas" as its own manifest component, and a manifest that only
hashed the strategy file would miss a silent change to the ATR that moved every
stop in every version.

Two conventions are inherited rather than invented:

* **Wilder smoothing**, not the exponential-moving-average approximation. Spec Q
  §7 names "Wilder ATR(14)" and "RSI(2) ... using Wilder smoothing"; the seed is
  the arithmetic mean of the first ``period`` values and every later value is
  ``(prev * (period - 1) + new) / period``.
* **Median dollar volume on the raw series**, mirroring
  ``data/prices/derived.py::median_dollar_volume`` exactly — ``statistics.median``
  of ``raw_close * volume``, median rather than mean so one block print cannot
  promote a name, and raw rather than adjusted because dollar volume is
  split-invariant only in raw units. That module cannot be imported here (the
  Strategy Lab does not reach ``data/``; see
  ``tests/test_strategy_lab_import_graph.py``), so the definition is restated
  and a test asserts the two agree on the same input.

Every function returns ``None`` when it does not have enough history rather than
padding, back-filling, or seeding from a shorter window. Spec Q agent-prompt
requirement 5: never impute or silently default a trading input. ``None`` is
what makes a strategy abstain instead of guess.
"""

from __future__ import annotations

import statistics
from typing import Sequence

__all__ = [
    "true_ranges",
    "wilder_average",
    "wilder_atr",
    "wilder_rsi",
    "sma",
    "cumulative_return",
    "median_dollar_volume",
]


def true_ranges(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
) -> tuple[float, ...]:
    """``max(h-l, |h-prev_close|, |l-prev_close|)`` for every bar after the first.

    The first bar has no previous close and therefore no true range; the result
    is one shorter than the input, which is why ATR(n) needs ``n + 1`` bars.
    """
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows and closes must be the same length")
    out = []
    for i in range(1, len(closes)):
        prev_close = closes[i - 1]
        out.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - prev_close),
                abs(lows[i] - prev_close),
            )
        )
    return tuple(out)


def wilder_average(values: Sequence[float], period: int) -> float | None:
    """Wilder's smoothed average of ``values``, or ``None`` if too short.

    Seeded with the arithmetic mean of the first ``period`` values, then
    ``(prev * (period - 1) + value) / period`` for each remaining value. This is
    Wilder's original recursion, not the ``2/(n+1)`` EMA that some libraries
    substitute for it; the two differ materially at short periods, which is
    exactly where ``RSI(2)`` lives.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    if len(values) < period:
        return None
    average = sum(values[:period]) / period
    for value in values[period:]:
        average = (average * (period - 1) + value) / period
    return average


def wilder_atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> float | None:
    """Wilder ATR over the bars given. ``None`` with fewer than ``period + 1``.

    Spec Q §7 requires ATR(14) computed "using only bars completed before
    entry". This function has no opinion about which bars those are: the caller
    passes the completed window, and passing a bar that had not closed yet is a
    point-in-time defect at the caller, not here.
    """
    return wilder_average(true_ranges(highs, lows, closes), period)


def wilder_rsi(closes: Sequence[float], period: int = 2) -> float | None:
    """Wilder RSI. ``None`` with fewer than ``period + 1`` closes.

    Returns 100.0 when the smoothed average loss is zero — an unbroken run of
    up-closes — which is the standard convention and, for the reversal arm's
    ``RSI(2) <= 10`` test, the safely non-qualifying side.
    """
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = wilder_average(gains, period)
    avg_loss = wilder_average(losses, period)
    if avg_gain is None or avg_loss is None:
        return None
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def sma(values: Sequence[float], period: int) -> float | None:
    """Simple mean of the last ``period`` values, or ``None`` if too short."""
    if period < 1:
        raise ValueError("period must be >= 1")
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def cumulative_return(closes: Sequence[float]) -> float | None:
    """``last / first - 1`` over the window given, or ``None``.

    Simple, not log, to match every other return in this repository (Spec N
    §5.0). ``None`` when the window is shorter than two closes or the first
    close is non-positive, because a return anchored on zero is not a number
    anyone should rank on.
    """
    if len(closes) < 2:
        return None
    first = closes[0]
    if first <= 0:
        return None
    return closes[-1] / first - 1.0


def median_dollar_volume(
    raw_closes: Sequence[float], volumes: Sequence[float], sessions: int
) -> float | None:
    """Median ``raw_close * volume`` over the last ``sessions`` sessions.

    Mirrors ``data/prices/derived.py::median_dollar_volume``. ``None`` when
    fewer than ``sessions`` sessions are available: a name whose history has not
    reached the window is absent from the screen, never defaulted into it.
    """
    if sessions < 1:
        raise ValueError("sessions must be >= 1")
    if len(raw_closes) != len(volumes):
        raise ValueError("raw_closes and volumes must be the same length")
    if len(raw_closes) < sessions:
        return None
    window = [
        raw_closes[i] * volumes[i] for i in range(len(raw_closes) - sessions, len(raw_closes))
    ]
    return statistics.median(window)
