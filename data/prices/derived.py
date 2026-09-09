"""The three series, the factors between them, and the price-file covariates.

Spec N §4.3 wants any one of the three series to be reconstructible from the
other two plus the factors. That is only true if the conventions are written
down once, so here they are.

Conventions
-----------

For a session `i` in an ascending series of `n` bars:

* `split_factor(i)` is the share multiplier whose **ex-date is session i** —
  2.0 for a 2-for-1. On an ordinary session it is 1.0.
* `dividend_cash(i)` is cash per share with the same ex-date, quoted in
  post-split shares (i.e. in the same units as `raw_close(i)`).
* **Forward split factor** `F(i) = prod(split_factor(k) for k > i)`. It is the
  cumulative number of shares that one share held on session `i` has become by
  the end of the series, and `F(n-1) = 1` by construction.

Then:

* `split_adjusted_close(i) = raw_close(i) / F(i)`.
* The one-session total return into session `i` is
  `r(i) = (raw_close(i) + dividend_cash(i)) * split_factor(i) / raw_close(i-1) - 1`.
  The `* split_factor(i)` is what undoes the mechanical price drop on a split
  ex-date; without it a 2-for-1 reads as a -50% day.
* `total_return_close` is that return series compounded and **anchored at the
  last bar**, `total_return_close(n-1) = raw_close(n-1)`, which is the ordinary
  back-adjustment convention and the one that makes the last row of all three
  series agree.

Anchoring at the end rather than the start matters in practice: it means adding
tomorrow's bar rescales the whole history, so a stored series is only valid for
the snapshot it was ingested under. That is why `price_snapshots` exists and
why a cohort records which snapshot it ran on.

Covariates (Spec N §4.5) are computed from stored bars alone — no licence, no
vintage problem: 20-session median dollar volume, 20-session realized
volatility, and their deciles as of a date, plus a price-level bucket. Market
cap is deliberately absent: it needs a share count, which the price file does
not carry, and which arrives with the SEC feed in Phase 3a.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import replace
from datetime import date
from typing import Mapping, Sequence

from data.prices.base import DailyBar, PricePlaneError

#: Sessions in the covariate windows (Spec N §4.5: "20-day").
DEFAULT_WINDOW_SESSIONS = 20

#: Trading sessions per year, for annualising realized volatility.
SESSIONS_PER_YEAR = 252

#: Price-level buckets (Spec N §4.5 "price level bucket"), upper bounds in USD.
PRICE_LEVEL_BUCKETS = (1.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0)

#: Tolerance for the reconstruction identity. Spec N asks for 1e-9; the checks
#: below use a relative tolerance so that a $900 close is held to the same
#: number of significant figures as a $9 one.
RECONSTRUCTION_RTOL = 1e-9


class SeriesError(PricePlaneError):
    """The bars handed in cannot support the requested derivation."""


def _ordered(bars: Sequence[DailyBar]) -> tuple[DailyBar, ...]:
    out = tuple(bars)
    if not out:
        raise SeriesError("empty series")
    dates = [b.session_date for b in out]
    if dates != sorted(set(dates)):
        raise SeriesError("bars must be unique and ascending by session_date")
    return out


def forward_split_factors(bars: Sequence[DailyBar]) -> tuple[float, ...]:
    """`F(i)` for each bar: the product of every split ex-dated after it."""
    ordered = _ordered(bars)
    factors = [1.0] * len(ordered)
    running = 1.0
    for i in range(len(ordered) - 1, -1, -1):
        factors[i] = running
        running *= ordered[i].split_factor
    return tuple(factors)


def split_adjusted_closes(bars: Sequence[DailyBar]) -> tuple[float, ...]:
    """Split-adjusted closes from raw closes plus the split factors."""
    ordered = _ordered(bars)
    return tuple(
        bar.raw_close / f for bar, f in zip(ordered, forward_split_factors(ordered))
    )


def raw_closes_from_split_adjusted(
    split_adjusted: Sequence[float],
    bars: Sequence[DailyBar],
) -> tuple[float, ...]:
    """The inverse: raw closes from the split-adjusted series plus the factors."""
    ordered = _ordered(bars)
    if len(split_adjusted) != len(ordered):
        raise SeriesError("split-adjusted series and factor series differ in length")
    return tuple(
        value * f for value, f in zip(split_adjusted, forward_split_factors(ordered))
    )


def total_return_relatives(bars: Sequence[DailyBar]) -> tuple[float, ...]:
    """`1 + r(i)` per session; the first element is 1.0 (no prior close)."""
    ordered = _ordered(bars)
    out = [1.0]
    for previous, current in zip(ordered, ordered[1:]):
        gross = (current.raw_close + current.dividend_cash) * current.split_factor
        out.append(gross / previous.raw_close)
    return tuple(out)


def total_return_closes(bars: Sequence[DailyBar]) -> tuple[float, ...]:
    """Total-return closes, back-adjusted so the last equals the last raw close."""
    ordered = _ordered(bars)
    relatives = total_return_relatives(ordered)
    out = [0.0] * len(ordered)
    out[-1] = ordered[-1].raw_close
    for i in range(len(ordered) - 2, -1, -1):
        out[i] = out[i + 1] / relatives[i + 1]
    return tuple(out)


def raw_closes_from_total_return(
    total_return: Sequence[float],
    bars: Sequence[DailyBar],
) -> tuple[float, ...]:
    """Raw closes back out of the total-return series plus the stored factors.

    `tr(i)/tr(i-1) = (raw(i) + div(i)) * split(i) / raw(i-1)`, so walking
    forward from the first raw close recovers every later one.
    """
    ordered = _ordered(bars)
    if len(total_return) != len(ordered):
        raise SeriesError("total-return series and factor series differ in length")
    out = [ordered[0].raw_close]
    for i in range(1, len(ordered)):
        if total_return[i - 1] == 0:
            raise SeriesError("total-return series contains a zero")
        relative = total_return[i] / total_return[i - 1]
        raw = relative * out[i - 1] / ordered[i].split_factor - ordered[i].dividend_cash
        out.append(raw)
    return tuple(out)


def with_derived_series(bars: Sequence[DailyBar]) -> tuple[DailyBar, ...]:
    """Fill `split_adjusted_close` and `total_return_close` from raw + factors.

    Used by every plane that publishes raw bars and factors and leaves the
    adjusted series to be computed — which is all of them except a vendor that
    ships its own adjusted closes, where the vendor's values are kept and
    `check_reconstruction` is what catches a disagreement.
    """
    ordered = _ordered(bars)
    adjusted = split_adjusted_closes(ordered)
    total = total_return_closes(ordered)
    return tuple(
        replace(bar, split_adjusted_close=a, total_return_close=t)
        for bar, a, t in zip(ordered, adjusted, total)
    )


def check_reconstruction(bars: Sequence[DailyBar], rtol: float = RECONSTRUCTION_RTOL) -> None:
    """Assert every stored series reconstructs from the other two plus factors.

    Raises `SeriesError` naming the first session that disagrees. This is the
    runtime form of `test_series_reconstruct_from_factors`; the backfill runs it
    on every name it stores, so a vendor whose adjusted closes do not match its
    own factors is caught at ingest rather than in a cohort six weeks later.
    """
    ordered = _ordered(bars)
    stored_adjusted = [b.split_adjusted_close for b in ordered]
    stored_total = [b.total_return_close for b in ordered]

    checks = (
        ("split_adjusted_close from raw + factors", stored_adjusted, split_adjusted_closes(ordered)),
        ("total_return_close from raw + factors", stored_total, total_return_closes(ordered)),
        (
            "raw_close from split_adjusted + factors",
            [b.raw_close for b in ordered],
            raw_closes_from_split_adjusted(stored_adjusted, ordered),
        ),
        (
            "raw_close from total_return + factors",
            [b.raw_close for b in ordered],
            raw_closes_from_total_return(stored_total, ordered),
        ),
    )
    for label, stored, computed in checks:
        for bar, want, got in zip(ordered, stored, computed):
            if not math.isclose(want, got, rel_tol=rtol, abs_tol=rtol):
                raise SeriesError(
                    f"{bar.ticker} {bar.session_date}: {label} disagrees — "
                    f"stored {want!r}, reconstructed {got!r}"
                )


# --------------------------------------------------------------------------- #
# Covariates computable from stored bars alone (Spec N §4.5)
# --------------------------------------------------------------------------- #


def _window(
    bars: Sequence[DailyBar],
    as_of: date,
    sessions: int,
    require_current: bool = True,
) -> tuple[DailyBar, ...]:
    """The last `sessions` bars on or before `as_of`.

    `require_current` is what stops a dead name from carrying a stale covariate
    forever: a security whose history ended in March has no liquidity "as of"
    July, and ranking it on its last twenty live sessions would keep a delisted
    name in the liquid universe indefinitely. A name that simply did not trade
    on `as_of` is excluded too, which is the same answer for a weaker reason and
    is the conservative direction.
    """
    ordered = _ordered(bars)
    upto = [b for b in ordered if b.session_date <= as_of]
    if len(upto) < sessions:
        raise SeriesError(
            f"{ordered[0].ticker}: {len(upto)} sessions on or before {as_of}, "
            f"need {sessions}"
        )
    if require_current and upto[-1].session_date != as_of:
        raise SeriesError(
            f"{ordered[0].ticker}: no bar on {as_of}; the series ends "
            f"{upto[-1].session_date}"
        )
    return tuple(upto[-sessions:])


def median_dollar_volume(
    bars: Sequence[DailyBar],
    as_of: date,
    sessions: int = DEFAULT_WINDOW_SESSIONS,
    require_current: bool = True,
) -> float:
    """Median of `raw_close x volume` over the last `sessions` sessions <= `as_of`.

    Median rather than mean because a single block print should not promote a
    name into the liquid universe, and raw rather than adjusted because dollar
    volume is split-invariant only in raw units. See `_window` for why a series
    that does not reach `as_of` raises instead of answering.
    """
    return statistics.median(
        b.dollar_volume for b in _window(bars, as_of, sessions, require_current)
    )


def realized_volatility(
    bars: Sequence[DailyBar],
    as_of: date,
    sessions: int = DEFAULT_WINDOW_SESSIONS,
    annualise: bool = True,
    require_current: bool = True,
) -> float:
    """Annualised stdev of simple daily returns on the split-adjusted series.

    `sessions` returns need `sessions + 1` closes. Simple (not log) returns, to
    match every other return in the engine (Spec N §5.0); split-adjusted, so a
    split is not a 50% move.
    """
    window = _window(bars, as_of, sessions + 1, require_current)
    closes = split_adjusted_closes(window)
    returns = [b / a - 1.0 for a, b in zip(closes, closes[1:])]
    sigma = statistics.stdev(returns)
    return sigma * math.sqrt(SESSIONS_PER_YEAR) if annualise else sigma


def price_level_bucket(close: float) -> int:
    """Bucket index 0..len(PRICE_LEVEL_BUCKETS) for a price level."""
    for i, upper in enumerate(PRICE_LEVEL_BUCKETS):
        if close < upper:
            return i
    return len(PRICE_LEVEL_BUCKETS)


def deciles(values: Mapping[str, float]) -> dict[str, int]:
    """Rank a `{key: value}` mapping into deciles 1 (lowest) .. 10 (highest).

    Ties break on the key, so the result is a pure function of the input — the
    property `test_liquid_universe_rule_reproducible` needs. With fewer than ten
    entries the deciles are simply sparse; that is a small-universe fact, not an
    error, and the caller reports `n` beside the decile.
    """
    if not values:
        return {}
    ordered = sorted(values.items(), key=lambda kv: (kv[1], kv[0]))
    n = len(ordered)
    out: dict[str, int] = {}
    for rank, (key, _value) in enumerate(ordered):
        out[key] = min(10, 1 + (rank * 10) // n)
    return out


def liquidity_deciles(
    bars_by_uid: Mapping[str, Sequence[DailyBar]],
    as_of: date,
    sessions: int = DEFAULT_WINDOW_SESSIONS,
) -> dict[str, int]:
    """Liquidity decile per security as of a date, from stored bars only.

    Names without a full window as of `as_of` are absent from the result rather
    than defaulted: a freshly listed name has no 20-session liquidity, and
    inventing one is how a covariate quietly becomes fiction.
    """
    measured: dict[str, float] = {}
    for uid, bars in bars_by_uid.items():
        try:
            measured[uid] = median_dollar_volume(bars, as_of, sessions)
        except SeriesError:
            continue
    return deciles(measured)


def realized_vol_deciles(
    bars_by_uid: Mapping[str, Sequence[DailyBar]],
    as_of: date,
    sessions: int = DEFAULT_WINDOW_SESSIONS,
) -> dict[str, int]:
    """Realized-volatility decile per security as of a date, from stored bars."""
    measured: dict[str, float] = {}
    for uid, bars in bars_by_uid.items():
        try:
            measured[uid] = realized_volatility(bars, as_of, sessions)
        except (SeriesError, statistics.StatisticsError):
            continue
    return deciles(measured)
