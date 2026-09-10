"""The three normative V1 execution policies (Spec Q §7), and nothing else.

A strategy decides *whether*; a policy decides *how the position is managed once
it fills*. Keeping them apart is what lets two strategies share one exit
contract and lets the same contract be replayed by the one simulator this
repository already has.

**There is exactly one simulator.** ``backtest/simulator.py`` is it: T+1-open
entry, adverse slippage on every fill, gap-through fills at the worse open,
pessimistic same-bar stop-before-target resolution, half out at target 1 with
the stop unchanged on the remainder, and a calendar-day time exit taken at the
close of the first bar on or after ``entry_date + max_holding_days``. Spec N's
cohort engine already drives it through ``comparables/outcomes.py::PolicySpec``,
whose fields are ``(slug, stop_frac, target1_frac, target2_frac,
max_holding_days, direction)`` — fractions of the entry reference.

This module produces **exactly those fields**, so Spec N and Spec Q share one
execution-policy contract rather than growing two.
:meth:`ResolvedExecutionPlan.policy_spec_fields` returns the ``PolicySpec``
constructor keywords verbatim; ``tests/test_strategy_lab_execution_policy.py``
builds a real ``PolicySpec`` from them and asserts the simulator produces the
identical trade. The import itself stays in the test: ``strategy_lab`` does not
reach ``comparables`` or ``backtest`` (Spec Q §5,
``tests/test_strategy_lab_import_graph.py``), and a fraction tuple crossing that
boundary is a better dependency than a package import.

Why fractions of an entry *reference* rather than absolute prices on the
decision: two of the three policies anchor the stop to ATR multiples of the
**filled** entry, and at decision time the fill does not exist — entry is the
next regular-session open. So a decision carries the policy version, the entry
style and the maximum hold (``RiskPlan``), and the arithmetic resolves against
the entry bar's open at execution or replay time, which is the same moment
``comparables/outcomes.py::_replay`` resolves it.

A policy is immutable data. Changing a multiple, a horizon or a slippage
assumption is a new policy version and therefore a new strategy version
(Spec Q §3, shared rule 8) — never an edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from strategy_lab.domain import RiskPlan, StrategyLabError

__all__ = [
    "ExecutionPolicyError",
    "ExecutionPolicy",
    "ResolvedExecutionPlan",
    "POLICIES",
    "require_policy",
    "EVENT_SWING_14CAL_V1",
    "REVERSAL_5CAL_V1",
    "MOMENTUM_QUARTERLY_89CAL_V1",
    "SWINGTRADER_MEMO_TRADE_PARAMS_V1",
    "policy_manifest",
]


class ExecutionPolicyError(StrategyLabError):
    """A policy was asked for a plan it cannot produce."""


#: Stop rules. ``atr_multiple`` subtracts ``multiple x ATR`` from the entry;
#: ``pct_below_entry`` subtracts a fixed fraction. Spec Q §7 uses the first for
#: the two swing policies and the second for momentum's hard risk overlay.
STOP_ATR_MULTIPLE = "atr_multiple"
STOP_PCT_BELOW_ENTRY = "pct_below_entry"
#: The compatibility rule: the stop, targets and horizon are not derived from a
#: formula at all — they were computed by the existing pipeline and frozen into
#: the snapshot. Spec Q §7A: the composite adapter "preserves current thresholds
#: and trade-parameter logic without modification".
STOP_FROM_FROZEN_PLAN = "frozen_trade_params"

#: When a signal may be taken. Declared rather than implied so the manifest
#: hashes it and a strategy can refuse a snapshot taken at the wrong moment.
CUTOFF_COMPLETED_SESSION = "completed_regular_session"
CUTOFF_QUARTER_END_SESSION = "quarter_end_regular_session"


@dataclass(frozen=True)
class ResolvedExecutionPlan:
    """One policy resolved against an entry reference price (and an ATR).

    ``stop_price`` and the targets are absolute prices; the ``*_frac`` values
    are those prices divided by the entry reference, which is the shape the
    simulator's Spec N caller uses. ``0.0`` for a target means *no target*:
    ``backtest.simulator._target_hit`` treats a non-positive target as absent,
    which is how ``momentum_quarterly_89cal_v1`` expresses "no profit target"
    without a second code path.
    """

    policy_version: str
    entry_reference: float
    atr: float | None
    stop_price: float
    target_prices: tuple[float, ...]
    max_holding_days: int
    slippage_bps: float
    direction: str = "long"

    @property
    def risk_per_share(self) -> float:
        """``R``: the distance from the entry reference to the stop."""
        return self.entry_reference - self.stop_price

    @property
    def stop_frac(self) -> float:
        return self.stop_price / self.entry_reference

    def target_frac(self, index: int) -> float:
        if index < len(self.target_prices):
            return self.target_prices[index] / self.entry_reference
        return 0.0

    def policy_spec_fields(self) -> dict:
        """``comparables.outcomes.PolicySpec`` constructor keywords, verbatim."""
        return {
            "slug": self.policy_version,
            "stop_frac": self.stop_frac,
            "target1_frac": self.target_frac(0),
            "target2_frac": self.target_frac(1),
            "max_holding_days": self.max_holding_days,
            "direction": self.direction,
        }

    def simulator_arguments(self) -> dict:
        """``backtest.simulator.simulate_trade`` keywords, in absolute prices."""
        return {
            "direction": self.direction,
            "stop": self.stop_price,
            "target_1": self.target_prices[0] if len(self.target_prices) > 0 else 0.0,
            "target_2": self.target_prices[1] if len(self.target_prices) > 1 else 0.0,
            "max_holding_days": self.max_holding_days,
            "slippage_bps": self.slippage_bps,
        }


@dataclass(frozen=True)
class ExecutionPolicy:
    """A named, immutable exit contract (Spec Q §7 "Execution policies")."""

    version: str
    entry_style: str
    signal_cutoff_rule: str
    stop_rule: str
    #: ``None`` only for :data:`STOP_FROM_FROZEN_PLAN`, where the horizon is
    #: whatever the frozen plan recorded rather than a property of the policy.
    max_hold_calendar_days: int | None
    baseline_slippage_bps: float
    stop_atr_multiple: float | None = None
    stop_pct: float | None = None
    atr_period: int | None = None
    #: Targets as R multiples of ``entry - stop``, in order. Empty means none.
    target_r_multiples: tuple[float, ...] = ()
    #: Additional adverse-slippage levels a report must show alongside the
    #: baseline. Spec Q §7 makes these mandatory for the reversal policy.
    stress_slippage_bps: tuple[float, ...] = ()
    citation: str = ""

    def __post_init__(self) -> None:
        if self.stop_rule == STOP_FROM_FROZEN_PLAN:
            if self.max_hold_calendar_days is not None:
                raise ExecutionPolicyError(
                    f"{self.version}: a frozen plan carries its own horizon; the "
                    "policy must not also declare one"
                )
            if self.target_r_multiples or self.stop_atr_multiple or self.stop_pct:
                raise ExecutionPolicyError(
                    f"{self.version}: a frozen plan derives nothing; it may not "
                    "declare stop or target arithmetic"
                )
            return
        if self.stop_rule == STOP_ATR_MULTIPLE:
            if not self.stop_atr_multiple or not self.atr_period:
                raise ExecutionPolicyError(
                    f"{self.version}: an ATR stop needs a multiple and a period"
                )
        elif self.stop_rule == STOP_PCT_BELOW_ENTRY:
            if not self.stop_pct or not 0 < self.stop_pct < 1:
                raise ExecutionPolicyError(
                    f"{self.version}: a percentage stop needs a fraction in (0, 1)"
                )
        else:
            raise ExecutionPolicyError(f"{self.version}: unknown stop rule {self.stop_rule!r}")
        if len(self.target_r_multiples) > 2:
            raise ExecutionPolicyError(
                f"{self.version}: the simulator manages at most two targets "
                "(half out at target 1, the remainder at target 2)"
            )
        if list(self.target_r_multiples) != sorted(self.target_r_multiples):
            raise ExecutionPolicyError(f"{self.version}: targets must ascend")
        if not isinstance(self.max_hold_calendar_days, int) or self.max_hold_calendar_days < 1:
            raise ExecutionPolicyError(f"{self.version}: max_hold_calendar_days must be >= 1")

    @property
    def requires_atr(self) -> bool:
        return self.stop_rule == STOP_ATR_MULTIPLE

    @property
    def derives_its_plan(self) -> bool:
        """False when the plan is frozen in the snapshot rather than computed."""
        return self.stop_rule != STOP_FROM_FROZEN_PLAN

    def resolve(
        self, entry_reference: float, *, atr: float | None = None, slippage_bps: float | None = None
    ) -> ResolvedExecutionPlan:
        """Turn the policy into absolute prices against one entry reference.

        ``entry_reference`` is the un-slipped entry price the plan is anchored
        to — the T+1 open in replay, the fill in live. It is deliberately not
        the signal-session close: Spec Q §7 anchors every stop and target to the
        entry, and anchoring to the close would quietly move every stop by the
        overnight gap.
        """
        if not self.derives_its_plan:
            raise ExecutionPolicyError(
                f"{self.version}: this policy derives no stop or target; the plan "
                "was frozen into the snapshot by the pipeline that produced it. "
                "Use resolve_frozen()."
            )
        if not isinstance(entry_reference, (int, float)) or entry_reference <= 0:
            raise ExecutionPolicyError(
                f"{self.version}: entry_reference must be a positive price"
            )
        if self.requires_atr:
            if atr is None:
                raise ExecutionPolicyError(
                    f"{self.version}: an ATR({self.atr_period}) stop cannot be "
                    "resolved without an ATR; a strategy that cannot compute one "
                    "abstains rather than substituting a default"
                )
            if atr <= 0:
                raise ExecutionPolicyError(f"{self.version}: ATR must be positive, got {atr!r}")
            stop = entry_reference - self.stop_atr_multiple * atr
        else:
            stop = entry_reference * (1.0 - self.stop_pct)

        if stop <= 0 or stop >= entry_reference:
            raise ExecutionPolicyError(
                f"{self.version}: the stop resolved to {stop!r} against an entry "
                f"reference of {entry_reference!r}; a long stop must sit strictly "
                "between zero and the entry"
            )
        risk = entry_reference - stop
        targets = tuple(entry_reference + multiple * risk for multiple in self.target_r_multiples)
        return ResolvedExecutionPlan(
            policy_version=self.version,
            entry_reference=float(entry_reference),
            atr=float(atr) if atr is not None else None,
            stop_price=stop,
            target_prices=targets,
            max_holding_days=self.max_hold_calendar_days,
            slippage_bps=(
                self.baseline_slippage_bps if slippage_bps is None else float(slippage_bps)
            ),
        )

    def resolve_frozen(
        self,
        *,
        entry_reference: float,
        stop_price: float,
        target_prices: Sequence[float],
        max_holding_days: int,
        slippage_bps: float | None = None,
    ) -> ResolvedExecutionPlan:
        """Wrap an already-computed plan in the same shape as a derived one.

        The compatibility arm's stop, targets and horizon came from the existing
        pipeline and are frozen in the snapshot. They still have to reach the one
        simulator through the one contract, so they are validated and wrapped
        here rather than passed around as a loose dict.
        """
        if self.derives_its_plan:
            raise ExecutionPolicyError(
                f"{self.version}: this policy derives its own plan; use resolve()"
            )
        if not entry_reference or entry_reference <= 0:
            raise ExecutionPolicyError(f"{self.version}: entry_reference must be positive")
        if not stop_price or not 0 < stop_price < entry_reference:
            raise ExecutionPolicyError(
                f"{self.version}: a long stop must sit strictly between zero and "
                f"the entry; got {stop_price!r} against {entry_reference!r}"
            )
        targets = tuple(float(t) for t in target_prices)
        if any(t <= entry_reference for t in targets):
            raise ExecutionPolicyError(
                f"{self.version}: every long target must be above the entry"
            )
        if list(targets) != sorted(targets):
            raise ExecutionPolicyError(f"{self.version}: targets must ascend")
        if len(targets) > 2:
            raise ExecutionPolicyError(f"{self.version}: at most two targets")
        if not isinstance(max_holding_days, int) or max_holding_days < 1:
            raise ExecutionPolicyError(
                f"{self.version}: max_holding_days must be a whole number >= 1"
            )
        return ResolvedExecutionPlan(
            policy_version=self.version,
            entry_reference=float(entry_reference),
            atr=None,
            stop_price=float(stop_price),
            target_prices=targets,
            max_holding_days=max_holding_days,
            slippage_bps=(
                self.baseline_slippage_bps if slippage_bps is None else float(slippage_bps)
            ),
        )

    def risk_plan(
        self,
        position_risk_pct: float,
        *,
        max_hold_calendar_days: int | None = None,
        stop_price: float | None = None,
        target_prices: Sequence[float] = (),
    ) -> RiskPlan:
        """The ``RiskPlan`` a ``long`` decision under this policy carries.

        ``stop_price`` and ``target_prices`` are left unset on purpose: they
        resolve against the fill, which does not exist at decision time. The
        decision carries the policy *version*, which is what makes the exit
        reproducible — :meth:`resolve` is a pure function of that version, the
        entry reference and the ATR the snapshot already fixes.
        """
        horizon = (
            max_hold_calendar_days
            if max_hold_calendar_days is not None
            else self.max_hold_calendar_days
        )
        if horizon is None:
            raise ExecutionPolicyError(
                f"{self.version}: the horizon comes from the frozen plan and "
                "must be supplied"
            )
        return RiskPlan(
            execution_policy_version=self.version,
            entry_style=self.entry_style,
            max_hold_calendar_days=horizon,
            position_risk_pct=position_risk_pct,
            stop_price=stop_price,
            target_prices=tuple(target_prices),
        )

    def canonical(self) -> dict:
        """The manifest's view of the policy: every output-affecting value."""
        return {
            "version": self.version,
            "entry_style": self.entry_style,
            "signal_cutoff_rule": self.signal_cutoff_rule,
            "stop_rule": self.stop_rule,
            "stop_atr_multiple": self.stop_atr_multiple,
            "stop_pct": self.stop_pct,
            "atr_period": self.atr_period,
            "target_r_multiples": list(self.target_r_multiples),
            "max_hold_calendar_days": self.max_hold_calendar_days,
            "baseline_slippage_bps": self.baseline_slippage_bps,
            "stress_slippage_bps": list(self.stress_slippage_bps),
        }


