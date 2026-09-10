"""A Strategy Lab arm wired to a Phase 6 ledger, for the §12 execution tests.

Three things every execution test needs and no existing fixture builds
together:

* a registered strategy version, a registered experiment, an arm in a stated
  mode, a recorded snapshot, and one recorded decision — the whole chain a
  ``strategy_trades`` row hangs off, built through the real registry so an
  execution test is not also a test of hand-written rows;
* a synced ledger with the cash Agentic account, from
  ``tests.proposalfixture``, because ``create_proposal`` reads it;
* a :class:`execution.brokers.fake.FakeExecutionBroker` registered at the venue
  the arm's mode is allowed to reach, so the venue binding under test is the
  one the service actually uses.

The name is deliberately not ``test_*``: ``unittest discover -p "test_*.py"``
would otherwise import it as a test module.
"""

from __future__ import annotations

from datetime import timedelta

from database.db import get_session
from execution.brokers.fake import FakeExecutionBroker
from execution.strategy_lifecycle import ArmExecutionRequest, StrategyExecutionService
from strategy_lab import registry
from strategy_lab.domain import (
    DecisionAction,
    ExecutionMode,
    StrategyDecision,
)
from tests import proposalfixture as pf
from tests.test_strategy_lab_domain import (
    CUTOFF,
    a_risk_plan,
    a_universe_snapshot,
    a_version_spec,
    an_experiment_spec,
)
from strategy_lab.execution import LIVE_VENUE, PAPER_VENUE

NOW = pf.NOW
EXPERIMENT = "q1_momentum_vs_composite"


def a_decision(snapshot, ticker: str = "AMD", **overrides) -> StrategyDecision:
    kwargs = dict(
        strategy_slug="momentum_v1",
        strategy_version="1.0.0",
        snapshot_hash=snapshot.content_hash,
        ticker=ticker,
        action=DecisionAction.LONG,
        reason_codes=("liquidity_ok", "top_decile"),
        signal_strength=0.82,
        risk_plan=a_risk_plan(),
    )
    kwargs.update(overrides)
    return StrategyDecision(**kwargs)


def build_lab(
    session,
    *,
    mode: ExecutionMode = ExecutionMode.PAPER,
    ticker: str = "AMD",
    risk_budget: float = 100_000.0,
    activate: bool = True,
    promote: bool = True,
):
    """Register a version, an experiment, an arm, a snapshot, and one decision.

    ``promote`` records a real owner ``promotion_event`` for a live arm, through
    :func:`strategy_lab.registry.record_promotion`, so a live execution test is
    gated by the same authorization production would need rather than by a row
    someone wrote by hand. ``promote=False`` is the "live without promotion"
    refusal case.
    """
    registry.register_strategy_version(session, a_version_spec())
    registry.register_experiment(session, an_experiment_spec())
    arm = _arm_at_tier(session, mode, risk_budget=risk_budget, promote=promote, activate=activate)

    snapshot = registry.record_snapshot(session, a_universe_snapshot())
    decision = registry.record_decision(session, arm.id, snapshot.id, a_decision(snapshot, ticker))
    session.flush()
    return arm, decision


def _tier(session, mode: ExecutionMode, *, risk_budget: float):
    """Create-or-return the one arm for this experiment, version and mode."""
    existing = (
        session.query(registry.models.ExperimentArm)
        .filter(registry.models.ExperimentArm.mode == mode.value)
        .order_by(registry.models.ExperimentArm.id.asc())
        .first()
    )
    if existing is not None:
        return existing
    return registry.create_arm(
        session, EXPERIMENT, "momentum_v1", "1.0.0", mode, risk_budget=risk_budget,
    )


def _promote(session, source, target):
    """A real owner promotion, through the registry's own binding rules."""
    if registry.promotions_for(session, target.id):
        return
    cutoff = CUTOFF + timedelta(days=source.id)
    evidence = registry.record_metric_snapshot(
        session, source.id, cutoff,
        n_decisions=400, n_matured=120, n_closed=110,
        warnings=("small_sample_in_one_regime",),
        metrics={"net_return_after_costs": 0.04},
    )
    registry.acknowledge_metric_warnings(session, evidence.id, "bryan")
    registry.record_promotion(
        session, source.id, target.id, evidence.id,
        owner="bryan", reason="preregistered gate met",
    )


def _arm_at_tier(session, mode, *, risk_budget, promote, activate):
    """Walk the shadow -> paper -> live ladder, because the registry enforces it.

    Spec Q's promotion ladder is one tier at a time, so a live arm that exists at
    all got there through a paper arm that got there through a shadow arm. The
    fixture builds the whole chain rather than writing a live arm by hand, which
    is the only way a live-execution test is gated by the authorization
    production would actually require.
    """
    shadow = _tier(session, ExecutionMode.SHADOW, risk_budget=risk_budget)
    if mode is ExecutionMode.SHADOW:
        if activate and shadow.status != "active":
            registry.activate_arm(session, shadow.id)
        return shadow

    paper = _tier(session, ExecutionMode.PAPER, risk_budget=risk_budget)
    if mode is ExecutionMode.PAPER:
        if promote:
            _promote(session, shadow, paper)
        elif activate and paper.status != "active":
            registry.activate_arm(session, paper.id)
        return paper

    _promote(session, shadow, paper)
    live = _tier(session, ExecutionMode.LIVE, risk_budget=risk_budget)
    if promote:
        _promote(session, paper, live)
    elif activate and live.status != "active":
        registry.activate_arm(session, live.id)
    return live


def service(
    *,
    broker=None,
    settings=None,
    pager=None,
    venue: str = PAPER_VENUE,
    adapters=None,
    resolver=None,
) -> StrategyExecutionService:
    """A service with one adapter registered at ``venue``."""
    broker = broker if broker is not None else FakeExecutionBroker(fill_price=100.0)
    return StrategyExecutionService(
        session_factory=get_session,
        settings=settings or pf.settings(),
        adapters=adapters if adapters is not None else {venue: broker},
        pager=pager,
        resolver=resolver or pf.resolver_for({}),
        owner_id="99887766",
    )


def live_settings(**overrides):
    """Settings with every live flag on. Never used against a real adapter."""
    base = dict(allow_live_trading=True, execution_mode="live")
    base.update(overrides)
    return pf.settings(**base)


def request_for(arm, decision, **overrides) -> ArmExecutionRequest:
    kwargs = dict(
        arm_id=arm.id,
        decision_id=decision.id,
        mode=ExecutionMode(arm.mode),
        ticker=decision.ticker,
        entry=100.0,
        stop=95.0,
        risk_fraction=0.005,
        expected_hold_sessions=5,
    )
    kwargs.update(overrides)
    return ArmExecutionRequest(**kwargs)


def synced(session, **kwargs):
    """Sync the Phase 6 ledger `create_proposal` reads."""
    return pf.synced_session(session, **kwargs)


__all__ = [
    "CUTOFF",
    "EXPERIMENT",
    "LIVE_VENUE",
    "NOW",
    "PAPER_VENUE",
    "a_decision",
    "build_lab",
    "live_settings",
    "request_for",
    "service",
    "synced",
    "timedelta",
]
