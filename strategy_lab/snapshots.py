"""Point-in-time snapshot construction, pure (Spec Q §6, §8).

A ``MarketSnapshot`` is the only thing a strategy ever sees. This module builds
one from normalized inputs and reads one back; it holds no session, opens no
connection and fetches nothing. ``strategy_lab/snapshot_builder.py`` is the
impure half — it queries ``price_bars``, ``universe_membership`` and
``source_observations`` and hands the rows here. The split is deliberate and
tested: every rule about what may enter a snapshot is stated in a pure function
that a unit test can drive with hand-written numbers.

**One cutoff, one snapshot.** Every fact, bar and membership row in a snapshot
is knowable at ``data_cutoff_utc``. A fact whose ``known_at_utc`` is later is
not filtered out quietly — it raises :class:`NonPointInTimeInput`, because a
builder that hands over a future fact has a bug and silently dropping it would
hide the bug while the number it would have changed goes unexplained.

**Cross-sectional means one snapshot, not many.** A universe-scoped snapshot
carries the whole constituent set under that single cutoff. Spec Q §6: ranks may
not be assembled from ticker snapshots created at different times, and the way
to make that impossible is for the ranking strategy to receive one object.

**Reconstructed is not point-in-time.** Rows backfilled from a vendor with no
availability provenance — the ``historical_events``/FMP rows this repository
already holds — arrive with ``provenance_class='archival_reconstructed'`` and
``replay_eligible=False``, and the snapshot they land in carries the
``archival_reconstructed`` warning. ``MarketSnapshot.replay_eligible`` then
reads ``False``, and Spec Q §10 keeps those results out of clean metrics and
away from every promotion gate. The snapshot is still built and still shadowed:
labelled exploratory evidence is worth more than none.

**Normalized, not raw.** Raw provider payloads stay in the tables that own them.
What is frozen here is the normalized input plus the ``source_observations`` ids
and hashes needed to reproduce the decision, in one documented schema
(:data:`SCHEMA`) that the accessors below are the only sanctioned reader of.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Mapping, Sequence

from strategy_lab.domain import (
    MarketSnapshot,
    SnapshotScope,
    StrategyLabError,
    naive_utc,
    sha256_of,
)
from utils.market_hours import is_trading_day

__all__ = [
    "SCHEMA",
    "BAR_COLUMNS",
    "SESSION_CLOSE_UTC",
    "PROVENANCE_OBSERVED_LIVE",
    "PROVENANCE_VENDOR_PIT",
    "PROVENANCE_ARCHIVAL",
    "PROVENANCE_CLASSES",
    "FACT_EARNINGS_RELEASE",
    "FACT_EPS_DILUTED",
    "FACT_EPS_BASIC",
    "FACT_CONSENSUS_EPS",
    "SnapshotError",
    "NonPointInTimeInput",
    "SnapshotBar",
    "ObservationFact",
    "EarningsRecord",
    "CompositeScoringResult",
    "TickerInputs",
    "session_close_utc",
    "is_quarter_end_session",
    "build_ticker_snapshot",
    "build_universe_snapshot",
    "tickers_of",
    "bars_of",
    "facts_of",
    "earnings_of",
    "composite_of",
    "composite_output_hash",
    "calendar_of",
    "price_meta_of",
    "exclusions_of",
    "staleness_seconds",
]

#: The normalized-input schema the accessors below read. Bumping it is a
#: snapshot-format change and therefore a new strategy version for anything
#: that reads it, because every manifest hashes this module.
SCHEMA = "strategy_lab.snapshot.v1"

#: Bar column order inside the schema. Compact lists rather than per-bar dicts:
#: a universe snapshot holds ~253 sessions for every constituent, and the key
#: names would be three quarters of the stored bytes.
BAR_COLUMNS = (
    "session_date", "raw_open", "raw_high", "raw_low", "raw_close",
    "volume", "split_adjusted_close", "total_return_close",
)

#: 21:00 UTC, mirroring ``data/prices/fixture_plane.SESSION_CLOSE_UTC``. That
#: module cannot be imported here (the Strategy Lab does not reach ``data/``),
#: so the constant is restated and ``tests/test_strategy_lab_snapshots.py``
#: asserts the two agree.
#:
#: It is a fixed instant, not an exchange calendar: under US daylight saving the
#: real close is 20:00 UTC. Everything here treats a session as complete only at
#: 21:00, which can withhold a just-closed bar during a summer afternoon but can
#: never make one available early — the only direction a point-in-time system may
#: err in.
SESSION_CLOSE_UTC = time(21, 0)

#: Last calendar day of each quarter-end month.
QUARTER_END_DAY = {3: 31, 6: 30, 9: 30, 12: 31}

#: Provenance classes, mirroring ``filings/observations.py``. Restated for the
#: same import-boundary reason, and asserted equal in the tests.
PROVENANCE_OBSERVED_LIVE = "observed_live"
PROVENANCE_VENDOR_PIT = "vendor_pit"
PROVENANCE_ARCHIVAL = "archival_reconstructed"
PROVENANCE_CLASSES = frozenset(
    {PROVENANCE_OBSERVED_LIVE, PROVENANCE_VENDOR_PIT, PROVENANCE_ARCHIVAL}
)

#: Fact types the V1 roster reads, spelled as the ingest planes write them.
FACT_EARNINGS_RELEASE = "earnings_release_8k_item_202"
FACT_EPS_DILUTED = "eps_diluted"
FACT_EPS_BASIC = "eps_basic"
FACT_CONSENSUS_EPS = "consensus_eps_news"


class SnapshotError(StrategyLabError):
    """A snapshot could not be built from the inputs given."""


class NonPointInTimeInput(SnapshotError):
    """An input was not knowable at the cutoff. Fails closed, never filtered."""


# --------------------------------------------------------------------------- #
# Input value objects
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SnapshotBar:
    """One daily session, carrying all three Spec N §4.3 series.

    ``split_adjusted_close`` is what signals and replay run on;
    ``raw_*`` is what a fill happened at and what dollar volume is measured in;
    ``total_return_close`` is the benchmark series. Keeping all three means a
    strategy never has to reconstruct one from another and get the convention
    wrong.
    """

    session_date: date
    raw_open: float
    raw_high: float
    raw_low: float
    raw_close: float
    volume: float
    split_adjusted_close: float
    total_return_close: float

    @property
    def adjustment_ratio(self) -> float:
        """``split_adjusted_close / raw_close`` — this session's split factor.

        ``data/prices/derived.py`` stores ``split_adjusted_close(i) =
        raw_close(i) / F(i)`` for a forward split factor ``F``, so the same
        ratio back-adjusts the rest of the bar exactly. A ``raw_close`` of zero
        has no ratio and raises rather than returning one.
        """
        if self.raw_close <= 0:
            raise SnapshotError(
                f"{self.session_date}: a bar with a non-positive raw close has "
                "no adjustment ratio"
            )
        return self.split_adjusted_close / self.raw_close

    @property
    def split_adjusted_open(self) -> float:
        return self.raw_open * self.adjustment_ratio

    @property
    def split_adjusted_high(self) -> float:
        return self.raw_high * self.adjustment_ratio

    @property
    def split_adjusted_low(self) -> float:
        return self.raw_low * self.adjustment_ratio

    @property
    def dollar_volume(self) -> float:
        """``raw_close * volume`` — split-invariant only in raw units."""
        return self.raw_close * self.volume

    def as_row(self) -> list:
        return [
            self.session_date.isoformat(),
            float(self.raw_open), float(self.raw_high), float(self.raw_low),
            float(self.raw_close), float(self.volume),
            float(self.split_adjusted_close), float(self.total_return_close),
        ]

    @classmethod
    def from_row(cls, row: Sequence) -> "SnapshotBar":
        return cls(
            session_date=date.fromisoformat(row[0]),
            raw_open=float(row[1]), raw_high=float(row[2]), raw_low=float(row[3]),
            raw_close=float(row[4]), volume=float(row[5]),
            split_adjusted_close=float(row[6]), total_return_close=float(row[7]),
        )


@dataclass(frozen=True)
class ObservationFact:
    """One ``source_observations`` row, reduced to what a strategy may read.

    Both times travel: ``valid_at`` (when the fact applies) and ``known_at_utc``
    (when SwingTrader could first have acted on it). So does the provenance
    class and the replay-eligibility flag, because a strategy that cannot tell
    an observed instant from a reconstructed one cannot honestly abstain.
    """

    observation_id: int
    fact_type: str
    valid_at: datetime
    known_at_utc: datetime
    precision: str
    provenance_class: str
    replay_eligible: bool
    source: str
    source_trust: str
    ticker: str | None = None
    value_numeric: float | None = None
    value_text: str | None = None
    unit: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.provenance_class not in PROVENANCE_CLASSES:
            raise SnapshotError(
                f"unknown provenance class {self.provenance_class!r}; the "
                f"vocabulary is {sorted(PROVENANCE_CLASSES)}"
            )
        object.__setattr__(self, "valid_at", naive_utc(self.valid_at, field_name="valid_at"))
        object.__setattr__(
            self, "known_at_utc", naive_utc(self.known_at_utc, field_name="known_at_utc")
        )

    def as_dict(self) -> dict:
        return {
            "observation_id": self.observation_id,
            "fact_type": self.fact_type,
            "ticker": self.ticker,
            "valid_at": self.valid_at.isoformat(),
            "known_at_utc": self.known_at_utc.isoformat(),
            "precision": self.precision,
            "provenance_class": self.provenance_class,
            "replay_eligible": bool(self.replay_eligible),
            "source": self.source,
            "source_trust": self.source_trust,
            "value_numeric": self.value_numeric,
            "value_text": self.value_text,
            "unit": self.unit,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, blob: Mapping[str, Any]) -> "ObservationFact":
        return cls(
            observation_id=int(blob["observation_id"]),
            fact_type=blob["fact_type"],
            ticker=blob.get("ticker"),
            valid_at=datetime.fromisoformat(blob["valid_at"]),
            known_at_utc=datetime.fromisoformat(blob["known_at_utc"]),
            precision=blob["precision"],
            provenance_class=blob["provenance_class"],
            replay_eligible=bool(blob["replay_eligible"]),
            source=blob["source"],
            source_trust=blob["source_trust"],
            value_numeric=blob.get("value_numeric"),
            value_text=blob.get("value_text"),
            unit=blob.get("unit"),
            payload=dict(blob.get("payload") or {}),
        )


@dataclass(frozen=True)
class EarningsRecord:
    """A structured earnings event, assembled from its component observations.

    Spec Q §7 requires "a structured earnings record with reported EPS,
    consensus EPS, event date, and source provenance available at cutoff". The
    assembly happens in the builder, not in the strategy, so that a strategy
    never does fact archaeology and an incomplete record is simply absent.

    ``known_at_utc`` is the **latest** of the components' known times: the
    record as a whole becomes actionable only once its last piece has landed.
    Taking the earliest would let a consensus figure published a week later
    qualify an entry that could not have been placed.
    """

    ticker: str
    event_date: date
    event_known_at_utc: datetime
    reported_eps: float
    consensus_eps: float
    known_at_utc: datetime
    observation_ids: tuple[int, ...]
    provenance_classes: tuple[str, ...]
    replay_eligible: bool
    sources: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "event_date": self.event_date.isoformat(),
            "event_known_at_utc": self.event_known_at_utc.isoformat(),
            "reported_eps": float(self.reported_eps),
            "consensus_eps": float(self.consensus_eps),
            "known_at_utc": self.known_at_utc.isoformat(),
            "observation_ids": list(self.observation_ids),
            "provenance_classes": list(self.provenance_classes),
            "replay_eligible": bool(self.replay_eligible),
            "sources": list(self.sources),
        }

    @classmethod
    def from_dict(cls, blob: Mapping[str, Any]) -> "EarningsRecord":
        return cls(
            ticker=blob["ticker"],
            event_date=date.fromisoformat(blob["event_date"]),
            event_known_at_utc=datetime.fromisoformat(blob["event_known_at_utc"]),
            reported_eps=float(blob["reported_eps"]),
            consensus_eps=float(blob["consensus_eps"]),
            known_at_utc=datetime.fromisoformat(blob["known_at_utc"]),
            observation_ids=tuple(int(i) for i in blob["observation_ids"]),
            provenance_classes=tuple(blob["provenance_classes"]),
            replay_eligible=bool(blob["replay_eligible"]),
            sources=tuple(blob["sources"]),
        )


@dataclass(frozen=True)
class CompositeScoringResult:
    """The already-produced ScoringEngine output, frozen (Spec Q §6, §7A).

    Every field here was computed by the existing pipeline before the snapshot
    was taken. ``swingtrader_composite_v1`` maps these values and calls no
    model; a snapshot carrying one is marked ``not_point_in_time`` because an
    LLM's conclusion is not reconstructible at a historical time T.
    """

    ticker: str
    scored_at: datetime
    run_id: str
    final_score: float
    classification: str
    direction: str
    cohort: str
    memo_generated: bool
    signal_breakdown: Mapping[str, Any]
    trade_params: Mapping[str, Any]
    model_provenance: Mapping[str, Any]
    portfolio_context_hash: str

    def as_dict(self) -> dict:
        body = {
            "ticker": self.ticker,
            "scored_at": naive_utc(self.scored_at, field_name="scored_at").isoformat(),
            "run_id": self.run_id,
            "final_score": float(self.final_score),
            "classification": self.classification,
            "direction": self.direction,
            "cohort": self.cohort,
            "memo_generated": bool(self.memo_generated),
            "signal_breakdown": dict(self.signal_breakdown),
            "trade_params": dict(self.trade_params),
            "model_provenance": dict(self.model_provenance),
            "portfolio_context_hash": self.portfolio_context_hash,
        }
        # The output hash covers everything above, so a later edit to a stored
        # scoring row is visible rather than silently adopted.
        body["output_hash"] = sha256_of(body)
        return body

    @classmethod
    def from_dict(cls, blob: Mapping[str, Any]) -> "CompositeScoringResult":
        return cls(
            ticker=blob["ticker"],
            scored_at=datetime.fromisoformat(blob["scored_at"]),
            run_id=blob["run_id"],
            final_score=float(blob["final_score"]),
            classification=blob["classification"],
            direction=blob["direction"],
            cohort=blob["cohort"],
            memo_generated=bool(blob["memo_generated"]),
            signal_breakdown=dict(blob.get("signal_breakdown") or {}),
            trade_params=dict(blob.get("trade_params") or {}),
            model_provenance=dict(blob.get("model_provenance") or {}),
            portfolio_context_hash=blob["portfolio_context_hash"],
        )


@dataclass(frozen=True)
class TickerInputs:
    """Everything known about one name at the cutoff.

    ``price_provenance_class`` and ``price_replay_eligible`` describe the *bar
    series*, which ``source_observations`` does not cover: the price plane is
    its own table with its own vintage. A backfilled series with no availability
    provenance says ``archival_reconstructed`` / ``False`` here, and that is what
    makes the whole snapshot exploratory.
    """

    ticker: str
    bars: tuple[SnapshotBar, ...] = ()
    facts: tuple[ObservationFact, ...] = ()
    earnings: EarningsRecord | None = None
    composite: CompositeScoringResult | None = None
    price_source: str = "unknown"
    price_provenance_class: str = PROVENANCE_ARCHIVAL
    price_replay_eligible: bool = False
    membership_known_at_utc: datetime | None = None
    delisting_known: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticker", (self.ticker or "").strip().upper())
        if not self.ticker:
            raise SnapshotError("TickerInputs requires a ticker")
        if self.price_provenance_class not in PROVENANCE_CLASSES:
            raise SnapshotError(
                f"{self.ticker}: unknown price provenance class "
                f"{self.price_provenance_class!r}"
            )
        dates = [bar.session_date for bar in self.bars]
        if dates != sorted(dates) or len(set(dates)) != len(dates):
            raise SnapshotError(
                f"{self.ticker}: bars must be strictly ascending by session date"
            )


# --------------------------------------------------------------------------- #
# Calendar
# --------------------------------------------------------------------------- #


def session_close_utc(day: date) -> datetime:
    """The instant a session's bar becomes complete."""
    return datetime.combine(day, SESSION_CLOSE_UTC)


