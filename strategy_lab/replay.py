"""Historical replay: the Strategy Lab's one path from a decision to a fill.

Spec Q §15 PR 3: "Must reuse ``backtest/simulator.py`` for the specified
calendar-day, intrabar-stop, target, and slippage semantics. Do not fork or
reinterpret them." So this module owns no exit logic at all. It resolves an
execution policy against the entry reference, expands the resulting fractions
back into absolute prices **exactly** the way ``comparables/outcomes.py::_replay``
does, and hands them to ``backtest.simulator.simulate_trade``. There is one
simulator, and this is a caller of it.

**Why fractions and not a ``PolicySpec``.** ``ResolvedExecutionPlan.policy_spec_fields()``
returns the ``comparables.outcomes.PolicySpec`` constructor keywords verbatim,
and ``tests/test_strategy_lab_execution_policy.py`` already proves the two ends
agree. ``strategy_lab`` may not import ``comparables``
(``tests/test_strategy_lab_import_graph.py``), so the dict crosses the boundary
instead of the class, and ``tests/test_strategy_lab_replay.py`` builds a real
``PolicySpec`` from the same dict and asserts the identical trade. Duplicating
the expansion arithmetic in a way that could drift from ``_replay`` is the
failure this arrangement exists to prevent, so the two lines that do it are
written here once and nowhere else.

**Three refusals, before any bar is touched.**

1. ``historically_replayable=False`` — the compatibility arm freezes an LLM's
   conclusion, which is not reconstructible at a historical time T (Spec Q §7A).
2. A snapshot carrying ``not_point_in_time`` or ``archival_reconstructed``
   cannot produce clean evidence (Spec Q §10). It may still be replayed *as an
   exploratory report*, which is what :func:`classify_evidence` labels and
   :func:`require_clean_replay` refuses. Today
   ``snapshot_builder.REPLAY_ELIGIBLE_PRICE_SOURCES`` is empty, so **every**
   snapshot built from stored bars is ``archival_reconstructed``: there is no
   promotion-eligible replay in this deployment, the scorecard says so, and
   nothing here routes around it.
3. An observation whose ``known_at_utc`` is after the decision cutoff is
   rejected outright (:func:`resolve_as_of`), and a revised record resolves to
   the vintage that was current *at* the cutoff rather than to the latest one.

**Which price series.** The split-adjusted series, matching
``comparables/outcomes.py::policy_bars``. Spec Q §7 asks for "executable
unadjusted OHLC for fills when available", but a hold window that spans a split
cannot mix the two conventions without inventing a return, and a second
convention would be a second set of semantics for the one simulator to
reinterpret. The choice is recorded on every outcome as ``price_basis`` so a
reader can see which series produced the number.

**Costs are explicit or absent.** :class:`CostAssumptions` has no default: a
replay run without a stated cost model produces a *gross* number that
``metrics.py`` refuses to score (Spec Q §16: "costs missing -> result blocked,
not zero-cost"). There is no silent zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Sequence

from backtest.simulator import TradeResult, simulate_trade
from strategy_lab import indicators, snapshots
from strategy_lab.domain import (
    MarketSnapshot,
    REPLAY_DISQUALIFYING_WARNINGS,
    StrategyDecision,
    StrategyLabError,
    StrategyVersion,
    naive_utc,
)
from strategy_lab.execution_policy import (
    ExecutionPolicy,
    ResolvedExecutionPlan,
    require_policy,
)
from strategy_lab.validation import ReplayRefused, guard_historical_replay

__all__ = [
    "ObservationAfterCutoff",
    "ReplayRefused",
    "InsufficientBars",
    "EVIDENCE_CLEAN",
    "EVIDENCE_EXPLORATORY",
    "PRICE_BASIS_SPLIT_ADJUSTED",
    "CostAssumptions",
    "ReplayBar",
    "ReplayOutcome",
    "classify_evidence",
    "require_clean_replay",
    "resolve_as_of",
    "reject_after_cutoff",
    "replay_bars",
    "atr_for",
    "build_plan",
    "run_policy",
    "replay_decision",
    "plan_of",
    "stress_levels_for",
]


class ObservationAfterCutoff(StrategyLabError):
    """A fact that was not knowable at the decision cutoff reached a replay."""


class InsufficientBars(StrategyLabError):
    """There is no bar after the signal, so there is no T+1 open to enter at."""


#: The two evidence classes Spec Q §10 keeps apart. They are never combined in
#: one statistic and the exploratory one can never satisfy a promotion gate.
EVIDENCE_CLEAN = "clean_replay"
EVIDENCE_EXPLORATORY = "archival_reconstructed"

#: The series every replay runs on. Recorded on the outcome, not assumed.
PRICE_BASIS_SPLIT_ADJUSTED = "split_adjusted"


# --------------------------------------------------------------------------- #
# Costs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CostAssumptions:
    """Every adverse basis point a fill pays, named and separated (Spec Q §10).

    All three are per-fill adverse price adjustments in basis points, which is
    exactly what ``backtest.simulator`` already models, so the whole model is
    applied by running the simulator at ``total_bps`` and the gross number is
    the same replay at zero.

    There is deliberately no ``default()``. A cost model is a claim about the
    market a strategy trades in; the one number that must never be inferred is
    the one that decides whether an edge survives friction.
    """

    slippage_bps: float
    half_spread_bps: float
    commission_bps: float = 0.0
    label: str = "baseline"

    def __post_init__(self) -> None:
        for name in ("slippage_bps", "half_spread_bps", "commission_bps"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise StrategyLabError(f"{name} must be a number of basis points")
            if value < 0:
                raise StrategyLabError(f"{name} must be >= 0, got {value!r}")
            object.__setattr__(self, name, float(value))

    @property
    def total_bps(self) -> float:
        return self.slippage_bps + self.half_spread_bps + self.commission_bps

    def at_slippage(self, slippage_bps: float, *, label: str) -> "CostAssumptions":
        """The same spread and commission under a different slippage stress."""
        return CostAssumptions(
            slippage_bps=slippage_bps,
            half_spread_bps=self.half_spread_bps,
            commission_bps=self.commission_bps,
            label=label,
        )

    def canonical(self) -> dict:
        return {
            "label": self.label,
            "slippage_bps": self.slippage_bps,
            "half_spread_bps": self.half_spread_bps,
            "commission_bps": self.commission_bps,
            "total_bps": self.total_bps,
        }


# --------------------------------------------------------------------------- #
# Bars
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReplayBar:
    """The shape ``backtest.simulator`` reads: ``date/open/high/low/close``.

    A distinct type rather than ``snapshots.SnapshotBar`` because the simulator
    reads one convention and the snapshot carries three series; converting once,
    here, is what stops a caller from handing raw prices to a plan anchored to
    adjusted ones.
    """

    date: date
    open: float
    high: float
    low: float
    close: float


def replay_bars(bars: Sequence[snapshots.SnapshotBar]) -> tuple[ReplayBar, ...]:
    """Split-adjusted OHLC, in order. See the module docstring on the choice."""
    out = []
    for bar in bars:
        ratio = bar.adjustment_ratio
        out.append(ReplayBar(
            date=bar.session_date,
            open=bar.raw_open * ratio,
            high=bar.raw_high * ratio,
            low=bar.raw_low * ratio,
            close=bar.raw_close * ratio,
        ))
    dates = [bar.date for bar in out]
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        raise StrategyLabError("replay bars must be strictly ascending by date")
    return tuple(out)


# --------------------------------------------------------------------------- #
# Bitemporal resolution (Spec Q §10, agent-prompt requirement 11)
# --------------------------------------------------------------------------- #


def reject_after_cutoff(
    facts: Iterable[snapshots.ObservationFact], cutoff: datetime
) -> tuple[snapshots.ObservationFact, ...]:
    """Every fact knowable at ``cutoff``, or raise naming the one that was not.

    Raising rather than filtering is the point: a replay that quietly drops a
    look-ahead fact reports a smaller sample and calls it clean. The caller that
    *wants* the filter calls :func:`resolve_as_of`, which says so in its name.
    """
    cutoff = naive_utc(cutoff, field_name="cutoff")
    kept = []
    for fact in facts:
        if fact.known_at_utc > cutoff:
            raise ObservationAfterCutoff(
                f"observation {fact.observation_id} ({fact.fact_type}) became "
                f"known at {fact.known_at_utc.isoformat()}, after the decision "
                f"cutoff {cutoff.isoformat()}; a replay that consumes it is "
                "measuring hindsight (Spec Q §10)"
            )
        kept.append(fact)
    return tuple(kept)


def resolve_as_of(
    facts: Iterable[snapshots.ObservationFact], cutoff: datetime
) -> tuple[snapshots.ObservationFact, ...]:
    """The vintage that was current at ``cutoff``, one fact per subject.

    Facts are keyed by ``(ticker, fact_type, valid_at)`` — the *subject* of the
    claim. Within a key the winner is the one with the greatest
    ``known_at_utc <= cutoff``, ties broken by the greater ``observation_id``,
    which is the row order the ledger appended them in. A later revision of the
    same subject therefore does not leak backwards: replaying 2024 sees the 2024
    vintage even though a restatement exists today (Spec Q §8
    ``source_observations``, agent-prompt requirement 11).

    Output is sorted by ``(ticker, fact_type, valid_at)`` so the result is a
    deterministic tuple regardless of input order.
    """
    cutoff = naive_utc(cutoff, field_name="cutoff")
    best: dict[tuple[str, str, datetime], snapshots.ObservationFact] = {}
    for fact in facts:
        if fact.known_at_utc > cutoff:
            continue
        key = (fact.ticker or "", fact.fact_type, fact.valid_at)
        incumbent = best.get(key)
        if incumbent is None or (
            (fact.known_at_utc, fact.observation_id)
            > (incumbent.known_at_utc, incumbent.observation_id)
        ):
            best[key] = fact
    return tuple(best[key] for key in sorted(best))


# --------------------------------------------------------------------------- #
# Evidence class
# --------------------------------------------------------------------------- #


def classify_evidence(snapshot: MarketSnapshot) -> str:
    """``clean_replay`` or ``archival_reconstructed``. Never a third thing."""
    disqualifying = set(snapshot.quality_warnings) & REPLAY_DISQUALIFYING_WARNINGS
    return EVIDENCE_EXPLORATORY if disqualifying else EVIDENCE_CLEAN


def require_clean_replay(version: StrategyVersion, snapshot: MarketSnapshot) -> None:
    """Refuse anything that cannot enter clean metrics or a promotion gate."""
    guard_historical_replay(version, snapshot)


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #


def atr_for(
    snapshot: MarketSnapshot, ticker: str, policy: ExecutionPolicy
) -> float | None:
    """Wilder ATR over the snapshot's split-adjusted bars, or ``None``.

    Deliberately recomputed from the snapshot rather than carried on the
    decision: Spec Q §6 keeps a decision's risk plan free of prices that do not
    exist until the fill, and the snapshot is frozen, so the same bars produce
    the same ATR every time this is called. "Only bars completed before entry"
    is satisfied by construction — a point-in-time snapshot has no later bar.
    """
    if not policy.requires_atr:
        return None
    bars = snapshots.bars_of(snapshot, ticker)
    if not bars:
        return None
    return indicators.wilder_atr(
        [bar.split_adjusted_high for bar in bars],
        [bar.split_adjusted_low for bar in bars],
        [bar.split_adjusted_close for bar in bars],
        policy.atr_period,
    )


def build_plan(
    version: StrategyVersion,
    snapshot: MarketSnapshot,
    ticker: str,
    *,
    entry_reference: float,
    slippage_bps: float | None = None,
) -> ResolvedExecutionPlan:
    """Resolve the version's execution policy against one entry reference.

    Handles both shapes the roster uses: a derived policy (ATR or percentage
    stop) resolves arithmetically, and the compatibility arm's frozen plan is
    lifted out of the snapshot's stored trade parameters. Either way the result
    is one :class:`ResolvedExecutionPlan`, so the simulator sees one contract.
    """
    policy = require_policy(version.execution_policy_version)
    if policy.derives_its_plan:
        return policy.resolve(
            entry_reference,
            atr=atr_for(snapshot, ticker, policy),
            slippage_bps=slippage_bps,
        )

    composite = snapshots.composite_of(snapshot, ticker)
    if composite is None:
        raise ReplayRefused(
            f"{version.identity} runs a frozen-plan policy, but snapshot "
            f"{snapshot.content_hash[:12]} carries no composite result for "
            f"{ticker}; the stop, targets and horizon were never computed"
        )
    params = dict(composite.trade_params or {})
    targets = tuple(
        float(params[key])
        for key in ("target_1", "target_2")
        if params.get(key) not in (None, 0, 0.0)
    )
    return policy.resolve_frozen(
        entry_reference=entry_reference,
        stop_price=float(params["stop_loss"]),
        target_prices=targets,
        max_holding_days=int(params["max_hold_days"]),
        slippage_bps=slippage_bps,
    )


# --------------------------------------------------------------------------- #
# The replay itself
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReplayOutcome:
    """One decision replayed, gross and net, with its maturity stated.

    ``matured`` is the field that keeps Spec Q §9 honest: a long-horizon
    strategy whose bars run out before its time boundary has an *open* position,
    not a zero-return one, and ``metrics.py`` refuses to rank it. A trade that
    stopped out or hit a target is matured whatever the data edge, because its
    exit really happened.
    """

    ticker: str
    strategy_slug: str
    strategy_version: str
    policy_version: str
    evidence_class: str
    price_basis: str
    entry_reference: float
    atr: float | None
    stop_price: float
    target_prices: tuple[float, ...]
    risk_pct: float
    max_holding_days: int
    costs: CostAssumptions | None
    gross: TradeResult
    net: TradeResult
    net_by_total_bps: tuple[tuple[float, float], ...]
    matured: bool
    maturity_boundary: date
    last_bar_date: date

    @property
    def net_pct(self) -> float:
        return self.net.pnl_pct

    @property
    def gross_pct(self) -> float:
        return self.gross.pnl_pct

    @property
    def r_multiple(self) -> float | None:
        """Net return in units of the risk the plan actually took."""
        if self.risk_pct <= 0:
            return None
        return self.net.pnl_pct / self.risk_pct

    @property
    def holding_days(self) -> int:
        return self.net.holding_days

    @property
    def stopped_out(self) -> bool:
        return self.net.rule_fired in ("stop", "t1_then_stop")

    def canonical(self) -> dict:
        return {
            "ticker": self.ticker,
            "strategy": f"{self.strategy_slug}@{self.strategy_version}",
            "policy_version": self.policy_version,
            "evidence_class": self.evidence_class,
            "price_basis": self.price_basis,
            "entry_date": self.net.entry_date.isoformat(),
            "entry_price": self.net.entry_price,
            "exit_date": self.net.exit_date.isoformat(),
            "exit_price": self.net.exit_price,
            "rule_fired": self.net.rule_fired,
            "holding_days": self.net.holding_days,
            "gross_pct": self.gross.pnl_pct,
            "net_pct": self.net.pnl_pct,
            "net_by_total_bps": [list(pair) for pair in self.net_by_total_bps],
            "risk_pct": self.risk_pct,
            "r_multiple": self.r_multiple,
            "matured": self.matured,
            "maturity_boundary": self.maturity_boundary.isoformat(),
            "last_bar_date": self.last_bar_date.isoformat(),
            "costs": self.costs.canonical() if self.costs else None,
        }


def run_policy(
    plan: ResolvedExecutionPlan,
    bars: Sequence[ReplayBar],
    signal_index: int,
    *,
    slippage_bps: float,
) -> TradeResult:
    """``simulate_trade`` through the ``PolicySpec`` fields, and nothing else.

    These four lines are the whole integration with the simulator, and they are
    a transcription of ``comparables/outcomes.py::_replay``: take the plan's
    ``PolicySpec`` keywords, multiply each fraction by the entry reference, and
    call. Keeping them in one function means "the Strategy Lab and the cohort
    engine fill the same way" is checkable by reading eight lines rather than by
    auditing every caller.
    """
    fields = plan.policy_spec_fields()
    reference = plan.entry_reference
    return simulate_trade(
        bars,
        signal_index,
        fields["direction"],
        reference * fields["stop_frac"],
        reference * fields["target1_frac"],
        reference * fields["target2_frac"],
        fields["max_holding_days"],
        slippage_bps=slippage_bps,
    )


def replay_decision(
    decision: StrategyDecision,
    version: StrategyVersion,
    snapshot: MarketSnapshot,
    forward_bars: Sequence[snapshots.SnapshotBar] | Sequence[ReplayBar],
    *,
    costs: CostAssumptions | None = None,
    stress_slippage_bps: Sequence[float] = (),
    signal_index: int = 0,
    historical: bool = True,
) -> ReplayOutcome:
    """Replay one ``long`` decision over the bars that followed the signal.

    ``forward_bars`` starts at the signal session and continues past it; the
    entry is the open of ``forward_bars[signal_index + 1]``, which is the
    simulator's T+1 rule and the reason a one-bar series is refused rather than
    entered at the signal close.

    With ``historical=True`` the Spec Q §10 guards run first, so a version that
    declares ``historically_replayable=False`` or a snapshot carrying
    ``archival_reconstructed`` never reaches a bar. A caller that wants the
    exploratory report passes ``historical=False`` and reads
    ``evidence_class`` — which is how the scorecard keeps the two apart instead
    of pretending the second kind does not exist.
    """
    if decision.risk_plan is None:
        raise ReplayRefused(
            f"{decision.ticker}: a {decision.action.value} decision places no "
            "order, so there is nothing to replay"
        )
    if historical:
        require_clean_replay(version, snapshot)

    bars = tuple(forward_bars)
    if bars and isinstance(bars[0], snapshots.SnapshotBar):
        bars = replay_bars(bars)
    if signal_index + 1 >= len(bars):
        raise InsufficientBars(
            f"{decision.ticker}: {len(bars)} bar(s) from the signal session; "
            "the simulator enters at the T+1 open, so at least two are needed"
        )

    entry_reference = bars[signal_index + 1].open
    plan = build_plan(
        version, snapshot, decision.ticker, entry_reference=entry_reference
    )
    total_bps = costs.total_bps if costs is not None else 0.0

    gross = run_policy(plan, bars, signal_index, slippage_bps=0.0)
    net = run_policy(plan, bars, signal_index, slippage_bps=total_bps)
    stresses = tuple(
        (
            float(bps) + (costs.half_spread_bps + costs.commission_bps if costs else 0.0),
            run_policy(
                plan, bars, signal_index,
                slippage_bps=float(bps)
                + (costs.half_spread_bps + costs.commission_bps if costs else 0.0),
            ).pnl_pct,
        )
        for bps in stress_slippage_bps
    )

    entry_bar_date = bars[signal_index + 1].date
    boundary = entry_bar_date + timedelta(days=plan.max_holding_days)
    ran_to_boundary = bars[-1].date >= boundary
    resolved_by_price = net.rule_fired in ("stop", "t1_then_stop", "t1_then_t2")

    risk_pct = (plan.entry_reference - plan.stop_price) / plan.entry_reference * 100.0
    return ReplayOutcome(
        ticker=decision.ticker,
        strategy_slug=decision.strategy_slug,
        strategy_version=decision.strategy_version,
        policy_version=plan.policy_version,
        evidence_class=classify_evidence(snapshot),
        price_basis=PRICE_BASIS_SPLIT_ADJUSTED,
        entry_reference=plan.entry_reference,
        atr=plan.atr,
        stop_price=plan.stop_price,
        target_prices=plan.target_prices,
        risk_pct=risk_pct,
        max_holding_days=plan.max_holding_days,
        costs=costs,
        gross=gross,
        net=net,
        net_by_total_bps=((total_bps, net.pnl_pct),) + stresses,
        matured=bool(resolved_by_price or ran_to_boundary),
        maturity_boundary=boundary,
        last_bar_date=bars[-1].date,
    )


def plan_of(outcome: "ReplayOutcome") -> ResolvedExecutionPlan:
    """Rebuild the plan a replay resolved, without recomputing its ATR.

    The outcome already carries every field of the plan, so a caller that needs
    the plan for sizing reads it back rather than re-deriving it from the
    snapshot — which would be a second chance for the two to disagree.
    """
    return ResolvedExecutionPlan(
        policy_version=outcome.policy_version,
        entry_reference=outcome.entry_reference,
        atr=outcome.atr,
        stop_price=outcome.stop_price,
        target_prices=outcome.target_prices,
        max_holding_days=outcome.max_holding_days,
        slippage_bps=outcome.costs.total_bps if outcome.costs else 0.0,
    )


def stress_levels_for(version: StrategyVersion) -> tuple[float, ...]:
    """The mandatory adverse-slippage levels the version's policy declares.

    Spec Q §7 makes 25 and 50 bps mandatory for ``reversal_5cal_v1``; reading
    them off the policy rather than hard-coding them here means a report cannot
    forget one and a new policy's levels arrive with the policy.
    """
    return require_policy(version.execution_policy_version).stress_slippage_bps


def outcomes_payload(outcomes: Sequence[ReplayOutcome]) -> list[dict]:
    """Every outcome's canonical form, sorted for a byte-stable artifact."""
    return [
        outcome.canonical()
        for outcome in sorted(
            outcomes, key=lambda o: (o.net.entry_date, o.ticker, o.strategy_slug)
        )
    ]
