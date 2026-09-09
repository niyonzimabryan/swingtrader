"""Explicit XBRL tag-alias maps, and the alert that replaces a silent gap.

XBRL coverage is **per tag, not per company** (verification claim 13). A filer
that moves from ``Revenues`` to
``RevenueFromContractWithCustomerExcludingAssessedTax`` at the ASC 606
transition has not stopped reporting revenue, but a pipeline that reads one tag
sees the series stop. The gap looks like missing quarters, the company drops out
of cohorts, and — because tag migration correlates with filer size — it drops
out **non-randomly**. That is a bias, not a coverage problem.

So each fact type is a named, ordered chain of tags, checked into code with
tests, and the ingest reports what it did rather than absorbing it:

``tag_migration``
    the series continued under a different tag. The merged series is
    *continuous*; the alert exists so a human can confirm the two tags mean the
    same thing for that filer rather than discovering it in a backtest.
``series_gap``
    after merging every alias, consecutive periods are still further apart than
    the cadence allows. This is real missing coverage and must be visible.
``no_coverage``
    the company reports none of the tags for this fact type at all.
``dual_tagged``
    one filing reported the same period under two tags in the chain. The
    higher-preference tag wins and the collision is recorded.

None of these stop an ingest. They are attached to the coverage report the
backfill prints and are what ``test_xbrl_alias_coverage_alert`` asserts on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Sequence

from filings.errors import AdapterSchemaError

# Duration buckets, in days, used to tell a quarterly fact from an annual one
# and from a year-to-date one. XBRL periods are not exactly 90/365 days —
# 52/53-week fiscal calendars move them by up to a week.
QUARTER_DAYS = (80, 100)
ANNUAL_DAYS = (340, 400)

#: Largest acceptable spacing between consecutive period ends before the gap is
#: reported. A quarterly cadence is ~91 days, so one missing quarter is ~182;
#: an annual cadence is ~365, so one missing year is ~730.
MAX_QUARTER_GAP_DAYS = 130
MAX_ANNUAL_GAP_DAYS = 500
#: Instant facts (the cover-page share count) appear on every 10-K and 10-Q.
MAX_INSTANT_GAP_DAYS = 200

CADENCE_QUARTER = "quarter"
CADENCE_ANNUAL = "annual"
CADENCE_INSTANT = "instant"
CADENCE_OTHER = "other"

MAX_GAP_DAYS = {
    CADENCE_QUARTER: MAX_QUARTER_GAP_DAYS,
    CADENCE_ANNUAL: MAX_ANNUAL_GAP_DAYS,
    CADENCE_INSTANT: MAX_INSTANT_GAP_DAYS,
}


@dataclass(frozen=True)
class FactTypeSpec:
    """One normalised fact type and the tags filers report it under.

    ``tags`` is a preference order, most specific and most current first. When
    a single filing reports the same period under two of them, the earlier
    entry wins and a ``dual_tagged`` alert records the collision.
    """

    fact_type: str
    taxonomy: str
    tags: tuple[str, ...]
    units: tuple[str, ...]
    instant: bool = False
    note: str = ""

    def preference(self, tag: str) -> int:
        return self.tags.index(tag)


#: The alias map. Every entry is a normalisation decision; the ones with a
#: ``note`` are the ones worth an owner's eye (see docs/SEC_INGESTION.md).
FACT_TYPES: dict[str, FactTypeSpec] = {
    "revenue": FactTypeSpec(
        fact_type="revenue",
        taxonomy="us-gaap",
        tags=(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
            "SalesRevenueGoodsNet",
            "SalesRevenueServicesNet",
            "RevenuesNetOfInterestExpense",
        ),
        units=("USD",),
        note=(
            "Excluding-assessed-tax is preferred over Including because it is "
            "revenue net of sales taxes collected as agent, which is what the "
            "other tags in this chain report. A filer that only tags the "
            "Including variant is used as-is and the difference is the sales "
            "tax, typically small but not zero."
        ),
    ),
    "eps_diluted": FactTypeSpec(
        fact_type="eps_diluted",
        taxonomy="us-gaap",
        tags=(
            "EarningsPerShareDiluted",
            "IncomeLossFromContinuingOperationsPerDilutedShare",
            "EarningsPerShareBasicAndDiluted",
        ),
        units=("USD/shares",),
        note=(
            "Continuing-operations EPS is a different number from total EPS "
            "when a filer has discontinued operations. It is third in the "
            "chain and only used when the filer tags nothing else."
        ),
    ),
    "eps_basic": FactTypeSpec(
        fact_type="eps_basic",
        taxonomy="us-gaap",
        tags=(
            "EarningsPerShareBasic",
            "IncomeLossFromContinuingOperationsPerBasicShare",
            "EarningsPerShareBasicAndDiluted",
        ),
        units=("USD/shares",),
    ),
    "net_income": FactTypeSpec(
        fact_type="net_income",
        taxonomy="us-gaap",
        tags=(
            "NetIncomeLoss",
            "ProfitLoss",
            "NetIncomeLossAvailableToCommonStockholdersBasic",
        ),
        units=("USD",),
        note=(
            "ProfitLoss includes income attributable to non-controlling "
            "interests; NetIncomeLoss does not. They differ for filers with "
            "consolidated subsidiaries."
        ),
    ),
    "shares_outstanding": FactTypeSpec(
        fact_type="shares_outstanding",
        taxonomy="dei",
        tags=("EntityCommonStockSharesOutstanding",),
        units=("shares",),
        instant=True,
        note=(
            "The cover-page share count. companyfacts does not expose the "
            "share-class axis, so multiple classes appear as several entries "
            "sharing an accession and a date; filings.sec_minimal sums the "
            "distinct values and flags the row."
        ),
    ),
    "shares_diluted_weighted_average": FactTypeSpec(
        fact_type="shares_diluted_weighted_average",
        taxonomy="us-gaap",
        tags=(
            "WeightedAverageNumberOfDilutedSharesOutstanding",
            "WeightedAverageNumberOfSharesOutstandingDiluted",
        ),
        units=("shares",),
    ),
}

#: What the coverage checkpoint reports a continuous series for.
CORE_FACT_TYPES = ("revenue", "eps_diluted")


# --- raw facts --------------------------------------------------------------


@dataclass(frozen=True)
class RawFact:
    """One entry from a companyfacts ``units`` array, with its tag attached."""

    fact_type: str
    taxonomy: str
    tag: str
    unit: str
    value: float
    period_end: date
    period_start: date | None
    accession: str
    form: str
    fiscal_year: int | None
    fiscal_period: str | None
    frame: str | None

    @property
    def duration_days(self) -> int | None:
        if self.period_start is None:
            return None
        return (self.period_end - self.period_start).days

    @property
    def cadence(self) -> str:
        days = self.duration_days
        if days is None:
            return CADENCE_INSTANT
        if QUARTER_DAYS[0] <= days <= QUARTER_DAYS[1]:
            return CADENCE_QUARTER
        if ANNUAL_DAYS[0] <= days <= ANNUAL_DAYS[1]:
            return CADENCE_ANNUAL
        return CADENCE_OTHER

    @property
    def period_key(self) -> tuple:
        return (self.period_start, self.period_end, self.unit)


@dataclass(frozen=True)
class CoverageAlert:
    """Something a human should look at before trusting this series."""

    kind: str
    fact_type: str
    entity_cik: str
    detail: str
    context: dict = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return f"[{self.kind}] {self.entity_cik} {self.fact_type}: {self.detail}"


# --- parsing ----------------------------------------------------------------


def _require(payload: Any, key: str, where: str) -> Any:
    if not isinstance(payload, dict):
        raise AdapterSchemaError(f"{where}: expected an object, got {type(payload).__name__}")
    if key not in payload:
        raise AdapterSchemaError(f"{where}: missing {key!r}; keys were {sorted(payload)}")
    return payload[key]


def _parse_date(raw: Any, where: str) -> date:
    if not isinstance(raw, str):
        raise AdapterSchemaError(f"{where}: expected an ISO date string, got {raw!r}")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise AdapterSchemaError(f"{where}: unparseable date {raw!r}") from exc


def parse_fact_entry(entry: Any, spec: FactTypeSpec, tag: str, unit: str) -> RawFact:
    """Turn one companyfacts entry into a :class:`RawFact`, or raise.

    The fields asserted here are the ones verification claim 13 confirmed live:
    ``val``, ``end``, ``accn``, ``form``, ``filed``, ``fy``, ``fp`` (and
    ``frame``, which is only present on facts that map to a calendar frame).
    ``filed`` is required to be present and is deliberately *not* carried onto
    the observation — the timestamp comes from the submissions join.
    """
    where = f"companyfacts {spec.taxonomy}:{tag} [{unit}]"
    value = _require(entry, "val", where)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise AdapterSchemaError(f"{where}: 'val' is not numeric: {value!r}")

    accession = _require(entry, "accn", where)
    if not isinstance(accession, str) or not accession.strip():
        raise AdapterSchemaError(f"{where}: 'accn' is not an accession number: {accession!r}")

    form = _require(entry, "form", where)
    _require(entry, "filed", where)

    period_end = _parse_date(_require(entry, "end", where), f"{where} end")
    raw_start = entry.get("start")
    period_start = _parse_date(raw_start, f"{where} start") if raw_start is not None else None

    if spec.instant and period_start is not None:
        raise AdapterSchemaError(
            f"{where}: {spec.fact_type} is an instant fact but this entry carries "
            f"a period start ({raw_start!r})."
        )

    return RawFact(
        fact_type=spec.fact_type,
        taxonomy=spec.taxonomy,
        tag=tag,
        unit=unit,
        value=float(value),
        period_end=period_end,
        period_start=period_start,
        accession=accession.strip(),
        form=str(form),
        fiscal_year=entry.get("fy"),
        fiscal_period=entry.get("fp"),
        frame=entry.get("frame"),
    )


def facts_for(companyfacts: Any, fact_type: str) -> tuple[list[RawFact], list[str]]:
    """Every alias's facts for one fact type, merged, plus the tags that fired.

    Preference order resolves a period reported under two tags in the same
    filing; the loser is dropped and reported as ``dual_tagged`` by
    :func:`coverage_alerts`.
    """
    spec = FACT_TYPES[fact_type]
    facts = _facts_by_tag(companyfacts, spec)

    # Exact repeats of the same entry are noise, not two facts.
    deduped: dict[tuple, RawFact] = {}
    for fact in facts:
        deduped.setdefault(
            (fact.tag, fact.accession, fact.period_start, fact.period_end, fact.unit, fact.value),
            fact,
        )

    grouped: dict[tuple, list[RawFact]] = {}
    for fact in deduped.values():
        grouped.setdefault(
            (fact.accession, fact.period_start, fact.period_end, fact.unit), []
        ).append(fact)

    # Only a *tag* collision is resolved by preference. Several entries under
    # the same tag on the same key are several facts — that is how companyfacts
    # renders a dual-class cover-page share count, whose class axis it drops —
    # and collapsing them would silently discard a share class.
    chosen: list[RawFact] = []
    for group in grouped.values():
        best = min(spec.preference(f.tag) for f in group)
        chosen.extend(f for f in group if spec.preference(f.tag) == best)

    merged = sorted(
        chosen,
        key=lambda f: (f.period_end, f.period_start or f.period_end, f.accession, f.tag, f.value),
    )
    tags_used = sorted({f.tag for f in merged}, key=spec.preference)
    return merged, tags_used


def _facts_by_tag(companyfacts: Any, spec: FactTypeSpec) -> list[RawFact]:
    facts_block = _require(companyfacts, "facts", "companyfacts")
    if not isinstance(facts_block, dict):
        raise AdapterSchemaError("companyfacts: 'facts' is not an object")

    taxonomy_block = facts_block.get(spec.taxonomy)
    if taxonomy_block is None:
        return []
    if not isinstance(taxonomy_block, dict):
        raise AdapterSchemaError(f"companyfacts: facts.{spec.taxonomy} is not an object")

    out: list[RawFact] = []
    for tag in spec.tags:
        tag_block = taxonomy_block.get(tag)
        if tag_block is None:
            continue
        units = _require(tag_block, "units", f"companyfacts {spec.taxonomy}:{tag}")
        if not isinstance(units, dict):
            raise AdapterSchemaError(
                f"companyfacts {spec.taxonomy}:{tag}: 'units' is not an object"
            )
        for unit, entries in units.items():
            if unit not in spec.units:
                continue
            if not isinstance(entries, list):
                raise AdapterSchemaError(
                    f"companyfacts {spec.taxonomy}:{tag} [{unit}]: expected a list"
                )
            for entry in entries:
                out.append(parse_fact_entry(entry, spec, tag, unit))
    return out


# --- alerts -----------------------------------------------------------------


def coverage_alerts(
    companyfacts: Any,
    fact_type: str,
    entity_cik: str,
    *,
    since: date | None = None,
) -> list[CoverageAlert]:
    """Tag migrations, dual tagging, and real gaps in one fact type's series."""
    spec = FACT_TYPES[fact_type]
    all_facts = _facts_by_tag(companyfacts, spec)
    merged, tags_used = facts_for(companyfacts, fact_type)

    if since is not None:
        merged = [f for f in merged if f.period_end >= since]
        all_facts = [f for f in all_facts if f.period_end >= since]

    alerts: list[CoverageAlert] = []

    if not merged:
        return [
            CoverageAlert(
                kind="no_coverage",
                fact_type=fact_type,
                entity_cik=entity_cik,
                detail=(
                    f"none of {len(spec.tags)} alias tags reported "
                    f"{spec.taxonomy}:{fact_type}"
                ),
                context={"tags": list(spec.tags)},
            )
        ]

    alerts.extend(_dual_tag_alerts(all_facts, merged, fact_type, entity_cik))

    for cadence in (CADENCE_QUARTER, CADENCE_ANNUAL, CADENCE_INSTANT):
        series = [f for f in merged if f.cadence == cadence]
        if len(series) < 2:
            continue
        alerts.extend(_migration_alerts(series, fact_type, entity_cik, cadence))
        alerts.extend(_gap_alerts(series, fact_type, entity_cik, cadence))

    if len(tags_used) > 1:
        alerts.append(
            CoverageAlert(
                kind="alias_map_used",
                fact_type=fact_type,
                entity_cik=entity_cik,
                detail=(
                    f"series assembled from {len(tags_used)} tags: "
                    + ", ".join(tags_used)
                ),
                context={"tags": tags_used},
            )
        )

    return alerts


