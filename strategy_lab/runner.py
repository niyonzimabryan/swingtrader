"""Experiment registration and the multi-arm run (Spec Q §8, §9, §15 PR 3).

Two jobs, and the order between them is the whole point.

**Registration freezes the question.** :func:`register_experiment` stores the
pre-registration — hypothesis, primary metric, guardrails, universe, benchmarks,
data cutoff, end criteria and the planned variant count — and creates one arm
per strategy version and mode *before* a single decision exists. Spec Q §8: "No
arm may run until the experiment is ``registered``. Registration freezes the
hypothesis and analysis plan." :func:`run_snapshot` refuses an arm whose
experiment is not in a runnable state, so the ordering is a check rather than a
convention.

**A run is idempotent, and partial failure is per-arm.** :func:`run_snapshot`
records the snapshot once, then evaluates every arm against it. Each arm is
guarded independently — manifest drift, a forbidden tier, a refused historical
replay — and a refusal is recorded on that arm's result instead of aborting the
others, because an experiment in which one broken arm hides three working ones
is worse than one that says which arm broke. Re-running the same snapshot
through the same arms writes nothing new: ``registry.record_decision`` returns
the stored row for an identical decision and refuses a *different* one for the
same ``(arm, snapshot, ticker)``.

**Every action is preserved.** ``long``, ``flat`` and ``abstain`` are all
persisted, and a universe arm writes one row per constituent under the shared
snapshot id. Dropping the ``flat`` rows would make a selection rate
unrecoverable, and dropping the ``abstain`` rows would let a data outage look
like a considered decision not to trade (Spec Q §6).

**No session is opened here.** The runner takes one and hands every write to
``registry.py``, which is the only module in the package that may import
``database`` (``tests/test_strategy_lab_import_graph.py``). Nothing in this
module reaches a broker, a scheduler or a flag — wiring an arm into the
production pipeline is Phase 4's work, behind a default-off flag it owns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

from strategy_lab import registry, replay, strategies, validation
from strategy_lab.domain import (
    ArmStatus,
    DecisionAction,
    ExecutionMode,
    ExperimentSpec,
    ExperimentStatus,
    MarketSnapshot,
    StrategyDecision,
    StrategyLabError,
    StrategyVersion,
    StrategyVersionStatus,
)
from utils.logger import get_logger

log = get_logger("strategy_lab_runner")

__all__ = [
    "ArmPlan",
    "ArmRef",
    "RegisteredExperiment",
    "ArmRun",
    "RunReport",
    "VariantLedger",
    "register_experiment",
    "run_snapshot",
    "variant_ledger",
]


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ArmPlan:
    """One requested arm: a roster strategy at an immutable tier.

    ``version`` defaults to whatever the roster currently declares, which is
    what a fixture wants; naming it explicitly is what a long-running experiment
    wants, so that a later roster bump does not silently re-point an arm.
    """

    slug: str
    mode: ExecutionMode = ExecutionMode.SHADOW
    version: str | None = None
    risk_budget: float = 0.0
    activate: bool = True


@dataclass(frozen=True)
class ArmRef:
    """A created arm, reduced to what a run needs to know about it."""

    arm_id: int
    slug: str
    version: str
    mode: ExecutionMode
    risk_budget: float
    status: ArmStatus

    @property
    def identity(self) -> str:
        return f"{self.slug}@{self.version}"

    @property
    def label(self) -> str:
        return f"{self.identity}/{self.mode.value}"


@dataclass(frozen=True)
class RegisteredExperiment:
    """The frozen experiment and the arms registered under it."""

    experiment_id: int
    name: str
    status: ExperimentStatus
    content_hash: str
    planned_variants: int
    arms: tuple[ArmRef, ...]

    def arm(self, slug: str, mode: ExecutionMode | str | None = None) -> ArmRef:
        wanted = ExecutionMode(mode) if mode is not None else None
        for ref in self.arms:
            if ref.slug == slug and (wanted is None or ref.mode is wanted):
                return ref
        raise KeyError(f"no arm for {slug!r} in experiment {self.name!r}")


@dataclass(frozen=True)
class ArmRun:
    """What one arm did with one snapshot.

    ``refused`` is a sentence, not a flag: the reason an arm produced nothing is
    part of the experiment's record, and a scorecard that shows three arms and
    silently omits the fourth is misleading in exactly the direction that
    flatters the result.
    """

    arm: ArmRef
    snapshot_id: int
    decisions: tuple[StrategyDecision, ...] = ()
    decision_ids: tuple[int, ...] = ()
    decision_set_hash: str = ""
    n_long: int = 0
    n_flat: int = 0
    n_abstain: int = 0
    n_deduplicated: int = 0
    refused: str | None = None

    @property
    def ran(self) -> bool:
        return self.refused is None

    def canonical(self) -> dict:
        return {
            "arm_id": self.arm.arm_id,
            "strategy": self.arm.identity,
            "mode": self.arm.mode.value,
            "snapshot_id": self.snapshot_id,
            "decision_set_hash": self.decision_set_hash,
            "n_long": self.n_long,
            "n_flat": self.n_flat,
            "n_abstain": self.n_abstain,
            "n_deduplicated": self.n_deduplicated,
            "refused": self.refused,
        }


@dataclass(frozen=True)
class RunReport:
    """One snapshot through N arms."""

    snapshot_id: int
    snapshot_hash: str
    evidence_class: str
    historical: bool
    arm_runs: tuple[ArmRun, ...]

    def for_arm(self, arm_id: int) -> ArmRun:
        for run in self.arm_runs:
            if run.arm.arm_id == arm_id:
                return run
        raise KeyError(f"arm {arm_id} did not take part in this run")

    @property
    def refusals(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (run.arm.label, run.refused) for run in self.arm_runs if run.refused
        )

    def canonical(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "snapshot_hash": self.snapshot_hash,
            "evidence_class": self.evidence_class,
            "historical": self.historical,
            "arms": [run.canonical() for run in self.arm_runs],
        }


@dataclass(frozen=True)
class VariantLedger:
    """Every variant tried against one experiment (Spec Q §10).

    The multiple-testing denominator is not the number of arms a scorecard
    happens to display. It is every ``(strategy version, mode)`` ever created
    under the experiment — retired and paused arms included — taken against the
    pre-registered ``planned_variants``. Understating it is how a tournament
    launders luck into evidence, so the ledger reports both and
    ``metrics.py`` uses the larger.
    """

    experiment: str
    planned_variants: int
    tried: tuple[tuple[str, str], ...]

    @property
    def n_tried(self) -> int:
        return len(self.tried)

    @property
    def n_trials(self) -> int:
        return max(self.planned_variants, self.n_tried)

    @property
    def undeclared(self) -> int:
        """Variants run beyond the pre-registered plan. Zero is the good case."""
        return max(0, self.n_tried - self.planned_variants)

    def canonical(self) -> dict:
        return {
            "experiment": self.experiment,
            "planned_variants": self.planned_variants,
            "variants_tried": [list(pair) for pair in self.tried],
            "n_tried": self.n_tried,
            "n_trials": self.n_trials,
            "undeclared_variants": self.undeclared,
        }


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def _arm_ref(row, slug: str, version: str) -> ArmRef:
    return ArmRef(
        arm_id=row.id,
        slug=slug,
        version=version,
        mode=ExecutionMode(row.mode),
        risk_budget=float(row.risk_budget),
        status=ArmStatus(row.status),
    )


def register_experiment(
    session,
    spec: ExperimentSpec,
    arms: Sequence[ArmPlan],
    *,
    versions: Mapping[str, StrategyVersion] | None = None,
    status: ExperimentStatus = ExperimentStatus.REGISTERED,
) -> RegisteredExperiment:
    """Freeze the plan, register the versions, create the arms.

    ``versions`` defaults to ``strategies.build_versions()`` — every roster
    version rebuilt from the source as it is right now, which is the object the
    manifest-drift check compares against. Passing an explicit mapping is for a
    test that wants a version the roster does not contain.

    Refusals, all before any arm exists:

    * a strategy the roster does not contain;
    * a version whose immutable config makes it structurally shadow-only being
      asked for a paper or live tier (Spec Q §7D);
    * a version whose implementation manifest no longer matches what was
      registered (Spec Q §6) — including the very first registration, where the
      manifest is stored and the check is trivially satisfied;
    * more arms than the experiment pre-registered variants for, which is a
      pre-registration that no longer describes the experiment.
    """
    if not arms:
        raise StrategyLabError(
            "an experiment with no arms measures nothing; register at least one"
        )
    catalogue = dict(versions or strategies.build_versions())

    plans: list[tuple[ArmPlan, StrategyVersion]] = []
    for plan in arms:
        version = catalogue.get(plan.slug)
        if version is None:
            raise StrategyLabError(
                f"unknown strategy {plan.slug!r}; the roster is {sorted(catalogue)}"
            )
        if plan.version is not None and plan.version != version.version:
            raise StrategyLabError(
                f"{plan.slug}: arm asks for version {plan.version}, the roster "
                f"declares {version.version}. A version that is not on disk "
                "cannot be run; register the arm against the version you have."
            )
        validation.require_mode_allowed(version, plan.mode)
        plans.append((plan, version))

    if len(plans) > spec.planned_variants:
        raise StrategyLabError(
            f"experiment {spec.name!r} pre-registered {spec.planned_variants} "
            f"variant(s) but {len(plans)} arms were requested; the planned count "
            "is the multiple-testing denominator (Spec Q §10) and raising it "
            "after the fact is how a tournament launders luck into evidence"
        )

    experiment = registry.register_experiment(session, spec, status=status)

    refs: list[ArmRef] = []
    for plan, version in plans:
        registry.register_strategy_version(session, version)
        registry.verify_registered_manifest(session, version)
        row = registry.create_arm(
            session,
            spec.name,
            version.slug,
            version.version,
            plan.mode,
            risk_budget=plan.risk_budget,
        )
        if plan.activate and ArmStatus(row.status) is not ArmStatus.ACTIVE:
            row = registry.activate_arm(session, row.id)
        refs.append(_arm_ref(row, version.slug, version.version))

    row = registry.require_experiment(session, spec.name)
    log.info(
        "experiment_arm_started",
        experiment=spec.name,
        arms=[ref.label for ref in refs],
        planned_variants=spec.planned_variants,
    )
    return RegisteredExperiment(
        experiment_id=row.id,
        name=row.name,
        status=ExperimentStatus(row.status),
        content_hash=row.content_hash,
        planned_variants=row.planned_variants,
        arms=tuple(refs),
    )


def variant_ledger(session, experiment_name: str) -> VariantLedger:
    """Every arm ever created under the experiment, whatever its status."""
    experiment = registry.require_experiment(session, experiment_name)
    tried: list[tuple[str, str]] = []
    for arm in registry.arms_for_experiment(session, experiment_name):
        version = registry.strategy_version_for_arm(session, arm.id)
        tried.append((f"{version.slug}@{version.version}", arm.mode))
    return VariantLedger(
        experiment=experiment_name,
        planned_variants=int(experiment.planned_variants),
        tried=tuple(sorted(tried)),
    )


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #


def _guard_arm(
    session,
    arm_row,
    version: StrategyVersion,
    snapshot: MarketSnapshot,
    *,
    historical: bool,
) -> None:
    """Every check that must pass before a strategy sees the snapshot."""
    experiment = registry.experiment_for_arm(session, arm_row.id)
    if ExperimentStatus(experiment.status) not in registry.RUNNABLE_EXPERIMENT_STATUSES:
        raise StrategyLabError(
            f"experiment {experiment.name!r} is {experiment.status}; no arm may "
            "run until it is registered (Spec Q §8)"
        )
    if ArmStatus(arm_row.status) is not ArmStatus.ACTIVE:
        raise StrategyLabError(
            f"arm {arm_row.id} is {arm_row.status}; only an active arm records "
            "decisions"
        )
    validation.require_mode_allowed(version, ExecutionMode(arm_row.mode))
    registry.verify_registered_manifest(session, version)
    if historical:
        replay.require_clean_replay(version, snapshot)


def run_snapshot(
    session,
    snapshot: MarketSnapshot,
    arm_ids: Sequence[int],
    *,
    versions: Mapping[str, StrategyVersion] | None = None,
    historical: bool = False,
) -> RunReport:
    """Run every arm over one snapshot, idempotently.

    The snapshot is recorded once and shared: two arms evaluating the same
    rebalance reference one ``market_snapshots`` row, which is what makes a
    cross-sectional rank reproducible and what lets a scorecard say "the same
    opportunity set" and mean it (Spec Q §6, §10).

    Returns a :class:`RunReport` whose arm results are ordered by ``arm_id``, so
    the same inputs produce the same report and a scorecard built from it is
    byte-stable.
    """
    if not isinstance(snapshot, MarketSnapshot):
        raise StrategyLabError("run_snapshot takes a domain MarketSnapshot")
    catalogue = dict(versions or strategies.build_versions())
    snapshot_row = registry.record_snapshot(session, snapshot)

    runs: list[ArmRun] = []
    for arm_id in sorted(set(int(a) for a in arm_ids)):
        arm_row = registry.require_arm(session, arm_id)
        version_row = registry.strategy_version_for_arm(session, arm_id)
        ref = _arm_ref(arm_row, version_row.slug, version_row.version)
        version = catalogue.get(version_row.slug)
        if version is None:
            runs.append(ArmRun(
                arm=ref, snapshot_id=snapshot_row.id,
                refused=(
                    f"{version_row.slug} is registered but is not on the roster; "
                    "its implementation cannot be rebuilt from source, so it "
                    "cannot be run"
                ),
            ))
            continue
        try:
            _guard_arm(session, arm_row, version, snapshot, historical=historical)
            strategy = strategies.get_strategy(version_row.slug)
            drafts = strategy.evaluate(snapshot)
            decisions = validation.validate_decision_set(snapshot, version, drafts)
        except StrategyLabError as exc:
            log.info(
                "strategy_decision_abstained",
                arm_id=arm_id, strategy=ref.identity, reason=str(exc),
            )
            runs.append(ArmRun(arm=ref, snapshot_id=snapshot_row.id, refused=str(exc)))
            continue
        except ValueError as exc:
            # A scope mismatch (a ticker strategy handed a universe snapshot)
            # is the strategy refusing, not the runner failing.
            runs.append(ArmRun(arm=ref, snapshot_id=snapshot_row.id, refused=str(exc)))
            continue

        already = {
            row.ticker
            for row in registry.decisions_for(session, arm_id, snapshot_row.id)
        }
        ids: list[int] = []
        for decision in decisions:
            row = registry.record_decision(session, arm_id, snapshot_row.id, decision)
            ids.append(row.id)

        runs.append(ArmRun(
            arm=ref,
            snapshot_id=snapshot_row.id,
            decisions=decisions,
            decision_ids=tuple(ids),
            decision_set_hash=validation.decision_set_hash(snapshot, version, decisions),
            n_long=sum(1 for d in decisions if d.action is DecisionAction.LONG),
            n_flat=sum(1 for d in decisions if d.action is DecisionAction.FLAT),
            n_abstain=sum(1 for d in decisions if d.action is DecisionAction.ABSTAIN),
            n_deduplicated=sum(1 for d in decisions if d.ticker in already),
        ))

    return RunReport(
        snapshot_id=snapshot_row.id,
        snapshot_hash=snapshot.content_hash,
        evidence_class=replay.classify_evidence(snapshot),
        historical=historical,
        arm_runs=tuple(runs),
    )


def start_experiment(session, name: str) -> None:
    """Move a registered experiment to ``running``. Idempotent."""
    row = registry.require_experiment(session, name)
    if ExperimentStatus(row.status) is ExperimentStatus.REGISTERED:
        registry.set_experiment_status(session, name, ExperimentStatus.RUNNING)


def promote_versions_to_shadow(
    session, versions: Mapping[str, StrategyVersion]
) -> None:
    """Move every draft version to ``shadow``, re-checking the manifest first.

    Spec Q §6 requires the manifest recomputation "on activation and before
    every run"; ``registry.activate_strategy_version`` is that activation path,
    and this is the loop a fixture or a Phase 4 job runs once per roster.
    """
    for version in versions.values():
        row = registry.require_strategy_version(session, version.slug, version.version)
        if StrategyVersionStatus(row.status) is StrategyVersionStatus.DRAFT:
            registry.activate_strategy_version(
                session, version, StrategyVersionStatus.SHADOW
            )


def cutoff_of(snapshot: MarketSnapshot) -> datetime:
    """The decision cutoff a bitemporal check runs against."""
    return snapshot.data_cutoff_utc
