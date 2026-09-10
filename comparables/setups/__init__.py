"""The pre-registered setup roster (Spec N §4.0).

> *"Setups are pre-registered, not improvised, in v1. A small fixed roster of
> typed setups ships in `comparables/setups/`, each with its conditioning set
> frozen. The general 'any predicate' engine is the same code path but the
> roster is what the multiplicity accounting (§7) counts against."*

Three entries ship in Phase 3c:

``gap_and_go_v1``
    Price-only. Every fact it conditions on comes from stored `price_bars`.
``earnings_sue_seasonal_v1``
    The seasonal-random-walk SUE from XBRL `companyfacts`, timed by the 8-K
    Item 2.02 `acceptanceDateTime`. No analyst consensus is involved anywhere
    (§4.0: a restated consensus used as a pre-print fact is lookahead in the
    direction that flatters the engine).
``insider_cluster_v1``
    Declared, frozen and hashed — and refused, because the Form 4 plane that
    supplies `insider_cluster_count` is Phase 4 work. A roster entry that
    exists and refuses is honest; one that quietly returns `insufficient`
    would read as "we looked and found nothing".

Every entry is a frozen :class:`~comparables.setup_spec.SetupSpec`, so it is
content-hashed and its family slug is derived rather than declared. Parameters
are applied through :meth:`RosterEntry.with_parameters`, which produces **a new
hash in the same family** — that is the whole point of §7's equivalence class:
moving a threshold is a new trial, not a new subject.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from comparables.setup_spec import SetupSpec, SetupSpecError

#: How a roster entry's candidate events are found. Not a free-form field: the
#: cohort builder dispatches on it and refuses an unknown value.
SOURCE_EARNINGS_8K = "earnings_8k_item_202"
SOURCE_PRICE_SESSION = "price_session"
SOURCE_PENDING = "pending_plane"
CANDIDATE_SOURCES = (SOURCE_EARNINGS_8K, SOURCE_PRICE_SESSION, SOURCE_PENDING)

#: The refusal a setup whose evidence plane does not exist yet returns.
STATUS_PENDING_PLANE = "pending_plane"


class UnknownSetup(KeyError):
    """A roster slug that is not registered. Never a silent empty cohort."""


@dataclass(frozen=True)
class RosterEntry:
    """One pre-registered setup, plus what the cohort builder needs to run it."""

    spec: SetupSpec
    candidate_source: str
    #: `source_observations.fact_type` values this setup cannot be built without.
    required_fact_types: tuple[str, ...]
    #: Which parameters `with_parameters` may move, and which condition each
    #: one is the threshold of. Anything else is refused by name.
    tunable: tuple[tuple[str, str], ...]
    description: str
    available: bool = True
    unavailable_reason: str | None = None

    @property
    def slug(self) -> str:
        return self.spec.slug

    @property
    def setup_hash(self) -> str:
        return self.spec.content_hash

    @property
    def family_slug(self) -> str:
        return self.spec.family_slug

    def with_parameters(self, parameters: Mapping[str, Any] | None = None) -> "RosterEntry":
        """A copy with the named thresholds moved. Same family, new hash.

        `horizons_sessions` and `lookback_years` are movable on every entry;
        every other name must appear in `tunable`, mapped to the fact whose
        condition it is the value of. An unknown parameter raises rather than
        being ignored, because a silently ignored threshold produces an answer
        to a question nobody asked.
        """
        if not parameters:
            return self
        tunable = dict(self.tunable)
        conditions = list(self.spec.conditions)
        spec_kwargs: dict[str, Any] = {}

        for name, value in sorted(parameters.items()):
            if name == "horizons_sessions":
                spec_kwargs["horizons_sessions"] = tuple(int(h) for h in value)
                continue
            if name == "lookback_years":
                spec_kwargs["lookback_years"] = int(value)
                continue
            if name not in tunable:
                raise SetupSpecError(
                    f"{self.slug}: {name!r} is not a tunable parameter of this "
                    f"setup; it takes {sorted(tunable) + ['horizons_sessions', 'lookback_years']}"
                )
            fact = tunable[name]
            for i, condition in enumerate(conditions):
                if condition.fact == fact:
                    conditions[i] = replace(condition, value=value)
                    break
            else:  # pragma: no cover - guarded by the roster's own construction
                raise SetupSpecError(f"{self.slug}: no condition on {fact!r} to move")

        spec = replace(self.spec, conditions=tuple(conditions), **spec_kwargs)
        return replace(self, spec=spec)


def _entries() -> dict[str, RosterEntry]:
    from comparables.setups import earnings_sue_seasonal, gap_and_go, insider_cluster

    out: dict[str, RosterEntry] = {}
    for entry in (gap_and_go.ENTRY, earnings_sue_seasonal.ENTRY, insider_cluster.ENTRY):
        if entry.candidate_source not in CANDIDATE_SOURCES:
            raise ValueError(
                f"{entry.slug}: unknown candidate source {entry.candidate_source!r}"
            )
        out[entry.slug] = entry
    return out


ROSTER: dict[str, RosterEntry] = _entries()


def slugs() -> tuple[str, ...]:
    return tuple(sorted(ROSTER))


def get(slug: str, parameters: Mapping[str, Any] | None = None) -> RosterEntry:
    """The roster entry for `slug`, with `parameters` applied."""
    try:
        entry = ROSTER[slug]
    except KeyError as exc:
        raise UnknownSetup(
            f"unknown setup {slug!r}; the pre-registered roster is {list(slugs())}. "
            "A free-form question is asked by passing an explicit SetupSpec, and "
            "it counts as a trial against the nearest family (Spec N §7)."
        ) from exc
    return entry.with_parameters(parameters)


#: Which candidate source a fact type implies. A setup that conditions on a
#: filing fact cannot be built by walking price sessions, and one that
#: conditions on a Phase 4 fact cannot be built at all yet.
FACT_SOURCES: tuple[tuple[str, str], ...] = (
    ("insider_cluster_count", SOURCE_PENDING),
    ("sue_seasonal", SOURCE_EARNINGS_8K),
    ("consensus_eps_news", SOURCE_EARNINGS_8K),
    ("guidance_direction", SOURCE_EARNINGS_8K),
)


def source_for_spec(spec: SetupSpec) -> str:
    """Where an *ad-hoc* `SetupSpec`'s candidate events come from.

    A roster entry declares its own source. A free-form spec — Spec N §4.0's
    "the general 'any predicate' engine is the same code path" — does not, and
    defaulting every one of them to price sessions would answer an earnings
    question with an empty cohort and call it `insufficient`. The fact types it
    conditions on decide, in the order above: a Phase 4 fact refuses outright, a
    filing fact walks the 8-K feed, and everything else walks price sessions.
    """
    facts = {c.fact for c in spec.conditions} | set(spec.match_covariates)
    for fact, source in FACT_SOURCES:
        if fact in facts:
            return source
    return SOURCE_PRICE_SESSION


def catalogue() -> tuple[dict, ...]:
    """The roster as plain data, for a tool listing or a doc table."""
    return tuple(
        {
            "slug": entry.slug,
            "version": entry.spec.version,
            "setup_hash": entry.setup_hash,
            "family_slug": entry.family_slug,
            "universe": entry.spec.universe,
            "horizons_sessions": list(entry.spec.horizons_sessions),
            "conditions": entry.spec.canonical()["conditions"],
            "candidate_source": entry.candidate_source,
            "required_fact_types": list(entry.required_fact_types),
            "tunable": sorted(dict(entry.tunable)) + ["horizons_sessions", "lookback_years"],
            "available": entry.available,
            "unavailable_reason": entry.unavailable_reason,
            "description": entry.description,
        }
        for entry in (ROSTER[slug] for slug in slugs())
    )


__all__ = [
    "CANDIDATE_SOURCES",
    "FACT_SOURCES",
    "source_for_spec",
    "ROSTER",
    "RosterEntry",
    "SOURCE_EARNINGS_8K",
    "SOURCE_PENDING",
    "SOURCE_PRICE_SESSION",
    "STATUS_PENDING_PLANE",
    "UnknownSetup",
    "catalogue",
    "get",
    "slugs",
]
