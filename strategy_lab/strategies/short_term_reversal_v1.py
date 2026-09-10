"""`short_term_reversal_v1` — oversold reversal, structurally shadow-only (Spec Q §7D).

The rule, transcribed from the normative V1 reference configuration:

* evaluate after a complete regular session;
* long when the three-session adjusted-close return is at or below ``-8%``,
  ``RSI(2)`` is at or below 10 using Wilder smoothing, the current close is above
  ``SMA(200)``, and the shared liquidity rules pass;
* no ranking is required to qualify, but the shadow arm is capped at the 10
  largest absolute three-session declines per signal date, ties by ticker
  ascending;
* execution policy ``reversal_5cal_v1``, which carries mandatory stress reports
  at 25 and 50 bps of adverse slippage per fill;
* it remains structurally shadow-only in V1.

**Why it is universe-scoped.** "Cap at the 10 largest absolute three-session
declines per signal date" is a cross-sectional rule: it cannot be evaluated one
ticker at a time without mixing cutoffs, which is exactly what Spec Q §6
forbids. So the arm takes one universe snapshot and returns one draft per
constituent, like ``momentum_v1``. Nothing about that makes it a ranking
strategy — every name that clears the three thresholds is a signal; the cap only
decides how many of them the shadow arm carries.

**Shadow-only, structurally.** ``config["shadow_only"]`` is part of the
immutable version content, and ``validation.require_mode_allowed`` refuses a
paper or live arm for a version that declares it — before a runner, an executor
or a broker sees the decision. Spec Q §7D: turnover and bid-ask effects can
dominate the apparent anomaly, and it "cannot become paper/live eligible until
replay shows stability under materially worse cost assumptions". Lifting the
flag is a new version with its own evidence, not an edit.
"""

from __future__ import annotations

from strategy_lab import universe
from strategy_lab.domain import (
    DecisionAction,
    Direction,
    MarketSnapshot,
    SnapshotScope,
    StrategyDecision,
    StrategyVersion,
    StrategyVersionStatus,
)
from strategy_lab.execution_policy import REVERSAL_5CAL_V1
from strategy_lab.indicators import (
    cumulative_return,
    median_dollar_volume,
    sma,
    wilder_atr,
    wilder_rsi,
)
from strategy_lab.validation import (
    SHADOW_ONLY_KEY,
    build_implementation_manifest,
    validate_decision_set,
)

SLUG = "short_term_reversal_v1"
VERSION = "1.0.0"
POLICY = REVERSAL_5CAL_V1

CONFIG = {
    "lookback_sessions": 3,
    "max_three_session_return": -0.08,
    "rsi_period": 2,
    "max_rsi": 10.0,
    "trend_filter_period": 200,
    "max_selected_per_signal_date": 10,
    "signal_strength_scale": 0.20,
    "liquidity_rules": universe.LIQUID_US_EQUITY_V1.canonical(),
    SHADOW_ONLY_KEY: True,
    "shadow_only_reason": (
        "turnover and bid-ask effects can dominate the apparent anomaly; Spec Q "
        "§7D requires replay stability under materially worse cost assumptions "
        "before any paper or live tier"
    ),
    "assumptions": [
        "universe-scoped, because the 10-name cap is cross-sectional",
        "signal_strength = min(abs(three_session_return) / signal_strength_scale, 1.0)",
        "equal weight => position_risk_pct = 1 / selected count",
    ],
    "citations": [
        "Spec Q §7D short_term_reversal_v1",
        "Spec Q §7 'short_term_reversal_v1 formula'",
        "Spec Q §7 'Execution policies' -> reversal_5cal_v1",
    ],
}

INDICATORS = {
    "cumulative_return": cumulative_return,
    "median_dollar_volume": median_dollar_volume,
    "sma": sma,
    "wilder_atr": wilder_atr,
    "wilder_rsi": wilder_rsi,
}

REASON_SELECTED = "oversold_reversal_signal"
REASON_DECLINE_TOO_SHALLOW = "three_session_decline_above_threshold"
REASON_RSI_TOO_HIGH = "rsi2_above_threshold"
REASON_BELOW_TREND_FILTER = "close_at_or_below_sma200"
REASON_CAPPED = "beyond_signal_date_cap"
REASON_HISTORY_SHORT = "indicator_window_incomplete"


def build_version(status: StrategyVersionStatus = StrategyVersionStatus.DRAFT) -> StrategyVersion:
    return StrategyVersion(
        slug=SLUG,
        version=VERSION,
        hypothesis=(
            "A sharp three-session decline in a liquid name still trading above "
            "its 200-session mean reverts over the following week, before "
            "transaction costs plausibly consume the edge."
        ),
        universe=universe.UNIVERSE_SLUG,
        direction=Direction.LONG,
        required_snapshot_fields=("calendar", "price_bars", "price_provenance"),
        execution_policy_version=POLICY.version,
        expected_holding_days=POLICY.max_hold_calendar_days,
        max_data_staleness_seconds=86_400,
        historically_replayable=True,
        replayability_reason=(
            "adjusted closes only; no model output and no event data enter the "
            "decision"
        ),
        implementation_manifest=build_implementation_manifest(
            strategy_module="strategy_lab.strategies.short_term_reversal_v1",
            execution_policy_version=POLICY.version,
            indicators=INDICATORS,
        ),
        config=CONFIG,
        status=status,
    )


