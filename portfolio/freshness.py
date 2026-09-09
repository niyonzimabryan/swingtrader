"""Freshness, provenance, and the difference between reading and proposing.

Spec K §4.2: every read tool returns a ``provenance`` block with ``as_of_utc``,
a per-field source, a staleness flag, and a ``data_quality`` tier — and a tool
that cannot meet its freshness contract **returns the stale value with the flag
set**, never a silent guess and never an exception the agent will paper over.

Spec L §5.1 adds the other half, and it points the other way: **any path that
feeds a proposal refuses rather than serves stale data.** A ledger that has
quietly stopped syncing is worse than one that loudly breaks, and the cost of
the two failures is not symmetric — a stale *answer* is a bad answer, a stale
*position size* is a real trade against a book that no longer exists.

So there are two entry points over the same measurement:

:func:`provenance`
    for read tools. Always returns; sets ``stale`` and drops ``data_quality``.
:func:`require_fresh`
    for anything that feeds a proposal. Raises :class:`StaleLedger` with the
    age, which is the part that makes the refusal actionable.

``propose_order`` itself is Phase 6. The guard ships now because the ledger it
guards ships now, and a guard written alongside the thing it protects is the
only kind that gets written at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

#: Spec L §4. Intraday budget in minutes; past it every response says so.
DEFAULT_FRESHNESS_BUDGET_MINUTES = 60

#: ``provenance.data_quality`` tiers, best first.
QUALITY_FRESH = "fresh"
QUALITY_STALE = "stale"
QUALITY_UNKNOWN = "unknown"


class StaleLedger(RuntimeError):
    """A proposal path asked for data older than the freshness budget."""

    def __init__(self, as_of_utc: datetime | None, age_minutes: float | None, budget_minutes: int):
        self.as_of_utc = as_of_utc
        self.age_minutes = age_minutes
        self.budget_minutes = budget_minutes
        if as_of_utc is None:
            detail = "the ledger has never been synced"
        else:
            detail = (
                f"the ledger is {age_minutes:.1f} minutes old "
                f"(as_of_utc={as_of_utc.isoformat()})"
            )
        super().__init__(
            f"stale_ledger: {detail}; the freshness budget is "
            f"{budget_minutes} minutes. Refusing rather than serving — run a "
            "portfolio sync and retry."
        )


def _naive_utc(value: datetime | None) -> datetime | None:
    """Naive UTC, matching what ``database.types.UtcDateTime`` stores."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def age_minutes(as_of_utc: datetime | None, now: datetime) -> float | None:
    """Minutes between ``as_of_utc`` and ``now``. ``None`` if never synced.

    Clamped at zero: a broker clock a few seconds ahead of ours should read as
    fresh, not as negative age.
    """
    as_of = _naive_utc(as_of_utc)
    if as_of is None:
        return None
    delta = (_naive_utc(now) - as_of).total_seconds() / 60.0
    return max(0.0, delta)


def is_stale(as_of_utc: datetime | None, now: datetime, budget_minutes: int) -> bool:
    """Never synced counts as stale."""
    age = age_minutes(as_of_utc, now)
    return True if age is None else age > budget_minutes


@dataclass(frozen=True)
class Provenance:
    """The block every read tool returns (Spec K §4.2).

    ``sources`` is per field, not per response: the holdings in one answer can
    come from a broker read while the sector weights beside them come from a
    local reference table, and collapsing that into one label is how a figure
    ends up trusted more than it should be.
    """

    as_of_utc: datetime | None
    stale: bool
    data_quality: str
    freshness_budget_minutes: int
    age_minutes: float | None = None
    sources: dict = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "as_of_utc": self.as_of_utc.isoformat() if self.as_of_utc else None,
            "stale": self.stale,
            "data_quality": self.data_quality,
            "age_minutes": (
                round(self.age_minutes, 2) if self.age_minutes is not None else None
            ),
            "freshness_budget_minutes": self.freshness_budget_minutes,
            "sources": dict(self.sources),
            "warnings": list(self.warnings),
        }


def provenance(
    as_of_utc: datetime | None,
    now: datetime,
    *,
    sources: dict | None = None,
    budget_minutes: int = DEFAULT_FRESHNESS_BUDGET_MINUTES,
    warnings=(),
) -> Provenance:
    """Build the provenance block for a read response. Never raises."""
    age = age_minutes(as_of_utc, now)
    if age is None:
        quality, stale = QUALITY_UNKNOWN, True
    elif age > budget_minutes:
        quality, stale = QUALITY_STALE, True
    else:
        quality, stale = QUALITY_FRESH, False
    extra = list(warnings)
    if stale and as_of_utc is None:
        extra.append("never_synced: no portfolio sync has ever completed.")
    elif stale:
        extra.append(
            f"stale: the ledger is {age:.1f} minutes old against a "
            f"{budget_minutes} minute budget."
        )
    return Provenance(
        as_of_utc=_naive_utc(as_of_utc),
        stale=stale,
        data_quality=quality,
        age_minutes=age,
        freshness_budget_minutes=budget_minutes,
        sources=dict(sources or {}),
        warnings=tuple(extra),
    )


def require_fresh(
    as_of_utc: datetime | None,
    now: datetime,
    *,
    budget_minutes: int = DEFAULT_FRESHNESS_BUDGET_MINUTES,
) -> float:
    """Return the age in minutes, or raise :class:`StaleLedger`.

    The proposal-side half of the freshness contract. Read tools call
    :func:`provenance` instead and flag; this one refuses.
    """
    if is_stale(as_of_utc, now, budget_minutes):
        raise StaleLedger(_naive_utc(as_of_utc), age_minutes(as_of_utc, now), budget_minutes)
    return age_minutes(as_of_utc, now) or 0.0


def budget_from_settings(settings, default: int = DEFAULT_FRESHNESS_BUDGET_MINUTES) -> int:
    value = getattr(settings, "portfolio_freshness_budget_minutes", None)
    try:
        return int(value) if value else default
    except (TypeError, ValueError):
        return default


def next_sync_due(as_of_utc: datetime | None, budget_minutes: int) -> datetime | None:
    """When the ledger goes stale, for ``/health`` and the sync scheduler."""
    as_of = _naive_utc(as_of_utc)
    return None if as_of is None else as_of + timedelta(minutes=budget_minutes)