def is_quarter_end_session(day: date) -> bool:
    """Is ``day`` the final regular session of a March/June/September/December?

    Answered without a single bar from after ``day``: it is the last session of
    its quarter exactly when no trading day remains between it and the quarter's
    last calendar day. ``utils.market_hours.is_trading_day`` supplies weekends
    and the US market holiday calendar, so a Good Friday that closes the market
    on 29 March does not push the rebalance off the 28th.

    The holiday table in ``utils/market_hours.py`` covers the near future only.
    A replay reaching back before it will see a quarter-end session that a
    holiday actually preceded, which is why the flag is computed once by the
    builder, frozen into the snapshot and carried in its provenance rather than
    recomputed by every reader.
    """
    if day.month not in (3, 6, 9, 12):
        return False
    quarter_end = date(day.year, day.month, QUARTER_END_DAY[day.month])
    probe = day + timedelta(days=1)
    while probe <= quarter_end:
        if is_trading_day(probe):
            return False
        probe += timedelta(days=1)
    return True


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #


def _check_point_in_time(inputs: Sequence[TickerInputs], cutoff: datetime) -> None:
    """Every fact and bar must be knowable at the cutoff. No exceptions."""
    cutoff_day = cutoff.date()
    for entry in inputs:
        for bar in entry.bars:
            if bar.session_date > cutoff_day:
                raise NonPointInTimeInput(
                    f"{entry.ticker}: bar {bar.session_date} is after the cutoff "
                    f"{cutoff.isoformat()}"
                )
            if session_close_utc(bar.session_date) > cutoff:
                raise NonPointInTimeInput(
                    f"{entry.ticker}: the {bar.session_date} session had not "
                    f"closed at {cutoff.isoformat()}; a snapshot may not contain "
                    "a bar that was still forming"
                )
        for fact in entry.facts:
            if fact.known_at_utc > cutoff:
                raise NonPointInTimeInput(
                    f"{entry.ticker}: observation {fact.observation_id} "
                    f"({fact.fact_type}) was known at "
                    f"{fact.known_at_utc.isoformat()}, after the cutoff "
                    f"{cutoff.isoformat()}"
                )
        record = entry.earnings
        if record is not None and record.known_at_utc > cutoff:
            raise NonPointInTimeInput(
                f"{entry.ticker}: the earnings record became complete at "
                f"{record.known_at_utc.isoformat()}, after the cutoff "
                f"{cutoff.isoformat()}"
            )
        if entry.membership_known_at_utc is not None and entry.membership_known_at_utc > cutoff:
            raise NonPointInTimeInput(
                f"{entry.ticker}: universe membership became knowable at "
                f"{entry.membership_known_at_utc.isoformat()}, after the cutoff"
            )
        if entry.composite is not None:
            scored_at = naive_utc(entry.composite.scored_at, field_name="scored_at")
            if scored_at > cutoff:
                raise NonPointInTimeInput(
                    f"{entry.ticker}: the frozen composite result was scored at "
                    f"{scored_at.isoformat()}, after the cutoff"
                )


