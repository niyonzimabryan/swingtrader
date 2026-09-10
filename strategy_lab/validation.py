"""The guards that run before a decision is trusted (Spec Q §6, §7, §10).

Four refusals live here, all pure, all callable without a database:

**Decision-set validity.** A ticker strategy returns exactly one draft for the
snapshot's ticker; a universe strategy returns exactly one for *every*
constituent — ``long`` for the selected, ``flat`` for the eligible-but-unselected,
``abstain`` for the unevaluable — ordered by ticker ascending with no repeats and
every draft pinned to this snapshot's content hash. Phase 3's runner calls
:func:`validate_decision_set` before persistence; the SDK calls it too, so a
strategy under test fails in its own unit test rather than in the runner.

**Implementation-manifest drift.** Spec Q §6: "On activation and before every
run, the registry recomputes the manifest hash and compares it with
``strategy_versions.implementation_manifest_hash``. A mismatch fails closed and
requires registration of a new strategy version; an in-place code, helper,
formula, policy, or runtime dependency change may never run under an existing
version identity." :func:`build_implementation_manifest` computes the transitive
manifest — the strategy's own source, every output-affecting helper in this
package, the execution policy's source *and* its resolved configuration, each
indicator formula's source, and the pinned runtime — and :func:`verify_manifest`
is the comparison. Editing an indicator therefore changes the manifest of every
strategy that uses it, which is the intended blast radius: the formula is part
of what the version *is*.

**Historical replayability.** ``swingtrader_composite_v1`` freezes an LLM's
conclusion. That conclusion is not reconstructible at a historical time T, so
the version declares ``historically_replayable=False`` and
:func:`guard_historical_replay` refuses to evaluate it in historical mode. The
same guard refuses any snapshot whose quality warnings make it exploratory —
Spec Q §10 keeps ``archival_reconstructed`` and ``not_point_in_time`` evidence
out of clean metrics and away from promotion gates.

**Structural shadow-only.** ``short_term_reversal_v1`` is shadow-only at launch
because turnover and spread can dominate the anomaly (Spec Q §7D). That is a
property of the version, declared in its immutable config, and
:func:`require_mode_allowed` refuses a paper or live arm for it — before a
runner, an executor or a broker is anywhere near the decision.
"""

from __future__ import annotations

import hashlib
import inspect
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from strategy_lab import execution_policy as execution_policy_module
from strategy_lab.domain import (
    ExecutionMode,
    MarketSnapshot,
    REPLAY_DISQUALIFYING_WARNINGS,
    SnapshotScope,
    StrategyDecision,
    StrategyLabError,
    StrategyVersion,
    sha256_of,
)

__all__ = [
    "DecisionSetInvalid",
    "ManifestDrift",
    "ReplayRefused",
    "ModeRefused",
    "MANIFEST_SCHEMA",
    "SHADOW_ONLY_KEY",
    "validate_decision_set",
    "decision_set_hash",
    "source_sha256",
    "build_implementation_manifest",
    "manifest_hash",
    "verify_manifest",
    "guard_historical_replay",
    "require_mode_allowed",
    "is_shadow_only",
]


class DecisionSetInvalid(StrategyLabError):
    """A strategy returned a decision set that does not match its snapshot."""


class ManifestDrift(StrategyLabError):
    """The code on disk is not the code the registered version was hashed from."""


class ReplayRefused(StrategyLabError):
    """A version or a snapshot cannot support a historical replay."""


class ModeRefused(StrategyLabError):
    """A version was asked to run in a tier its own config forbids."""


MANIFEST_SCHEMA = "strategy_lab.manifest.v1"

#: The immutable-config key that makes a version structurally shadow-only.
SHADOW_ONLY_KEY = "shadow_only"

#: Modules in this package whose contents can change a decision. Every manifest
#: covers all of them, not only the ones a given strategy imports: a helper
#: reached indirectly is still a helper, and enumerating the closure per module
#: would turn a static guarantee into a code-reading exercise.
HELPER_MODULES = (
    "strategy_lab/domain.py",
    "strategy_lab/execution_policy.py",
    "strategy_lab/indicators.py",
    "strategy_lab/snapshots.py",
    "strategy_lab/universe.py",
    "strategy_lab/validation.py",
)

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Decision sets
# --------------------------------------------------------------------------- #


