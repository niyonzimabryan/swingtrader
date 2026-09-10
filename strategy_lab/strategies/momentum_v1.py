"""`momentum_v1` — long-only cross-sectional momentum (Spec Q §7C).

The rule, transcribed from the normative V1 reference configuration:

* one universe snapshot for the entire rebalance;
* score each eligible constituent by cumulative adjusted return from session
  ``T-252`` through ``T-21`` inclusive, excluding the most recent 20 complete
  sessions;
* rank descending, ties broken by ticker ascending;
* select the top decile, capped at 20 names and requiring at least 5 selected;
* if fewer than 50 eligible constituents exist, abstain for the entire arm;
* equal weight the selected names within the arm's virtual risk budget;
* execution policy ``momentum_quarterly_89cal_v1``;
* delisting/survivorship and universe-version warnings must be included when
  the available data cannot reproduce them.

**The window, precisely.** With the signal session indexed 0 and earlier
sessions counted backwards, the formation window runs from ``T-252`` to
``T-21`` inclusive. That is 232 sessions of *returns* measured over 232
closes — anchored at the ``T-252`` close and ending at the ``T-21`` close — and
it skips the 20 most recent complete sessions, ``T-20`` through ``T``. A name
therefore needs 253 bars, and the shared 252-bar screen is not by itself
sufficient; a name with exactly 252 is eligible for the universe and still
cannot be scored, which is an ``abstain`` for that name, not a silent drop.

**Top decile of what.** Of the eligible set, which is the population the rank is
taken over: ``ceil(len(eligible) / 10)``, then capped at 20. With the 50-name
floor this is between 5 and 20 names, so the "at least 5" condition can only
fail through the cap arithmetic, and if it does the whole arm abstains rather
than trading a thinner basket than the specification describes.

**Only on a rebalance session.** The execution policy's signal cutoff is the
final complete trading session of March, June, September or December, and the
snapshot carries that determination (``snapshots.is_quarter_end_session``,
frozen by the builder). On any other session every constituent is ``flat``: the
strategy was evaluated and selected nothing, which is a different and more
useful record than not running it.
"""

from __future__ import annotations

import math

from strategy_lab import snapshots, universe
from strategy_lab.domain import (
    DecisionAction,
    Direction,
    MarketSnapshot,
    SnapshotScope,
    StrategyDecision,
    StrategyVersion,
    StrategyVersionStatus,
)
from strategy_lab.execution_policy import MOMENTUM_QUARTERLY_89CAL_V1
from strategy_lab.indicators import cumulative_return, median_dollar_volume
from strategy_lab.validation import build_implementation_manifest, validate_decision_set

SLUG = "momentum_v1"
VERSION = "1.0.0"
POLICY = MOMENTUM_QUARTERLY_89CAL_V1

CONFIG = {
    "formation_start_sessions_ago": 252,
    "formation_end_sessions_ago": 21,
    "skip_sessions": 20,
    "select_decile": 10,
    "max_selected": 20,
    "min_selected": 5,
    "min_eligible_constituents": 50,
    "liquidity_rules": universe.LIQUID_US_EQUITY_V1.canonical(),
    "assumptions": [
        "the top decile is ceil(eligible / 10) of the eligible population",
        "equal weight => position_risk_pct = 1 / selected count",
    ],
    "citations": [
        "Spec Q §7C momentum_v1",
        "Spec Q §7 'momentum_v1 formula'",
        "Spec Q §7 'Shared liquid-equity universe liquid_us_equity_v1'",
        "long-only adaptation of the cited cross-sectional momentum literature; "
        "the adaptation is part of the versioned hypothesis",
    ],
}

INDICATORS = {
    "cumulative_return": cumulative_return,
    "median_dollar_volume": median_dollar_volume,
}

