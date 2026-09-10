"""The paper tournament: eligible paper arms, dispatched through PR 5 (Spec Q §11, §12).

PR 4 wired the shadow pass; this is the same shape one tier up, and the
difference is the whole point of the tier. A shadow arm's fill is simulated by
``strategy_lab/shadow.py``, which cannot import a broker at all. A paper arm's
order is *placed*, at Alpaca paper, through
:class:`execution.strategy_lifecycle.StrategyExecutionService` — PR 5's
explicit-mode entry point on Phase 6's ``ExecutionService``. Nothing here
re-implements a placement, a risk check, a reservation or a state transition.

Four properties are worth stating, because each one is a rule rather than an
implementation detail:

**The mode is carried and the venue follows from it.** Every request this module
builds names ``mode=paper``, and ``bind_adapter`` then allows exactly one venue —
``alpaca_paper``. A paper arm reaches Alpaca paper when ``EXECUTION_MODE=live``,
when ``BROKER_PRIMARY=robinhood``, and when both; the global settings are not
consulted, and a mismatch is refused before broker review or placement (Spec Q
§11, §12 invariant 11). ``tests/test_strategy_lab_e2e.py`` proves the live
adapter sees zero calls in exactly that configuration.

**The budget is virtual and independent.** Sizing is judged first against the
arm's own paper book — ``STRATEGY_LAB_PAPER_*`` — through PR 3's pure
:func:`strategy_lab.shadow.assess`, which is where the max-open-positions,
position-fraction, daily-notional and ticker-already-held rules already live. The
production ledger is *not* that budget; it is a ceiling Phase 6 applies on top,
and it can only make an order smaller.

**No duplicate orders, at three levels.** Per decision, the partial unique index
on ``strategy_trades.decision_id`` holds it in the database. Per ticker, a name
with a non-terminal execution in the same mode is reserved, so two arms cannot
open the same position twice (Spec Q §11). Per pass, the dispatcher re-reads the
book between proposals so the caps bind *within* a run the way they would across
a day.

**A stale snapshot abstains.** The entry reference is the decision's own snapshot
close, so the snapshot's age is the quote's age (Spec Q §12 invariant 7). Past
``STRATEGY_LAB_PAPER_MAX_SNAPSHOT_AGE_MINUTES`` the decision is skipped with a
reason rather than priced off a number nobody should trade on.

Like the shadow pass, :func:`dispatch_for_scan` never raises: it is called from a
scan that has already delivered its memos, and an experiment must not take the
production pipeline down (Spec Q §14).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Sequence

from orchestrator import strategy_lab_shadow as sl
from utils.logger import get_logger
from utils.timeutils import utcnow_naive

log = get_logger("strategy_lab_paper")

#: Skip reasons, stable strings so a log query can count them.
SKIP_LAB_DISABLED = "strategy_lab_disabled"
SKIP_PAPER_DISABLED = "strategy_lab_paper_disabled"
SKIP_PHASE6_DISABLED = "phase6_execution_disabled"
SKIP_NO_ADAPTER = "no_alpaca_paper_adapter"
SKIP_NO_ARMS = "no_active_paper_arms"

#: Per-decision skip reasons.
SKIP_ALREADY_EXECUTED = "already_has_execution"
SKIP_NO_RISK_PLAN = "decision_has_no_risk_plan"
SKIP_NO_ENTRY_REFERENCE = "no_entry_reference_in_snapshot"
SKIP_STALE_SNAPSHOT = "stale_snapshot"
SKIP_TICKER_RESERVED = "ticker_reserved_by_another_arm"
SKIP_PLAN_UNRESOLVABLE = "execution_plan_unresolvable"
SKIP_RUN_CAP = "dispatch_cap_reached"


def paper_enabled(settings) -> bool:
    """Both gates: the master switch and the paper switch (Spec Q §14)."""
    return bool(getattr(settings, "strategy_lab_enabled", False)) and bool(
        getattr(settings, "strategy_lab_paper_enabled", False)
    )


def _int(settings, name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


def _float(settings, name: str, default: float) -> float:
    try:
        return float(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class PaperDispatchSummary:
    """What one dispatch pass did, in countable terms."""

    experiment: str = ""
    enabled: bool = False
    skipped_reason: str = ""
    arms: int = 0
    candidates: int = 0
    proposed: int = 0
    blocked: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    executions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ran(self) -> bool:
        return self.enabled and not self.skipped_reason

    def as_log_fields(self) -> dict:
        return {
            "experiment": self.experiment,
            "enabled": self.enabled,
            "skipped_reason": self.skipped_reason,
            "arms": self.arms,
            "candidates": self.candidates,
            "proposed": self.proposed,
            "blocked": self.blocked,
            "skipped": len(self.skipped),
            "errors": len(self.errors),
        }


# --------------------------------------------------------------------------- #
# The arms and the virtual book
# --------------------------------------------------------------------------- #


def active_paper_arms(session, settings) -> tuple:
    """``(arm_id, slug, version)`` for every active paper arm, in a stable order.

    Paper arms exist only because an owner promoted one: there is no code path
    that creates a paper arm, and :func:`orchestrator.strategy_lab_shadow.arm_plans`
    produces shadow arms exclusively. So an empty tuple here is the normal state
    and not a misconfiguration.
    """
    from strategy_lab import registry
    from strategy_lab.domain import ArmStatus, ExecutionMode

    out = []
    for arm in registry.arms_for_experiment(
        session, sl._experiment_name(settings), statuses=(ArmStatus.ACTIVE,)
    ):
        if ExecutionMode(arm.mode) is not ExecutionMode.PAPER:
            continue
        version = registry.strategy_version_for_arm(session, arm.id)
        out.append((arm.id, version.slug, version.version))
    return tuple(sorted(out))


def paper_context(settings, *, as_of: datetime, open_tickers: Sequence[str] = (),
                  daily_notional_used: float = 0.0):
    """The paper book's virtual context (Spec Q §11).

    Separate settings from the shadow book, and separate from the production
    ledger: a paper arm's hypothetical performance must not change because a live
    position was opened elsewhere, and a production drawdown must not silently
    resize the tournament.
    """
    from strategy_lab.shadow import PortfolioContext

    return PortfolioContext(
        as_of_utc=as_of,
        equity=_float(settings, "strategy_lab_paper_equity", 100_000.0),
        open_tickers=tuple(open_tickers),
        daily_notional_used=float(daily_notional_used),
        max_open_positions=_int(settings, "strategy_lab_paper_max_open_positions", 5),
        max_position_fraction=_float(
            settings, "strategy_lab_paper_max_position_fraction", 0.1
        ),
        max_daily_notional=_float(settings, "strategy_lab_paper_daily_notional", 0.0),
    )


def reserved_tickers(session, *, mode: str) -> tuple[str, ...]:
    """Names already carrying a non-terminal execution in ``mode``, across arms.

    Spec Q §11's ticker-level exposure reservation. It is read across every arm
    in the mode rather than per arm on purpose: the failure it prevents is two
    *different* arms opening the same position, which a per-arm check cannot see.
    Modes do not reserve against each other — a paper position at Alpaca and a
    live position at Robinhood are different books, and treating them as one
    would make the tournament's exposure depend on the champion's.
    """
    from database import models
    from strategy_lab import registry
    from strategy_lab.domain import TERMINAL_EXECUTION_STATES

    terminal = {state.value for state in TERMINAL_EXECUTION_STATES}
    rows = (
        session.query(models.StrategyTrade)
        .filter(models.StrategyTrade.mode == mode)
        .filter(models.StrategyTrade.status.notin_(sorted(terminal)))
        .all()
    )
    tickers: set[str] = set()
    for row in rows:
        try:
            tickers.add(registry.decision_row(session, row.decision_id).ticker)
        except registry.NotFound:  # pragma: no cover - a foreign key prevents it
            continue
    return tuple(sorted(tickers))


def arm_open_tickers(session, arm_id: int) -> tuple[str, ...]:
    """The names *this* arm still holds, for its own virtual caps."""
    return sl._open_tickers(session, arm_id)


def arm_notional_today(session, arm_id: int, *, on_day: datetime) -> float:
    """Notional this arm has committed today, counted from rows that hold it.

    ``holds_reservation`` is the same predicate the daily counter uses, so a
    terminal row releases here exactly when it releases there rather than on a
    second rule that could disagree (Spec Q §12 invariant 12).
    """
    from strategy_lab import registry
    from strategy_lab.execution import holds_reservation

    start = datetime(on_day.year, on_day.month, on_day.day)
    end = start + timedelta(days=1)
    total = 0.0
    for row in registry.executions_for_arm(session, arm_id):
        created = row.created_at
        if created is None or not (start <= created < end):
            continue
        if holds_reservation(row.status):
            total += float(row.notional or 0.0)
    return total


# --------------------------------------------------------------------------- #
# One decision
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Candidate:
    """A `long` decision that has no execution yet, with its resolved plan.

    ``decision`` is the rebuilt domain object rather than the row, so the sizing
    gate below can be the same pure function PR 3 already uses and the row stays
    behind ``registry.py``.
    """

    arm_id: int
    decision_id: int
    ticker: str
    entry: float
    stop: float
    target_prices: tuple[float, ...]
    max_holding_days: int
    position_risk_pct: float
    snapshot_as_of: datetime
    decision: object = None


def candidates_for_arm(
    session,
    settings,
    arm_id: int,
    *,
    now: datetime,
    skipped: list[tuple[str, str]] | None = None,
) -> tuple[Candidate, ...]:
    """Every dispatchable `long` decision for one arm, oldest snapshot first.

    A decision is dispatchable when it has a risk plan, has no ``strategy_trades``
    row at all, sits on a snapshot fresh enough to price against, and resolves
    through the one execution-policy contract. Each refusal is recorded with its
    reason rather than dropped, because "the arm proposed nothing today" and "the
    arm had nothing to propose" are different facts.
    """
    from strategy_lab import registry, replay, shadow, snapshots

    skipped = skipped if skipped is not None else []
    version = registry.load_strategy_version(
        registry.strategy_version_for_arm(session, arm_id)
    )
    max_age = timedelta(minutes=_int(settings, "strategy_lab_paper_max_snapshot_age_minutes", 90))

    out: list[Candidate] = []
    for row in registry.decisions_for_arm(session, arm_id, actions=("long",)):
        label = f"arm:{arm_id}/decision:{row.id}/{row.ticker}"
        if sl._has_any_execution(session, row.id):
            skipped.append((label, SKIP_ALREADY_EXECUTED))
            continue
        decision = shadow.decision_of(row)
        if decision.risk_plan is None:
            skipped.append((label, SKIP_NO_RISK_PLAN))
            continue
        snapshot = registry.load_snapshot(registry.snapshot_row(session, row.snapshot_id))
        if now - snapshot.as_of_utc > max_age:
            skipped.append((label, SKIP_STALE_SNAPSHOT))
            continue
        bars = snapshots.bars_of(snapshot, row.ticker)
        if not bars:
            skipped.append((label, SKIP_NO_ENTRY_REFERENCE))
            continue
        # The split-adjusted close of the last bar in the snapshot, which is the
        # series every signal, policy and replay in this package runs on (Spec N
        # §4.3). For the most recent session the forward split factor is 1, so it
        # is also the raw last trade — and anchoring the plan and the order to the
        # same number is what keeps the stop a fixed distance from the entry
        # rather than a fixed distance from a differently-adjusted price.
        entry_reference = float(bars[-1].split_adjusted_close)
        if entry_reference <= 0:
            skipped.append((label, SKIP_NO_ENTRY_REFERENCE))
            continue
        try:
            plan = replay.build_plan(
                version, snapshot, row.ticker, entry_reference=entry_reference
            )
        except Exception as exc:
            skipped.append((label, f"{SKIP_PLAN_UNRESOLVABLE}: {str(exc)[:120]}"))
            continue
        out.append(
            Candidate(
                arm_id=arm_id,
                decision_id=row.id,
                ticker=row.ticker,
                entry=plan.entry_reference,
                stop=plan.stop_price,
                target_prices=plan.target_prices,
                max_holding_days=plan.max_holding_days,
                position_risk_pct=float(decision.risk_plan.position_risk_pct),
                snapshot_as_of=snapshot.as_of_utc,
                decision=decision,
            )
        )
    return tuple(out)


# --------------------------------------------------------------------------- #
# The pass
# --------------------------------------------------------------------------- #


def build_service(settings, *, adapters, pager=None, owner_id: str = "", resolver=None):
    """PR 5's explicit-mode entry point, with the paper adapter registered.

    Importing :mod:`execution.strategy_lifecycle` here rather than at module
    scope keeps the import cost (and the broker import graph) behind the flag:
    with ``STRATEGY_LAB_PAPER_ENABLED`` false this module is never asked for a
    service and never reaches ``execution/``.
    """
    from execution.strategy_lifecycle import StrategyExecutionService

    return StrategyExecutionService(
        session_factory=_session_factory(),
        settings=settings,
        adapters=adapters,
        pager=pager,
        resolver=resolver,
        owner_id=str(owner_id or ""),
    )


def _session_factory():
    from database.db import get_session

    return get_session


def _preflight(settings, summary: PaperDispatchSummary, *, adapters) -> bool:
    from strategy_lab.execution import PAPER_VENUE

    if not bool(getattr(settings, "strategy_lab_enabled", False)):
        summary.skipped_reason = SKIP_LAB_DISABLED
        return False
    if not bool(getattr(settings, "strategy_lab_paper_enabled", False)):
        summary.skipped_reason = SKIP_PAPER_DISABLED
        return False
    summary.enabled = True
    if not bool(getattr(settings, "phase6_execution_enabled", False)):
        summary.skipped_reason = SKIP_PHASE6_DISABLED
        return False
    if (adapters or {}).get(PAPER_VENUE) is None:
        summary.skipped_reason = SKIP_NO_ADAPTER
        return False
    return True


def dispatch_for_scan(
    settings,
    *,
    adapters,
    run_id: str = "",
    now: datetime | None = None,
    pager=None,
    owner_id: str = "",
    resolver=None,
    service=None,
) -> PaperDispatchSummary:
    """Propose an execution for every eligible paper decision. Never raises.

    Each proposal is a ``proposed`` ``strategy_trades`` row plus a Phase 6
    approval card. **Nothing is placed here**: the card carries a signed,
    expiring, single-use owner reference, and the placement happens only when the
    owner taps Approve and ``StrategyExecutionService.on_approval`` runs (Spec L
    §6.3, Spec Q §13). A dispatch pass that proposes five executions has placed
    zero orders.
    """
    summary = PaperDispatchSummary(experiment=sl._experiment_name(settings))
    if not _preflight(settings, summary, adapters=adapters):
        log.info("strategy_lab_paper_funnel", run_id=run_id, **summary.as_log_fields())
        return summary

    now = now or utcnow_naive()
    try:
        service = service or build_service(
            settings, adapters=adapters, pager=pager, owner_id=owner_id, resolver=resolver
        )
        _dispatch(settings, summary, service=service, now=now)
    except Exception as exc:  # pragma: no cover - defence in depth
        summary.errors.append(f"{type(exc).__name__}: {exc}")
        log.error("strategy_lab_paper_failed", error=str(exc)[:300])

    log.info("strategy_lab_paper_funnel", run_id=run_id, **summary.as_log_fields())
    return summary


def _dispatch(settings, summary: PaperDispatchSummary, *, service, now: datetime) -> None:
    from database.db import get_session
    from execution.strategy_lifecycle import ArmExecutionRefused, ArmExecutionRequest
    from strategy_lab import registry, shadow
    from strategy_lab.domain import ExecutionMode

    cap = max(0, _int(settings, "strategy_lab_paper_max_proposals_per_run", 5))
    with get_session() as session:
        arms = active_paper_arms(session, settings)
        summary.arms = len(arms)
        if not arms:
            summary.skipped_reason = SKIP_NO_ARMS
            return
        experiment = sl._experiment_name(settings)
        plan: list[tuple[Candidate, float, str]] = []
        reserved = set(reserved_tickers(session, mode=ExecutionMode.PAPER.value))

        for arm_id, slug, version in arms:
            # The arm's own immutable budget, never a global setting.
            budget = float(registry.require_arm(session, arm_id).risk_budget or 0.0)
            open_tickers = list(arm_open_tickers(session, arm_id))
            used = arm_notional_today(session, arm_id, on_day=now)
            for candidate in candidates_for_arm(
                session, settings, arm_id, now=now, skipped=summary.skipped
            ):
                label = f"arm:{arm_id}/decision:{candidate.decision_id}/{candidate.ticker}"
                summary.candidates += 1
                if len(plan) >= cap:
                    summary.skipped.append((label, SKIP_RUN_CAP))
                    continue
                if candidate.ticker in reserved:
                    summary.skipped.append((label, SKIP_TICKER_RESERVED))
                    continue
                context = paper_context(
                    settings,
                    as_of=now,
                    open_tickers=open_tickers,
                    daily_notional_used=used,
                )
                verdict = shadow.assess(
                    candidate.decision,
                    _resolved_plan(candidate),
                    context,
                    arm_risk_budget=budget,
                )
                if not verdict.allowed:
                    summary.skipped.append((label, f"virtual_budget:{verdict.blocked_reason}"))
                    continue
                sizing = verdict.sizing
                risk_fraction = budget * candidate.position_risk_pct
                tag = f"lab:{experiment}/arm:{arm_id}/{slug}@{version}"
                plan.append((candidate, risk_fraction, tag))
                reserved.add(candidate.ticker)
                open_tickers.append(candidate.ticker)
                used += float(sizing.notional if sizing else 0.0)

    # The proposals are made outside the read session: `propose` opens and commits
    # its own, and holding a second one across it is how a SQLite writer lock
    # turns a dispatch pass into a deadlock.
    for candidate, risk_fraction, tag in plan:
        request = ArmExecutionRequest(
            arm_id=candidate.arm_id,
            decision_id=candidate.decision_id,
            mode=ExecutionMode.PAPER,
            ticker=candidate.ticker,
            entry=candidate.entry,
            stop=candidate.stop,
            risk_fraction=risk_fraction,
            expected_hold_sessions=candidate.max_holding_days,
            target1_price=candidate.target_prices[0] if candidate.target_prices else None,
            target2_price=(
                candidate.target_prices[1] if len(candidate.target_prices) > 1 else None
            ),
            experiment_tag=tag,
        )
        label = f"arm:{candidate.arm_id}/decision:{candidate.decision_id}/{candidate.ticker}"
        try:
            card = service.propose(request, now=now)
        except ArmExecutionRefused as exc:
            summary.blocked += 1
            summary.skipped.append((label, f"refused:{exc.code}"))
            continue
        except Exception as exc:
            summary.errors.append(f"{label}: {type(exc).__name__}: {exc}")
            continue
        if card.blocked_reason:
            summary.blocked += 1
            summary.skipped.append((label, f"blocked:{card.blocked_reason}"))
            continue
        summary.proposed += 1
        summary.executions.append(card.execution_id)
        log.info(
            "strategy_trade_intended",
            arm_id=candidate.arm_id,
            decision_id=candidate.decision_id,
            execution_id=card.execution_id,
            ticker=candidate.ticker,
            mode=ExecutionMode.PAPER.value,
            status=card.status,
        )


def _resolved_plan(candidate: Candidate):
    """The candidate's plan, back in the shape :func:`shadow.assess` sizes against."""
    from strategy_lab.execution_policy import ResolvedExecutionPlan

    return ResolvedExecutionPlan(
        policy_version="paper_dispatch",
        entry_reference=candidate.entry,
        atr=None,
        stop_price=candidate.stop,
        target_prices=candidate.target_prices,
        max_holding_days=candidate.max_holding_days,
        slippage_bps=0.0,
    )


