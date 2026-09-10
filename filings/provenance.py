"""The provenance block every plane read returns — Spec K section 4.2.

> Every read tool returns a ``provenance`` block: ``as_of_utc``, per-field
> source, staleness flags, and a ``data_quality`` tier. A tool that cannot meet
> its freshness contract returns the stale value **with the flag set**, never a
> silent guess and never an exception that the agent will paper over.

It lives in ``filings/`` because that is where the shared ledger seam already
lives and all three planes import it; it is not filings-specific.

The quality tiers, worst to best:

``insufficient``
    Nothing was knowable at the cutoff. An empty answer, said out loud.
``degraded``
    Something came back but at least one row is not replay-eligible or carries
    a quality warning. Usable for context; not for a ``clean_pit`` cohort.
``vendor_pit``
    Every row carries a timestamp a vendor or regulator measured.
``clean_pit``
    Every row is replay-eligible, second-precision, warning-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

QUALITY_INSUFFICIENT = "insufficient"
QUALITY_DEGRADED = "degraded"
QUALITY_VENDOR_PIT = "vendor_pit"
QUALITY_CLEAN_PIT = "clean_pit"

#: Worst to best; index compares two tiers.
QUALITY_ORDER: tuple[str, ...] = (
    QUALITY_INSUFFICIENT,
    QUALITY_DEGRADED,
    QUALITY_VENDOR_PIT,
    QUALITY_CLEAN_PIT,
)


@dataclass
class Provenance:
    as_of_utc: str
    sources: dict = field(default_factory=dict)
    staleness: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    data_quality: str = QUALITY_INSUFFICIENT
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "as_of_utc": self.as_of_utc,
            "sources": self.sources,
            "staleness": self.staleness,
            "warnings": self.warnings,
            "data_quality": self.data_quality,
            "notes": self.notes,
        }


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def staleness_days(as_of: datetime, when: datetime | None) -> float | None:
    if when is None:
        return None
    return round((_aware(as_of) - _aware(when)).total_seconds() / 86400.0, 4)


def quality_for(rows: Sequence[Any]) -> str:
    """The tier a set of ledger rows earns. Empty is ``insufficient``."""
    if not rows:
        return QUALITY_INSUFFICIENT
    if any(not getattr(row, "replay_eligible", False) for row in rows):
        return QUALITY_DEGRADED
    if any(getattr(row, "warnings", None) for row in rows):
        return QUALITY_DEGRADED
    if any(getattr(row, "precision", "") != "second" for row in rows):
        return QUALITY_VENDOR_PIT
    return QUALITY_CLEAN_PIT


def build(
    *,
    as_of: datetime,
    rows: Sequence[Any] = (),
    sources: dict | None = None,
    staleness: dict | None = None,
    notes: Iterable[str] = (),
    quality: str | None = None,
) -> dict:
    """The block, from the rows that produced the answer."""
    warnings = sorted(
        {w for row in rows for w in (getattr(row, "warnings", None) or [])}
    )
    provenance = Provenance(
        as_of_utc=_aware(as_of).isoformat(),
        sources=dict(sources or {}),
        staleness=dict(staleness or {}),
        warnings=warnings,
        data_quality=quality or quality_for(rows),
        notes=list(notes),
    )
    return provenance.as_dict()
