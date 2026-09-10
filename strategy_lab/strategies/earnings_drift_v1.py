"""`earnings_drift_v1` — post-earnings-announcement drift, long-only (Spec Q §7B).

The rule, transcribed from the normative V1 reference configuration and not
tuned here:

* the input is a structured earnings record with reported EPS, consensus EPS,
  an event date and source provenance, all available at the cutoff;
* ``surprise_pct = (reported_eps - consensus_eps) / max(abs(consensus_eps), 0.01) * 100``;
* long when ``surprise_pct >= 5.0`` and the shared liquidity rules pass;
* no post-announcement price or volume may qualify the entry;
* entry is the first regular-session open strictly after the recorded event
  date, which stays executable when the source lacks intraday announcement
  timing;
* execution policy ``event_swing_14cal_v1``;
* records without trustworthy ``known_at_utc`` provenance are exploratory only
  and cannot contribute to promotion evidence.

**When the signal fires.** Spec Q fixes the entry but not the evaluation
cadence, and the two interact: an 8-K accepted at 22:30 UTC is not knowable at
that day's 21:00 close, so an arm evaluating at each session close first sees it
the following evening. The rule used here is that the record fires on the first
evaluation at which it was knowable — ``prior_session_close < known_at_utc <=
cutoff`` — and never again. That is the honest point-in-time reading (it cannot
act on a fact before the fact existed), it cannot re-fire on later evaluations
(so one announcement is one decision), and it is reproducible from the stored
snapshot alone, because the snapshot carries both session boundaries. When the
record lands more than a session after the event date the decision still fires
and carries ``delayed_entry_after_event_date``, so the drift measured from a
late-landing consensus is visible in the evidence rather than silently mixed in.
That cadence rule is a **project assumption**, labelled as one; the thresholds
above are the cited specification.

**The normalization of signal strength** — ``min(surprise_pct / 50, 1)`` — is
also a project assumption. ``StrategyDecision`` requires a signal strength in
[0, 1] for a long, and nothing in Spec Q defines the mapping; the scale is
declared in the immutable config so that changing it is a new version rather
than a quiet re-weighting.
"""

from __future__ import annotations

from datetime import date, datetime

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
from strategy_lab.execution_policy import EVENT_SWING_14CAL_V1
from strategy_lab.indicators import median_dollar_volume, wilder_atr
from strategy_lab.validation import build_implementation_manifest, validate_decision_set

SLUG = "earnings_drift_v1"
VERSION = "1.0.0"
POLICY = EVENT_SWING_14CAL_V1

#: Every threshold, in one immutable place. Spec Q §7: "A worker must not invent
#: replacements. Any change creates a new version."
CONFIG = {
    "min_surprise_pct": 5.0,
    "consensus_floor_abs": 0.01,
    "signal_strength_scale_pct": 50.0,
    "liquidity_rules": universe.LIQUID_US_EQUITY_V1.canonical(),
    "assumptions": [
        "fires on the first evaluation at which the record was knowable",
        "signal_strength = min(surprise_pct / signal_strength_scale_pct, 1.0)",
        "position_risk_pct = 1.0 of the arm's per-trade risk budget; the dollar "
        "budget is deployment configuration (Spec Q §3)",
    ],
    "citations": [
        "Spec Q §7B earnings_drift_v1",
        "Spec Q §7 'earnings_drift_v1 formula'",
        "Spec Q §7 'Shared liquid-equity universe liquid_us_equity_v1'",
    ],
}

INDICATORS = {
    "wilder_atr": wilder_atr,
    "median_dollar_volume": median_dollar_volume,
}

# --- reason codes -----------------------------------------------------------
REASON_NO_EARNINGS_RECORD = "earnings_record_absent"
REASON_RECORD_NOT_NEW = "earnings_record_not_new_at_cutoff"
REASON_SURPRISE_MET = "positive_surprise_at_or_above_threshold"
REASON_SURPRISE_BELOW = "surprise_below_threshold"
REASON_DELAYED_ENTRY = "delayed_entry_after_event_date"
REASON_ATR_UNAVAILABLE = "atr_unavailable"
REASON_CALENDAR_MISSING = "snapshot_calendar_missing"


def build_version(status: StrategyVersionStatus = StrategyVersionStatus.DRAFT) -> StrategyVersion:
    return StrategyVersion(
        slug=SLUG,
        version=VERSION,
        hypothesis=(
            "A large positive earnings surprise is followed by drift in the same "
            "direction over the following two weeks, net of a 2-ATR stop and 10 "
            "bps of adverse slippage per fill."
        ),
        universe=universe.UNIVERSE_SLUG,
        direction=Direction.LONG,
        required_snapshot_fields=(
            "calendar", "earnings_record", "price_bars", "price_provenance",
        ),
        execution_policy_version=POLICY.version,
        expected_holding_days=POLICY.max_hold_calendar_days,
        # One session: the record must be evaluated against a series whose last
        # bar is the signal session. A two-day-old series is a stale input, not
        # a slightly worse one.
        max_data_staleness_seconds=86_400,
        historically_replayable=True,
        replayability_reason=(
            "structured earnings observations with known_at_utc plus adjusted "
            "prices; no model output enters the decision"
        ),
        implementation_manifest=build_implementation_manifest(
            strategy_module="strategy_lab.strategies.earnings_drift_v1",
            execution_policy_version=POLICY.version,
            indicators=INDICATORS,
        ),
        config=CONFIG,
        status=status,
    )


