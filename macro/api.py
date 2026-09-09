"""``macro_state`` — the stable read API for the macro plane (Spec K section 4.2).

    macro_state(session, as_of=today)        -> current values, as now
    macro_state(session, as_of=2024-03-15)   -> the values a person could have
                                                seen on 2024-03-15, including
                                                the release lag

The second one is the whole point. A series whose print had not been published
at ``as_of`` is **absent** from the result, not back-filled, and the absence is
listed in ``missing`` so a caller can tell "no value" from "did not ask".

No ``workspace/`` service package exists on ``main`` yet (Phase 0b), so this is
a plain function with a stable signature; registering it as an MCP tool is the
ten-line follow-up in ``docs/MACRO_PLANE.md``.

It is a **read**, it calls no model, and the regime label it returns comes from
``macro.regime_v1`` — a deterministic classifier whose import graph is asserted
to reach no model client.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Sequence

from filings import provenance
from filings.observations import day_precision_known_at, observations_known_at
from macro import regime_v1
from macro import series as series_registry
from macro.vintage import (
    MACRO_ENTITY,
    SOURCE_ALFRED,
    fact_type_for,
    stored_series_as_of,
)

#: The series ``macro_state`` reports by default: everything regime_v1 reads,
#: plus the two-year point so the curve can be shown two ways.
DEFAULT_SERIES: tuple[str, ...] = regime_v1.INPUT_SERIES + ("DGS2",)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _as_datetime(as_of: date | datetime) -> datetime:
    """A bare date means the **close** of that date, to the microsecond.

    It must be the exact instant ``day_precision_known_at`` writes: a cutoff
    of 23:59:59 excludes a row stamped 23:59:59.999999 by a millionth of a
    second, so ``macro_state(as_of=<a release date>)`` would silently miss the
    print released that very day.
    """
    if isinstance(as_of, datetime):
        return _aware(as_of)
    return day_precision_known_at(as_of)


def macro_state(
    session,
    *,
    as_of: date | datetime | None = None,
    series_ids: Sequence[str] = DEFAULT_SERIES,
    include_regime: bool = True,
) -> dict:
    """The macro picture as it stood at ``as_of``, from stored vintages."""
    cutoff = _as_datetime(as_of or datetime.now(timezone.utc))

    values: dict[str, dict] = {}
    missing: list[str] = []
    for series_id in series_ids:
        spec = series_registry.spec_for(series_id)
        points = stored_series_as_of(session, spec.series_id, as_of=cutoff)
        if not points:
            missing.append(spec.series_id)
            continue
        reference = max(points)
        values[spec.series_id] = {
            "value": points[reference],
            "reference_date": reference.isoformat(),
            "label": spec.label,
            "units": spec.units,
            "never_revised": spec.never_revised,
            "observations": len(points),
        }

    regime = None
    if include_regime:
        regime = _regime_at(session, cutoff)

    rows = observations_known_at(
        session,
        fact_type=[fact_type_for(s) for s in series_ids],
        cutoff=cutoff,
        entity_cik=MACRO_ENTITY,
    )
    latest = max((r.known_at_utc for r in rows), default=None)
    return {
        "as_of": cutoff.isoformat(),
        "series": values,
        "missing": missing,
        "regime": regime,
        "provenance": provenance.build(
            as_of=cutoff,
            rows=rows,
            sources={SOURCE_ALFRED: "FRED/ALFRED archival vintages"},
            staleness={
                "days_since_latest_release": provenance.staleness_days(cutoff, latest)
            },
            notes=[
                "Every macro row is precision='day' and is known at the close of "
                "its release date (Spec O section 2), so an intraday cutoff "
                "cannot inherit a print released that morning.",
                "A series absent from `series` and listed in `missing` had no "
                "release on or before as_of. It is absent, not zero.",
            ],
        ),
    }


def _regime_at(session, cutoff: datetime) -> dict | None:
    """Label the regime from stored, never-revised series only."""
    closes = stored_series_as_of(session, "SP500", as_of=cutoff)
    vix = stored_series_as_of(session, "VIXCLS", as_of=cutoff)
    dgs10 = stored_series_as_of(session, "DGS10", as_of=cutoff)
    dgs3mo = stored_series_as_of(session, "DGS3MO", as_of=cutoff)

    inputs = regime_v1.build_inputs(
        as_of=cutoff.date(),
        index_closes=[closes[key] for key in sorted(closes)],
        vix=vix[max(vix)] if vix else None,
        dgs10=dgs10[max(dgs10)] if dgs10 else None,
        dgs3mo=dgs3mo[max(dgs3mo)] if dgs3mo else None,
    )
    return regime_v1.classify(inputs).as_dict()