def _dual_tag_alerts(all_facts, merged, fact_type, entity_cik) -> list[CoverageAlert]:
    kept: dict[tuple, RawFact] = {}
    for fact in merged:
        kept.setdefault((fact.accession, fact.period_start, fact.period_end, fact.unit), fact)
    collisions: dict[tuple, set[str]] = {}
    for fact in all_facts:
        key = (fact.accession, fact.period_start, fact.period_end, fact.unit)
        winner = kept.get(key)
        if winner is not None and fact.tag != winner.tag:
            collisions.setdefault(key, set()).add(fact.tag)
    out = []
    for key, losers in sorted(collisions.items(), key=lambda kv: str(kv[0])):
        winner = kept[key]
        out.append(
            CoverageAlert(
                kind="dual_tagged",
                fact_type=fact_type,
                entity_cik=entity_cik,
                detail=(
                    f"{key[0]} period ending {key[2]} was reported under "
                    f"{winner.tag} and {', '.join(sorted(losers))}; kept {winner.tag}"
                ),
                context={
                    "accession": key[0],
                    "period_end": key[2].isoformat(),
                    "kept": winner.tag,
                    "dropped": sorted(losers),
                },
            )
        )
    return out


def _first_reported(series: Sequence[RawFact]) -> list[RawFact]:
    """One fact per period: the earliest accession that reported it.

    Restatements re-report an old period under whatever tag is current, which
    would otherwise look like a migration in the wrong direction.
    """
    by_period: dict[tuple, RawFact] = {}
    for fact in sorted(series, key=lambda f: (f.period_end, f.accession)):
        by_period.setdefault(fact.period_key, fact)
    return sorted(by_period.values(), key=lambda f: f.period_end)