def surprise_pct(reported_eps: float, consensus_eps: float) -> float:
    """The Spec Q §7 formula, verbatim, including the 0.01 consensus floor."""
    floor = CONFIG["consensus_floor_abs"]
    return (reported_eps - consensus_eps) / max(abs(consensus_eps), floor) * 100.0


class EarningsDriftV1:
    """Ticker-scoped: one draft for the snapshot's ticker, always."""

    def __init__(self, metadata: StrategyVersion | None = None) -> None:
        self.metadata = metadata or build_version()

    # -- helpers ---------------------------------------------------------- #

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

    # -- the rule --------------------------------------------------------- #

    def evaluate(self, snapshot: MarketSnapshot) -> tuple[StrategyDecision, ...]:
        if snapshot.scope is not SnapshotScope.TICKER:
            raise ValueError(
                f"{SLUG} is ticker-scoped; it was handed a "
                f"{snapshot.scope.value}-scoped snapshot"
            )
        ticker = snapshot.ticker
        decision = self._evaluate_one(snapshot, ticker)
        return validate_decision_set(snapshot, self.metadata, (decision,))

    def _evaluate_one(self, snapshot: MarketSnapshot, ticker: str) -> StrategyDecision:
        snapshot_hash = snapshot.content_hash
        eligibility = universe.screen(
            snapshot,
            ticker,
            max_staleness_seconds=self.metadata.max_data_staleness_seconds,
        )
        if eligibility.blocked:
            return self._decision(
                snapshot_hash, ticker,
                action=DecisionAction.ABSTAIN,
                reason_codes=eligibility.reason_codes,
                blocked_reasons=eligibility.blocked_reasons,
            )

        calendar = snapshots.calendar_of(snapshot)
        signal_session = calendar.get("signal_session")
        if not signal_session:
            return self._decision(
                snapshot_hash, ticker,
                action=DecisionAction.ABSTAIN,
                reason_codes=(REASON_CALENDAR_MISSING,),
                blocked_reasons=("missing_dependency",),
            )

        record = snapshots.earnings_of(snapshot, ticker)
        if record is None:
            # No structured record is not a data outage for this name — the
            # strategy evaluated it and there is simply no event. That is a
            # considered `flat`, and it is the overwhelmingly common answer.
            return self._decision(
                snapshot_hash, ticker,
                action=DecisionAction.FLAT,
                reason_codes=tuple(sorted(set(eligibility.reason_codes) | {REASON_NO_EARNINGS_RECORD})),
            )

        codes = set(eligibility.reason_codes)
        if not record.replay_eligible:
            codes.add(universe.REASON_DATA_QUALITY_WARNING)

        if not self._is_new_at_cutoff(snapshot, calendar, record.known_at_utc):
            codes.add(REASON_RECORD_NOT_NEW)
            return self._decision(
                snapshot_hash, ticker,
                action=DecisionAction.FLAT,
                reason_codes=tuple(sorted(codes)),
            )

        if record.event_date < date.fromisoformat(signal_session):
            codes.add(REASON_DELAYED_ENTRY)

        surprise = surprise_pct(record.reported_eps, record.consensus_eps)
        if surprise < CONFIG["min_surprise_pct"]:
            codes.add(REASON_SURPRISE_BELOW)
            return self._decision(
                snapshot_hash, ticker,
                action=DecisionAction.FLAT,
                reason_codes=tuple(sorted(codes)),
            )

        if not eligibility.eligible:
            # Evaluated, surprise qualified, but the shared liquidity rules did
            # not. Flat with the failing screen named, never a silent drop.
            return self._decision(
                snapshot_hash, ticker,
                action=DecisionAction.FLAT,
                reason_codes=tuple(sorted(codes)),
            )

        bars = eligibility.bars
        atr = wilder_atr(
            [bar.split_adjusted_high for bar in bars],
            [bar.split_adjusted_low for bar in bars],
            [bar.split_adjusted_close for bar in bars],
            POLICY.atr_period,
        )
        if atr is None or atr <= 0:
            # The stop is 2 ATR below the fill. With no ATR there is no stop,
            # and a long without a stop is not a decision this system makes.
            codes.add(REASON_ATR_UNAVAILABLE)
            return self._decision(
                snapshot_hash, ticker,
                action=DecisionAction.ABSTAIN,
                reason_codes=tuple(sorted(codes)),
                blocked_reasons=("missing_dependency",),
            )

        codes.add(REASON_SURPRISE_MET)
        scale = CONFIG["signal_strength_scale_pct"]
        return self._decision(
            snapshot_hash, ticker,
            action=DecisionAction.LONG,
            reason_codes=tuple(sorted(codes)),
            signal_strength=min(surprise / scale, 1.0),
            risk_plan=POLICY.risk_plan(position_risk_pct=1.0),
        )

    @staticmethod
    def _is_new_at_cutoff(
        snapshot: MarketSnapshot, calendar: dict, known_at_utc: datetime
    ) -> bool:
        """Did the record become knowable in this evaluation's window?

        The window opens at the prior session's close and closes at the cutoff.
        With no prior session in the snapshot the window opens at the beginning
        of time, which only happens for a series too short to pass the screen.
        """
        prior = calendar.get("prior_session")
        if known_at_utc > snapshot.data_cutoff_utc:  # pragma: no cover - builder refuses this
            return False
        if not prior:
            return True
        return known_at_utc > snapshots.session_close_utc(date.fromisoformat(prior))


STRATEGY = EarningsDriftV1()
