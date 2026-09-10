"""Strategy Lab domain contracts (Spec Q §6, §8, §9, §12).

Pure stdlib. No database session, no broker, no Telegram, no LLM, no network,
and no ``config`` import: a strategy's execution mode is carried by the
immutable experiment arm it runs under and is never read from a mutable global
setting (Spec Q §11, §12 invariant 11, shared rule 13).

Three ideas do the work here.

**Content hashing.** ``StrategyVersion``, ``ExperimentSpec``, ``MarketSnapshot``
and ``StrategyDecision`` are frozen dataclasses that hash their own canonical
form, in the shape ``comparables/setup_spec.py`` established. Change a rule,
change the hash; the registry then refuses to store it under the existing
identity (Spec Q §6: "an in-place code, helper, formula, policy, or runtime
dependency change may never run under an existing version identity").

**Status is not content.** A registered version's rules are frozen forever, but
its *status* moves (`draft -> shadow -> paper -> live_eligible -> retired`). So
``content_hash`` covers the frozen fields and deliberately excludes ``status``,
which is what lets the database row change status without becoming a different
version (Spec Q agent-prompt requirement 4).

**Decisions are reproducible; execution is not part of them.** A decision hash
covers the strategy identity, the snapshot content hash, the ticker, the action
and the risk plan — and *not* the arm it ran under, portfolio state, cash, or
broker state. The same version over the same snapshot therefore produces the
same hash in shadow, paper and live, and a portfolio change can block an
execution without mutating or duplicating the decision (Spec Q §6).

The trade lifecycle vocabulary (``ExecutionState``) is transcribed from the
§12 diagram because the ``strategy_trades`` partial unique index needs to know
which states are terminal. The *service* that drives that machine is Phase 5's;
this module only states what the states are and which moves between them exist.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class StrategyLabError(ValueError):
    """A Strategy Lab contract was violated. Base class for the refusals below."""


class ImmutabilityError(StrategyLabError):
    """A frozen registered object was asked to change."""


class InvalidTransition(StrategyLabError):
    """A lifecycle state was asked to move somewhere it cannot go."""


class PromotionRefused(StrategyLabError):
    """A promotion's evidence, target arm, or tier transition does not bind."""


# --------------------------------------------------------------------------- #
# Vocabularies
# --------------------------------------------------------------------------- #


class ExecutionMode(str, Enum):
    """The tier an arm runs in. Immutable per arm (Spec Q §8, §11)."""

    SHADOW = "shadow"
    PAPER = "paper"
    LIVE = "live"


class StrategyVersionStatus(str, Enum):
    """Spec Q §6. A version's rules never change; this does."""

    DRAFT = "draft"
    SHADOW = "shadow"
    PAPER = "paper"
    LIVE_ELIGIBLE = "live_eligible"
    RETIRED = "retired"


class ExperimentStatus(str, Enum):
    """Spec Q §8. No arm may run until the experiment is ``registered``."""

    DRAFT = "draft"
    REGISTERED = "registered"
    RUNNING = "running"
    PAUSED = "paused"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ArmStatus(str, Enum):
    """An arm is created ``inactive``; promotion activates it (Spec Q §8)."""

    INACTIVE = "inactive"
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"


class DecisionAction(str, Enum):
    """Spec Q §6. V1 has no short leg.

    ``flat`` and ``abstain`` are different claims: ``flat`` means the strategy
    evaluated this constituent and did not select it, ``abstain`` means it could
    not evaluate it at all. Collapsing them would let a data outage look like a
    considered decision not to trade.
    """

    LONG = "long"
    FLAT = "flat"
    ABSTAIN = "abstain"


class SnapshotScope(str, Enum):
    """``ticker`` for one name, ``universe`` for a cross-sectional rebalance."""

    TICKER = "ticker"
    UNIVERSE = "universe"


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    LONG_SHORT = "long_short"


class PromotionKind(str, Enum):
    PROMOTION = "promotion"
    DEMOTION = "demotion"


class ExecutionState(str, Enum):
    """``strategy_trades.status`` — the Spec Q §12 state machine.

    Phase 5 implements the service. This enum exists in Phase 1 because the
    database constraint "at most one non-terminal execution per decision" has to
    name the terminal states in DDL.
    """

    PROPOSED = "proposed"
    OWNER_APPROVED = "owner_approved"
    RISK_RESERVED = "risk_reserved"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PLACEMENT_UNKNOWN = "placement_unknown"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    PROTECTION_PENDING = "protection_pending"
    PROTECTED = "protected"
    PROTECTION_FAILED = "protection_failed"
    CLOSING = "closing"
    # --- terminal ---
    OWNER_REJECTED = "owner_rejected"
    RISK_REJECTED = "risk_rejected"
    REVIEW_REJECTED = "review_rejected"
    ORDER_REJECTED = "order_rejected"
    FAILED_NO_ORDER = "failed_no_order"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    CLOSED = "closed"


#: Spec Q §12: "Every terminal path releases its notional/risk reservation
#: exactly once." ``placement_unknown`` is deliberately **not** here — an
#: unknown outcome keeps its reservation until reconciliation resolves it.
TERMINAL_EXECUTION_STATES: frozenset[ExecutionState] = frozenset({
    ExecutionState.OWNER_REJECTED,
    ExecutionState.RISK_REJECTED,
    ExecutionState.REVIEW_REJECTED,
    ExecutionState.ORDER_REJECTED,
    ExecutionState.FAILED_NO_ORDER,
    ExecutionState.CANCELLED,
    ExecutionState.EXPIRED,
    ExecutionState.CLOSED,
})