def _quality_warnings(inputs: Sequence[TickerInputs], *, universe_version: str | None) -> set[str]:
    warnings: set[str] = set()
    for entry in inputs:
        if not entry.bars:
            warnings.add("missing")
        if entry.price_provenance_class == PROVENANCE_ARCHIVAL or not entry.price_replay_eligible:
            warnings.add("archival_reconstructed")
        if not entry.delisting_known:
            warnings.add("delisting_unknown")
        if entry.membership_known_at_utc is None and universe_version is not None:
            warnings.add("universe_version_unknown")
        for fact in entry.facts:
            if fact.provenance_class == PROVENANCE_ARCHIVAL:
                warnings.add("archival_reconstructed")
            if not fact.replay_eligible:
                warnings.add("archival_reconstructed")
        record = entry.earnings
        if record is not None and not record.replay_eligible:
            warnings.add("archival_reconstructed")
        if entry.composite is not None:
            # A model's conclusion is not reconstructible at a historical T.
            warnings.add("not_point_in_time")
    return warnings


def _ticker_block(entry: TickerInputs) -> dict:
    block = {
        "bars": [bar.as_row() for bar in entry.bars],
        "facts": [fact.as_dict() for fact in sorted(
            entry.facts, key=lambda f: (f.known_at_utc, f.fact_type, f.observation_id)
        )],
        "price": {
            "source": entry.price_source,
            "provenance_class": entry.price_provenance_class,
            "replay_eligible": bool(entry.price_replay_eligible),
            "delisting_known": bool(entry.delisting_known),
        },
        "membership_known_at_utc": (
            entry.membership_known_at_utc.isoformat()
            if entry.membership_known_at_utc is not None else None
        ),
    }
    if entry.earnings is not None:
        block["earnings"] = entry.earnings.as_dict()
    if entry.composite is not None:
        block["composite"] = entry.composite.as_dict()
    return block