def validate_decision_set(
    snapshot: MarketSnapshot,
    version: StrategyVersion,
    decisions: Sequence[StrategyDecision],
) -> tuple[StrategyDecision, ...]:
    """Cardinality, membership, uniqueness, order and pinning. Or refuse.

    Returns the decisions unchanged so a caller can write
    ``return validate_decision_set(...)`` and be certain nothing downstream sees
    an unvalidated set.
    """
    if any(not isinstance(d, StrategyDecision) for d in decisions):
        raise DecisionSetInvalid("every element must be a StrategyDecision")

    tickers = [d.ticker for d in decisions]
    if len(set(tickers)) != len(tickers):
        duplicates = sorted({t for t in tickers if tickers.count(t) > 1})
        raise DecisionSetInvalid(
            f"{version.identity}: one decision per ticker; {duplicates} repeat"
        )
    if tickers != sorted(tickers):
        raise DecisionSetInvalid(
            f"{version.identity}: decisions must be ordered by ticker ascending "
            "before hashing (Spec Q §6)"
        )

    expected_hash = snapshot.content_hash
    for decision in decisions:
        if decision.strategy_slug != version.slug or decision.strategy_version != version.version:
            raise DecisionSetInvalid(
                f"decision for {decision.ticker} claims "
                f"{decision.strategy_slug}@{decision.strategy_version}, but the "
                f"strategy is {version.identity}"
            )
        if decision.snapshot_hash != expected_hash:
            raise DecisionSetInvalid(
                f"decision for {decision.ticker} is pinned to snapshot "
                f"{decision.snapshot_hash[:12]}, not to {expected_hash[:12]}; "
                "a cross-sectional rank assembled from two snapshots is exactly "
                "what Spec Q §6 forbids"
            )
        if decision.risk_plan is not None:
            if decision.risk_plan.execution_policy_version != version.execution_policy_version:
                raise DecisionSetInvalid(
                    f"decision for {decision.ticker} carries execution policy "
                    f"{decision.risk_plan.execution_policy_version!r}, but "
                    f"{version.identity} declares "
                    f"{version.execution_policy_version!r}"
                )

    if snapshot.scope is SnapshotScope.TICKER:
        expected = (snapshot.ticker,)
    else:
        expected = snapshot.constituents
    if tuple(tickers) != tuple(expected):
        missing = sorted(set(expected) - set(tickers))
        extra = sorted(set(tickers) - set(expected))
        raise DecisionSetInvalid(
            f"{version.identity}: a {snapshot.scope.value}-scoped snapshot needs "
            f"exactly one decision per constituent — missing {missing}, "
            f"unexpected {extra}"
        )
    return tuple(decisions)


def decision_set_hash(
    snapshot: MarketSnapshot, version: StrategyVersion, decisions: Sequence[StrategyDecision]
) -> str:
    """The hash of the whole ordered set, alongside each decision's own hash.

    Spec Q §6: the runner "hashes both the ordered decision set and each
    constituent decision". The set hash is what makes "the same version over the
    same snapshot selected the same basket" checkable in one comparison.
    """
    return sha256_of({
        "strategy": version.identity,
        "snapshot_hash": snapshot.content_hash,
        "decisions": [d.decision_hash for d in decisions],
    })


# --------------------------------------------------------------------------- #
# Implementation manifest
# --------------------------------------------------------------------------- #


def source_sha256(path: str | Path) -> str:
    """sha256 of a repository file's bytes, by path relative to the repo root."""
    target = Path(path)
    if not target.is_absolute():
        target = _PACKAGE_ROOT / target
    return hashlib.sha256(target.read_bytes()).hexdigest()


def _function_sha256(fn: Callable) -> str:
    return hashlib.sha256(inspect.getsource(fn).encode("utf-8")).hexdigest()