REASON_SELECTED = "top_decile_by_formation_return"
REASON_NOT_SELECTED = "outside_top_decile"
REASON_NOT_REBALANCE_SESSION = "not_a_quarterly_rebalance_session"
REASON_FORMATION_WINDOW_SHORT = "formation_window_incomplete"
REASON_TOO_FEW_ELIGIBLE = "eligible_constituents_below_minimum"
REASON_TOO_FEW_SELECTED = "selection_below_minimum"
REASON_UNIVERSE_VERSION_UNKNOWN = "universe_version_unknown"
REASON_DELISTING_UNKNOWN = "delisting_status_unknown"


def build_version(status: StrategyVersionStatus = StrategyVersionStatus.DRAFT) -> StrategyVersion:
    return StrategyVersion(
        slug=SLUG,
        version=VERSION,
        hypothesis=(
            "Cross-sectional relative strength over the prior twelve months, "
            "skipping the most recent month, persists over the following "
            "quarter in a long-only liquid US equity basket."
        ),
        universe=universe.UNIVERSE_SLUG,
        direction=Direction.LONG,
        required_snapshot_fields=(
            "calendar", "price_bars", "price_provenance", "universe_membership",
        ),
        execution_policy_version=POLICY.version,
        expected_holding_days=POLICY.max_hold_calendar_days,
        max_data_staleness_seconds=86_400,
        historically_replayable=True,
        replayability_reason=(
            "adjusted closes and point-in-time universe membership only; no "
            "model output enters the decision"
        ),
        implementation_manifest=build_implementation_manifest(
            strategy_module="strategy_lab.strategies.momentum_v1",
            execution_policy_version=POLICY.version,
            indicators=INDICATORS,
        ),
        config=CONFIG,
        status=status,
    )


def formation_return(bars) -> float | None:
    """Cumulative adjusted return from ``T-252`` to ``T-21`` inclusive.

    ``None`` when the series is shorter than 253 bars — the window needs a
    ``T-252`` close to anchor on and cannot be shortened without changing what
    the number means.
    """
    start = CONFIG["formation_start_sessions_ago"]
    end = CONFIG["formation_end_sessions_ago"]
    if len(bars) < start + 1:
        return None
    window = bars[len(bars) - 1 - start: len(bars) - end]
    return cumulative_return([bar.split_adjusted_close for bar in window])


def select_count(eligible: int) -> int:
    """Top decile of the eligible population, capped."""
    return min(math.ceil(eligible / CONFIG["select_decile"]), CONFIG["max_selected"])