def _migration_alerts(series, fact_type, entity_cik, cadence) -> list[CoverageAlert]:
    ordered = _first_reported(series)
    out = []
    for previous, current in zip(ordered, ordered[1:]):
        if previous.tag != current.tag:
            out.append(
                CoverageAlert(
                    kind="tag_migration",
                    fact_type=fact_type,
                    entity_cik=entity_cik,
                    detail=(
                        f"{cadence} series moved from {previous.tag} "
                        f"(period ending {previous.period_end}) to {current.tag} "
                        f"(period ending {current.period_end}); the alias map "
                        "kept the series continuous"
                    ),
                    context={
                        "cadence": cadence,
                        "from_tag": previous.tag,
                        "to_tag": current.tag,
                        "from_period_end": previous.period_end.isoformat(),
                        "to_period_end": current.period_end.isoformat(),
                    },
                )
            )
    return out


def _gap_alerts(series, fact_type, entity_cik, cadence) -> list[CoverageAlert]:
    limit = MAX_GAP_DAYS[cadence]
    ends = sorted({f.period_end for f in series})
    out = []
    for previous, current in zip(ends, ends[1:]):
        spacing = (current - previous).days
        if spacing > limit:
            out.append(
                CoverageAlert(
                    kind="series_gap",
                    fact_type=fact_type,
                    entity_cik=entity_cik,
                    detail=(
                        f"{cadence} series jumps {spacing} days from {previous} to "
                        f"{current} (limit {limit}); no alias tag covers the gap"
                    ),
                    context={
                        "cadence": cadence,
                        "gap_after": previous.isoformat(),
                        "gap_before": current.isoformat(),
                        "days": spacing,
                    },
                )
            )
    return out


def has_continuous_series(companyfacts: Any, fact_type: str, entity_cik: str, *, since: date | None = None) -> bool:
    """True when the merged series has facts and no ``series_gap`` alert."""
    alerts = coverage_alerts(companyfacts, fact_type, entity_cik, since=since)
    kinds = {a.kind for a in alerts}
    return "no_coverage" not in kinds and "series_gap" not in kinds
