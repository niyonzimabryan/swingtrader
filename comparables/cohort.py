"""Cohort construction from stored data (Spec N §4).

Phase 3b built the maths over in-memory dataclasses. This module is the other
half: it turns *stored rows* into the `EventRecord` / `TradingCalendar` shapes
that `comparables/report.py` already consumes, and it is the only place in the
engine that touches a database.

It obeys five rules, and each one has a test named after it.

**Facts come through one seam.** Every qualifying fact is read with
:func:`filings.observations.observations_known_at`, which applies
`known_at_utc <= event_cutoff` in SQL. There is no raw query on
`source_observations` anywhere in this package, and no import of
`filings.sec_minimal` — the Phase 4 planes are behind the same door.
(`test_lookahead_rejected`, `test_sec_minimal_contract_in_phase3`.)

**Membership is stored, not computed.** Universe membership is read as of the
event date from `universe_membership` (Spec N §4.2), never from today's list. A
universe with no membership rows covering the period does not silently fall
back to "every security we happen to have": the cohort is capped at
`archival_reconstructed` and says so. (`test_universe_is_stored_not_computed`,
`test_survivorship_downgrades_tier`.)

**A delisted name stays in, with its terminal outcome.** The terminal return
comes from the venue-specific Shumway convention in `comparables.config` when
the snapshot's delisting audit says the price file *stops* rather than
collapsing, and from the stored series itself when the audit says it carries
the collapse. A snapshot with no recorded audit cannot back a `clean_pit` or
`vendor_pit` cohort at all. (`test_delisting_audit_recorded`.)

**A covariate is never invented.** `market_cap_decile` needs a share count that
was knowable at the event date; a name without one is excluded by name and
reason, never computed from a later count. (`test_market_cap_has_share_source`.)

**The analog ranker may propose, never select.** `build_cohort` accepts a
`candidate_generator` that can reorder or truncate the candidate list; the
qualified set, and therefore every statistic, is invariant to it.
(`test_analog_generator_adds_no_bias`.)
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from data.prices import derived, store
from data.prices.base import DailyBar
from filings.observations import day_precision_known_at, observations_known_at

from comparables import config
from comparables.outcomes import (
    Bar,
    CorporateAction,
    EventRecord,
    PolicySpec,
    PriceSeries,
    TerminalOutcome,
    TradingCalendar,
)
from comparables.setup_spec import Condition, SetupSpec
from comparables.setups import (
    SOURCE_EARNINGS_8K,
    SOURCE_PENDING,
    SOURCE_PRICE_SESSION,
    RosterEntry,
    source_for_spec,
)

# --------------------------------------------------------------------------- #
# Vocabularies
# --------------------------------------------------------------------------- #

PROVENANCE_ORDER = ("observed_live", "vendor_pit", "archival_reconstructed")

FACT_TYPE_EARNINGS_8K = "earnings_release_8k_item_202"
FACT_TYPE_EPS = "eps_diluted"
FACT_TYPE_SHARES = "shares_outstanding"

#: The only EDGAR field a `known_at_utc` may come from. `filings/observations.py`
#: refuses anything else at write time; this is the read-time half, because a
#: row written before that rule existed would otherwise slip through.
EDGAR_KNOWN_AT_FIELD = "acceptanceDateTime"

#: How much a universe's membership source is worth as provenance. Unknown
#: sources are `archival_reconstructed`, which is the conservative direction:
#: a membership table nobody has classified is a reconstruction until shown
#: otherwise.
MEMBERSHIP_PROVENANCE: dict[str, str] = {
    "rule:liquid_us_equity_v1": "vendor_pit",
    "fixture": "vendor_pit",
    "sp500_wikipedia_v1": "archival_reconstructed",
}

#: The delisting reasons that carry a configured terminal return (§4.2). A
#: merger without stored deal terms and an unexplained stop are *censored*,
#: not matured — conflating them is how terminal losses disappear (§4.4).
RESOLVED_DELISTING_REASONS = ("performance",)


class CohortConstructionError(RuntimeError):
    """The cohort could not be built at all — not the same as `insufficient`."""


class PendingPlane(CohortConstructionError):
    """A setup whose evidence plane does not exist yet (Spec N §4.0)."""


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CohortContext:
    """Where the stored data lives. Everything else is derived from it."""

    universe_slug: str
    price_snapshot_slug: str
    benchmark_security_uid: str
    risk_free_daily: float = 0.0
    #: `((ticker, cik), ...)` for the `market_cap_decile` join. The price
    #: plane's security master has no CIK column and the SEC feed stamps a
    #: ticker, so the join is explicit and injected rather than guessed.
    #: Phase 4's entity-history plane replaces it with a stored point-in-time
    #: mapping. A cohort that needs `market_cap_decile` and has no map refuses
    #: every name by reason, which is the point (`test_market_cap_has_share_source`).
    cik_by_ticker: tuple[tuple[str, str], ...] = ()

    @property
    def cik_map(self) -> dict[str, str]:
        return {t.upper(): c for t, c in self.cik_by_ticker}

    def __post_init__(self) -> None:
        for name in ("universe_slug", "price_snapshot_slug", "benchmark_security_uid"):
            if not getattr(self, name):
                raise CohortConstructionError(
                    f"CohortContext.{name} is required: a cohort must be able to "
                    f"name the universe, the price snapshot and the benchmark it "
                    f"was built on (Spec N §4.2, §5.2)"
                )


@dataclass(frozen=True)
class ExcludedEvent:
    """A candidate that did not become a cohort member, and exactly why.

    Exclusions are data, not a log line: `market_cap_decile` refused for want
    of a share count and `gap_pct` below the threshold are different facts
    about the cohort, and both belong beside it.
    """

    event_id: str
    ticker: str
    event_date: date
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class SubjectQualification:
    """Whether one named ticker meets the setup's conditions, and when.

    A `SetupSpec` is a *pattern*, not a name (Spec N §4.0), so "the same ticker"
    in Spec L §6.6 is not a property of the spec and cannot be recovered from a
    stored answer after the fact. It is a property of the **query**: the caller
    said which name it was asking about, and the engine ran that name through
    the same qualification the cohort members went through and wrote the verdict
    down beside the answer.

    `qualifies` is about the subject's **most recent candidate at or before
    `as_of`** — the last time the setup had an opportunity to fire for that
    name. `event_date` is when that was, and it is stored precisely because
    "qualified" and "qualified nine months ago" are different statements and the
    citation-age rule bounds the age of the *answer*, not of the event. A
    reader gets both.
    """

    ticker: str
    qualifies: bool
    reason: str
    event_id: str = ""
    event_date: date | None = None

    def as_dict(self) -> dict:
        """The wire shape. `event_id` is deliberately absent.

        A replayed answer reconstructs this from stored columns, and the event
        id is not one of them — the date is what a reader needs and the id would
        be `None` on half the paths, which is worse than a field that is never
        there at all.
        """
        return {
            "ticker": self.ticker,
            "qualifies": self.qualifies,
            "reason": self.reason,
            "event_date": self.event_date.isoformat() if self.event_date else None,
        }


@dataclass(frozen=True)
class UniverseStatus:
    """Whether membership could be established point-in-time (§4.2)."""

    slug: str
    point_in_time: bool
    reason: str
    sources: tuple[str, ...]
    provenance: str
    n_member_dates: int
    n_members_seen: int


@dataclass(frozen=True)
class SnapshotStatus:
    """The price snapshot a cohort ran on, and what its audit said (§4.2)."""

    slug: str
    snapshot_id: int | None
    exists: bool
    audit_recorded: bool
    audit_reason: str
    terminal_returns_synthesised: bool
    collapse_rate_of_classified: float | None
    source: str | None

    @property
    def can_back_point_in_time(self) -> bool:
        """A snapshot with no recorded audit backs `archival_reconstructed` only."""
        return self.exists and self.audit_recorded


@dataclass(frozen=True)
class CohortBuild:
    """A constructed cohort plus everything needed to say what it rests on."""

    setup: SetupSpec
    as_of: date
    context: CohortContext
    calendar: TradingCalendar
    benchmark: PriceSeries
    events: tuple[EventRecord, ...]
    excluded: tuple[ExcludedEvent, ...]
    #: Every candidate that was *eligible* on its date — a universe member with
    #: computable covariates — whether or not it went on to meet the setup's
    #: conditions. This is the "point-in-time eligible universe", and it is used
    #: twice: as the comparison group for the §4.5 matched-vs-pool diagnostic
    #: (without it the SMD is computed against the cohort itself and always says
    #: "balanced", which is how a cohort quietly becomes the twenty most liquid
    #: names in the pool), and as the draw pool for §6.3's random-cohort null
    #: test, which needs whole events rather than covariate maps.
    pool: tuple[EventRecord, ...]
    universe: UniverseStatus
    snapshot: SnapshotStatus
    evidence_cap: str
    universe_delisting_rate: float | None
    sector_codes: tuple[tuple[str, float], ...]
    warnings: tuple[str, ...]
    candidate_order: tuple[str, ...]
    n_candidates: int
    #: The §6.6 subject, when the query named one. `None` means the question was
    #: asked about the pattern and about no particular name, which is the
    #: ordinary case and is *not* the same as "the name did not qualify".
    subject: SubjectQualification | None = None

    @property
    def provenance_mix(self) -> dict[str, int]:
        mix = {name: 0 for name in PROVENANCE_ORDER}
        for event in self.events:
            mix[event.provenance] += 1
        return mix

    def blocks(self) -> "ProvenanceBlocks":
        """Split the cohort so archival facts are never pooled (§8)."""
        return ProvenanceBlocks(
            point_in_time=tuple(
                e for e in self.events if e.provenance != "archival_reconstructed"
            ),
            archival=tuple(
                e for e in self.events if e.provenance == "archival_reconstructed"
            ),
        )


@dataclass(frozen=True)
class ProvenanceBlocks:
    """`observed_live` and `vendor_pit` may be pooled; `archival` never is.

    Spec N §8: *"`archival_reconstructed` results are never combined with
    `clean_pit` results in the same statistic. They are displayed in a separate
    block."* Phase 3b implemented the safe half — a mixed cohort was refused.
    This is the other half: two blocks, each measured on its own, and a
    `provenance_mix` that sums to n across them.
    """

    point_in_time: tuple[EventRecord, ...]
    archival: tuple[EventRecord, ...]

    @property
    def mix(self) -> dict[str, int]:
        mix = {name: 0 for name in PROVENANCE_ORDER}
        for event in (*self.point_in_time, *self.archival):
            mix[event.provenance] += 1
        return mix

    @property
    def n(self) -> int:
        return len(self.point_in_time) + len(self.archival)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def worst_provenance(classes: Iterable[str]) -> str:
    """The weakest class present. Two good sources and one guess is a guess."""
    worst = 0
    for name in classes:
        if name not in PROVENANCE_ORDER:
            raise CohortConstructionError(f"unknown provenance class {name!r}")
        worst = max(worst, PROVENANCE_ORDER.index(name))
    return PROVENANCE_ORDER[worst]


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _session_close(day: date) -> datetime:
    return datetime.combine(day, config.SESSION_CLOSE_UTC)


def _last_session_closed_by(sessions: Sequence[date], cutoff: datetime) -> date | None:
    """The last session whose close is at or before `cutoff`.

    A covariate is computed from a session's bar, and that bar does not exist
    until the session has closed. Picking the calendar date instead would give
    an 11:00 UTC filing the close of a session that had not happened yet.
    """
    chosen: date | None = None
    for day in sessions:
        if _session_close(day) <= cutoff:
            chosen = day
        else:
            break
    return chosen


def price_series_from_bars(bars: Sequence[DailyBar]) -> PriceSeries:
    """The three stored series plus the factors between them (Spec N §4.3).

    `price_bars` stores raw OHLCV and the two adjusted *closes*; the adjusted
    open, high and low are that session's close ratio applied to its own raw
    prices, which is what an adjustment factor is. Nothing is re-derived from
    the factor chain here — `data.prices.derived.check_reconstruction` already
    guarantees the stored closes and the stored factors agree.
    """
    if not bars:
        raise CohortConstructionError("cannot build a price series from no bars")
    raw: list[Bar] = []
    split_adjusted: list[Bar] = []
    total_return: list[Bar] = []
    actions: list[CorporateAction] = []

    for bar in bars:
        raw.append(Bar(bar.session_date, bar.raw_open, bar.raw_high, bar.raw_low, bar.raw_close))
        fs = bar.split_adjusted_close / bar.raw_close
        split_adjusted.append(Bar(
            bar.session_date, bar.raw_open * fs, bar.raw_high * fs,
            bar.raw_low * fs, bar.split_adjusted_close,
        ))
        ft = bar.total_return_close / bar.raw_close
        total_return.append(Bar(
            bar.session_date, bar.raw_open * ft, bar.raw_high * ft,
            bar.raw_low * ft, bar.total_return_close,
        ))
        if bar.split_factor != 1.0:
            actions.append(CorporateAction(bar.session_date, "split", bar.split_factor))
        if bar.dividend_cash:
            actions.append(CorporateAction(bar.session_date, "dividend", bar.dividend_cash))

    return PriceSeries(
        ticker=bars[0].ticker,
        raw=tuple(raw),
        split_adjusted=tuple(split_adjusted),
        total_return=tuple(total_return),
        actions=tuple(actions),
    )


def _adjusted_closes(bars: Sequence[DailyBar]) -> list[float]:
    return [b.split_adjusted_close for b in bars]


def _index_upto(bars: Sequence[DailyBar], day: date) -> int:
    """Index of the bar on `day`, or -1 when the name did not trade that day."""
    for i, bar in enumerate(bars):
        if bar.session_date == day:
            return i
    return -1


# --------------------------------------------------------------------------- #
# Point-in-time covariates and price-derived facts
# --------------------------------------------------------------------------- #


def gap_pct(bars: Sequence[DailyBar], index: int) -> float | None:
    """Open against the prior split-adjusted close, in percent.

    Split-adjusted on both sides deliberately: an economically neutral 2-for-1
    is a -50% raw gap and is not an event (Spec N §4.3).
    """
    if index < 1:
        return None
    today, prior = bars[index], bars[index - 1]
    open_adjusted = today.raw_open * (today.split_adjusted_close / today.raw_close)
    if prior.split_adjusted_close <= 0:
        return None
    return (open_adjusted / prior.split_adjusted_close - 1.0) * 100.0


def dist_from_sma50(bars: Sequence[DailyBar], index: int, window: int = 50) -> float | None:
    """Close against its `window`-session simple moving average, in percent."""
    if index + 1 < window:
        return None
    closes = _adjusted_closes(bars[index + 1 - window:index + 1])
    sma = sum(closes) / len(closes)
    if sma <= 0:
        return None
    return (closes[-1] / sma - 1.0) * 100.0


def atr_pct(bars: Sequence[DailyBar], index: int, window: int = 14) -> float | None:
    """Average true range over `window` sessions as a percent of the close."""
    if index < window:
        return None
    trs: list[float] = []
    for i in range(index - window + 1, index + 1):
        bar, prev = bars[i], bars[i - 1]
        scale = bar.split_adjusted_close / bar.raw_close
        high, low = bar.raw_high * scale, bar.raw_low * scale
        trs.append(max(high - low, abs(high - prev.split_adjusted_close),
                       abs(low - prev.split_adjusted_close)))
    close = bars[index].split_adjusted_close
    if close <= 0:
        return None
    return sum(trs) / len(trs) / close * 100.0


def _dollar_volume(bars: Sequence[DailyBar], day: date) -> float | None:
    try:
        return derived.median_dollar_volume(bars, day, derived.DEFAULT_WINDOW_SESSIONS)
    except derived.SeriesError:
        return None


def _realized_vol(bars: Sequence[DailyBar], day: date) -> float | None:
    try:
        return derived.realized_volatility(bars, day, derived.DEFAULT_WINDOW_SESSIONS)
    except (derived.SeriesError, statistics.StatisticsError):
        return None


# --------------------------------------------------------------------------- #
# Stored facts, always through the one seam
# --------------------------------------------------------------------------- #


def _known_at_source(row) -> str:
    payload = row.payload or {}
    if isinstance(payload, dict):
        return str(payload.get("known_at_source") or "")
    return ""


def shares_outstanding_known_at(session, *, entity_cik: str, cutoff: datetime):
    """The latest share count knowable at `cutoff`, or `None`.

    `None` is a real answer, and the only honest one: Spec N §4.0 says market
    cap needs a share count and the price file does not carry one, so a name
    without one has no market cap — never a market cap computed from a share
    count it could not have had.
    """
    rows = observations_known_at(
        session, fact_type=FACT_TYPE_SHARES, cutoff=cutoff, entity_cik=entity_cik
    )
    usable = [r for r in rows if r.value_numeric and r.value_numeric > 0]
    return usable[-1] if usable else None


def seasonal_sue(session, *, entity_cik: str, cutoff: datetime) -> tuple[float | None, str]:
    """The seasonal random-walk SUE knowable at `cutoff` (Spec N §4.0).

    Actual diluted EPS minus the same quarter a year earlier, scaled by the
    standard deviation of that difference, computed **entirely** from XBRL
    `companyfacts` rows in `source_observations`. There is no vendor estimate
    field anywhere on this path and no analyst consensus: no retail source
    offers a verifiable point-in-time consensus archive, and a restated one
    used as a pre-print fact is lookahead in the direction that flatters the
    engine. (`test_sue_from_xbrl_only`.)

    Returns `(sue, reason)`. `sue` is `None` whenever the history is too short
    or the seasonal differences have no spread, and `reason` says which.
    """
    rows = observations_known_at(
        session, fact_type=FACT_TYPE_EPS, cutoff=cutoff, entity_cik=entity_cik
    )
    by_period: dict[date, float] = {}
    for row in rows:
        if row.value_numeric is None:
            continue
        # `observations_known_at` orders by valid_at then known_at, so the last
        # row for a period is the latest restatement that was knowable then.
        by_period[_aware(row.valid_at).date()] = float(row.value_numeric)

    if len(by_period) < config.SUE_MIN_SEASONAL_DIFFS + 4:
        return None, f"eps_history_too_short:{len(by_period)}_periods"

    periods = sorted(by_period)
    tolerance = timedelta(days=config.SUE_SEASONAL_MATCH_DAYS)
    diffs: list[tuple[date, float]] = []
    for period in periods:
        target = period - timedelta(days=365)
        prior = min(
            (p for p in periods if abs(p - target) <= tolerance),
            key=lambda p: (abs(p - target), p),
            default=None,
        )
        if prior is None:
            continue
        diffs.append((period, by_period[period] - by_period[prior]))

    if len(diffs) < config.SUE_MIN_SEASONAL_DIFFS:
        return None, f"seasonal_pairs_too_few:{len(diffs)}"

    latest_period, latest_diff = diffs[-1]
    history = [d for _, d in diffs[:-1]]
    if len(history) < 2:
        return None, "seasonal_history_too_few"
    sigma = statistics.stdev(history)
    if sigma <= 0.0:
        return None, "seasonal_sigma_zero"
    return latest_diff / sigma, f"period:{latest_period.isoformat()}"


def _announced_period(
    session, *, entity_cik: str, announced: datetime, as_of_cutoff: datetime
) -> date | None:
    """The fiscal period an 8-K accepted at `announced` is reporting.

    The latest EPS period end at or before the announcement. Reading it off the
    *facts* rather than off the 8-K payload keeps one definition of a period in
    the engine, and the 8-K index's `report_date` is a filer-supplied date the
    ledger already refuses to let become a `known_at_utc`.
    """
    rows = observations_known_at(
        session, fact_type=FACT_TYPE_EPS, cutoff=as_of_cutoff, entity_cik=entity_cik
    )
    periods = [
        _aware(r.valid_at).date() for r in rows
        if r.value_numeric is not None and _aware(r.valid_at).date() <= announced.date()
    ]
    if not periods:
        return None
    latest = max(periods)
    if (announced.date() - latest).days > config.SUE_MAX_ANNOUNCEMENT_LAG_DAYS:
        # The announced quarter is missing from the ledger entirely. Reaching
        # back to the previous one would qualify the event on a four-month-old
        # surprise, and a company that stops filing XBRL would go on being
        # qualified on its last known quarter forever.
        return None
    return latest


def _eps_known_at_for_period(session, *, entity_cik: str, cutoff: datetime,
                             period: date) -> datetime | None:
    """When the EPS for `period` first became knowable, at or before `cutoff`."""
    rows = observations_known_at(
        session, fact_type=FACT_TYPE_EPS, cutoff=cutoff, entity_cik=entity_cik
    )
    stamps = [
        _aware(r.known_at_utc) for r in rows
        if r.value_numeric is not None and _aware(r.valid_at).date() == period
    ]
    return min(stamps) if stamps else None


# --------------------------------------------------------------------------- #
# Condition evaluation
# --------------------------------------------------------------------------- #


def _compare(op: str, left: Any, right: Any) -> bool:
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == "in":
        return left in right
    if op == "not_in":
        return left not in right
    raise CohortConstructionError(f"unsupported operator {op!r}")


def evaluate_conditions(
    conditions: Sequence[Condition], facts: Mapping[str, Any]
) -> tuple[bool, str]:
    """`(passed, reason)`. A missing fact is a refusal, never a default."""
    for condition in conditions:
        if condition.fact not in facts or facts[condition.fact] is None:
            return False, f"missing_fact:{condition.fact}"
        if not _compare(condition.op, facts[condition.fact], condition.value):
            return False, f"condition_failed:{condition.fact}{condition.op}{condition.value!r}"
    return True, "qualified"


# --------------------------------------------------------------------------- #
# Candidate records, before the covariates and the outcome are attached
# --------------------------------------------------------------------------- #


@dataclass
class _Candidate:
    event_id: str
    security_uid: str
    ticker: str
    entity_cik: str | None
    covariate_session: date
    cutoff: datetime
    announced_at: datetime | None
    facts: dict[str, Any] = field(default_factory=dict)
    provenance: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# The build
# --------------------------------------------------------------------------- #


CandidateGenerator = Callable[[Sequence[str]], Sequence[str]]


def build_cohort(
    session,
    setup: SetupSpec | RosterEntry,
    *,
    as_of: date,
    context: CohortContext,
    candidate_source: str | None = None,
    candidate_generator: CandidateGenerator | None = None,
    require_sue_at_announcement: bool = False,
    subject_ticker: str = "",
) -> CohortBuild:
    """Turn stored rows into the cohort the report layer measures.

    `subject_ticker` names the one security the question is *about* (Spec L
    §6.6). It changes nothing about the cohort — the same candidates are
    generated and the same ones qualify — and is answered by reading the verdict
    the ordinary qualification pass already reached for that name, so there is
    one qualification path and not two.

    `candidate_generator` is the §4.5 seam for `data/analog_ranker.py`: it may
    reorder or truncate the candidate identifiers, and the qualified set is
    computed over **all** candidates regardless, so the statistics are
    invariant to whatever it does. The order it proposed is recorded on the
    build so the invariance is checkable rather than asserted.
    """
    entry = setup if isinstance(setup, RosterEntry) else None
    spec = entry.spec if entry is not None else setup
    source = candidate_source or (
        entry.candidate_source if entry is not None else source_for_spec(spec)
    )

    if entry is not None and not entry.available:
        raise PendingPlane(entry.unavailable_reason or "pending_plane")
    if source == SOURCE_PENDING:
        raise PendingPlane(
            f"pending_plane: {spec.slug} has no evidence plane in this phase"
        )

    warnings: list[str] = []
    as_of_cutoff = day_precision_known_at(as_of)

    snapshot = _snapshot_status(session, context.price_snapshot_slug)
    if not snapshot.exists:
        warnings.append(
            f"no price snapshot named {context.price_snapshot_slug!r}: the cohort "
            f"cannot name the price file it ran on, so it is capped at "
            f"archival_reconstructed (Spec N §4.2)"
        )
    elif not snapshot.audit_recorded:
        warnings.append(
            f"price snapshot {context.price_snapshot_slug!r} carries no delisting "
            f"audit: a file that merely stops at the last quote looks identical "
            f"to one that carried the collapse, so the cohort is capped at "
            f"archival_reconstructed (Spec N §4.2)"
        )

    start = as_of - timedelta(days=365 * spec.lookback_years)
    sessions = store.session_dates(session, start=start, end=as_of)
    if len(sessions) < derived.DEFAULT_WINDOW_SESSIONS + 2:
        raise CohortConstructionError(
            f"{len(sessions)} stored sessions between {start} and {as_of}; a cohort "
            f"needs at least {derived.DEFAULT_WINDOW_SESSIONS + 2} to compute a "
            f"single point-in-time covariate"
        )
    calendar = TradingCalendar(sessions)

    bars_by_uid = store.load_all_bars(session, start=start, end=as_of)
    benchmark_bars = bars_by_uid.get(context.benchmark_security_uid)
    if not benchmark_bars:
        raise CohortConstructionError(
            f"no stored bars for benchmark security {context.benchmark_security_uid!r} "
            f"between {start} and {as_of}; every abnormal return in Spec N §5.2 is "
            f"measured against it, so there is nothing to measure"
        )
    benchmark = price_series_from_bars(benchmark_bars)
    bars_by_uid.pop(context.benchmark_security_uid, None)

    securities = _securities_by_uid(session)
    membership = _membership_index(session, context.universe_slug, sessions)
    universe = _universe_status(context.universe_slug, membership, sessions)
    if not universe.point_in_time:
        warnings.append(universe.reason)

    evidence_cap = worst_provenance([
        universe.provenance,
        "vendor_pit" if snapshot.can_back_point_in_time else "archival_reconstructed",
    ])

    candidates, excluded = _candidates(
        session,
        spec,
        source,
        sessions=sessions,
        bars_by_uid=bars_by_uid,
        securities=securities,
        membership=membership,
        universe=universe,
        as_of_cutoff=as_of_cutoff,
        require_sue_at_announcement=require_sue_at_announcement,
    )

    proposed = tuple(c.event_id for c in candidates)
    if candidate_generator is not None:
        # A generator may propose any order and any subset. It changes nothing:
        # the qualified set below is built from `candidates`, untouched.
        proposed = tuple(candidate_generator([c.event_id for c in candidates]))

    events, more_excluded, pool, sector_codes, cov_warnings, subject = _qualify(
        session,
        spec,
        candidates,
        bars_by_uid=bars_by_uid,
        securities=securities,
        membership=membership,
        universe=universe,
        calendar=calendar,
        snapshot=snapshot,
        evidence_cap=evidence_cap,
        cik_map=context.cik_map,
        subject_ticker=subject_ticker,
    )
    excluded = (*excluded, *more_excluded)
    warnings.extend(cov_warnings)

    # The pool and the universe delisting rate are bounded by the cohort's own
    # span. Without that, an answer moves when a *later* month's universe
    # rebuild or a *later* candidate lands — facts published after every event
    # in the cohort — which the §10 lookahead harness catches and is right to.
    if events:
        last_cutoff = max(e.known_at_utc for e in events)
        pool = tuple(p for p in pool if p.known_at_utc <= last_cutoff)
        span = [d for d in sessions if d <= last_cutoff.date()] or list(sessions)
    else:
        span = list(sessions)
    universe_delisting_rate = _universe_delisting_rate(
        membership, securities, span
    )

    return CohortBuild(
        setup=spec,
        as_of=as_of,
        context=context,
        calendar=calendar,
        benchmark=benchmark,
        events=events,
        excluded=excluded,
        pool=pool,
        universe=universe,
        snapshot=snapshot,
        evidence_cap=evidence_cap,
        universe_delisting_rate=universe_delisting_rate,
        sector_codes=sector_codes,
        warnings=tuple(warnings),
        candidate_order=proposed,
        n_candidates=len(candidates),
        subject=subject,
    )


# --------------------------------------------------------------------------- #
# Stored-state helpers
# --------------------------------------------------------------------------- #


def snapshot_status(session, slug: str) -> SnapshotStatus:
    """The named price snapshot and what its delisting audit said (§4.2).

    Public because a caller has to be able to name the snapshot in a stored
    query *before* it builds anything — the cache key includes it.
    """
    return _snapshot_status(session, slug)


def _snapshot_status(session, slug: str) -> SnapshotStatus:
    row = store.get_snapshot(session, slug)
    if row is None:
        return SnapshotStatus(
            slug=slug, snapshot_id=None, exists=False, audit_recorded=False,
            audit_reason="no_snapshot_row", terminal_returns_synthesised=True,
            collapse_rate_of_classified=None, source=None,
        )
    audit = row.delisting_audit or {}
    recorded = bool(audit) and bool(audit.get("counts"))
    return SnapshotStatus(
        slug=slug,
        snapshot_id=row.id,
        exists=True,
        audit_recorded=recorded,
        audit_reason="audit_recorded" if recorded else "delisting_audit_json_empty",
        # With no audit, assume the file stops rather than collapses: applying
        # the Shumway return to a series that already carried the collapse
        # double-counts, but assuming a collapse that is not there deletes the
        # loss entirely, and only one of those is survivorship bias.
        terminal_returns_synthesised=(
            bool(audit.get("terminal_returns_must_be_synthesised", True))
            if recorded else True
        ),
        collapse_rate_of_classified=audit.get("collapse_rate_of_classified"),
        source=row.source,
    )


def _securities_by_uid(session) -> dict[str, list]:
    out: dict[str, list] = {}
    for row in store.load_securities(session):
        out.setdefault(row.security_uid, []).append(row)
    return out


def _security_for(securities: Mapping[str, Sequence], uid: str, day: date):
    rows = securities.get(uid) or []
    for row in rows:
        starts = row.ticker_valid_from is None or row.ticker_valid_from <= day
        ends = row.ticker_valid_to is None or day < row.ticker_valid_to
        if starts and ends:
            return row
    return rows[-1] if rows else None


def _membership_index(session, slug: str, sessions: Sequence[date]) -> dict[date, tuple]:
    """`{session_date: (rows,)}` — the stored point-in-time filter, per date.

    One query per session date is the honest implementation of "membership is
    evaluated as of the event date": the interval arithmetic lives in
    `data.prices.store.members_as_of`, in SQL, in one place.
    """
    return {day: store.members_as_of(session, slug, day) for day in sessions}


def _universe_status(
    slug: str, membership: Mapping[date, Sequence], sessions: Sequence[date]
) -> UniverseStatus:
    dates_with_members = [d for d, rows in membership.items() if rows]
    seen = {row.security_uid for rows in membership.values() for row in rows}
    sources = tuple(sorted({row.source for rows in membership.values() for row in rows}))
    if not dates_with_members:
        return UniverseStatus(
            slug=slug,
            point_in_time=False,
            reason=(
                f"universe {slug!r} has no universe_membership rows covering "
                f"{sessions[0]}..{sessions[-1]}. Membership evaluated from today's "
                f"list is survivorship bias wearing a disguise, so the cohort is "
                f"capped at archival_reconstructed and every name in range is "
                f"treated as eligible (Spec N §4.2)"
            ),
            sources=(),
            provenance="archival_reconstructed",
            n_member_dates=0,
            n_members_seen=0,
        )
    provenance = worst_provenance(
        [MEMBERSHIP_PROVENANCE.get(s, "archival_reconstructed") for s in sources]
    )
    return UniverseStatus(
        slug=slug,
        point_in_time=True,
        reason="membership read as of each event date from universe_membership",
        sources=sources,
        provenance=provenance,
        n_member_dates=len(dates_with_members),
        n_members_seen=len(seen),
    )


def _eligible_uids(
    membership: Mapping[date, Sequence], universe: UniverseStatus,
    bars_by_uid: Mapping[str, Sequence[DailyBar]], day: date,
) -> tuple[str, ...]:
    if not universe.point_in_time:
        return tuple(sorted(bars_by_uid))
    return tuple(sorted({row.security_uid for row in membership.get(day, ())}))


def _universe_delisting_rate(
    membership: Mapping[date, Sequence],
    securities: Mapping[str, Sequence],
    sessions: Sequence[date],
) -> float | None:
    """Share of the universe's members that performance-delisted in the window.

    The §4.2 composition check needs a denominator that is *the universe*, not
    the cohort: a cohort with no delistings is only suspicious when the pool it
    came from had some.
    """
    span = set(sessions)
    members = {
        row.security_uid
        for day, rows in membership.items() if day in span
        for row in rows
    }
    if not members:
        return None
    first, last = sessions[0], sessions[-1]
    delisted = 0
    for uid in members:
        rows = securities.get(uid) or []
        for row in rows:
            if (row.delisting_date is not None
                    and row.delisting_reason in RESOLVED_DELISTING_REASONS
                    and first <= row.delisting_date <= last):
                delisted += 1
                break
    return delisted / len(members)


# --------------------------------------------------------------------------- #
# Candidates
# --------------------------------------------------------------------------- #


def _candidates(
    session,
    spec: SetupSpec,
    source: str,
    *,
    sessions: Sequence[date],
    bars_by_uid: Mapping[str, Sequence[DailyBar]],
    securities: Mapping[str, Sequence],
    membership: Mapping[date, Sequence],
    universe: UniverseStatus,
    as_of_cutoff: datetime,
    require_sue_at_announcement: bool,
) -> tuple[list[_Candidate], tuple[ExcludedEvent, ...]]:
    if source == SOURCE_PRICE_SESSION:
        return _price_session_candidates(
            sessions=sessions, bars_by_uid=bars_by_uid,
            membership=membership, universe=universe,
        )
    if source == SOURCE_EARNINGS_8K:
        return _earnings_candidates(
            session, spec,
            sessions=sessions, bars_by_uid=bars_by_uid, securities=securities,
            as_of_cutoff=as_of_cutoff,
            require_sue_at_announcement=require_sue_at_announcement,
        )
    raise CohortConstructionError(f"unknown candidate source {source!r}")


def _price_session_candidates(
    *,
    sessions: Sequence[date],
    bars_by_uid: Mapping[str, Sequence[DailyBar]],
    membership: Mapping[date, Sequence],
    universe: UniverseStatus,
) -> tuple[list[_Candidate], tuple[ExcludedEvent, ...]]:
    """Every (security, session) whose price facts could have been computed then.

    The qualifying fact is stamped with the ledger's own day-precision
    convention — a dated fact is known at the **close** of its day
    (`filings.observations.day_precision_known_at`) — so §5.0 puts session 0 on
    the following session and entry at its open. A gap *is* an open; entering
    at the same print is a fill nobody could have got.
    """
    candidates: list[_Candidate] = []
    excluded: list[ExcludedEvent] = []
    for day in sessions:
        eligible = _eligible_uids(membership, universe, bars_by_uid, day)
        for uid in eligible:
            bars = bars_by_uid.get(uid)
            if not bars:
                continue
            index = _index_upto(bars, day)
            if index < 0:
                continue
            facts: dict[str, Any] = {
                "gap_pct": gap_pct(bars, index),
                "dist_from_sma50": dist_from_sma50(bars, index),
                "atr_pct": atr_pct(bars, index),
                "dollar_volume_20d": _dollar_volume(bars, day),
            }
            candidates.append(_Candidate(
                event_id=f"{uid}:{day.isoformat()}",
                security_uid=uid,
                ticker=bars[index].ticker,
                entity_cik=None,
                covariate_session=day,
                cutoff=day_precision_known_at(day),
                announced_at=None,
                facts=facts,
                # The price file's stamp is a session date: reconstructed but
                # defensible, which is exactly what `vendor_pit` names.
                provenance=["vendor_pit"],
            ))
    return candidates, tuple(excluded)


def _earnings_candidates(
    session,
    spec: SetupSpec,
    *,
    sessions: Sequence[date],
    bars_by_uid: Mapping[str, Sequence[DailyBar]],
    securities: Mapping[str, Sequence],
    as_of_cutoff: datetime,
    require_sue_at_announcement: bool,
) -> tuple[list[_Candidate], tuple[ExcludedEvent, ...]]:
    """One candidate per 8-K Item 2.02, timed by its acceptance instant.

    Two timestamps, kept apart because they answer different questions:

    ``announced_at``
        the 8-K Item 2.02 ``acceptanceDateTime`` — when the company told the
        market. An observation whose ``known_at_source`` is anything else is
        rejected outright, at read time as well as at write time
        (`test_announcement_time_from_8k_acceptance`).
    ``cutoff``
        the instant **every** qualifying fact was knowable, which is the later
        of the announcement and the SUE's own acceptance. XBRL EPS usually
        arrives with the 10-Q, days after the release; using the announcement
        as the cutoff would then let a fact that did not exist yet qualify the
        event. When they differ the event carries `sue_known_after_announcement`
        and the gap is reported, so the drift is visible rather than assumed
        away. `require_sue_at_announcement=True` drops those events instead.
    """
    uid_by_ticker: dict[str, str] = {}
    for uid, rows in securities.items():
        for row in rows:
            uid_by_ticker.setdefault(row.ticker, uid)

    candidates: list[_Candidate] = []
    excluded: list[ExcludedEvent] = []

    rows = observations_known_at(
        session, fact_type=FACT_TYPE_EARNINGS_8K, cutoff=as_of_cutoff
    )
    for row in rows:
        announced = _aware(row.known_at_utc)
        event_day = announced.date()
        ticker = (row.ticker_at_time or "").upper()
        event_id = f"{row.accession or row.id}:{ticker}"

        field_name = _known_at_source(row)
        if field_name != EDGAR_KNOWN_AT_FIELD:
            excluded.append(ExcludedEvent(
                event_id, ticker, event_day, "known_at_not_acceptance",
                f"known_at_source={field_name!r}; an event dated from a filing "
                f"date is a one-session lookahead leak (Spec N §4.0)",
            ))
            continue

        uid = uid_by_ticker.get(ticker)
        if uid is None or uid not in bars_by_uid:
            excluded.append(ExcludedEvent(
                event_id, ticker, event_day, "no_stored_prices",
                "no security in the price plane carries this ticker in range",
            ))
            continue

        covariate_day = _last_session_closed_by(sessions, announced)
        if covariate_day is None:
            excluded.append(ExcludedEvent(
                event_id, ticker, event_day, "no_closed_session_before_event", "",
            ))
            continue

        # Which fiscal quarter this release is about. Naming the period first
        # is what stops the engine qualifying an earnings event on *last*
        # quarter's surprise when this quarter's EPS is not tagged yet: a
        # stale-but-real number is the most convincing kind of wrong.
        period = _announced_period(
            session, entity_cik=row.entity_cik, announced=announced,
            as_of_cutoff=as_of_cutoff,
        )
        if period is None:
            excluded.append(ExcludedEvent(
                event_id, ticker, event_day, "no_eps_period_for_announcement",
                "no XBRL EPS period ends at or before this release",
            ))
            continue

        want = f"period:{period.isoformat()}"
        sue, sue_note = seasonal_sue(session, entity_cik=row.entity_cik, cutoff=announced)
        cutoff = announced
        warnings: list[str] = []

        if sue is None or sue_note != want:
            if require_sue_at_announcement:
                excluded.append(ExcludedEvent(
                    event_id, ticker, event_day, "no_sue_at_announcement", sue_note,
                ))
                continue
            # The announced quarter's EPS is usually tagged on the 10-Q, days or
            # weeks after the release. The event is then knowable *later*, not
            # now, so it is re-stamped at the instant its SUE actually became
            # computable. The alternative is to date the event at the
            # announcement and qualify it on a number that did not exist yet,
            # which is the failure mode §4.1 exists to prevent. The recomputed
            # SUE must still be the SUE of the announced quarter — reaching for
            # whatever quarter is latest at the query date would date a 2022
            # event on a 2023 number, the same leak wearing a later timestamp.
            sue = None
            eps_known = _eps_known_at_for_period(
                session, entity_cik=row.entity_cik, cutoff=as_of_cutoff, period=period
            )
            if eps_known is not None and eps_known > announced:
                later_sue, later_note = seasonal_sue(
                    session, entity_cik=row.entity_cik, cutoff=eps_known
                )
                if later_sue is not None and later_note == want:
                    sue = later_sue
                    cutoff = eps_known
                    covariate_day = (
                        _last_session_closed_by(sessions, cutoff) or covariate_day
                    )
                    warnings.append(
                        f"sue_known_after_announcement: announced "
                        f"{announced.isoformat()}, SUE for {period.isoformat()} "
                        f"knowable {eps_known.isoformat()}"
                    )
            if sue is None:
                excluded.append(ExcludedEvent(
                    event_id, ticker, event_day, "no_sue_available",
                    f"no seasonal SUE for {period.isoformat()} is computable from "
                    f"stored XBRL at or before the query date ({sue_note})",
                ))
                continue

        bars = bars_by_uid[uid]
        index = _index_upto(bars, covariate_day)
        if index < 0:
            excluded.append(ExcludedEvent(
                event_id, ticker, covariate_day, "no_bar_on_covariate_session", "",
            ))
            continue

        candidates.append(_Candidate(
            event_id=event_id,
            security_uid=uid,
            ticker=ticker,
            entity_cik=row.entity_cik,
            covariate_session=covariate_day,
            cutoff=cutoff,
            announced_at=announced,
            facts={
                "sue_seasonal": sue,
                "gap_pct": gap_pct(bars, index),
                "dist_from_sma50": dist_from_sma50(bars, index),
                "atr_pct": atr_pct(bars, index),
                "dollar_volume_20d": _dollar_volume(bars, covariate_day),
            },
            provenance=[row.provenance_class],
            warnings=warnings,
        ))
    return candidates, tuple(excluded)


# --------------------------------------------------------------------------- #
# Qualification: covariates, membership, outcomes
# --------------------------------------------------------------------------- #


def _qualify(
    session,
    spec: SetupSpec,
    candidates: Sequence[_Candidate],
    *,
    bars_by_uid: Mapping[str, Sequence[DailyBar]],
    securities: Mapping[str, Sequence],
    membership: Mapping[date, Sequence],
    universe: UniverseStatus,
    calendar: TradingCalendar,
    snapshot: SnapshotStatus,
    evidence_cap: str,
    cik_map: Mapping[str, str],
    subject_ticker: str = "",
) -> tuple[
    tuple[EventRecord, ...],
    tuple[ExcludedEvent, ...],
    tuple[EventRecord, ...],
    tuple[tuple[str, float], ...],
    list[str],
    SubjectQualification | None,
]:
    excluded: list[ExcludedEvent] = []
    subject = (subject_ticker or "").strip().upper()
    #: `(day, event_id, qualifies, reason)` for the subject's latest candidate.
    #: Days are walked in ascending order below, so the last write wins and is
    #: the most recent opportunity the setup had to fire for that name.
    subject_verdict: tuple[date, str, bool, str] | None = None

    def _note_subject(candidate, day, qualifies: bool, reason: str) -> None:
        nonlocal subject_verdict
        if subject and candidate.ticker.strip().upper() == subject:
            subject_verdict = (day, candidate.event_id, qualifies, reason)

    pool: list[EventRecord] = []
    series_cache: dict[str, PriceSeries] = {}
    warnings: list[str] = []
    wants_market_cap = _wants(spec, "market_cap_decile")
    needs_sector = _wants(spec, "sector")

    by_date: dict[date, list[_Candidate]] = {}
    for candidate in candidates:
        by_date.setdefault(candidate.covariate_session, []).append(candidate)

    sector_codes: dict[str, float] = {}
    sector_by_uid = _sector_by_uid(session, securities) if needs_sector else {}

    events: list[EventRecord] = []
    for day in sorted(by_date):
        eligible = _eligible_uids(membership, universe, bars_by_uid, day)
        eligible_set = set(eligible)
        pool_bars = {uid: bars_by_uid[uid] for uid in eligible if uid in bars_by_uid}

        liquidity = derived.liquidity_deciles(pool_bars, day)
        vol = derived.realized_vol_deciles(pool_bars, day)
        market_cap = _market_cap_deciles(
            session, pool_bars, securities, day, cik_map
        ) if wants_market_cap else {}

        for candidate in sorted(by_date[day], key=lambda c: c.event_id):
            if universe.point_in_time and candidate.security_uid not in eligible_set:
                excluded.append(ExcludedEvent(
                    candidate.event_id, candidate.ticker, day, "not_a_universe_member",
                    f"{candidate.ticker} was not in {universe.slug} on {day}",
                ))
                _note_subject(candidate, day, False, "not_a_universe_member")
                continue

            bars = bars_by_uid.get(candidate.security_uid)
            index = _index_upto(bars or (), day)
            if not bars or index < 0:  # pragma: no cover - filtered upstream
                excluded.append(ExcludedEvent(
                    candidate.event_id, candidate.ticker, day, "no_bar_on_event_date", "",
                ))
                _note_subject(candidate, day, False, "no_bar_on_event_date")
                continue

            facts = dict(candidate.facts)
            liq = liquidity.get(candidate.security_uid)
            facts["liquidity_decile"] = float(liq) if liq is not None else None
            rv = vol.get(candidate.security_uid)
            facts["realized_vol_decile"] = float(rv) if rv is not None else None
            facts["price_bucket"] = float(
                derived.price_level_bucket(bars[index].split_adjusted_close)
            )
            if wants_market_cap:
                cap = market_cap.get(candidate.security_uid)
                if cap is None:
                    excluded.append(ExcludedEvent(
                        candidate.event_id, candidate.ticker, day,
                        "market_cap_no_share_source",
                        "no shares_outstanding was knowable at the event date; a "
                        "market cap computed from a later count is lookahead "
                        "(Spec N §4.0)",
                    ))
                    _note_subject(candidate, day, False, "market_cap_no_share_source")
                    continue
                facts["market_cap_decile"] = float(cap)
            if needs_sector:
                label = sector_by_uid.get(candidate.security_uid)
                if label is not None:
                    code = sector_codes.setdefault(label, float(len(sector_codes) + 1))
                    facts["sector"] = code

            if liq is None:
                excluded.append(ExcludedEvent(
                    candidate.event_id, candidate.ticker, day,
                    "no_liquidity_decile",
                    "the §5.3 cost model is a half-spread by liquidity decile; "
                    "without one there is no honest net policy return",
                ))
                _note_subject(candidate, day, False, "no_liquidity_decile")
                continue

            covariates = tuple(sorted(
                (name, float(value)) for name, value in facts.items()
                if isinstance(value, (int, float)) and value is not None
                and not isinstance(value, bool)
            ))
            terminal, terminal_note = _terminal_outcome(
                securities, candidate.security_uid, day, bars, calendar, snapshot
            )
            if candidate.security_uid not in series_cache:
                series_cache[candidate.security_uid] = price_series_from_bars(bars)
            provenance = worst_provenance([*candidate.provenance, evidence_cap])
            try:
                record = EventRecord(
                    event_id=candidate.event_id,
                    ticker=candidate.ticker,
                    known_at_utc=candidate.cutoff,
                    series=series_cache[candidate.security_uid],
                    liquidity_decile=int(liq),
                    provenance=provenance,
                    regime=_regime_label(day),
                    covariates=covariates,
                    terminal=terminal,
                )
            except ValueError as exc:  # pragma: no cover - defensive
                excluded.append(ExcludedEvent(
                    candidate.event_id, candidate.ticker, day, "event_rejected", str(exc),
                ))
                _note_subject(candidate, day, False, "event_rejected")
                continue

            # Eligible, covariates computed, outcome resolvable: in the pool
            # whatever the conditions go on to say. §4.5's second diagnostic and
            # §6.3's random-cohort null both need the events that *could* have
            # qualified, not the ones that did.
            pool.append(record)

            passed, reason = evaluate_conditions(spec.conditions, facts)
            _note_subject(candidate, day, passed, reason)
            if not passed:
                excluded.append(ExcludedEvent(
                    candidate.event_id, candidate.ticker, day, reason, "",
                ))
                continue

            if terminal_note:
                warnings.append(f"{candidate.ticker}: {terminal_note}")
            events.append(record)
            warnings.extend(f"{candidate.ticker}: {w}" for w in candidate.warnings)

    qualification: SubjectQualification | None = None
    if subject:
        if subject_verdict is None:
            # Not "the conditions were false": the setup never had an
            # opportunity to fire for this name inside the cohort's window at
            # all — it is not in the universe, it has no bars, or its candidate
            # source produced no event for it. Both are "does not qualify" for
            # §6.6, and a reader who cannot tell them apart cannot fix either.
            qualification = SubjectQualification(
                ticker=subject, qualifies=False, reason="no_candidate",
            )
        else:
            day, event_id, passed, reason = subject_verdict
            qualification = SubjectQualification(
                ticker=subject,
                qualifies=bool(passed),
                reason=reason,
                event_id=event_id,
                event_date=day,
            )

    return (tuple(events), tuple(excluded), tuple(pool),
            tuple(sorted(sector_codes.items())), warnings, qualification)


def _wants(spec: SetupSpec, fact: str) -> bool:
    return (fact in spec.match_covariates
            or any(c.fact == fact for c in spec.conditions))


def _sector_by_uid(session, securities: Mapping[str, Sequence]) -> dict[str, str]:
    """Sector, from whatever current-vintage source exists (§4.5).

    Historical GICS assignment is an institutional licence; every sector field
    available at retail is the company's sector *today*. The value is used, and
    it is labelled `vintage=current` by `comparables.balance` because
    `sector` is in `config.CURRENT_VINTAGE_COVARIATES`.
    """
    tickers = {row.ticker for rows in securities.values() for row in rows}
    labels: dict[str, str] = {}
    try:
        from database.models import CompanyProfile
    except Exception:  # pragma: no cover - the model is part of the baseline
        return labels
    rows = (
        session.query(CompanyProfile)
        .filter(CompanyProfile.ticker.in_(sorted(tickers)))
        .all()
    )
    by_ticker = {r.ticker: r.sector for r in rows if getattr(r, "sector", None)}
    for uid, security_rows in securities.items():
        for security in security_rows:
            if security.ticker in by_ticker:
                labels[uid] = by_ticker[security.ticker]
                break
    return labels


def _market_cap_deciles(
    session,
    pool_bars: Mapping[str, Sequence[DailyBar]],
    securities: Mapping[str, Sequence],
    day: date,
    cik_map: Mapping[str, str],
) -> dict[str, int]:
    """`price x shares_outstanding` ranked within the universe that day.

    A name with no share count knowable at `day` is **absent** from the result,
    not defaulted, so the caller refuses it by name rather than ranking it on a
    number it invented.
    """
    cutoff = _session_close(day)
    values: dict[str, float] = {}
    for uid, bars in pool_bars.items():
        index = _index_upto(bars, day)
        if index < 0:
            continue
        cik = _cik_for(securities, uid, day, cik_map)
        if not cik:
            continue
        row = shares_outstanding_known_at(session, entity_cik=cik, cutoff=cutoff)
        if row is None:
            continue
        values[uid] = bars[index].raw_close * float(row.value_numeric)
    return derived.deciles(values)


def _cik_for(
    securities: Mapping[str, Sequence], uid: str, day: date,
    cik_map: Mapping[str, str],
) -> str | None:
    """The CIK a `security_uid` maps to on `day`, or `None`.

    The join is by ticker against `source_observations.ticker_at_time`, which
    is what the SEC feed stamps. It is a current-vintage join and is why
    `market_cap_decile` is not a covariate of the price-only setup.
    """
    security = _security_for(securities, uid, day)
    if security is None:
        return None
    return cik_map.get((security.ticker or "").upper())


def _regime_label(day: date) -> str:
    """The event's market regime label.

    Spec O §4's regime plane is Phase 4. Until it exists the label is the
    calendar half-year, which is a *stratification*, not a regime: it splits
    the cohort into cells that are contiguous in time so §5.4's per-regime
    breakdown has something real to slice on, and it is named so nobody
    mistakes it for a bull/bear classifier.
    """
    return f"{day.year}H{1 if day.month <= 6 else 2}"


def _terminal_outcome(
    securities: Mapping[str, Sequence],
    uid: str,
    day: date,
    bars: Sequence[DailyBar],
    calendar: TradingCalendar,
    snapshot: SnapshotStatus,
) -> tuple[TerminalOutcome | None, str]:
    """How this name's history ends (§4.2, §4.4).

    * a **performance** delisting is *matured*: the terminal return applies on
      the delisting date and is carried flat through every remaining horizon;
    * a merger with no stored deal terms, an unclassified stop, and an unknown
      reason are **censored** — counted, reported, never entering a mean;
    * a name that simply ran out of history before the query date is censored
      by `comparables.outcomes.maturity`, which needs no terminal record.
    """
    security = _security_for(securities, uid, day)
    if security is None or security.delisting_date is None:
        return None, ""
    delisting = security.delisting_date
    if delisting <= day:
        return None, ""

    reason = security.delisting_reason or "unknown"
    if reason not in RESOLVED_DELISTING_REASONS:
        return TerminalOutcome(delisting, f"unresolved_{reason}", 0.0, False), (
            f"delisted {delisting} for {reason!r} with no stored terms; censored, "
            f"not matured (Spec N §4.4)"
        )

    venue = getattr(security, "venue", "unknown")
    key = f"performance_{venue}"
    if key not in config.DELISTING_TERMINAL_RETURN:
        return TerminalOutcome(delisting, f"unresolved_venue_{venue}", 0.0, False), (
            f"performance delisting on venue {venue!r} has no configured terminal "
            f"return; censored rather than guessed (Spec N §4.2)"
        )

    if not snapshot.terminal_returns_synthesised:
        # The audit says this file carries the collapse. The stored bars are the
        # terminal decline; applying Shumway on top would count it twice. The
        # event matures at the first session after the last stored bar, flat.
        last = bars[-1].session_date
        after = next((d for d in calendar.sessions if d > last), None)
        if after is None:
            return None, ""
        return TerminalOutcome(after, "performance_carried_in_series", 0.0, True), ""

    return TerminalOutcome.performance_delisting(delisting, venue), ""


# --------------------------------------------------------------------------- #
# From a build to the §8 answer, in provenance blocks
# --------------------------------------------------------------------------- #


def policy_for(slug: str | None) -> PolicySpec:
    """The named execution policy the §5.3 leg replays under.

    An unknown slug raises. Defaulting it to "something plausible" would put a
    stop and a time exit the reader never chose behind the one number in the
    response that claims to describe what the system would actually have done.
    """
    if not slug:
        raise CohortConstructionError(
            "the setup names no execution_policy, so there is no honest §5.3 "
            "policy-simulated return to report"
        )
    try:
        params = config.EXECUTION_POLICIES[slug]
    except KeyError as exc:
        raise CohortConstructionError(
            f"unknown execution policy {slug!r}; known policies are "
            f"{sorted(config.EXECUTION_POLICIES)}"
        ) from exc
    return PolicySpec(slug=slug, **params)


@dataclass(frozen=True)
class CohortResult:
    """The answer, in the blocks Spec N §8 requires them to be rendered in.

    `primary` measures the `observed_live` + `vendor_pit` events, which §8 says
    may be pooled with the mix printed. `archival` measures the
    `archival_reconstructed` events **separately**, because those are never
    combined with the other classes in the same statistic. `provenance_mix`
    sums to the whole cohort across both.
    """

    build: CohortBuild
    primary: Any
    archival: Any | None
    provenance_mix: tuple[tuple[str, int], ...]
    blocks: ProvenanceBlocks

    @property
    def n_events(self) -> int:
        return self.blocks.n


def answer_for(
    build: CohortBuild,
    *,
    depth: str = "quick",
    registry: Any = None,
    query_covariates: Mapping[str, float] | None = None,
    family_cohorts: Sequence[Any] = (),
    reps: int | None = None,
    seed: int = config.DEFAULT_SEED,
    include_regimes: bool = True,
) -> CohortResult:
    """Measure a built cohort, one answer per provenance block.

    Nothing here computes a statistic: `comparables.report.build_answer` does,
    unchanged from Phase 3b. This function's whole job is deciding *which
    events go into which call*, which is the §8 pooling rule.
    """
    from comparables import report as report_mod

    blocks = build.blocks()
    policy = policy_for(build.setup.execution_policy)
    reps = config.BOOTSTRAP_REPS if reps is None else reps
    sources = _sources_for(build)

    def measure(events: Sequence[EventRecord], reg: Any):
        return report_mod.build_answer(
            build.setup,
            events,
            build.benchmark,
            build.calendar,
            depth=depth,
            policy=policy,
            registry=reg,
            query_covariates=query_covariates,
            pool=_pool_events(build, events),
            family_cohorts=family_cohorts,
            include_regimes=include_regimes,
            universe_delisting_rate=build.universe_delisting_rate,
            sources=sources,
            reps=reps,
            seed=seed,
        )

    primary = measure(blocks.point_in_time or blocks.archival, registry)
    archival = None
    if blocks.point_in_time and blocks.archival:
        # A second trial would be double-counted: the two blocks are one
        # question asked once. The archival block is measured against a
        # registry that records nothing.
        archival = measure(blocks.archival, _NullRegistry())

    return CohortResult(
        build=build,
        primary=primary,
        archival=archival,
        provenance_mix=tuple(sorted(blocks.mix.items())),
        blocks=blocks,
    )


class _NullRegistry:
    """Records nothing and reports the trial count it was handed nothing about.

    Rendering a cohort's second provenance block is not a second trial against
    the family (§7), and letting it count as one would inflate every subsequent
    multiplicity correction.
    """

    def record(self, spec: SetupSpec) -> int:
        return 0


def _pool_events(build: CohortBuild, events: Sequence[EventRecord]):
    """The eligible pool, or the cohort itself when there is nothing wider.

    `build_answer` uses it for both §4.5's matched-vs-pool diagnostic and
    §6.3's random-cohort null, and both want whole events.
    """
    return build.pool or events


def _sources_for(build: CohortBuild):
    from comparables.report import SourceRef

    stamp = datetime.combine(build.as_of, config.SESSION_CLOSE_UTC)
    out = [
        SourceRef("price_snapshot", build.snapshot.slug, stamp),
        SourceRef("universe_membership", build.universe.slug, stamp),
        SourceRef("benchmark", build.context.benchmark_security_uid, stamp),
    ]
    for source in build.universe.sources:
        out.append(SourceRef("universe_source", source, stamp))
    return tuple(out)