# --------------------------------------------------------------------------- #
# The normative V1 policies. Values transcribed from Spec Q §7; a worker must
# not invent replacements, and any change is a new version.
# --------------------------------------------------------------------------- #

EVENT_SWING_14CAL_V1 = ExecutionPolicy(
    version="event_swing_14cal_v1",
    entry_style="first_regular_session_open_after_signal_tradable",
    signal_cutoff_rule=CUTOFF_COMPLETED_SESSION,
    stop_rule=STOP_ATR_MULTIPLE,
    stop_atr_multiple=2.0,
    atr_period=14,
    target_r_multiples=(2.0, 3.0),
    max_hold_calendar_days=14,
    baseline_slippage_bps=10.0,
    citation="Spec Q §7 'Execution policies' -> event_swing_14cal_v1",
)

REVERSAL_5CAL_V1 = ExecutionPolicy(
    version="reversal_5cal_v1",
    entry_style="next_regular_session_open_after_signal_close",
    signal_cutoff_rule=CUTOFF_COMPLETED_SESSION,
    stop_rule=STOP_ATR_MULTIPLE,
    stop_atr_multiple=1.5,
    atr_period=14,
    # One target only. The remainder keeps the original stop and leaves at the
    # time boundary, which is precisely what the simulator does when target 2
    # is absent (`RULE_T1_THEN_STOP` / `RULE_T1_THEN_TIME`).
    target_r_multiples=(1.0,),
    max_hold_calendar_days=5,
    baseline_slippage_bps=10.0,
    stress_slippage_bps=(25.0, 50.0),
    citation="Spec Q §7 'Execution policies' -> reversal_5cal_v1",
)