def _observation_ids(inputs: Sequence[TickerInputs]) -> tuple[int, ...]:
    ids: set[int] = set()
    for entry in inputs:
        ids.update(fact.observation_id for fact in entry.facts)
        if entry.earnings is not None:
            ids.update(entry.earnings.observation_ids)
    return tuple(sorted(ids))


def _calendar_block(inputs: Sequence[TickerInputs]) -> dict:
    """The session calendar the snapshot itself witnesses.

    ``signal_session`` is the latest session any constituent has a bar for, and
    ``prior_session`` the one before it. Both come from the bars in the snapshot
    and from nothing else, so a strategy asking "was this fact new at this
    evaluation" gets an answer that is reproducible from the stored snapshot
    alone. ``is_quarter_end_session`` is frozen here for the reason in
    :func:`is_quarter_end_session`.
    """
    sessions = sorted({bar.session_date for entry in inputs for bar in entry.bars})
    if not sessions:
        return {
            "signal_session": None,
            "prior_session": None,
            "is_quarter_end_session": False,
        }
    return {
        "signal_session": sessions[-1].isoformat(),
        "prior_session": sessions[-2].isoformat() if len(sessions) > 1 else None,
        "is_quarter_end_session": is_quarter_end_session(sessions[-1]),
    }


def _build(
    *,
    scope: SnapshotScope,
    as_of_utc: datetime,
    data_cutoff_utc: datetime,
    inputs: Sequence[TickerInputs],
    provenance: Mapping[str, Any],
    universe_version: str | None,
    ticker: str | None,
    exclusions: Sequence[tuple[str, str]],
    extra_warnings: Iterable[str],
) -> MarketSnapshot:
    cutoff = naive_utc(data_cutoff_utc, field_name="data_cutoff_utc")
    _check_point_in_time(inputs, cutoff)

    seen = [entry.ticker for entry in inputs]
    if len(set(seen)) != len(seen):
        raise SnapshotError(f"duplicate tickers in the inputs: {sorted(seen)}")

    normalized = {
        "schema": SCHEMA,
        "bar_columns": list(BAR_COLUMNS),
        "calendar": _calendar_block(inputs),
        "tickers": {entry.ticker: _ticker_block(entry) for entry in inputs},
        "exclusions": [
            {"ticker": symbol.strip().upper(), "reason": reason}
            for symbol, reason in sorted(exclusions)
        ],
    }
    warnings = _quality_warnings(inputs, universe_version=universe_version)
    warnings.update(extra_warnings)

    return MarketSnapshot(
        scope=scope,
        as_of_utc=as_of_utc,
        data_cutoff_utc=cutoff,
        provenance=dict(provenance),
        ticker=ticker,
        universe_version=universe_version,
        constituents=tuple(sorted(entry.ticker for entry in inputs)) if scope is SnapshotScope.UNIVERSE else (),
        normalized_inputs=normalized,
        source_observation_ids=_observation_ids(inputs),
        quality_warnings=tuple(sorted(warnings)),
    )


