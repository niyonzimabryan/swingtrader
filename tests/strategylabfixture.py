"""Synthetic snapshots for the Strategy Lab SDK tests.

Hand-shaped price series with arithmetic simple enough to verify on paper: the
golden vectors in ``tests/test_strategy_lab_strategies.py`` are computed from
these by hand and asserted exactly, not recorded from a run.

The name is deliberately not ``test_*``: ``unittest discover -p "test_*.py"``
would otherwise import it as a test module.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Sequence

from strategy_lab import snapshots

#: A cutoff that is the final session of Q1 2026 — 31 March 2026 is a Tuesday,
#: so it is the quarter's last regular session and `momentum_v1` rebalances.
Q1_2026_CLOSE = datetime(2026, 3, 31, 21, 0)
Q1_2026_SESSION = date(2026, 3, 31)


def sessions_ending(last: date, count: int) -> list[date]:
    """`count` weekday sessions ending on `last`, most recent last.

    Weekdays only. Market holidays are irrelevant to these fixtures — nothing
    under test reads the gap between two sessions — and leaving them out keeps
    the dates predictable.
    """
    out: list[date] = []
    day = last
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day -= timedelta(days=1)
    return list(reversed(out))


def bars_from_closes(
    closes: Sequence[float],
    *,
    last_session: date = Q1_2026_SESSION,
    volume: float = 1_000_000.0,
    range_half_width: float = 1.0,
    adjustment_ratio: float = 1.0,
) -> tuple[snapshots.SnapshotBar, ...]:
    """One bar per close: open == close, high/low a fixed distance either side.

    With `range_half_width=1.0` and a flat series every true range is exactly
    `max(2, 1, 1) = 2`, so Wilder ATR(14) is exactly 2.0 — which is what makes
    the earnings golden vector checkable without a spreadsheet.

    `adjustment_ratio` is `split_adjusted_close / raw_close`; the default 1.0
    means raw and adjusted agree. The *closes given are the adjusted ones*, so a
    ratio of 0.5 describes a series whose raw prices were twice these.
    """
    days = sessions_ending(last_session, len(closes))
    out = []
    for day, adjusted_close in zip(days, closes):
        raw_close = adjusted_close / adjustment_ratio
        out.append(snapshots.SnapshotBar(
            session_date=day,
            raw_open=raw_close,
            raw_high=raw_close + range_half_width / adjustment_ratio,
            raw_low=raw_close - range_half_width / adjustment_ratio,
            raw_close=raw_close,
            volume=volume,
            split_adjusted_close=adjusted_close,
            total_return_close=adjusted_close,
        ))
    return tuple(out)


def flat_bars(
    count: int = 260,
    close: float = 100.0,
    *,
    last_session: date = Q1_2026_SESSION,
    volume: float = 1_000_000.0,
) -> tuple[snapshots.SnapshotBar, ...]:
    """A liquid, unmoving series: $100 x 1m shares = $100m median dollar volume."""
    return bars_from_closes([close] * count, last_session=last_session, volume=volume)


def ramp_bars(
    start: float, end: float, count: int, *, last_session: date = Q1_2026_SESSION,
    volume: float = 1_000_000.0,
) -> tuple[snapshots.SnapshotBar, ...]:
    """A straight line from `start` to `end` inclusive, over `count` sessions."""
    if count < 2:
        raise ValueError("a ramp needs at least two sessions")
    step = (end - start) / (count - 1)
    return bars_from_closes(
        [start + step * i for i in range(count)],
        last_session=last_session, volume=volume,
    )


def replayable_inputs(ticker: str, bars, **overrides) -> snapshots.TickerInputs:
    """Inputs whose price plane claims point-in-time provenance.

    Real ``price_bars`` rows do not (see ``strategy_lab/snapshot_builder.py``);
    these fixtures say they do so that a test about a strategy rule is not also
    a test about the price plane's provenance. The tests that are about
    provenance set it back.
    """
    kwargs = dict(
        ticker=ticker,
        bars=bars,
        price_source="fixture",
        price_provenance_class=snapshots.PROVENANCE_VENDOR_PIT,
        price_replay_eligible=True,
        # Membership becomes knowable at the close of the last session in the
        # series, which keeps the fixture point-in-time whatever cutoff the
        # test builds the snapshot at.
        membership_known_at_utc=(
            snapshots.session_close_utc(bars[-1].session_date) if bars else None
        ),
        delisting_known=True,
    )
    kwargs.update(overrides)
    return snapshots.TickerInputs(**kwargs)


def ticker_snapshot(inputs: snapshots.TickerInputs, *, cutoff: datetime = Q1_2026_CLOSE, **kw):
    return snapshots.build_ticker_snapshot(
        as_of_utc=cutoff,
        data_cutoff_utc=cutoff,
        inputs=inputs,
        provenance={"prices": "fixture"},
        **kw,
    )


def universe_snapshot(
    inputs: Sequence[snapshots.TickerInputs],
    *,
    cutoff: datetime = Q1_2026_CLOSE,
    universe_version: str = "liquid_us_equity_v1@2026-03-31",
    **kw,
):
    return snapshots.build_universe_snapshot(
        as_of_utc=cutoff,
        data_cutoff_utc=cutoff,
        universe_version=universe_version,
        inputs=tuple(inputs),
        provenance={"prices": "fixture", "universe": "liquid_us_equity_v1"},
        **kw,
    )


def earnings_record(
    ticker: str,
    *,
    reported_eps: float,
    consensus_eps: float,
    event_date: date = Q1_2026_SESSION,
    known_at_utc: datetime | None = None,
    replay_eligible: bool = True,
) -> snapshots.EarningsRecord:
    """A complete record, knowable in the final evaluation window by default."""
    known = known_at_utc or datetime(2026, 3, 31, 20, 30)
    return snapshots.EarningsRecord(
        ticker=ticker,
        event_date=event_date,
        event_known_at_utc=known,
        reported_eps=reported_eps,
        consensus_eps=consensus_eps,
        known_at_utc=known,
        observation_ids=(101, 102, 103),
        provenance_classes=(snapshots.PROVENANCE_VENDOR_PIT,),
        replay_eligible=replay_eligible,
        sources=("news_plane", "sec_eight_k_index", "sec_companyfacts"),
    )


def composite_result(
    ticker: str = "AAPL",
    *,
    final_score: float = 0.82,
    classification: str = "high_conviction",
    direction: str = "long",
    cohort: str = "memo",
    memo_generated: bool = True,
    scored_at: datetime | None = None,
    trade_params: dict | None = None,
) -> snapshots.CompositeScoringResult:
    return snapshots.CompositeScoringResult(
        ticker=ticker,
        scored_at=scored_at or datetime(2026, 3, 31, 20, 0),
        run_id="run-2026-03-31",
        final_score=final_score,
        classification=classification,
        direction=direction,
        cohort=cohort,
        memo_generated=memo_generated,
        signal_breakdown={"catalyst": 0.9, "fundamental": 0.7, "pattern": 0.8},
        trade_params=trade_params if trade_params is not None else {
            "entry_price": 100.10,
            "stop_loss": 94.00,
            "target_1": 112.20,
            "target_2": 118.30,
            "max_hold_days": 20,
            "position_pct": 5.0,
            "regime_multiplier": 1.0,
        },
        model_provenance={"memo_id": 7, "run_id": "run-2026-03-31"},
        portfolio_context_hash="c" * 64,
    )


# --------------------------------------------------------------------------- #
# Phase 3: the bars that come *after* a snapshot
# --------------------------------------------------------------------------- #


def sessions_from(first: date, count: int) -> list[date]:
    """`count` weekday sessions starting at `first`, ascending.

    The forward counterpart of :func:`sessions_ending`. A replay needs bars the
    snapshot deliberately does not contain — a point-in-time snapshot has no bar
    from after its cutoff — so a fixture builds them separately and hands them
    to `strategy_lab.replay`.
    """
    out: list[date] = []
    day = first
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def forward_bars(
    closes: Sequence[float],
    *,
    first_session: date = Q1_2026_SESSION,
    volume: float = 1_000_000.0,
    range_half_width: float = 1.0,
) -> tuple[snapshots.SnapshotBar, ...]:
    """The signal bar and everything after it, ascending from `first_session`.

    `closes[0]` is the **signal** session; the simulator enters at the open of
    `closes[1]`, so a two-element series is the shortest replayable one.
    """
    days = sessions_from(first_session, len(closes))
    return tuple(
        snapshots.SnapshotBar(
            session_date=day,
            raw_open=close,
            raw_high=close + range_half_width,
            raw_low=close - range_half_width,
            raw_close=close,
            volume=volume,
            split_adjusted_close=close,
            total_return_close=close,
        )
        for day, close in zip(days, closes)
    )


def rising_forward(
    n: int = 16, *, start: float = 100.0, step: float = 1.0,
    first_session: date = Q1_2026_SESSION,
) -> tuple[snapshots.SnapshotBar, ...]:
    """A straight line up from the signal session. Hits targets, never the stop."""
    return forward_bars(
        [start + step * i for i in range(n)], first_session=first_session
    )


def falling_forward(
    n: int = 16, *, start: float = 100.0, step: float = 1.0,
    first_session: date = Q1_2026_SESSION,
) -> tuple[snapshots.SnapshotBar, ...]:
    """A straight line down. Crosses a 2-ATR stop within a session or two."""
    return forward_bars(
        [start - step * i for i in range(n)], first_session=first_session
    )
