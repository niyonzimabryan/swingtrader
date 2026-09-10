"""The V1 strategy roster and the protocol every member satisfies (Spec Q §6, §7).

A strategy is a small, pure object: immutable metadata plus one method that
turns a ``MarketSnapshot`` into an ordered tuple of ``StrategyDecision`` drafts.
It receives no session, no broker, no portfolio and no model client — Spec Q §6
excludes portfolio and risk context from strategy evaluation on purpose, so that
the same version over the same snapshot decides the same thing in shadow, paper
and live, and a portfolio change blocks an *execution* without mutating a
decision.

``evaluate`` returns drafts, not persisted rows: a draft carries the strategy
identity, the snapshot hash, the ticker, the action, the reason codes and the
risk plan, and knows nothing about the arm or experiment it will be recorded
under. Phase 3's runner supplies those and calls
``strategy_lab.validation.validate_decision_set`` before persistence — as every
strategy here already does before returning, so a violation fails in the
strategy's own unit test rather than in the runner.

The roster:

===============================  ========  =================================
slug                             scope     execution policy
===============================  ========  =================================
``swingtrader_composite_v1``     ticker    ``swingtrader_memo_trade_params_v1``
``earnings_drift_v1``            ticker    ``event_swing_14cal_v1``
``momentum_v1``                  universe  ``momentum_quarterly_89cal_v1``
``short_term_reversal_v1``       universe  ``reversal_5cal_v1``
===============================  ========  =================================

Nothing in this package wires any of them into the production pipeline. That is
Phase 4's work, behind a flag defaulting off.
"""

from __future__ import annotations

from typing import Mapping, Protocol, runtime_checkable

from strategy_lab.domain import MarketSnapshot, StrategyDecision, StrategyVersion
from strategy_lab.strategies import (
    earnings_drift_v1,
    momentum_v1,
    short_term_reversal_v1,
    swingtrader_composite_v1,
)

__all__ = [
    "Strategy",
    "ROSTER",
    "MODULES",
    "get_strategy",
    "build_versions",
]


@runtime_checkable
class Strategy(Protocol):
    """Spec Q §6's strategy interface."""

    metadata: StrategyVersion

    def evaluate(self, snapshot: MarketSnapshot) -> tuple[StrategyDecision, ...]:
        ...


#: Slug -> the module that defines the version. Kept alongside the instances so
#: a caller can rebuild a version's metadata from source (which is what the
#: manifest-drift check compares against) without importing four modules.
MODULES: Mapping[str, object] = {
    swingtrader_composite_v1.SLUG: swingtrader_composite_v1,
    earnings_drift_v1.SLUG: earnings_drift_v1,
    momentum_v1.SLUG: momentum_v1,
    short_term_reversal_v1.SLUG: short_term_reversal_v1,
}

#: Slug -> the singleton strategy instance.
ROSTER: Mapping[str, Strategy] = {
    swingtrader_composite_v1.SLUG: swingtrader_composite_v1.STRATEGY,
    earnings_drift_v1.SLUG: earnings_drift_v1.STRATEGY,
    momentum_v1.SLUG: momentum_v1.STRATEGY,
    short_term_reversal_v1.SLUG: short_term_reversal_v1.STRATEGY,
}


def get_strategy(slug: str) -> Strategy:
    try:
        return ROSTER[slug]
    except KeyError:
        raise KeyError(
            f"unknown strategy {slug!r}; the V1 roster is {sorted(ROSTER)}"
        ) from None


def build_versions() -> dict[str, StrategyVersion]:
    """Rebuild every version's metadata from the source as it is right now.

    This is what the registry compares against a stored
    ``implementation_manifest_hash``: if an edit to a strategy, a helper, an
    indicator, the execution policy or the runtime has landed since
    registration, the rebuilt manifest hashes differently and the run is
    refused (Spec Q §6).
    """
    return {slug: module.build_version() for slug, module in MODULES.items()}