def build_ticker_snapshot(
    *,
    as_of_utc: datetime,
    data_cutoff_utc: datetime,
    inputs: TickerInputs,
    provenance: Mapping[str, Any],
    extra_warnings: Iterable[str] = (),
) -> MarketSnapshot:
    """One name, one cutoff."""
    return _build(
        scope=SnapshotScope.TICKER,
        as_of_utc=as_of_utc,
        data_cutoff_utc=data_cutoff_utc,
        inputs=(inputs,),
        provenance=provenance,
        universe_version=None,
        ticker=inputs.ticker,
        exclusions=(),
        extra_warnings=extra_warnings,
    )


def build_universe_snapshot(
    *,
    as_of_utc: datetime,
    data_cutoff_utc: datetime,
    universe_version: str,
    inputs: Sequence[TickerInputs],
    provenance: Mapping[str, Any],
    exclusions: Sequence[tuple[str, str]] = (),
    extra_warnings: Iterable[str] = (),
) -> MarketSnapshot:
    """The whole constituent set under one cutoff (Spec Q §6, §7C)."""
    if not inputs:
        raise SnapshotError("a universe snapshot requires at least one constituent")
    return _build(
        scope=SnapshotScope.UNIVERSE,
        as_of_utc=as_of_utc,
        data_cutoff_utc=data_cutoff_utc,
        inputs=inputs,
        provenance=provenance,
        universe_version=universe_version,
        ticker=None,
        exclusions=exclusions,
        extra_warnings=extra_warnings,
    )