def is_terminal(state: ExecutionState | str) -> bool:
    return ExecutionState(state) in TERMINAL_EXECUTION_STATES


#: ``blocked_reasons`` on a decision covers signal generation only (Spec Q §6).
#: Portfolio, cash, and broker problems are execution concerns and belong on
#: ``strategy_trades``; if they could appear here, a decision would stop being
#: reproducible from its snapshot.
BLOCKED_REASONS: frozenset[str] = frozenset({
    "stale_data",
    "missing_dependency",
    "invalid_point_in_time_input",
})

#: ``market_snapshots.data_quality`` (Spec Q §6, §8). ``archival_reconstructed``
#: and ``not_point_in_time`` are the two that make a snapshot replay-ineligible:
#: results built on them are exploratory and can never satisfy a promotion gate
#: (Spec Q §10).
QUALITY_WARNINGS: frozenset[str] = frozenset({
    "missing",
    "stale",
    "revised",
    "not_point_in_time",
    "archival_reconstructed",
    "universe_version_unknown",
    "delisting_unknown",
})

#: A snapshot carrying either of these cannot support promotion evidence.
REPLAY_DISQUALIFYING_WARNINGS: frozenset[str] = frozenset({
    "not_point_in_time",
    "archival_reconstructed",
})


# --------------------------------------------------------------------------- #
# Transitions
# --------------------------------------------------------------------------- #

#: Spec Q §8: ``draft -> registered -> running -> evaluating -> completed``,
#: with ``paused`` hanging off ``registered``/``running`` and ``cancelled``
#: reachable until the experiment completes. Registration freezes the
#: hypothesis and the analysis plan, so there is no edge back to ``draft``.
EXPERIMENT_TRANSITIONS: Mapping[ExperimentStatus, frozenset[ExperimentStatus]] = MappingProxyType({
    ExperimentStatus.DRAFT: frozenset({ExperimentStatus.REGISTERED, ExperimentStatus.CANCELLED}),
    ExperimentStatus.REGISTERED: frozenset({
        ExperimentStatus.RUNNING, ExperimentStatus.PAUSED, ExperimentStatus.CANCELLED,
    }),
    ExperimentStatus.RUNNING: frozenset({
        ExperimentStatus.EVALUATING, ExperimentStatus.PAUSED, ExperimentStatus.CANCELLED,
    }),
    ExperimentStatus.PAUSED: frozenset({ExperimentStatus.RUNNING, ExperimentStatus.CANCELLED}),
    ExperimentStatus.EVALUATING: frozenset({
        ExperimentStatus.COMPLETED, ExperimentStatus.RUNNING, ExperimentStatus.CANCELLED,
    }),
    ExperimentStatus.COMPLETED: frozenset(),
    ExperimentStatus.CANCELLED: frozenset(),
})

#: An arm is created inactive and is activated by a promotion (Spec Q §8).
#: ``retired`` is terminal: a retired arm's mode and budget were part of an
#: experiment that is over, and reusing it would silently reuse its evidence.
ARM_TRANSITIONS: Mapping[ArmStatus, frozenset[ArmStatus]] = MappingProxyType({
    ArmStatus.INACTIVE: frozenset({ArmStatus.ACTIVE, ArmStatus.RETIRED}),
    ArmStatus.ACTIVE: frozenset({ArmStatus.PAUSED, ArmStatus.INACTIVE, ArmStatus.RETIRED}),
    ArmStatus.PAUSED: frozenset({ArmStatus.ACTIVE, ArmStatus.RETIRED}),
    ArmStatus.RETIRED: frozenset(),
})

#: Spec Q §6/§9. A version climbs one tier at a time and can always be retired
#: or demoted; it can never go back to ``draft``, because a drafted rule that
#: has produced shadow evidence is no longer a draft.
STRATEGY_VERSION_TRANSITIONS: Mapping[StrategyVersionStatus, frozenset[StrategyVersionStatus]] = MappingProxyType({
    StrategyVersionStatus.DRAFT: frozenset({
        StrategyVersionStatus.SHADOW, StrategyVersionStatus.RETIRED,
    }),
    StrategyVersionStatus.SHADOW: frozenset({
        StrategyVersionStatus.PAPER, StrategyVersionStatus.RETIRED,
    }),
    StrategyVersionStatus.PAPER: frozenset({
        StrategyVersionStatus.LIVE_ELIGIBLE, StrategyVersionStatus.SHADOW,
        StrategyVersionStatus.RETIRED,
    }),
    StrategyVersionStatus.LIVE_ELIGIBLE: frozenset({
        StrategyVersionStatus.PAPER, StrategyVersionStatus.RETIRED,
    }),
    StrategyVersionStatus.RETIRED: frozenset(),
})