# --------------------------------------------------------------------------- #
# The three scheduled jobs PR 5 deferred (Spec Q §12 invariant 5, §15 PR 6)
# --------------------------------------------------------------------------- #


def resume_executions(settings, *, adapters, service=None) -> list[dict]:
    """Resolve every non-terminal execution from the broker's own answer.

    PR 5 wrote :meth:`StrategyExecutionService.resume` and deliberately did not
    schedule it, because the flag that would gate the schedule did not exist.
    It does now. Two rules the pass never breaks, both asserted by PR 5's tests:
    no entry order is ever placed, and a read that fails is not an answer.
    """
    if not paper_enabled(settings):
        return []
    service = service or build_service(settings, adapters=adapters)
    return [action.as_dict() for action in service.resume()]


def expire_stale_approvals(settings, *, adapters, service=None) -> list[str]:
    """Expire `proposed` executions whose approval reference has lapsed.

    An expired card cannot be used, so without this the row sits non-terminal
    forever holding its decision's one open-execution slot. Expiry is terminal
    and releases nothing, because a ``proposed`` row reserved nothing.
    """
    if not paper_enabled(settings):
        return []
    service = service or build_service(settings, adapters=adapters)
    return list(service.expire_stale())


def reconcile(settings, *, adapters, mode=None, service=None):
    """Compare the paper execution ledger against the broker, and fail closed.

    Every mismatch moves its execution to ``reconciliation_required``, which
    blocks new entries through the existing kill-switch gate rather than a second
    switch, and pages with a recovery instruction (Spec Q §12 invariant 10).
    """
    from strategy_lab.domain import ExecutionMode

    if not paper_enabled(settings):
        return None
    service = service or build_service(settings, adapters=adapters)
    return service.reconcile(mode=mode or ExecutionMode.PAPER)