class MomentumV1:
    """Universe-scoped: one draft for every constituent, ordered by ticker."""

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

    def _warnings(self, snapshot: MarketSnapshot) -> set[str]:
        codes: set[str] = set()
        if "universe_version_unknown" in snapshot.quality_warnings:
            codes.add(REASON_UNIVERSE_VERSION_UNKNOWN)
        if "delisting_unknown" in snapshot.quality_warnings:
            codes.add(REASON_DELISTING_UNKNOWN)
        if "archival_reconstructed" in snapshot.quality_warnings:
            codes.add(universe.REASON_DATA_QUALITY_WARNING)
        return codes

    def evaluate(self, snapshot: MarketSnapshot) -> tuple[StrategyDecision, ...]:
        if snapshot.scope is not SnapshotScope.UNIVERSE:
            raise ValueError(
                f"{SLUG} ranks a cross-section and requires a universe-scoped "
                f"snapshot; it was handed a {snapshot.scope.value}-scoped one"
            )
        snapshot_hash = snapshot.content_hash
        shared = self._warnings(snapshot)
        constituents = snapshot.constituents

        screened = {
            ticker: universe.screen(
                snapshot, ticker,
                max_staleness_seconds=self.metadata.max_data_staleness_seconds,
            )
            for ticker in constituents
        }

        rebalance = bool(snapshots.calendar_of(snapshot).get("is_quarter_end_session"))
        if not rebalance:
            return validate_decision_set(snapshot, self.metadata, tuple(
                self._flat_or_abstain(
                    snapshot_hash, ticker, screened[ticker],
                    shared | {REASON_NOT_REBALANCE_SESSION},
                )
                for ticker in constituents
            ))

        # Score every eligible name. A name whose formation window is short is
        # eligible for the universe but not scoreable, and abstains.
        scores: dict[str, float] = {}
        unscoreable: set[str] = set()
        for ticker in constituents:
            result = screened[ticker]
            if result.blocked or not result.eligible:
                continue
            value = formation_return(result.bars)
            if value is None:
                unscoreable.add(ticker)
            else:
                scores[ticker] = value

        eligible_count = sum(
            1 for r in screened.values() if r.eligible and not r.blocked
        )
        if eligible_count < CONFIG["min_eligible_constituents"]:
            # Spec Q §7: "If fewer than 50 eligible constituents exist, abstain
            # for the entire arm." Not a thin basket — no basket.
            codes = tuple(sorted(shared | {REASON_TOO_FEW_ELIGIBLE}))
            return validate_decision_set(snapshot, self.metadata, tuple(
                self._decision(
                    snapshot_hash, ticker,
                    action=DecisionAction.ABSTAIN,
                    reason_codes=codes,
                    blocked_reasons=("missing_dependency",),
                )
                for ticker in constituents
            ))

        ranked = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
        wanted = select_count(eligible_count)
        selected = [ticker for ticker, _ in ranked[:wanted]]

        if len(selected) < CONFIG["min_selected"]:
            codes = tuple(sorted(shared | {REASON_TOO_FEW_SELECTED}))
            return validate_decision_set(snapshot, self.metadata, tuple(
                self._decision(
                    snapshot_hash, ticker,
                    action=DecisionAction.ABSTAIN,
                    reason_codes=codes,
                    blocked_reasons=("missing_dependency",),
                )
                for ticker in constituents
            ))

        chosen = set(selected)
        weight = 1.0 / len(selected)
        best = ranked[0][1] if ranked else 0.0
        worst = ranked[min(wanted, len(ranked)) - 1][1] if ranked else 0.0

        decisions = []
        for ticker in constituents:
            result = screened[ticker]
            if ticker in chosen:
                decisions.append(self._decision(
                    snapshot_hash, ticker,
                    action=DecisionAction.LONG,
                    reason_codes=tuple(sorted(
                        shared | set(result.reason_codes) | {REASON_SELECTED}
                    )),
                    signal_strength=_normalised_rank(scores[ticker], best, worst),
                    risk_plan=POLICY.risk_plan(position_risk_pct=weight),
                ))
            elif ticker in unscoreable:
                decisions.append(self._decision(
                    snapshot_hash, ticker,
                    action=DecisionAction.ABSTAIN,
                    reason_codes=tuple(sorted(
                        shared | set(result.reason_codes) | {REASON_FORMATION_WINDOW_SHORT}
                    )),
                    blocked_reasons=("missing_dependency",),
                ))
            else:
                decisions.append(
                    self._flat_or_abstain(
                        snapshot_hash, ticker, result, shared | {REASON_NOT_SELECTED}
                    )
                )
        return validate_decision_set(snapshot, self.metadata, tuple(decisions))

    def _flat_or_abstain(self, snapshot_hash, ticker, result, extra_codes) -> StrategyDecision:
        if result.blocked:
            return self._decision(
                snapshot_hash, ticker,
                action=DecisionAction.ABSTAIN,
                reason_codes=tuple(sorted(set(result.reason_codes))),
                blocked_reasons=result.blocked_reasons,
            )
        return self._decision(
            snapshot_hash, ticker,
            action=DecisionAction.FLAT,
            reason_codes=tuple(sorted(set(result.reason_codes) | set(extra_codes))),
        )


def _normalised_rank(value: float, best: float, worst: float) -> float:
    """Map a selected name's formation return into [0, 1] within the basket.

    The top name scores 1.0 and the weakest selected name scores 0.0 when the
    two differ; a degenerate basket where every selected return is identical
    scores 1.0 throughout. This is presentation, not sizing — every selected
    name is equal-weighted regardless — and it is a project assumption, declared
    here rather than smuggled into the risk plan.
    """
    if best == worst:
        return 1.0
    return max(0.0, min(1.0, (value - worst) / (best - worst)))


STRATEGY = MomentumV1()