#: Spec Q §12, read off the diagram. "Any state can move to
#: ``reconciliation_required``", which is added to every non-terminal row below
#: rather than written out fourteen times.
_EXECUTION_EDGES: dict[ExecutionState, set[ExecutionState]] = {
    ExecutionState.PROPOSED: {
        ExecutionState.OWNER_APPROVED, ExecutionState.OWNER_REJECTED,
        ExecutionState.RISK_REJECTED, ExecutionState.CANCELLED, ExecutionState.EXPIRED,
    },
    ExecutionState.OWNER_APPROVED: {
        ExecutionState.RISK_RESERVED, ExecutionState.REVIEW_REJECTED,
        ExecutionState.RISK_REJECTED, ExecutionState.CANCELLED, ExecutionState.EXPIRED,
        ExecutionState.FAILED_NO_ORDER,
    },
    ExecutionState.RISK_RESERVED: {
        ExecutionState.SUBMITTED, ExecutionState.REVIEW_REJECTED,
        ExecutionState.CANCELLED, ExecutionState.EXPIRED, ExecutionState.FAILED_NO_ORDER,
    },
    ExecutionState.SUBMITTED: {
        ExecutionState.ACCEPTED, ExecutionState.ORDER_REJECTED,
        ExecutionState.PLACEMENT_UNKNOWN, ExecutionState.CANCELLED,
    },
    ExecutionState.ACCEPTED: {
        ExecutionState.PARTIALLY_FILLED, ExecutionState.FILLED,
        ExecutionState.ORDER_REJECTED, ExecutionState.CANCELLED, ExecutionState.EXPIRED,
    },
    # Not terminal and it does not release its reservation: it is resolved by
    # reconciliation, which either proves no order exists or finds the order.
    ExecutionState.PLACEMENT_UNKNOWN: set(),
    ExecutionState.PARTIALLY_FILLED: {ExecutionState.FILLED, ExecutionState.PROTECTION_PENDING},
    ExecutionState.FILLED: {ExecutionState.PROTECTION_PENDING},
    ExecutionState.PROTECTION_PENDING: {
        ExecutionState.PROTECTED, ExecutionState.PROTECTION_FAILED,
    },
    ExecutionState.PROTECTED: {ExecutionState.CLOSING},
    # Pauses all new live entries; the position itself still has to be closed.
    ExecutionState.PROTECTION_FAILED: {ExecutionState.CLOSING, ExecutionState.PROTECTED},
    ExecutionState.CLOSING: {ExecutionState.CLOSED},
    # Reconciliation resolves back into the normal lifecycle, or terminally.
    ExecutionState.RECONCILIATION_REQUIRED: {
        state for state in ExecutionState if state is not ExecutionState.PROPOSED
    },
}

EXECUTION_TRANSITIONS: Mapping[ExecutionState, frozenset[ExecutionState]] = MappingProxyType({
    state: frozenset(
        _EXECUTION_EDGES.get(state, set())
        | ({ExecutionState.RECONCILIATION_REQUIRED} if state not in TERMINAL_EXECUTION_STATES else set())
    ) - {state}
    for state in ExecutionState
})


def require_transition(table: Mapping[Any, frozenset], current, proposed, *, label: str):
    """Raise :class:`InvalidTransition` unless ``current -> proposed`` is an edge.

    A no-op move (``current == proposed``) is allowed: every caller here is
    idempotent, and re-asserting the state a row is already in is how a retry
    behaves.
    """
    kind = type(next(iter(table)))
    current, proposed = kind(current), kind(proposed)
    if current == proposed:
        return proposed
    allowed = table.get(current, frozenset())
    if proposed not in allowed:
        options = sorted(state.value for state in allowed) or ["<terminal>"]
        raise InvalidTransition(
            f"{label}: {current.value} -> {proposed.value} is not a legal "
            f"transition; from {current.value} the only moves are {options}"
        )
    return proposed


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_]*$")
_TICKER = re.compile(r"^[A-Z0-9.\-]{1,20}$")


