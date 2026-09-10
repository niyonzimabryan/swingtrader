"""The shared liquid-equity screen, `liquid_us_equity_v1` (Spec Q §7).

Three strategies share one eligibility rule, so it is written once. Spec Q §7
states it as:

* the point-in-time active SwingTrader universe from the universe snapshot;
* adjusted close at the signal cutoff at least $5;
* at least 252 valid daily bars ending at or before the cutoff;
* median daily dollar volume over the prior 20 complete sessions at least $20m;
* long-only, regular trading hours;
* corporate-action-adjusted prices for signals, executable unadjusted OHLC for
  fills;
* "if point-in-time membership, delisting, or adjustment status is unknown,
  preserve the decision in shadow with ``data_quality_warning``; it is not
  paper/live eligible."

That last clause is why the screen distinguishes three outcomes rather than two.
A *blocked* name cannot be evaluated at all — no bars, or bars too stale — and
becomes an ``abstain`` carrying a Spec Q §6 blocked reason. An *ineligible* name
was evaluated and failed a threshold, and becomes a ``flat``: a considered
decision not to trade, which is a different claim and is stored as one. An
*eligible* name may still carry ``data_quality_warning``, and the decision is
preserved with the warning attached rather than discarded.

The universe *slug* matches ``data/prices/universes.py::UNIVERSE_SLUG``
deliberately: membership comes from that table, computed point-in-time by that
module's month-end liquidity rank. The screen here is the additional per-name
filter Spec Q §7 layers on top of membership, not a second definition of
membership — and it does not import ``data`` (Spec Q §5).
"""

from __future__ import annotations

from dataclasses import dataclass

from strategy_lab.domain import MarketSnapshot
from strategy_lab.indicators import median_dollar_volume
from strategy_lab import snapshots

__all__ = [
    "UNIVERSE_SLUG",
    "LiquidityRules",
    "LIQUID_US_EQUITY_V1",
    "Eligibility",
    "screen",
    "REASON_ELIGIBLE",
    "REASON_INSUFFICIENT_HISTORY",
    "REASON_PRICE_BELOW_MINIMUM",
    "REASON_ILLIQUID",
    "REASON_DATA_QUALITY_WARNING",
    "REASON_NOT_IN_SNAPSHOT",
]

UNIVERSE_SLUG = "liquid_us_equity_v1"

REASON_ELIGIBLE = "liquidity_screen_passed"
REASON_INSUFFICIENT_HISTORY = "insufficient_price_history"
REASON_PRICE_BELOW_MINIMUM = "adjusted_close_below_minimum"
REASON_ILLIQUID = "median_dollar_volume_below_minimum"
REASON_DATA_QUALITY_WARNING = "data_quality_warning"
REASON_NOT_IN_SNAPSHOT = "ticker_absent_from_snapshot"


@dataclass(frozen=True)
class LiquidityRules:
    """Immutable thresholds. A change here is a new universe version."""

    slug: str
    min_split_adjusted_close: float
    min_sessions: int
    median_dollar_volume_sessions: int
    min_median_dollar_volume: float

    def canonical(self) -> dict:
        return {
            "slug": self.slug,
            "min_split_adjusted_close": self.min_split_adjusted_close,
            "min_sessions": self.min_sessions,
            "median_dollar_volume_sessions": self.median_dollar_volume_sessions,
            "min_median_dollar_volume": self.min_median_dollar_volume,
        }


LIQUID_US_EQUITY_V1 = LiquidityRules(
    slug=UNIVERSE_SLUG,
    min_split_adjusted_close=5.0,
    min_sessions=252,
    median_dollar_volume_sessions=20,
    min_median_dollar_volume=20_000_000.0,
)


@dataclass(frozen=True)
class Eligibility:
    """The screen's verdict for one name under one snapshot."""

    ticker: str
    eligible: bool
    #: Spec Q §6 blocked reasons. Non-empty means ``abstain``, not ``flat``.
    blocked_reasons: tuple[str, ...]
    reason_codes: tuple[str, ...]
    bars: tuple[snapshots.SnapshotBar, ...] = ()
    median_dollar_volume: float | None = None

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_reasons)


def screen(
    snapshot: MarketSnapshot,
    ticker: str,
    *,
    rules: LiquidityRules = LIQUID_US_EQUITY_V1,
    max_staleness_seconds: int,
) -> Eligibility:
    """Apply the shared screen. Never imputes, never defaults a missing input."""
    symbol = (ticker or "").strip().upper()
    bars = snapshots.bars_of(snapshot, symbol)
    if not bars:
        return Eligibility(
            ticker=symbol,
            eligible=False,
            blocked_reasons=("missing_dependency",),
            reason_codes=(REASON_NOT_IN_SNAPSHOT,),
        )

    staleness = snapshots.staleness_seconds(snapshot, symbol)
    if staleness is None or staleness > max_staleness_seconds:
        return Eligibility(
            ticker=symbol,
            eligible=False,
            blocked_reasons=("stale_data",),
            reason_codes=("price_series_stale",),
            bars=bars,
        )

    codes: list[str] = []
    meta = snapshots.price_meta_of(snapshot, symbol)
    if not meta.get("replay_eligible", False) or not meta.get("delisting_known", True):
        codes.append(REASON_DATA_QUALITY_WARNING)

    failures: list[str] = []
    if len(bars) < rules.min_sessions:
        failures.append(REASON_INSUFFICIENT_HISTORY)
    if bars[-1].split_adjusted_close < rules.min_split_adjusted_close:
        failures.append(REASON_PRICE_BELOW_MINIMUM)

    mdv = median_dollar_volume(
        [bar.raw_close for bar in bars],
        [bar.volume for bar in bars],
        rules.median_dollar_volume_sessions,
    )
    if mdv is None or mdv < rules.min_median_dollar_volume:
        failures.append(REASON_ILLIQUID)

    if failures:
        return Eligibility(
            ticker=symbol,
            eligible=False,
            blocked_reasons=(),
            reason_codes=tuple(sorted(set(codes + failures))),
            bars=bars,
            median_dollar_volume=mdv,
        )
    return Eligibility(
        ticker=symbol,
        eligible=True,
        blocked_reasons=(),
        reason_codes=tuple(sorted(set(codes + [REASON_ELIGIBLE]))),
        bars=bars,
        median_dollar_volume=mdv,
    )
