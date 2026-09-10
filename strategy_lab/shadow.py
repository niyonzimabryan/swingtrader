"""The shadow executor: what an arm *would* have done, and nothing more.

Spec Q §2: "**Shadow:** records exactly what a strategy would do, but sends no
broker order." There is no broker import in this package at all
(``tests/test_strategy_lab_import_graph.py``), so "sends no order" is
structural rather than conditional — and this module refuses any arm whose mode
is not ``shadow``, because a paper arm belongs to the Alpaca adapter (PR 6) and
a live arm to the promoted broker path (PR 5).

**The separation this module exists to make.** A ``StrategyDecision`` is
reproducible from its snapshot and carries no portfolio state (Spec Q §6). An
*execution* is judged against a portfolio context that is recomputed from
scratch for every attempt and never reused from the decision. So the same
decision can be executed on Monday and refused on Tuesday because the portfolio
changed, and neither outcome mutates or duplicates the decision:
:func:`open_execution` writes a new ``strategy_trades`` row and leaves
``strategy_decisions`` untouched. ``portfolio_context_hash`` records which
context each attempt was judged against, so "why was this one blocked" is
answerable from the row.

**The lifecycle a shadow fill walks.** ``strategy_trades.status`` is the Spec Q
§12 vocabulary, and a shadow trade walks the same machine with the lab standing
in for owner, risk desk and broker. That is deliberate: Phase 5's real state
machine should be exercised by months of shadow evidence rather than met for the
first time with money on it, and every hop goes through
``registry.set_execution_state``, which refuses an illegal edge. Nothing can
mistake a simulated approval for a real one — ``mode`` says ``shadow`` on every
row, and Spec Q §12 invariant 11 makes a shadow row structurally unable to reach
an order API.

**Sizing is arithmetic, never a choice.** The decision carries a
``position_risk_pct`` of the arm's virtual budget; the notional falls out of
that, the arm's budget and the plan's stop distance, under the caps in the
portfolio context (Spec Q §6.6 / ``AGENTS.md`` §6). Percentage returns and R
are what the scorecard ranks on, precisely because they do not depend on the
virtual budget a shadow arm happened to be given.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

from strategy_lab import registry, replay, snapshots
from strategy_lab.domain import (
    DecisionAction,
    ExecutionMode,
    ExecutionState,
    MarketSnapshot,
    StrategyDecision,
    StrategyLabError,
    StrategyVersion,
    naive_utc,
    sha256_of,
)
from strategy_lab.execution_policy import ResolvedExecutionPlan
from utils.logger import get_logger

log = get_logger("strategy_lab_shadow")

__all__ = [
    "ShadowRefused",
    "PortfolioContext",
    "EligibilityVerdict",
    "SizedPosition",
    "ShadowExecution",
    "BLOCK_REASONS",
    "assess",
    "size_position",
    "open_execution",
    "fill",
    "close",
    "settle",
]


class ShadowRefused(StrategyLabError):
    """A shadow execution was asked for something shadow does not do."""


#: Execution-side blocks. These live on ``strategy_trades`` and deliberately do
#: **not** overlap ``domain.BLOCKED_REASONS``, which covers signal generation
#: only: if a portfolio problem could be written onto a decision, a decision
#: would stop being reproducible from its snapshot (Spec Q §6).
BLOCK_TICKER_ALREADY_HELD = "ticker_already_held_by_this_portfolio"
BLOCK_MAX_OPEN_POSITIONS = "max_open_positions_reached"
BLOCK_DAILY_NOTIONAL_CAP = "daily_notional_cap_reached"
BLOCK_NO_RISK_BUDGET = "arm_risk_budget_is_zero"
BLOCK_STOP_NOT_RESOLVABLE = "execution_plan_has_no_positive_risk"
BLOCK_EQUITY_UNKNOWN = "portfolio_equity_unknown"

BLOCK_REASONS: frozenset[str] = frozenset({
    BLOCK_TICKER_ALREADY_HELD,
    BLOCK_MAX_OPEN_POSITIONS,
    BLOCK_DAILY_NOTIONAL_CAP,
    BLOCK_NO_RISK_BUDGET,
    BLOCK_STOP_NOT_RESOLVABLE,
    BLOCK_EQUITY_UNKNOWN,
})

#: The hops a simulated fill takes. Every one is checked against
#: ``domain.EXECUTION_TRANSITIONS``; writing the path out here means the machine
#: is exercised in shadow rather than first met in live (Spec Q §12).
_FILL_PATH = (
    ExecutionState.OWNER_APPROVED,
    ExecutionState.RISK_RESERVED,
    ExecutionState.SUBMITTED,
    ExecutionState.ACCEPTED,
    ExecutionState.FILLED,
    ExecutionState.PROTECTION_PENDING,
    ExecutionState.PROTECTED,
)
_CLOSE_PATH = (ExecutionState.CLOSING, ExecutionState.CLOSED)


# --------------------------------------------------------------------------- #
# Portfolio context
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PortfolioContext:
    """The portfolio facts an execution attempt is judged against.

    Rebuilt for every attempt and hashed, never carried on a decision. A caller
    that reuses one of these across two attempts is making the claim that the
    portfolio did not change between them, and the hash on each row is what lets
    a reader check it.
    """

    as_of_utc: datetime
    equity: float
    open_tickers: tuple[str, ...] = ()
    daily_notional_used: float = 0.0
    max_open_positions: int = 10
    max_daily_notional: float = 0.0
    max_position_fraction: float = 0.2

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "as_of_utc", naive_utc(self.as_of_utc, field_name="as_of_utc")
        )
        tickers = tuple(sorted({(t or "").strip().upper() for t in self.open_tickers}))
        object.__setattr__(self, "open_tickers", tickers)
        for name in ("equity", "daily_notional_used", "max_daily_notional",
                     "max_position_fraction"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise StrategyLabError(f"{name} must be a number")
            if value < 0:
                raise StrategyLabError(f"{name} must be >= 0")
            object.__setattr__(self, name, float(value))
        if not isinstance(self.max_open_positions, int) or self.max_open_positions < 0:
            raise StrategyLabError("max_open_positions must be a whole number >= 0")
        if not 0 < self.max_position_fraction <= 1:
            raise StrategyLabError("max_position_fraction is a fraction in (0, 1]")

    @property
    def n_open(self) -> int:
        return len(self.open_tickers)

    def canonical(self) -> dict:
        return {
            "as_of_utc": self.as_of_utc.isoformat(),
            "equity": self.equity,
            "open_tickers": list(self.open_tickers),
            "daily_notional_used": self.daily_notional_used,
            "max_open_positions": self.max_open_positions,
            "max_daily_notional": self.max_daily_notional,
            "max_position_fraction": self.max_position_fraction,
        }

    @property
    def context_hash(self) -> str:
        return sha256_of(self.canonical())

    def with_position(self, ticker: str, *, notional: float) -> "PortfolioContext":
        """The context after one more position opens. Used to walk a run."""
        return PortfolioContext(
            as_of_utc=self.as_of_utc,
            equity=self.equity,
            open_tickers=self.open_tickers + ((ticker or "").strip().upper(),),
            daily_notional_used=self.daily_notional_used + float(notional),
            max_open_positions=self.max_open_positions,
            max_daily_notional=self.max_daily_notional,
            max_position_fraction=self.max_position_fraction,
        )


# --------------------------------------------------------------------------- #
# Sizing and eligibility
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SizedPosition:
    """Notional and quantity, with every cap that bound them named."""

    notional: float
    quantity: float
    risk_dollars: float
    risk_pct: float
    caps_applied: tuple[str, ...] = ()

    def canonical(self) -> dict:
        return {
            "notional": self.notional,
            "quantity": self.quantity,
            "risk_dollars": self.risk_dollars,
            "risk_pct": self.risk_pct,
            "caps_applied": list(self.caps_applied),
        }


def size_position(
    plan: ResolvedExecutionPlan,
    context: PortfolioContext,
    *,
    arm_risk_budget: float,
    position_risk_pct: float,
) -> SizedPosition:
    """Risk-based sizing, with the caps that bound it recorded.

    ``risk_dollars = equity x arm_risk_budget x position_risk_pct``, and the
    notional is that divided by the stop distance as a fraction of the entry.
    The position cap in the context then bounds it. Nothing here is a judgement:
    the decision supplied the risk fraction, the arm supplied the budget, and
    both are immutable inputs.
    """
    risk_fraction = plan.risk_per_share / plan.entry_reference
    if risk_fraction <= 0:
        raise ShadowRefused(
            "the execution plan has no positive risk per share; there is "
            "nothing to size against"
        )
    risk_dollars = context.equity * float(arm_risk_budget) * float(position_risk_pct)
    notional = risk_dollars / risk_fraction
    caps: list[str] = []
    ceiling = context.equity * context.max_position_fraction
    if notional > ceiling:
        notional = ceiling
        caps.append("max_position_fraction")
    if context.max_daily_notional:
        remaining = max(0.0, context.max_daily_notional - context.daily_notional_used)
        if notional > remaining:
            notional = remaining
            caps.append("max_daily_notional")
    return SizedPosition(
        notional=notional,
        quantity=notional / plan.entry_reference if plan.entry_reference else 0.0,
        risk_dollars=min(risk_dollars, notional * risk_fraction),
        risk_pct=risk_fraction * 100.0,
        caps_applied=tuple(caps),
    )


@dataclass(frozen=True)
class EligibilityVerdict:
    """Whether *this attempt*, against *this context*, may proceed."""

    allowed: bool
    blocked_reason: str
    context_hash: str
    sizing: SizedPosition | None = None


def assess(
    decision: StrategyDecision,
    plan: ResolvedExecutionPlan,
    context: PortfolioContext,
    *,
    arm_risk_budget: float,
) -> EligibilityVerdict:
    """Fresh execution eligibility. Pure, and never consulted at decision time.

    Spec Q §11: "A ticker-level exposure reservation prevents two arms from
    accidentally creating duplicate live positions" and "portfolio risk is
    checked after combining all existing broker positions, not arm by arm". Both
    are properties of the context handed in, which is why it is rebuilt per
    attempt rather than cached on the decision.
    """
    if decision.action is not DecisionAction.LONG or decision.risk_plan is None:
        raise ShadowRefused(
            f"{decision.ticker}: a {decision.action.value} decision places no "
            "order; there is no execution to assess"
        )
    context_hash = context.context_hash

    def refuse(reason: str) -> EligibilityVerdict:
        return EligibilityVerdict(False, reason, context_hash)

    if context.equity <= 0:
        return refuse(BLOCK_EQUITY_UNKNOWN)
    if float(arm_risk_budget) <= 0:
        return refuse(BLOCK_NO_RISK_BUDGET)
    if decision.ticker in context.open_tickers:
        return refuse(BLOCK_TICKER_ALREADY_HELD)
    if context.n_open >= context.max_open_positions:
        return refuse(BLOCK_MAX_OPEN_POSITIONS)
    if plan.risk_per_share <= 0:
        return refuse(BLOCK_STOP_NOT_RESOLVABLE)

    sizing = size_position(
        plan, context,
        arm_risk_budget=arm_risk_budget,
        position_risk_pct=decision.risk_plan.position_risk_pct,
    )
    if sizing.notional <= 0:
        return refuse(BLOCK_DAILY_NOTIONAL_CAP)
    return EligibilityVerdict(True, "", context_hash, sizing)


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ShadowExecution:
    """One ``strategy_trades`` row, as this module sees it."""

    execution_id: str
    trade_id: int
    arm_id: int
    decision_id: int
    ticker: str
    status: ExecutionState
    blocked_reason: str
    context_hash: str
    sizing: SizedPosition | None = None

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_reason)

    def canonical(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "arm_id": self.arm_id,
            "decision_id": self.decision_id,
            "ticker": self.ticker,
            "status": self.status.value,
            "blocked_reason": self.blocked_reason,
            "portfolio_context_hash": self.context_hash,
            "sizing": self.sizing.canonical() if self.sizing else None,
        }


def _require_shadow(session, arm_id: int):
    arm = registry.require_arm(session, arm_id)
    if ExecutionMode(arm.mode) is not ExecutionMode.SHADOW:
        raise ShadowRefused(
            f"arm {arm_id} runs in {arm.mode}; the shadow executor handles "
            "shadow arms only. A paper arm must select the Alpaca paper adapter "
            "and a live arm the promoted broker path, and an arm's mode is an "
            "immutable input rather than a setting (Spec Q §11, §12 invariant 11)."
        )
    return arm


def open_execution(
    session,
    arm_id: int,
    decision_id: int,
    plan: ResolvedExecutionPlan,
    context: PortfolioContext,
) -> ShadowExecution:
    """Judge one decision against a fresh context and record the attempt.

    A refusal is a *terminal* ``risk_rejected`` row, so the decision keeps its
    single non-terminal execution slot free for a later attempt against a
    different portfolio, and a re-run against the same context returns the
    stored refusal rather than appending a second identical one.
    """
    arm = _require_shadow(session, arm_id)
    row = registry.decision_row(session, decision_id)
    if row.arm_id != arm_id:
        raise ShadowRefused(
            f"decision {decision_id} belongs to arm {row.arm_id}, not {arm_id}"
        )
    decision = _decision_of(row)
    verdict = assess(decision, plan, context, arm_risk_budget=float(arm.risk_budget))

    if not verdict.allowed:
        trade = registry.record_execution(
            session, arm_id, decision_id,
            status=ExecutionState.RISK_REJECTED,
            portfolio_context_hash=verdict.context_hash,
            blocked_reason=verdict.blocked_reason,
        )
        log.info(
            "strategy_trade_intended",
            arm_id=arm_id, decision_id=decision_id, ticker=row.ticker,
            blocked_reason=verdict.blocked_reason, mode=arm.mode,
        )
        return ShadowExecution(
            execution_id=trade.execution_id, trade_id=trade.id, arm_id=arm_id,
            decision_id=decision_id, ticker=row.ticker,
            status=ExecutionState(trade.status), blocked_reason=trade.blocked_reason,
            context_hash=verdict.context_hash,
        )

    sizing = verdict.sizing
    trade = registry.record_execution(
        session, arm_id, decision_id,
        status=ExecutionState.PROPOSED,
        portfolio_context_hash=verdict.context_hash,
        intended_entry_price=plan.entry_reference,
        stop_price=plan.stop_price,
        target1_price=plan.target_prices[0] if len(plan.target_prices) > 0 else None,
        target2_price=plan.target_prices[1] if len(plan.target_prices) > 1 else None,
        quantity=sizing.quantity,
        notional=sizing.notional,
    )
    return ShadowExecution(
        execution_id=trade.execution_id, trade_id=trade.id, arm_id=arm_id,
        decision_id=decision_id, ticker=row.ticker,
        status=ExecutionState(trade.status), blocked_reason="",
        context_hash=verdict.context_hash, sizing=sizing,
    )


def _decision_of(row) -> StrategyDecision:
    """Rebuild the domain decision from its stored canonical JSON."""
    import json

    from strategy_lab.domain import RiskPlan

    blob = json.loads(row.decision_json or "{}")
    plan_blob = blob.get("risk_plan")
    return StrategyDecision(
        strategy_slug=blob["strategy_slug"],
        strategy_version=blob["strategy_version"],
        snapshot_hash=blob["snapshot_hash"],
        ticker=blob["ticker"],
        action=blob["action"],
        reason_codes=tuple(blob.get("reason_codes") or ()),
        signal_strength=blob.get("signal_strength"),
        confidence=blob.get("confidence"),
        blocked_reasons=tuple(blob.get("blocked_reasons") or ()),
        risk_plan=RiskPlan(
            execution_policy_version=plan_blob["execution_policy_version"],
            entry_style=plan_blob["entry_style"],
            max_hold_calendar_days=plan_blob["max_hold_calendar_days"],
            position_risk_pct=plan_blob["position_risk_pct"],
            stop_price=plan_blob.get("stop_price"),
            target_prices=tuple(plan_blob.get("target_prices") or ()),
        ) if plan_blob else None,
    )


def fill(
    session,
    execution_id: str,
    *,
    filled_entry_price: float,
    filled_at: datetime,
    quantity: float | None = None,
) -> ExecutionState:
    """Walk a shadow execution from ``proposed`` to ``protected``. Idempotent.

    Idempotent because ``registry.set_execution_state`` allows a no-op move and
    the path is walked from wherever the row already is: a re-run stops as soon
    as it reaches ``protected`` rather than replaying the hops.
    """
    row = registry.execution_row(session, execution_id)
    columns = {"filled_entry_price": float(filled_entry_price), "filled_at": naive_utc(
        filled_at, field_name="filled_at"
    )}
    if quantity is not None:
        columns["quantity"] = float(quantity)
    state = ExecutionState(row.status)
    if state in (ExecutionState.PROTECTED, ExecutionState.CLOSING, ExecutionState.CLOSED):
        return state
    for target in _FILL_PATH:
        extra = columns if target is ExecutionState.FILLED else {}
        row = registry.set_execution_state(session, execution_id, target, **extra)
    return ExecutionState(row.status)


def close(
    session,
    execution_id: str,
    *,
    exit_price: float,
    exit_reason: str,
    realized_pnl: float,
    costs: float,
    closed_at: datetime,
) -> ExecutionState:
    """Close a protected shadow position. Idempotent."""
    row = registry.execution_row(session, execution_id)
    if ExecutionState(row.status) is ExecutionState.CLOSED:
        return ExecutionState.CLOSED
    columns = {
        "exit_price": float(exit_price),
        "exit_reason": exit_reason,
        "realized_pnl": float(realized_pnl),
        "costs": float(costs),
        "closed_at": naive_utc(closed_at, field_name="closed_at"),
    }
    for target in _CLOSE_PATH:
        extra = columns if target is ExecutionState.CLOSED else {}
        row = registry.set_execution_state(session, execution_id, target, **extra)
    log.info(
        "strategy_trade_closed",
        execution_id=execution_id, exit_reason=exit_reason, realized_pnl=realized_pnl,
    )
    return ExecutionState(row.status)


def settle(
    session,
    execution: ShadowExecution,
    outcome: replay.ReplayOutcome,
) -> ExecutionState:
    """Fill and close one shadow execution from a replayed outcome.

    An **immature** outcome is filled and left open: Spec Q §9 forbids ranking a
    long-horizon strategy against a five-day one using incomplete positions, and
    the way to keep that promise is to leave the position in the state it is
    actually in rather than closing it at whatever the last bar happened to be.
    """
    if execution.blocked:
        return execution.status
    notional = execution.sizing.notional if execution.sizing else 0.0
    state = fill(
        session, execution.execution_id,
        filled_entry_price=outcome.net.entry_price,
        filled_at=snapshots.session_close_utc(outcome.net.entry_date),
        quantity=(notional / outcome.net.entry_price) if outcome.net.entry_price else None,
    )
    if not outcome.matured:
        return state
    return close(
        session, execution.execution_id,
        exit_price=outcome.net.exit_price,
        exit_reason=outcome.net.rule_fired,
        realized_pnl=notional * outcome.net_pct / 100.0,
        costs=notional * (outcome.gross_pct - outcome.net_pct) / 100.0,
        closed_at=snapshots.session_close_utc(outcome.net.exit_date),
    )


def bars_for(
    forward_bars: Mapping[str, Sequence[snapshots.SnapshotBar]], ticker: str
) -> tuple[snapshots.SnapshotBar, ...]:
    """The forward series for one name, or an empty tuple."""
    return tuple(forward_bars.get(ticker) or ())


def execute_arm(
    session,
    arm_id: int,
    version: StrategyVersion,
    snapshot: MarketSnapshot,
    decision_ids: Sequence[int],
    forward_bars: Mapping[str, Sequence[snapshots.SnapshotBar]],
    *,
    context: PortfolioContext,
    costs: replay.CostAssumptions | None = None,
) -> tuple[tuple[ShadowExecution, ...], tuple[replay.ReplayOutcome, ...]]:
    """Shadow one arm's ``long`` decisions over the bars that followed.

    The portfolio context advances as positions open, so the caps bind within a
    run the way they would in a day: the eleventh name of a ten-position
    portfolio is blocked, and the row says which cap blocked it.

    ``historical=False`` on the replay call is deliberate and is *not* a way
    around Spec Q §10. Shadow execution is a forward simulation of a decision
    that was made from a current-time snapshot; the resulting outcome carries
    ``evidence_class`` from the snapshot, and ``metrics.py`` is what keeps
    reconstructed evidence out of clean statistics. A *historical* replay run
    calls ``replay.require_clean_replay`` first, and the runner does exactly
    that when it is asked for one.
    """
    executions: list[ShadowExecution] = []
    outcomes: list[replay.ReplayOutcome] = []
    working = context
    for decision_id in decision_ids:
        row = registry.decision_row(session, decision_id)
        if row.action != DecisionAction.LONG.value:
            continue
        decision = _decision_of(row)
        bars = bars_for(forward_bars, row.ticker)
        if len(bars) < 2:
            continue
        outcome = replay.replay_decision(
            decision, version, snapshot, bars,
            costs=costs,
            stress_slippage_bps=replay.stress_levels_for(version),
            historical=False,
        )
        execution = open_execution(
            session, arm_id, decision_id, replay.plan_of(outcome), working
        )
        settle(session, execution, outcome)
        executions.append(execution)
        outcomes.append(outcome)
        if not execution.blocked and execution.sizing:
            working = working.with_position(
                row.ticker, notional=execution.sizing.notional
            )
    return tuple(executions), tuple(outcomes)