def canonical_json(payload: Any) -> str:
    """The one serialisation every hash in this package is taken over."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def sha256_of(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def naive_utc(value: datetime, *, field_name: str) -> datetime:
    """Normalise to the naive-UTC representation the database stores.

    An aware datetime is converted; a naive one is taken to be UTC already,
    which is what ``utils.timeutils.utcnow_naive`` produces everywhere else in
    this repository. Done here rather than at the database boundary so that two
    snapshots differing only in ``tzinfo`` hash identically.
    """
    if not isinstance(value, datetime):
        raise StrategyLabError(f"{field_name} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def frozen_mapping(value: Mapping[str, Any] | None, *, field_name: str) -> Mapping[str, Any]:
    """A read-only, JSON-round-tripped copy of ``value``.

    The round trip is the point: it rejects anything that cannot be persisted
    and hashed, and it normalises tuples to lists so that two spellings of the
    same configuration cannot produce two hashes.
    """
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise StrategyLabError(f"{field_name} must be a mapping, got {type(value).__name__}")
    try:
        normalised = json.loads(json.dumps(dict(value), sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise StrategyLabError(f"{field_name} must be JSON-serialisable: {exc}") from exc
    return MappingProxyType(normalised)


def _require_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StrategyLabError(f"{field_name} is required")
    return value


def _sorted_unique(values, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise StrategyLabError(f"{field_name} must be a tuple, got {type(values).__name__}")
    if any(not isinstance(v, str) or not v.strip() for v in values):
        raise StrategyLabError(f"{field_name} entries must be non-empty strings")
    if len(set(values)) != len(values):
        raise StrategyLabError(f"{field_name} must not repeat")
    if list(values) != sorted(values):
        raise StrategyLabError(f"{field_name} must be sorted ascending, for a stable hash")
    return values


# --------------------------------------------------------------------------- #
# StrategyVersion
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StrategyVersion:
    """An immutable, content-hashed strategy identity (Spec Q §6, §8).

    ``content_hash`` covers every frozen field and **not** ``status``: the rules
    are the version, the status is where the version has got to. Registering the
    same ``(slug, version)`` with any other content is refused by the registry —
    a rule change is a new version, never an edit (Spec Q §3, shared rule 8).

    ``implementation_manifest`` is Spec Q §6's transitive executable manifest:
    strategy source, output-affecting helpers, execution policy, indicator
    formulas, and pinned runtime versions. Phase 2 computes it and re-checks it
    before every run. Phase 1 requires only that it is non-empty, so a version
    cannot be registered with no record of what code produced it.
    """

    slug: str
    version: str
    hypothesis: str
    universe: str
    direction: Direction
    required_snapshot_fields: tuple[str, ...]
    execution_policy_version: str
    expected_holding_days: int
    max_data_staleness_seconds: int
    historically_replayable: bool
    replayability_reason: str
    implementation_manifest: Mapping[str, Any]
    config: Mapping[str, Any] = field(default_factory=dict)
    dependencies: tuple[str, ...] = ()
    status: StrategyVersionStatus = StrategyVersionStatus.DRAFT

    def __post_init__(self) -> None:
        _require_text(self.slug, field_name="slug")
        if not _SLUG.match(self.slug):
            raise StrategyLabError(
                f"slug {self.slug!r} must be lowercase alphanumeric with underscores"
            )
        if not isinstance(self.version, str) or not _SEMVER.match(self.version):
            raise StrategyLabError(
                f"version {self.version!r} must be a semantic version like '1.0.0'"
            )
        _require_text(self.hypothesis, field_name="hypothesis")
        _require_text(self.universe, field_name="universe")
        _require_text(self.execution_policy_version, field_name="execution_policy_version")
        _require_text(self.replayability_reason, field_name="replayability_reason")

        object.__setattr__(self, "direction", Direction(self.direction))
        if self.direction is not Direction.LONG:
            raise StrategyLabError(
                "Spec Q §3 scopes V1 to long-only US equities; a short or "
                "long/short arm needs that owner decision revisited first"
            )
        object.__setattr__(self, "status", StrategyVersionStatus(self.status))

        object.__setattr__(
            self,
            "required_snapshot_fields",
            _sorted_unique(self.required_snapshot_fields, field_name="required_snapshot_fields"),
        )
        if not self.required_snapshot_fields:
            raise StrategyLabError(
                "required_snapshot_fields must name at least one field; a "
                "strategy that declares no inputs cannot be checked for staleness"
            )
        object.__setattr__(
            self, "dependencies", _sorted_unique(self.dependencies, field_name="dependencies")
        )

        for name in ("expected_holding_days", "max_data_staleness_seconds"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise StrategyLabError(f"{name} must be a whole number")
        if self.expected_holding_days < 1:
            raise StrategyLabError("expected_holding_days must be >= 1")
        if self.max_data_staleness_seconds < 0:
            raise StrategyLabError("max_data_staleness_seconds must be >= 0")

        if not isinstance(self.historically_replayable, bool):
            raise StrategyLabError("historically_replayable must be a bool")

        object.__setattr__(
            self,
            "implementation_manifest",
            frozen_mapping(self.implementation_manifest, field_name="implementation_manifest"),
        )
        if not self.implementation_manifest:
            raise StrategyLabError(
                "implementation_manifest is required (Spec Q §6): a version "
                "registered with no record of the code that produces it cannot "
                "be checked for drift before a run"
            )
        object.__setattr__(self, "config", frozen_mapping(self.config, field_name="config"))

    # -- identity ---------------------------------------------------------- #

    def canonical(self) -> dict:
        """Everything frozen at registration. ``status`` is excluded on purpose."""
        return {
            "slug": self.slug,
            "version": self.version,
            "hypothesis": self.hypothesis,
            "universe": self.universe,
            "direction": self.direction.value,
            "required_snapshot_fields": list(self.required_snapshot_fields),
            "execution_policy_version": self.execution_policy_version,
            "expected_holding_days": self.expected_holding_days,
            "max_data_staleness_seconds": self.max_data_staleness_seconds,
            "historically_replayable": self.historically_replayable,
            "replayability_reason": self.replayability_reason,
            "implementation_manifest": dict(self.implementation_manifest),
            "config": dict(self.config),
            "dependencies": list(self.dependencies),
        }

    @property
    def content_hash(self) -> str:
        return sha256_of(self.canonical())

    @property
    def manifest_hash(self) -> str:
        """Spec Q §6: recomputed at activation and before every run (Phase 2)."""
        return sha256_of(dict(self.implementation_manifest))

    @property
    def identity(self) -> str:
        return f"{self.slug}@{self.version}"

    def with_status(self, status: StrategyVersionStatus | str) -> "StrategyVersion":
        """A copy at a new status, checked against the lifecycle.

        The content hash is unchanged by construction, which is the property the
        registry relies on when it refuses a content change but allows this one.
        """
        target = require_transition(
            STRATEGY_VERSION_TRANSITIONS, self.status, status,
            label=f"strategy version {self.identity}",
        )
        replacement = StrategyVersion(**{**self.__dict__, "status": target})
        return replacement


# --------------------------------------------------------------------------- #
# ExperimentSpec
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExperimentSpec:
    """The pre-registration (Spec Q §8, §10). Frozen when the experiment registers.

    Metrics, universe, benchmarks and end criteria are declared *before* any arm
    runs, so a disappointing result cannot be rescued by picking a different
    primary metric afterwards. ``planned_variants`` is the denominator of the
    multiple-testing diagnostic Phase 3 computes; understating it is how a
    tournament launders luck into evidence.
    """

    name: str
    hypothesis: str
    universe_spec: str
    primary_metric: str
    benchmarks: tuple[str, ...]
    end_criteria: Mapping[str, Any]
    owner: str
    planned_variants: int = 1
    guardrail_metrics: tuple[str, ...] = ()
    start_criteria: Mapping[str, Any] = field(default_factory=dict)
    preregistration: Mapping[str, Any] = field(default_factory=dict)
    data_cutoff_utc: datetime | None = None
    status: ExperimentStatus = ExperimentStatus.DRAFT

    def __post_init__(self) -> None:
        _require_text(self.name, field_name="name")
        if not _SLUG.match(self.name):
            raise StrategyLabError(
                f"name {self.name!r} must be lowercase alphanumeric with underscores"
            )
        _require_text(self.hypothesis, field_name="hypothesis")
        _require_text(self.universe_spec, field_name="universe_spec")
        _require_text(self.primary_metric, field_name="primary_metric")
        _require_text(self.owner, field_name="owner")

        object.__setattr__(self, "status", ExperimentStatus(self.status))
        object.__setattr__(
            self, "benchmarks", _sorted_unique(self.benchmarks, field_name="benchmarks")
        )
        if not self.benchmarks:
            raise StrategyLabError(
                "at least one benchmark is required (Spec Q §10): a result with "
                "nothing to compare against is not a result"
            )
        object.__setattr__(
            self,
            "guardrail_metrics",
            _sorted_unique(self.guardrail_metrics, field_name="guardrail_metrics"),
        )
        if not isinstance(self.planned_variants, int) or isinstance(self.planned_variants, bool):
            raise StrategyLabError("planned_variants must be a whole number")
        if self.planned_variants < 1:
            raise StrategyLabError("planned_variants must be >= 1")

        object.__setattr__(
            self, "end_criteria", frozen_mapping(self.end_criteria, field_name="end_criteria")
        )
        if not self.end_criteria:
            raise StrategyLabError(
                "end_criteria is required (Spec Q §9): an experiment with no "
                "stated stopping rule ends whenever its results look best"
            )
        object.__setattr__(
            self, "start_criteria", frozen_mapping(self.start_criteria, field_name="start_criteria")
        )
        object.__setattr__(
            self,
            "preregistration",
            frozen_mapping(self.preregistration, field_name="preregistration"),
        )
        if self.data_cutoff_utc is not None:
            object.__setattr__(
                self, "data_cutoff_utc",
                naive_utc(self.data_cutoff_utc, field_name="data_cutoff_utc"),
            )

    def canonical(self) -> dict:
        return {
            "name": self.name,
            "hypothesis": self.hypothesis,
            "universe_spec": self.universe_spec,
            "primary_metric": self.primary_metric,
            "benchmarks": list(self.benchmarks),
            "guardrail_metrics": list(self.guardrail_metrics),
            "end_criteria": dict(self.end_criteria),
            "start_criteria": dict(self.start_criteria),
            "preregistration": dict(self.preregistration),
            "planned_variants": self.planned_variants,
            "owner": self.owner,
            "data_cutoff_utc": self.data_cutoff_utc.isoformat() if self.data_cutoff_utc else None,
        }

    @property
    def content_hash(self) -> str:
        return sha256_of(self.canonical())


# --------------------------------------------------------------------------- #
# MarketSnapshot
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MarketSnapshot:
    """One immutable point-in-time input bundle (Spec Q §6, §8).

    A universe-scoped snapshot carries the whole constituent set under one
    cutoff, which is what makes a cross-sectional rank reproducible: Phase 2's
    momentum arm ranks every name against the same snapshot ID instead of
    assembling ranks from ticker snapshots built at different moments.

    ``source_observation_ids`` are ``source_observations.id`` values held as
    plain integers, not a foreign key. Phase 3a owns that table and Phases 3c/4
    are landing beside this one; a cross-phase foreign key would make the
    integration merge revision a real migration instead of a no-op join
    (``migrations/README.md``).
    """

    scope: SnapshotScope
    as_of_utc: datetime
    data_cutoff_utc: datetime
    provenance: Mapping[str, Any]
    ticker: str | None = None
    universe_version: str | None = None
    constituents: tuple[str, ...] = ()
    normalized_inputs: Mapping[str, Any] = field(default_factory=dict)
    source_observation_ids: tuple[int, ...] = ()
    quality_warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", SnapshotScope(self.scope))
        object.__setattr__(
            self, "as_of_utc", naive_utc(self.as_of_utc, field_name="as_of_utc")
        )
        object.__setattr__(
            self, "data_cutoff_utc",
            naive_utc(self.data_cutoff_utc, field_name="data_cutoff_utc"),
        )
        if self.data_cutoff_utc > self.as_of_utc:
            raise StrategyLabError(
                "data_cutoff_utc is after as_of_utc: a snapshot cannot contain "
                "data from after the moment it was taken"
            )

        if self.scope is SnapshotScope.TICKER:
            if not self.ticker:
                raise StrategyLabError("a ticker-scoped snapshot requires a ticker")
            if self.constituents:
                raise StrategyLabError(
                    "a ticker-scoped snapshot has no constituent set; use "
                    "scope='universe' for a cross-sectional rebalance"
                )
            object.__setattr__(self, "ticker", self.ticker.strip().upper())
            if not _TICKER.match(self.ticker):
                raise StrategyLabError(f"ticker {self.ticker!r} is not a ticker symbol")
        else:
            if self.ticker:
                raise StrategyLabError(
                    "a universe-scoped snapshot carries constituents, not one ticker"
                )
            if not self.universe_version:
                raise StrategyLabError(
                    "a universe-scoped snapshot requires universe_version: "
                    "membership at the cutoff is part of the evidence (Spec Q §7)"
                )
            constituents = tuple(t.strip().upper() for t in self.constituents)
            object.__setattr__(
                self, "constituents", _sorted_unique(constituents, field_name="constituents")
            )
            if not self.constituents:
                raise StrategyLabError("a universe-scoped snapshot requires constituents")
            for symbol in self.constituents:
                if not _TICKER.match(symbol):
                    raise StrategyLabError(f"constituent {symbol!r} is not a ticker symbol")

        object.__setattr__(
            self, "provenance", frozen_mapping(self.provenance, field_name="provenance")
        )
        if not self.provenance:
            raise StrategyLabError(
                "provenance is required (Spec Q §6): a snapshot that cannot say "
                "which providers it came from cannot be audited"
            )
        object.__setattr__(
            self,
            "normalized_inputs",
            frozen_mapping(self.normalized_inputs, field_name="normalized_inputs"),
        )

        ids = self.source_observation_ids
        if not isinstance(ids, tuple):
            raise StrategyLabError("source_observation_ids must be a tuple")
        if any((not isinstance(i, int)) or isinstance(i, bool) or i < 1 for i in ids):
            raise StrategyLabError("source_observation_ids must be positive integers")
        if len(set(ids)) != len(ids) or list(ids) != sorted(ids):
            raise StrategyLabError("source_observation_ids must be sorted and unique")

        warnings = _sorted_unique(self.quality_warnings, field_name="quality_warnings")
        unknown = sorted(set(warnings) - QUALITY_WARNINGS)
        if unknown:
            raise StrategyLabError(
                f"unknown quality warnings {unknown}; the vocabulary is "
                f"{sorted(QUALITY_WARNINGS)}"
            )
        object.__setattr__(self, "quality_warnings", warnings)

    def canonical(self) -> dict:
        return {
            "scope": self.scope.value,
            "as_of_utc": self.as_of_utc.isoformat(),
            "data_cutoff_utc": self.data_cutoff_utc.isoformat(),
            "ticker": self.ticker,
            "universe_version": self.universe_version,
            "constituents": list(self.constituents),
            "normalized_inputs": dict(self.normalized_inputs),
            "source_observation_ids": list(self.source_observation_ids),
            "provenance": dict(self.provenance),
            "quality_warnings": list(self.quality_warnings),
        }

    @property
    def content_hash(self) -> str:
        return sha256_of(self.canonical())

    @property
    def replay_eligible(self) -> bool:
        """False when any warning makes this exploratory evidence (Spec Q §10).

        A reconstructed or non-point-in-time snapshot may still be shadowed and
        reported; it can never rank a winner or satisfy a promotion gate.
        """
        return not (set(self.quality_warnings) & REPLAY_DISQUALIFYING_WARNINGS)

    def covers(self, ticker: str) -> bool:
        symbol = (ticker or "").strip().upper()
        if self.scope is SnapshotScope.TICKER:
            return symbol == self.ticker
        return symbol in self.constituents


# --------------------------------------------------------------------------- #
# StrategyDecision
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RiskPlan:
    """The versioned execution intent a ``long`` decision carries (Spec Q §6).

    It names an execution policy rather than restating one: the stop and target
    arithmetic lives in Phase 2's ``execution_policy.py`` and is applied against
    the fill, which does not exist yet at decision time. ``stop_price`` and
    ``target_prices`` are optional because a next-open entry has no price to
    anchor them to until it fills.
    """

    execution_policy_version: str
    entry_style: str
    max_hold_calendar_days: int
    position_risk_pct: float
    stop_price: float | None = None
    target_prices: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.execution_policy_version, field_name="execution_policy_version")
        _require_text(self.entry_style, field_name="entry_style")
        if not isinstance(self.max_hold_calendar_days, int) or isinstance(
            self.max_hold_calendar_days, bool
        ):
            raise StrategyLabError("max_hold_calendar_days must be a whole number")
        if self.max_hold_calendar_days < 1:
            raise StrategyLabError("max_hold_calendar_days must be >= 1")
        if not isinstance(self.position_risk_pct, (int, float)) or isinstance(
            self.position_risk_pct, bool
        ):
            raise StrategyLabError("position_risk_pct must be a number")
        if not 0 < float(self.position_risk_pct) <= 1:
            raise StrategyLabError(
                "position_risk_pct is a fraction of the arm's risk budget in (0, 1]"
            )
        object.__setattr__(self, "position_risk_pct", float(self.position_risk_pct))
        if self.stop_price is not None:
            if not isinstance(self.stop_price, (int, float)) or self.stop_price <= 0:
                raise StrategyLabError("stop_price must be a positive number when given")
            object.__setattr__(self, "stop_price", float(self.stop_price))
        targets = tuple(float(t) for t in self.target_prices)
        if any(t <= 0 for t in targets):
            raise StrategyLabError("target prices must be positive")
        if list(targets) != sorted(targets):
            raise StrategyLabError("target_prices must be ascending")
        object.__setattr__(self, "target_prices", targets)

    def canonical(self) -> dict:
        return {
            "execution_policy_version": self.execution_policy_version,
            "entry_style": self.entry_style,
            "max_hold_calendar_days": self.max_hold_calendar_days,
            "position_risk_pct": self.position_risk_pct,
            "stop_price": self.stop_price,
            "target_prices": list(self.target_prices),
        }


@dataclass(frozen=True)
class StrategyDecision:
    """What one strategy version decided about one ticker under one snapshot.

    The hash covers the strategy identity, the snapshot's content hash, the
    ticker, the action, the reason codes and the risk plan. It does **not**
    cover the arm: the same version over the same snapshot decides the same
    thing in shadow, paper and live, and Spec Q §6 requires that reproducibility
    to survive a portfolio change. Execution eligibility is recomputed from a
    fresh portfolio context on ``strategy_trades`` instead.
    """

    strategy_slug: str
    strategy_version: str
    snapshot_hash: str
    ticker: str
    action: DecisionAction
    reason_codes: tuple[str, ...]
    signal_strength: float | None = None
    confidence: float | None = None
    risk_plan: RiskPlan | None = None
    blocked_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.strategy_slug, field_name="strategy_slug")
        if not _SEMVER.match(self.strategy_version or ""):
            raise StrategyLabError("strategy_version must be a semantic version")
        if not re.fullmatch(r"[0-9a-f]{64}", self.snapshot_hash or ""):
            raise StrategyLabError("snapshot_hash must be a sha256 hex digest")

        object.__setattr__(self, "ticker", (self.ticker or "").strip().upper())
        if not _TICKER.match(self.ticker):
            raise StrategyLabError(
                "a decision needs a ticker: Spec Q §8 makes it non-null so a "
                "universe snapshot emits one decision per constituent"
            )
        object.__setattr__(self, "action", DecisionAction(self.action))
        object.__setattr__(
            self, "reason_codes", _sorted_unique(self.reason_codes, field_name="reason_codes")
        )
        if not self.reason_codes:
            raise StrategyLabError(
                "reason_codes is required (Spec Q §6): a decision states reason "
                "codes, not only prose"
            )

        for name in ("signal_strength", "confidence"):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise StrategyLabError(f"{name} must be a number in [0, 1]")
            if not 0.0 <= float(value) <= 1.0:
                raise StrategyLabError(f"{name} must be normalised into [0, 1]")
            object.__setattr__(self, name, float(value))

        blocked = _sorted_unique(self.blocked_reasons, field_name="blocked_reasons")
        unknown = sorted(set(blocked) - BLOCKED_REASONS)
        if unknown:
            raise StrategyLabError(
                f"unknown blocked_reasons {unknown}; on a decision these cover "
                f"signal generation only ({sorted(BLOCKED_REASONS)}). A portfolio "
                "or broker block belongs on strategy_trades (Spec Q §6)."
            )
        object.__setattr__(self, "blocked_reasons", blocked)

        if self.action is DecisionAction.LONG:
            if self.risk_plan is None:
                raise StrategyLabError("a long decision requires a risk plan")
            if blocked:
                raise StrategyLabError("a long decision cannot carry blocked_reasons")
            if self.signal_strength is None:
                raise StrategyLabError("a long decision requires a signal_strength")
        else:
            if self.risk_plan is not None:
                raise StrategyLabError(
                    f"a {self.action.value} decision places no order, so it "
                    "carries no risk plan"
                )
        if self.action is DecisionAction.ABSTAIN and not blocked:
            raise StrategyLabError(
                "abstain requires at least one blocked_reason: it means the "
                "strategy could not evaluate this name, which is a different "
                "claim from evaluating it and staying flat (Spec Q §6)"
            )
        if self.action is DecisionAction.FLAT and blocked:
            raise StrategyLabError(
                "flat means evaluated and not selected; a blocked input is an "
                "abstain"
            )
        if not isinstance(self.risk_plan, (RiskPlan, type(None))):
            raise StrategyLabError("risk_plan must be a RiskPlan")

    def canonical(self) -> dict:
        return {
            "strategy_slug": self.strategy_slug,
            "strategy_version": self.strategy_version,
            "snapshot_hash": self.snapshot_hash,
            "ticker": self.ticker,
            "action": self.action.value,
            "reason_codes": list(self.reason_codes),
            "signal_strength": self.signal_strength,
            "confidence": self.confidence,
            "blocked_reasons": list(self.blocked_reasons),
            "risk_plan": self.risk_plan.canonical() if self.risk_plan else None,
        }

    @property
    def decision_hash(self) -> str:
        return sha256_of(self.canonical())


# --------------------------------------------------------------------------- #
# Promotion
# --------------------------------------------------------------------------- #

#: Spec Q §8: the tier ladder. A promotion climbs one rung; a demotion drops
#: any distance. Nothing skips ``paper``.
PROMOTION_LADDER: tuple[ExecutionMode, ...] = (
    ExecutionMode.SHADOW, ExecutionMode.PAPER, ExecutionMode.LIVE,
)


@dataclass(frozen=True)
class ArmFacts:
    """The arm fields a promotion decision depends on, lifted out of the row.

    A value object so the binding rules below are pure and testable without a
    database, and so ``registry.py`` stays the only module that knows what an
    ORM row looks like.
    """

    arm_id: int
    experiment_id: int
    strategy_version_id: int
    mode: ExecutionMode
    status: ArmStatus
    risk_budget: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", ExecutionMode(self.mode))
        object.__setattr__(self, "status", ArmStatus(self.status))


@dataclass(frozen=True)
class EvidenceFacts:
    """An ``experiment_metric_snapshots`` row, reduced to its binding fields."""

    metric_snapshot_id: int
    arm_id: int
    warnings_acknowledged: bool = False


@dataclass(frozen=True)
class PromotionAuthorization:
    """A validated, append-only tier change (Spec Q §8, §13).

    Three bindings make it an authorization rather than a suggestion: the
    evidence belongs to the *source* arm, the target is a *distinct inactive*
    arm carrying the *same immutable strategy version*, and the tier move is a
    legal rung. Spec Q §8 is explicit that a strategy-version-only authorization
    is never valid — evidence for one arm cannot authorize another arm's live
    capital.
    """

    source_arm_id: int
    target_arm_id: int
    strategy_version_id: int
    evidence_metric_snapshot_id: int
    from_mode: ExecutionMode
    to_mode: ExecutionMode
    kind: PromotionKind
    owner: str
    reason: str
    previous_risk_budget: float
    new_risk_budget: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "from_mode", ExecutionMode(self.from_mode))
        object.__setattr__(self, "to_mode", ExecutionMode(self.to_mode))
        object.__setattr__(self, "kind", PromotionKind(self.kind))

    def canonical(self) -> dict:
        return {
            "source_arm_id": self.source_arm_id,
            "target_arm_id": self.target_arm_id,
            "strategy_version_id": self.strategy_version_id,
            "evidence_metric_snapshot_id": self.evidence_metric_snapshot_id,
            "from_mode": self.from_mode.value,
            "to_mode": self.to_mode.value,
            "kind": self.kind.value,
            "owner": self.owner,
            "reason": self.reason,
            "previous_risk_budget": self.previous_risk_budget,
            "new_risk_budget": self.new_risk_budget,
        }


def tier_move(from_mode: ExecutionMode, to_mode: ExecutionMode) -> PromotionKind:
    """Classify a tier change, refusing the ones Spec Q §8 does not allow."""
    from_mode, to_mode = ExecutionMode(from_mode), ExecutionMode(to_mode)
    if from_mode == to_mode:
        raise PromotionRefused(
            f"source and target are both {from_mode.value}: a promotion event "
            "records a tier change"
        )
    rise = PROMOTION_LADDER.index(to_mode) - PROMOTION_LADDER.index(from_mode)
    if rise > 1:
        raise PromotionRefused(
            f"{from_mode.value} -> {to_mode.value} skips a tier; the ladder is "
            f"{' -> '.join(m.value for m in PROMOTION_LADDER)}"
        )
    return PromotionKind.PROMOTION if rise > 0 else PromotionKind.DEMOTION


def authorize_promotion(
    source: ArmFacts,
    target: ArmFacts,
    evidence: EvidenceFacts,
    *,
    owner: str,
    reason: str,
    require_warning_acknowledgement: bool = True,
) -> PromotionAuthorization:
    """Validate the bindings and return the authorization, or refuse.

    Pure: it takes value objects, touches no database, and makes no decision of
    its own. Spec Q §8's "the system may recommend promotion but must never
    perform it automatically" lives one layer up — this function says whether an
    owner's request is *coherent*, not whether it is *wise*.
    """
    if not (owner or "").strip():
        raise PromotionRefused("promotions are owner-only and must record the owner")
    if not (reason or "").strip():
        raise PromotionRefused("a promotion must record why")

    if source.arm_id == target.arm_id:
        raise PromotionRefused(
            "the target must be a distinct arm: activating the source arm in a "
            "higher mode would mutate an immutable arm's mode (Spec Q §8)"
        )
    if evidence.arm_id != source.arm_id:
        raise PromotionRefused(
            f"evidence snapshot {evidence.metric_snapshot_id} belongs to arm "
            f"{evidence.arm_id}, not to source arm {source.arm_id}; evidence "
            "collected by one arm cannot authorize another"
        )
    if target.strategy_version_id != source.strategy_version_id:
        raise PromotionRefused(
            "the target arm runs a different strategy version "
            f"({target.strategy_version_id}) from the one the evidence was "
            f"collected under ({source.strategy_version_id}); a rule change is a "
            "new version and needs its own evidence"
        )
    if target.status is not ArmStatus.INACTIVE:
        raise PromotionRefused(
            f"the target arm is {target.status.value}; a promotion activates a "
            "prepared inactive arm, it does not re-point a running one"
        )
    if require_warning_acknowledgement and not evidence.warnings_acknowledged:
        raise PromotionRefused(
            "the evidence snapshot's warnings have not been acknowledged "
            "(Spec Q §8: complete evidence and warning acknowledgement are "
            "required for a tier change)"
        )

    kind = tier_move(source.mode, target.mode)
    return PromotionAuthorization(
        source_arm_id=source.arm_id,
        target_arm_id=target.arm_id,
        strategy_version_id=source.strategy_version_id,
        evidence_metric_snapshot_id=evidence.metric_snapshot_id,
        from_mode=source.mode,
        to_mode=target.mode,
        kind=kind,
        owner=owner.strip(),
        reason=reason.strip(),
        previous_risk_budget=float(source.risk_budget),
        new_risk_budget=float(target.risk_budget),
    )
