"""The typed setup definition (Spec N §4.0).

A setup is a predicate, not a vibe: every field is required, the whole thing is
frozen, and it is content-hashed so a cohort answer is keyed by
`(setup_hash, as_of_date, data_snapshot_version)` and is reproducible.

The *family slug* (§6.4, §7) is the equivalence class used both for
empirical-Bayes shrinkage and for the multiplicity trial count. It is derived
from the primary condition and the universe and deliberately **does not depend
on `slug`**, so renaming a setup cannot reset its trial count.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any

OPERATORS = frozenset({">", ">=", "<", "<=", "==", "!=", "in", "not_in"})

#: Conditions reference fact types, never free text (Spec N §4.0).
FACT_TYPES = frozenset({
    "sue_seasonal",
    "consensus_eps_news",
    "guidance_direction",
    "gap_pct",
    "dollar_volume_20d",
    "atr_pct",
    "dist_from_sma50",
    "market_cap_decile",
    "realized_vol_decile",
    "days_since_prior_event",
    "sector",
    # Phase 4's Form 4 plane supplies this one. The roster's
    # `insider_cluster_v1` is declared against it now so that the setup is
    # frozen, hashed and countable against its family before the facts exist;
    # the engine refuses the query with `pending_plane` until they do.
    "insider_cluster_count",
})


class SetupSpecError(ValueError):
    """A setup definition that cannot be used to build a cohort."""


@dataclass(frozen=True)
class Condition:
    """One typed predicate over a fact type."""

    fact: str
    op: str
    value: Any

    def __post_init__(self) -> None:
        if self.fact not in FACT_TYPES:
            raise SetupSpecError(
                f"unknown fact type {self.fact!r}; conditions reference fact "
                f"types, never free text (Spec N §4.0)"
            )
        if self.op not in OPERATORS:
            raise SetupSpecError(f"unknown operator {self.op!r}")
        if isinstance(self.value, list):
            raise SetupSpecError("condition values must be hashable; use a tuple")

    def canonical(self) -> dict:
        value = list(self.value) if isinstance(self.value, tuple) else self.value
        return {"fact": self.fact, "op": self.op, "value": value}


@dataclass(frozen=True)
class SetupSpec:
    """A content-hashed, immutable setup definition."""

    slug: str
    version: str
    conditions: tuple[Condition, ...]
    universe: str
    horizons_sessions: tuple[int, ...]
    execution_policy: str | None
    match_covariates: tuple[str, ...]
    lookback_years: int

    def __post_init__(self) -> None:
        if not self.slug or not isinstance(self.slug, str):
            raise SetupSpecError("slug is required")
        if not self.version or not isinstance(self.version, str):
            raise SetupSpecError("version is required")
        if not isinstance(self.conditions, tuple) or not self.conditions:
            raise SetupSpecError("at least one condition is required, as a tuple")
        if not all(isinstance(c, Condition) for c in self.conditions):
            raise SetupSpecError("conditions must be Condition instances")
        if not self.universe or not isinstance(self.universe, str):
            raise SetupSpecError("universe is required")
        if not isinstance(self.horizons_sessions, tuple) or not self.horizons_sessions:
            raise SetupSpecError("horizons_sessions is required, as a tuple")
        if any((not isinstance(h, int)) or isinstance(h, bool) or h < 1
               for h in self.horizons_sessions):
            raise SetupSpecError("horizons are whole trading sessions >= 1")
        if len(set(self.horizons_sessions)) != len(self.horizons_sessions):
            raise SetupSpecError("horizons_sessions must not repeat")
        if list(self.horizons_sessions) != sorted(self.horizons_sessions):
            raise SetupSpecError("horizons_sessions must be ascending")
        if self.execution_policy is not None and not isinstance(self.execution_policy, str):
            raise SetupSpecError("execution_policy is a slug or None")
        if not isinstance(self.match_covariates, tuple):
            raise SetupSpecError("match_covariates is required, as a tuple")
        if not isinstance(self.lookback_years, int) or isinstance(self.lookback_years, bool):
            raise SetupSpecError("lookback_years is required")
        if self.lookback_years < 1:
            raise SetupSpecError("lookback_years must be >= 1")

    # -- identity ---------------------------------------------------------- #

    def canonical(self) -> dict:
        return {
            "slug": self.slug,
            "version": self.version,
            "conditions": [c.canonical() for c in self.conditions],
            "universe": self.universe,
            "horizons_sessions": list(self.horizons_sessions),
            "execution_policy": self.execution_policy,
            "match_covariates": list(self.match_covariates),
            "lookback_years": self.lookback_years,
        }

    @property
    def content_hash(self) -> str:
        """SHA-256 over the canonical form. Change a condition, change the hash."""
        blob = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @property
    def primary_condition(self) -> Condition:
        """The first condition, which defines the family (Spec N §6.4)."""
        return self.conditions[0]

    @property
    def family_slug(self) -> str:
        """The equivalence class for shrinkage (§6.4) and trial counting (§7).

        Keyed by universe plus the primary condition's *fact and operator* — not
        its threshold and not the setup slug, so "every `sue_seasonal > x`
        cohort" is one family and renaming a setup does not reset its count.
        """
        p = self.primary_condition
        return f"{self.universe}:{p.fact}:{p.op}"

    def renamed(self, slug: str, version: str | None = None) -> "SetupSpec":
        """A copy under a different name — same family, different hash."""
        return replace(self, slug=slug, version=version or self.version)
