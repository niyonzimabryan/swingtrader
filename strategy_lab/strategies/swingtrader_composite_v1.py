"""`swingtrader_composite_v1` — the incumbent, as a compatibility adapter (Spec Q §7A).

The existing pipeline runs first. Its complete result — final score,
classification, direction, cohort, signal breakdown, trade parameters, model and
trace provenance, output hash and the hash of the portfolio context the scorer
saw — is frozen into the snapshot by the builder. **This adapter maps that
stored payload and makes no model call.** It recomputes no score, re-derives no
threshold, and calls nothing that could reach a network.

It is the champion because it is the system being replaced, not because it is
proven (Spec Q §2).

**Historical replay is prohibited.** ``historically_replayable=False``: an LLM's
conclusion and a web-research result are not reconstructible at a historical
time T, and a model asked today what it thought in March knows what happened in
April. ``validation.guard_historical_replay`` is the refusal, and every snapshot
carrying a frozen composite result is marked ``not_point_in_time``, so
``MarketSnapshot.replay_eligible`` is ``False`` and Spec Q §10 keeps the arm's
numbers out of clean metrics and away from promotion gates. Forward shadow
evidence is what this arm can produce, and it is enough to be a champion.

**What "long" means here.** The pipeline's own actionability gate is the memo
cohort: ``tracking/shadow_ledger.py::classify_cohort`` puts a name in ``memo``
when its final score clears ``auto_approve_min_score``, and that is the band in
which the current system produces a memo and asks Bryan to approve a trade. The
adapter maps ``cohort == "memo"`` with a long direction to ``long``, and
everything else the pipeline scored to ``flat``. It does not re-derive the
threshold — the cohort was computed by the pipeline and frozen — which is what
"preserves current thresholds without modification" requires. When the pipeline
recorded a memo cohort but no memo was actually generated, the decision still
fires and carries ``memo_not_generated`` so the discrepancy is in the evidence
rather than lost.

**The trade parameters are the plan.** Unlike the three quantitative arms, this
one's stop and targets exist at decision time: the pipeline already computed
them. They travel on the ``RiskPlan`` as absolute prices under the
``swingtrader_memo_trade_params_v1`` policy, whose horizon comes from the frozen
``max_hold_days`` rather than from the policy. An incomplete or non-positive
trade-parameter block is a missing dependency and abstains; it is never patched
with a default.
"""

from __future__ import annotations

from strategy_lab import snapshots
from strategy_lab.domain import (
    DecisionAction,
    Direction,
    MarketSnapshot,
    SnapshotScope,
    StrategyDecision,
    StrategyVersion,
    StrategyVersionStatus,
)
from strategy_lab.execution_policy import SWINGTRADER_MEMO_TRADE_PARAMS_V1
from strategy_lab.validation import build_implementation_manifest, validate_decision_set

SLUG = "swingtrader_composite_v1"
VERSION = "1.0.0"
POLICY = SWINGTRADER_MEMO_TRADE_PARAMS_V1

#: The pipeline's own universe: whatever discovery, screening and scoring put in
#: front of the scorer. Deliberately not `liquid_us_equity_v1` — applying a
#: different universe to the champion would change its behaviour, which Spec Q
#: §7A forbids.
UNIVERSE = "swingtrader_pipeline_v1"

#: The cohort the existing pipeline treats as actionable.
ACTIONABLE_COHORT = "memo"

CONFIG = {
    "actionable_cohort": ACTIONABLE_COHORT,
    "direction": "long",
    "required_trade_params": ["entry_price", "stop_loss", "target_1", "max_hold_days"],
    "assumptions": [
        "cohort == 'memo' is the pipeline's actionability gate "
        "(tracking/shadow_ledger.py::classify_cohort)",
        "position_risk_pct = 1.0 of the arm's per-trade risk budget; the dollar "
        "budget is deployment configuration (Spec Q §3)",
    ],
    "citations": [
        "Spec Q §7A swingtrader_composite_v1",
        "Spec Q §6 'for the compatibility arm only' snapshot fields",
        "Spec Q §14 'Compatibility adapter'",
    ],
}

#: No indicator formulas: the adapter computes nothing. Recorded as an empty
#: mapping rather than omitted, so the manifest distinguishes "no formulas" from
#: "nobody wrote them down".
INDICATORS: dict = {}

REASON_MEMO_COHORT = "pipeline_memo_cohort"
REASON_NOT_ACTIONABLE_COHORT = "pipeline_cohort_not_actionable"
REASON_DIRECTION_NOT_LONG = "pipeline_direction_not_long"
REASON_NO_COMPOSITE_RESULT = "frozen_composite_result_absent"
REASON_RESULT_STALE = "frozen_composite_result_stale"
REASON_TRADE_PARAMS_INCOMPLETE = "frozen_trade_params_incomplete"
REASON_MEMO_NOT_GENERATED = "memo_not_generated"