MOMENTUM_QUARTERLY_89CAL_V1 = ExecutionPolicy(
    version="momentum_quarterly_89cal_v1",
    entry_style="next_regular_session_open",
    signal_cutoff_rule=CUTOFF_QUARTER_END_SESSION,
    stop_rule=STOP_PCT_BELOW_ENTRY,
    stop_pct=0.10,
    # No profit target: the basket is cleared by the 89-calendar-day boundary,
    # deliberately before the next quarter's entry window.
    target_r_multiples=(),
    max_hold_calendar_days=89,
    baseline_slippage_bps=10.0,
    citation="Spec Q §7 'Execution policies' -> momentum_quarterly_89cal_v1",
)

#: The compatibility arm's exit contract: whatever the existing pipeline already
#: computed. It is a *declared* policy rather than an implicit one so that the
#: composite arm's decisions carry a versioned execution plan like every other
#: arm, and so that the manifest hashes it.
SWINGTRADER_MEMO_TRADE_PARAMS_V1 = ExecutionPolicy(
    version="swingtrader_memo_trade_params_v1",
    entry_style="memo_limit_entry_at_frozen_entry_price",
    signal_cutoff_rule=CUTOFF_COMPLETED_SESSION,
    stop_rule=STOP_FROM_FROZEN_PLAN,
    max_hold_calendar_days=None,
    baseline_slippage_bps=10.0,
    citation="Spec Q §7A swingtrader_composite_v1 compatibility adapter",
)

POLICIES: Mapping[str, ExecutionPolicy] = MappingProxyType({
    policy.version: policy
    for policy in (
        EVENT_SWING_14CAL_V1,
        REVERSAL_5CAL_V1,
        MOMENTUM_QUARTERLY_89CAL_V1,
        SWINGTRADER_MEMO_TRADE_PARAMS_V1,
    )
})


def require_policy(version: str) -> ExecutionPolicy:
    try:
        return POLICIES[version]
    except KeyError:
        raise ExecutionPolicyError(
            f"unknown execution policy {version!r}; the registered policies are "
            f"{sorted(POLICIES)}"
        ) from None


def policy_manifest() -> dict[str, Any]:
    """Every policy's canonical form, for the implementation manifest."""
    return {version: policy.canonical() for version, policy in sorted(POLICIES.items())}