def build_implementation_manifest(
    *,
    strategy_module: str,
    execution_policy_version: str,
    indicators: Mapping[str, Callable],
    dependencies: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The canonical transitive manifest for one strategy version (Spec Q §6).

    ``strategy_module`` is a dotted module name; its file is hashed from disk,
    as are every helper in :data:`HELPER_MODULES` and the source of every
    indicator formula the strategy uses. The execution policy contributes both
    its source hash and its resolved configuration, so changing ``2.0`` to
    ``2.5`` ATR is drift even though the file's structure is unchanged.

    ``dependencies`` is the pinned third-party set. The V1 roster is stdlib
    only, so it is empty and *stated* to be empty rather than omitted — a
    manifest that silently lacks a dependency section cannot distinguish "no
    dependencies" from "nobody recorded them".

    The Python version is recorded to minor precision. Patch releases move under
    a deployment without a code change and would otherwise invalidate every
    registered version on a routine base-image bump; a minor-version move is a
    real runtime change and should require re-registration.
    """
    policy = execution_policy_module.require_policy(execution_policy_version)
    module_path = Path(strategy_module.replace(".", "/") + ".py")
    return {
        "schema": MANIFEST_SCHEMA,
        "strategy": {
            "module": strategy_module,
            "source_sha256": source_sha256(module_path),
        },
        "helpers": {name: source_sha256(name) for name in sorted(HELPER_MODULES)},
        "execution_policy": {
            "version": policy.version,
            "config": policy.canonical(),
            "source_sha256": source_sha256("strategy_lab/execution_policy.py"),
        },
        "indicator_formulas": {
            name: _function_sha256(fn) for name, fn in sorted(indicators.items())
        },
        "runtime": {
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "dependencies": dict(sorted((dependencies or {}).items())),
        },
    }


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    return sha256_of(dict(manifest))


def verify_manifest(version: StrategyVersion, registered_manifest_hash: str) -> None:
    """Refuse to run when the code on disk is not what was registered.

    ``version`` is the *freshly built* domain object — the one whose
    ``implementation_manifest`` was computed from the files as they are now.
    ``registered_manifest_hash`` is what the ``strategy_versions`` row stored.
    Different means someone edited a strategy, a helper, an indicator, the
    execution policy, or the runtime without registering a new version.
    """
    current = version.manifest_hash
    if current != registered_manifest_hash:
        raise ManifestDrift(
            f"{version.identity}: the implementation manifest on disk hashes to "
            f"{current[:12]}, but the registered version was hashed from "
            f"{registered_manifest_hash[:12]}. An in-place code, helper, "
            "formula, policy, or runtime change may never run under an existing "
            "version identity (Spec Q §6) — register a new version."
        )


# --------------------------------------------------------------------------- #
# Replay and tier guards
# --------------------------------------------------------------------------- #


def guard_historical_replay(version: StrategyVersion, snapshot: MarketSnapshot) -> None:
    """Refuse a historical replay the evidence cannot support (Spec Q §10)."""
    if not version.historically_replayable:
        raise ReplayRefused(
            f"{version.identity} declares historically_replayable=False "
            f"({version.replayability_reason}); it may run only from a "
            "current-time snapshot. Spec Q §4 'do not build again': historical "
            "replay of LLM conclusions."
        )
    disqualifying = sorted(set(snapshot.quality_warnings) & REPLAY_DISQUALIFYING_WARNINGS)
    if disqualifying:
        raise ReplayRefused(
            f"snapshot {snapshot.content_hash[:12]} carries {disqualifying}; a "
            "reconstructed or non-point-in-time snapshot produces exploratory "
            "results only and can never enter clean metrics or satisfy a "
            "promotion gate (Spec Q §10)."
        )


def is_shadow_only(version: StrategyVersion) -> bool:
    return bool(version.config.get(SHADOW_ONLY_KEY, False))


def require_mode_allowed(version: StrategyVersion, mode: ExecutionMode | str) -> ExecutionMode:
    """Refuse a tier the version's immutable config forbids (Spec Q §7D)."""
    mode = ExecutionMode(mode)
    if is_shadow_only(version) and mode is not ExecutionMode.SHADOW:
        raise ModeRefused(
            f"{version.identity} is structurally shadow-only and cannot run in "
            f"{mode.value}: {version.config.get('shadow_only_reason', 'declared in its immutable config')}. "
            "Making it paper- or live-eligible is a new version, and Spec Q §7D "
            "requires replay stability under materially worse cost assumptions "
            "first."
        )
    return mode