def build_version(status: StrategyVersionStatus = StrategyVersionStatus.DRAFT) -> StrategyVersion:
    return StrategyVersion(
        slug=SLUG,
        version=VERSION,
        hypothesis=(
            "The existing AI-assisted composite score identifies swing setups "
            "that beat cash and a broad-market benchmark net of costs. It is the "
            "champion because it is the incumbent, not because it is proven."
        ),
        universe=UNIVERSE,
        direction=Direction.LONG,
        required_snapshot_fields=("composite_result", "composite_trade_params"),
        execution_policy_version=POLICY.version,
        # A declared expectation only; each decision's actual horizon is the
        # frozen `max_hold_days`, which is deployment configuration
        # (config/settings.py defaults to 20) and not part of this version.
        expected_holding_days=20,
        # The scored row must be from this evaluation, not yesterday's run.
        max_data_staleness_seconds=86_400,
        historically_replayable=False,
        replayability_reason=(
            "the score embeds LLM and live web-research output, which is not "
            "reconstructible at a historical time T; Spec Q §4 lists historical "
            "replay of LLM conclusions under 'do not build again'"
        ),
        implementation_manifest=build_implementation_manifest(
            strategy_module="strategy_lab.strategies.swingtrader_composite_v1",
            execution_policy_version=POLICY.version,
            indicators=INDICATORS,
        ),
        config=CONFIG,
        status=status,
    )


class SwingtraderCompositeV1:
    """Ticker-scoped: one draft for the snapshot's ticker, mapped not computed."""

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
        if snapshot.scope is not SnapshotScope.TICKER:
            raise ValueError(
                f"{SLUG} is ticker-scoped; it was handed a "
                f"{snapshot.scope.value}-scoped snapshot"
            )
        ticker = snapshot.ticker
        return validate_decision_set(
            snapshot, self.metadata, (self._evaluate_one(snapshot, ticker),)
        )

    def _evaluate_one(self, snapshot: MarketSnapshot, ticker: str) -> StrategyDecision:
        snapshot_hash = snapshot.content_hash
        result = snapshots.composite_of(snapshot, ticker)
        if result is None:
            return self._decision(
                snapshot_hash, ticker,
                action=DecisionAction.ABSTAIN,
                reason_codes=(REASON_NO_COMPOSITE_RESULT,),
                blocked_reasons=("missing_dependency",),
            )

        age = (snapshot.data_cutoff_utc - result.scored_at).total_seconds()
        if age > self.metadata.max_data_staleness_seconds:
            return self._decision(
                snapshot_hash, ticker,
                action=DecisionAction.ABSTAIN,
                reason_codes=(REASON_RESULT_STALE,),
                blocked_reasons=("stale_data",),
            )

        codes = {f"pipeline_classification_{result.classification or 'unknown'}"}
        if result.direction != "long":
            codes.add(REASON_DIRECTION_NOT_LONG)
            return self._decision(
                snapshot_hash, ticker,
                action=DecisionAction.FLAT,
                reason_codes=tuple(sorted(codes)),
            )
        if result.cohort != ACTIONABLE_COHORT:
            codes.add(REASON_NOT_ACTIONABLE_COHORT)
            return self._decision(
                snapshot_hash, ticker,
                action=DecisionAction.FLAT,
                reason_codes=tuple(sorted(codes)),
            )

        params = result.trade_params
        plan = _frozen_plan(params)
        if plan is None:
            codes.add(REASON_TRADE_PARAMS_INCOMPLETE)
            return self._decision(
                snapshot_hash, ticker,
                action=DecisionAction.ABSTAIN,
                reason_codes=tuple(sorted(codes)),
                blocked_reasons=("missing_dependency",),
            )
        entry, stop, targets, max_hold = plan

        codes.add(REASON_MEMO_COHORT)
        if not result.memo_generated:
            codes.add(REASON_MEMO_NOT_GENERATED)

        return self._decision(
            snapshot_hash, ticker,
            action=DecisionAction.LONG,
            reason_codes=tuple(sorted(codes)),
            # The pipeline's final score is already normalised into [0, 1].
            signal_strength=max(0.0, min(1.0, float(result.final_score))),
            confidence=None,
            risk_plan=POLICY.risk_plan(
                position_risk_pct=1.0,
                max_hold_calendar_days=max_hold,
                stop_price=stop,
                target_prices=targets,
            ),
        )


def _frozen_plan(params) -> tuple[float, float, tuple[float, ...], int] | None:
    """Read the frozen trade parameters, or ``None`` if they are unusable.

    No defaults and no repair: a memo whose stop is missing, non-positive, or
    above its entry is a broken record, and the honest answer is to abstain and
    say the dependency is missing.
    """
    try:
        entry = float(params.get("entry_price"))
        stop = float(params.get("stop_loss"))
        target_1 = float(params.get("target_1"))
        max_hold = int(params.get("max_hold_days"))
    except (TypeError, ValueError):
        return None
    if entry <= 0 or not 0 < stop < entry or target_1 <= entry or max_hold < 1:
        return None
    targets = [target_1]
    raw_t2 = params.get("target_2")
    if raw_t2 is not None:
        try:
            target_2 = float(raw_t2)
        except (TypeError, ValueError):
            return None
        if target_2 <= target_1:
            return None
        targets.append(target_2)
    return entry, stop, tuple(targets), max_hold


STRATEGY = SwingtraderCompositeV1()