def three_session_return(bars) -> float | None:
    """``close[T] / close[T-3] - 1`` on the split-adjusted series."""
    lookback = CONFIG["lookback_sessions"]
    if len(bars) < lookback + 1:
        return None
    return cumulative_return(
        [bar.split_adjusted_close for bar in bars[-(lookback + 1):]]
    )


class ShortTermReversalV1:
    """Universe-scoped: one draft per constituent, capped at ten longs."""

    def __init__(self, metadata: StrategyVersion | None = None) -> None:
        self.metadata = metadata or build_version()

    @staticmethod
    def _decision(snapshot_hash: str, ticker: str, **kwargs) -> StrategyDecision:
        """One draft, pinned to the snapshot hash the caller computed once.

        ``MarketSnapshot.content_hash`` re-serialises the whole normalized
        input block on every access, and a universe snapshot holds ~253
        sessions for every constituent. ``evaluate`` takes the hash once and
        threads it through; the snapshot is frozen, so the value cannot differ
        between constituents, and nothing here has to hold state to know it.
        """
        return StrategyDecision(
            strategy_slug=SLUG,
            strategy_version=VERSION,
            snapshot_hash=snapshot_hash,
            ticker=ticker,
            **kwargs,
        )

    def evaluate(self, snapshot: MarketSnapshot) -> tuple[StrategyDecision, ...]:
        if snapshot.scope is not SnapshotScope.UNIVERSE:
            raise ValueError(
                f"{SLUG} caps its signals across the cross-section and requires "
                f"a universe-scoped snapshot; it was handed a "
                f"{snapshot.scope.value}-scoped one"
            )
        snapshot_hash = snapshot.content_hash
        constituents = snapshot.constituents
        screened = {
            ticker: universe.screen(
                snapshot, ticker,
                max_staleness_seconds=self.metadata.max_data_staleness_seconds,
            )
            for ticker in constituents
        }

        qualifying: dict[str, float] = {}
        codes_by_ticker: dict[str, set[str]] = {}
        blocked: dict[str, tuple[str, ...]] = {}

        for ticker in constituents:
            result = screened[ticker]
            codes = set(result.reason_codes)
            codes_by_ticker[ticker] = codes
            if result.blocked:
                blocked[ticker] = result.blocked_reasons
                continue
            if not result.eligible:
                continue

            closes = [bar.split_adjusted_close for bar in result.bars]
            decline = three_session_return(result.bars)
            rsi = wilder_rsi(closes, CONFIG["rsi_period"])
            trend = sma(closes, CONFIG["trend_filter_period"])
            if decline is None or rsi is None or trend is None:
                codes.add(REASON_HISTORY_SHORT)
                blocked[ticker] = ("missing_dependency",)
                continue

            failed = False
            if decline > CONFIG["max_three_session_return"]:
                codes.add(REASON_DECLINE_TOO_SHALLOW)
                failed = True
            if rsi > CONFIG["max_rsi"]:
                codes.add(REASON_RSI_TOO_HIGH)
                failed = True
            if closes[-1] <= trend:
                codes.add(REASON_BELOW_TREND_FILTER)
                failed = True
            if not failed:
                qualifying[ticker] = decline

        # The cap: the largest absolute declines first, ties by ticker ascending.
        ordered = sorted(qualifying.items(), key=lambda pair: (pair[1], pair[0]))
        cap = CONFIG["max_selected_per_signal_date"]
        selected = [ticker for ticker, _ in ordered[:cap]]
        for ticker, _ in ordered[cap:]:
            codes_by_ticker[ticker].add(REASON_CAPPED)

        weight = 1.0 / len(selected) if selected else None
        scale = CONFIG["signal_strength_scale"]

        decisions = []
        for ticker in constituents:
            codes = tuple(sorted(codes_by_ticker[ticker]))
            if ticker in blocked:
                decisions.append(self._decision(
                    snapshot_hash, ticker,
                    action=DecisionAction.ABSTAIN,
                    reason_codes=codes,
                    blocked_reasons=blocked[ticker],
                ))
            elif ticker in selected:
                decisions.append(self._decision(
                    snapshot_hash, ticker,
                    action=DecisionAction.LONG,
                    reason_codes=tuple(sorted(set(codes) | {REASON_SELECTED})),
                    signal_strength=min(abs(qualifying[ticker]) / scale, 1.0),
                    risk_plan=POLICY.risk_plan(position_risk_pct=weight),
                ))
            else:
                decisions.append(self._decision(
                    snapshot_hash, ticker,
                    action=DecisionAction.FLAT,
                    reason_codes=codes,
                ))
        return validate_decision_set(snapshot, self.metadata, tuple(decisions))


STRATEGY = ShortTermReversalV1()