# --------------------------------------------------------------------------- #
# Reading — the only sanctioned view of the normalized inputs
# --------------------------------------------------------------------------- #


def _tickers_block(snapshot: MarketSnapshot) -> Mapping[str, Any]:
    inputs = snapshot.normalized_inputs
    schema = inputs.get("schema")
    if schema != SCHEMA:
        raise SnapshotError(
            f"snapshot carries schema {schema!r}, this reader understands {SCHEMA!r}"
        )
    return inputs.get("tickers") or {}


def tickers_of(snapshot: MarketSnapshot) -> tuple[str, ...]:
    return tuple(sorted(_tickers_block(snapshot)))


def bars_of(snapshot: MarketSnapshot, ticker: str) -> tuple[SnapshotBar, ...]:
    block = _tickers_block(snapshot).get((ticker or "").strip().upper())
    if not block:
        return ()
    return tuple(SnapshotBar.from_row(row) for row in block.get("bars") or ())


def facts_of(
    snapshot: MarketSnapshot, ticker: str, fact_type: str | None = None
) -> tuple[ObservationFact, ...]:
    block = _tickers_block(snapshot).get((ticker or "").strip().upper())
    if not block:
        return ()
    facts = tuple(ObservationFact.from_dict(blob) for blob in block.get("facts") or ())
    if fact_type is None:
        return facts
    return tuple(fact for fact in facts if fact.fact_type == fact_type)


def earnings_of(snapshot: MarketSnapshot, ticker: str) -> EarningsRecord | None:
    block = _tickers_block(snapshot).get((ticker or "").strip().upper())
    if not block or "earnings" not in block:
        return None
    return EarningsRecord.from_dict(block["earnings"])


def composite_of(snapshot: MarketSnapshot, ticker: str) -> CompositeScoringResult | None:
    block = _tickers_block(snapshot).get((ticker or "").strip().upper())
    if not block or "composite" not in block:
        return None
    return CompositeScoringResult.from_dict(block["composite"])


def composite_output_hash(snapshot: MarketSnapshot, ticker: str) -> str | None:
    block = _tickers_block(snapshot).get((ticker or "").strip().upper())
    if not block or "composite" not in block:
        return None
    return block["composite"].get("output_hash")


def price_meta_of(snapshot: MarketSnapshot, ticker: str) -> Mapping[str, Any]:
    block = _tickers_block(snapshot).get((ticker or "").strip().upper())
    return dict((block or {}).get("price") or {})


def calendar_of(snapshot: MarketSnapshot) -> Mapping[str, Any]:
    inputs = snapshot.normalized_inputs
    if inputs.get("schema") != SCHEMA:
        raise SnapshotError(
            f"snapshot carries schema {inputs.get('schema')!r}, this reader "
            f"understands {SCHEMA!r}"
        )
    return dict(inputs.get("calendar") or {})


def exclusions_of(snapshot: MarketSnapshot) -> tuple[tuple[str, str], ...]:
    inputs = snapshot.normalized_inputs
    return tuple(
        (blob["ticker"], blob["reason"]) for blob in inputs.get("exclusions") or ()
    )


def staleness_seconds(snapshot: MarketSnapshot, ticker: str) -> float | None:
    """Seconds between the last session close and the cutoff, or ``None``.

    ``None`` means there are no bars at all, which is a missing dependency
    rather than a stale one — a different abstention with a different reason
    code, and collapsing the two would hide an outage behind a staleness alarm.
    """
    bars = bars_of(snapshot, ticker)
    if not bars:
        return None
    return (snapshot.data_cutoff_utc - session_close_utc(bars[-1].session_date)).total_seconds()
