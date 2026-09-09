"""The Spec N §5.0 reference fixture, as data.

Documented in full, with the arithmetic, in
`docs/investment-workspace/comparables-fixture.md`. Six events over two event
dates, one mid-hold 2-for-1 split, one Nasdaq performance delisting at -55%, a
total-return benchmark, horizons (1, 5, 10) sessions.

Nothing here is imported by production code paths; it exists so the doc, the
tests and any future reviewer read the same numbers.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from comparables.outcomes import (
    Bar,
    CorporateAction,
    EventRecord,
    PriceSeries,
    PolicySpec,
    TerminalOutcome,
    TradingCalendar,
)
from comparables.setup_spec import Condition, SetupSpec

# --------------------------------------------------------------------------- #

SESSION_DATES = tuple(date.fromisoformat(d) for d in (
    "2023-12-18", "2023-12-19", "2023-12-20", "2023-12-21", "2023-12-22",
    "2023-12-26", "2023-12-27", "2023-12-28", "2023-12-29",
    "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08",
    "2024-01-09", "2024-01-10", "2024-01-11", "2024-01-12", "2024-01-16",
    "2024-01-17", "2024-01-18", "2024-01-19", "2024-01-22", "2024-01-23",
    "2024-01-24", "2024-01-25", "2024-01-26", "2024-01-29", "2024-01-30",
    "2024-01-31", "2024-02-01", "2024-02-02",
))
#: 2023-12-25 (Christmas) and 2024-01-15 (MLK Day) are absent, as are weekends.
HOLIDAYS = (date(2023, 12, 25), date(2024, 1, 15))

CALENDAR = TradingCalendar(SESSION_DATES)

BENCHMARK_OPEN0 = 400.00
BENCHMARK_STEPS = (
    1, 2, -1, 3, -2, 1, 4, -3, 2, 1, -1, 3, -2, 2, 1, -3,
    4, -1, 2, 1, -2, 3, -1, 2, 1, -2, 3, 1, -1, 2, -3, 2,
)
BENCHMARK_STEP_SIZE = 0.40

CLOSES: dict[str, tuple[float, ...]] = {
    "AAA": (45.00, 45.20, 45.10, 45.40, 45.30, 45.60, 45.50, 45.80, 45.70, 46.10, 46.00,
            50.50, 51.00, 50.75, 51.50, 52.00, 51.75, 52.50, 53.00, 52.75, 53.50,
            53.25, 53.75, 54.00, 53.80, 54.20, 54.50, 54.30, 54.75, 55.00, 54.80, 55.20),
    "BBB": (21.00, 20.90, 21.10, 20.80, 20.95, 20.70, 20.85, 20.60, 20.75, 20.50, 20.60,
            19.80, 19.60, 19.90, 19.50, 19.40, 19.60, 19.30, 19.20, 19.40, 19.00,
            19.10, 18.90, 19.00, 18.80, 18.90, 18.70, 18.80, 18.60, 18.70, 18.50, 18.60),
    "CCC": (72.00, 72.40, 72.20, 72.80, 72.60, 73.20, 73.00, 73.60, 73.40, 74.20, 74.00,
            81.60, 82.40, 81.60, 83.20, 84.00, 83.20, 84.80, 86.40, 85.60, 88.00,
            87.20, 88.80, 89.60, 88.80, 90.40, 91.20, 90.40, 92.00, 92.80, 92.00, 93.60),
    "DDD": (28.00, 28.20, 28.10, 28.40, 28.30, 28.60, 28.50, 28.80, 28.70, 29.00, 28.90,
            29.20, 29.10, 29.40, 29.30, 29.60, 29.50, 29.80, 29.70,
            30.60, 30.30, 30.90, 31.20, 30.60, 31.50, 31.80, 31.20, 32.10, 32.40,
            32.20, 32.60, 32.80),
    "EEE": (11.00, 10.95, 11.05, 10.90, 11.00, 10.85, 10.95, 10.80, 10.90, 10.75, 10.85,
            10.70, 10.80, 10.65, 10.75, 10.60, 10.70, 10.55, 10.65,
            9.90, 10.10, 9.80, 9.60, 9.50, 9.40, 9.20),
    "FFF": (38.00, 38.20, 38.10, 38.40, 38.30, 38.60, 38.50, 38.80, 38.70, 39.00, 38.90,
            39.20, 39.10, 39.40, 39.30, 39.60, 39.50, 39.80, 39.70,
            40.40, 40.80, 41.20, 40.80, 41.60, 42.00, 41.60, 42.40, 42.80, 43.20,
            43.00, 43.40, 43.60),
}

SESSION_ZERO_INDEX = {"AAA": 11, "BBB": 11, "CCC": 11, "DDD": 19, "EEE": 19, "FFF": 19}
ENTRY_OPEN = {"AAA": 50.00, "BBB": 20.00, "CCC": 80.00,
              "DDD": 30.00, "EEE": 10.00, "FFF": 40.00}
LIQUIDITY_DECILE = {"AAA": 8, "BBB": 5, "CCC": 9, "DDD": 7, "EEE": 3, "FFF": 6}
PROVENANCE = {"AAA": "vendor_pit", "BBB": "vendor_pit", "CCC": "vendor_pit",
              "DDD": "observed_live", "EEE": "vendor_pit", "FFF": "observed_live"}
REGIME = {"AAA": "bull", "BBB": "bull", "CCC": "bull",
          "DDD": "chop", "EEE": "chop", "FFF": "chop"}
KNOWN_AT = {
    "AAA": datetime(2024, 1, 3, 21, 5, tzinfo=timezone.utc),
    "BBB": datetime(2024, 1, 3, 21, 5, tzinfo=timezone.utc),
    "CCC": datetime(2024, 1, 3, 21, 5, tzinfo=timezone.utc),
    "DDD": datetime(2024, 1, 16, 21, 10, tzinfo=timezone.utc),
    "EEE": datetime(2024, 1, 16, 21, 10, tzinfo=timezone.utc),
    "FFF": datetime(2024, 1, 16, 21, 10, tzinfo=timezone.utc),
}
COVARIATES = {
    "AAA": (("market_cap_decile", 8.0), ("liquidity_decile", 8.0),
            ("realized_vol_decile", 4.0), ("price_bucket", 3.0)),
    "BBB": (("market_cap_decile", 5.0), ("liquidity_decile", 5.0),
            ("realized_vol_decile", 6.0), ("price_bucket", 2.0)),
    "CCC": (("market_cap_decile", 9.0), ("liquidity_decile", 9.0),
            ("realized_vol_decile", 3.0), ("price_bucket", 4.0)),
    "DDD": (("market_cap_decile", 7.0), ("liquidity_decile", 7.0),
            ("realized_vol_decile", 5.0), ("price_bucket", 3.0)),
    "EEE": (("market_cap_decile", 2.0), ("liquidity_decile", 3.0),
            ("realized_vol_decile", 9.0), ("price_bucket", 1.0)),
    "FFF": (("market_cap_decile", 6.0), ("liquidity_decile", 6.0),
            ("realized_vol_decile", 5.0), ("price_bucket", 3.0)),
}

#: CCC's 2-for-1 split, ex-date 2024-01-10 = event-clock session 4.
SPLIT_TICKER = "CCC"
SPLIT_EX_DATE = date(2024, 1, 10)
SPLIT_FACTOR = 2.0

#: EEE's Nasdaq performance-related delisting at event-clock session 7.
DELISTED_TICKER = "EEE"
DELISTING_DATE = date(2024, 1, 26)

POLICY = PolicySpec(
    slug="event_swing_14cal_v1",
    stop_frac=0.94,
    target1_frac=1.04,
    target2_frac=1.08,
    max_holding_days=14,
)

SETUP = SetupSpec(
    slug="fixture_earnings_gap_v1",
    version="1.0.0",
    conditions=(
        Condition("sue_seasonal", ">", 1.5),
        Condition("gap_pct", ">", 3.0),
    ),
    universe="liquid_us_equity_v1",
    horizons_sessions=(1, 5, 10),
    execution_policy="event_swing_14cal_v1",
    match_covariates=("market_cap_decile", "liquidity_decile", "realized_vol_decile"),
    lookback_years=5,
)

HORIZONS = SETUP.horizons_sessions
TICKERS = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def _round2(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _bars(ticker: str) -> tuple[Bar, ...]:
    """Adjusted bars. Highs and lows follow the rule stated in the doc:
    ``high = ROUND_HALF_UP(max(open, close) x 1.005, 2)`` and
    ``low = ROUND_HALF_UP(min(open, close) x 0.995, 2)``.
    """
    closes = CLOSES[ticker]
    zero = SESSION_ZERO_INDEX[ticker]
    out = []
    for i, close in enumerate(closes):
        if i == zero:
            open_ = ENTRY_OPEN[ticker]
        elif i == 0:
            open_ = close
        else:
            open_ = closes[i - 1]
        out.append(Bar(
            date=SESSION_DATES[i],
            open=open_,
            high=_round2(max(open_, close) * 1.005),
            low=_round2(min(open_, close) * 0.995),
            close=close,
        ))
    return tuple(out)


def _raw_bars(ticker: str, adjusted: tuple[Bar, ...]) -> tuple[Bar, ...]:
    """Un-adjust the split: raw = adjusted x factor before the ex-date."""
    if ticker != SPLIT_TICKER:
        return adjusted
    out = []
    for bar in adjusted:
        if bar.date < SPLIT_EX_DATE:
            out.append(Bar(bar.date, bar.open * SPLIT_FACTOR, bar.high * SPLIT_FACTOR,
                           bar.low * SPLIT_FACTOR, bar.close * SPLIT_FACTOR))
        else:
            out.append(bar)
    return tuple(out)


def benchmark_series() -> PriceSeries:
    """`BENCH`, a total-return index level with no overnight gaps."""
    level = BENCHMARK_OPEN0
    bars = []
    for i, step in enumerate(BENCHMARK_STEPS):
        open_ = level
        level = _round2(level + BENCHMARK_STEP_SIZE * step)
        bars.append(Bar(SESSION_DATES[i], open_, max(open_, level),
                        min(open_, level), level))
    bars = tuple(bars)
    return PriceSeries("BENCH", bars, bars, bars)


def price_series(ticker: str) -> PriceSeries:
    adjusted = _bars(ticker)
    actions = ()
    if ticker == SPLIT_TICKER:
        actions = (CorporateAction(SPLIT_EX_DATE, "split", SPLIT_FACTOR),)
    return PriceSeries(
        ticker=ticker,
        raw=_raw_bars(ticker, adjusted),
        split_adjusted=adjusted,
        total_return=adjusted,          # no dividends in the fixture window
        actions=actions,
    )


def event(ticker: str, terminal: TerminalOutcome | None | str = "default") -> EventRecord:
    if terminal == "default":
        terminal = (
            TerminalOutcome.performance_delisting(DELISTING_DATE, "nasdaq")
            if ticker == DELISTED_TICKER else None
        )
    return EventRecord(
        event_id=ticker,
        ticker=ticker,
        known_at_utc=KNOWN_AT[ticker],
        series=price_series(ticker),
        liquidity_decile=LIQUIDITY_DECILE[ticker],
        provenance=PROVENANCE[ticker],
        regime=REGIME[ticker],
        covariates=COVARIATES[ticker],
        terminal=terminal,
    )


def cohort() -> tuple[EventRecord, ...]:
    """The six fixture events, EEE carrying its resolved -55% delisting."""
    return tuple(event(t) for t in TICKERS)


def cohort_without_terminal() -> tuple[EventRecord, ...]:
    """The counterfactual of §10: EEE's last print carried flat instead.

    This is the silent failure §4.2 exists to catch — a vendor file that stops
    at the last quote with no terminal collapse, which the engine would then
    read as a flat, matured, perfectly ordinary event.
    """
    flat = TerminalOutcome(DELISTING_DATE, "last_print_carried_flat", 0.0, True)
    return tuple(
        event(t, terminal=flat) if t == DELISTED_TICKER else event(t, terminal=None)
        for t in TICKERS
    )


def cohort_with_unknown_end() -> tuple[EventRecord, ...]:
    """The censoring variant: EEE halts after session 6 with no resolution."""
    return tuple(
        event(t, terminal=TerminalOutcome.unknown_end(DELISTING_DATE))
        if t == DELISTED_TICKER else event(t)
        for t in TICKERS
    )


def split_free_twin() -> EventRecord:
    """CCC with no corporate action at all, for `test_split_invariance`.

    Its adjusted series is by construction identical to CCC's, so an identical
    `TradeResult` is the assertion that replay never sees the split.
    """
    adjusted = _bars(SPLIT_TICKER)
    series = PriceSeries(SPLIT_TICKER + "_NOSPLIT", adjusted, adjusted, adjusted, ())
    return EventRecord(
        event_id="CCC_NOSPLIT",
        ticker=SPLIT_TICKER + "_NOSPLIT",
        known_at_utc=KNOWN_AT[SPLIT_TICKER],
        series=series,
        liquidity_decile=LIQUIDITY_DECILE[SPLIT_TICKER],
        provenance=PROVENANCE[SPLIT_TICKER],
        regime=REGIME[SPLIT_TICKER],
        covariates=COVARIATES[SPLIT_TICKER],
        terminal=None,
    )


# --------------------------------------------------------------------------- #
# Synthetic cohorts — for the tests that need to clear the §8 floors
# --------------------------------------------------------------------------- #


def _business_days(start: date, count: int) -> tuple[date, ...]:
    from datetime import timedelta

    out: list[date] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day = day + timedelta(days=1)
    return tuple(out)


def _series_from_returns(ticker: str, days: tuple[date, ...],
                         returns, level: float = 100.0) -> PriceSeries:
    bars = []
    prev = level
    for day, r in zip(days, returns):
        open_ = prev
        close = prev * (1.0 + float(r))
        bars.append(Bar(day, open_, max(open_, close) * 1.002,
                        min(open_, close) * 0.998, close))
        prev = close
    bars = tuple(bars)
    return PriceSeries(ticker, bars, bars, bars)


def synthetic_cohort(
    *,
    n_dates: int,
    per_date: int,
    sessions: int = 400,
    seed: int = 11,
    effect_daily: float = 0.0,
    late_effect_daily: float | None = None,
    common_shock_sd: float = 0.0,
    idio_sd: float = 0.01,
    market_sd: float = 0.008,
    market_drift: float = 0.0004,
    beta: float = 1.0,
    horizon_max: int = 10,
    start: date = date(2022, 1, 3),
    regimes: tuple[str, ...] = ("bull", "chop"),
):
    """A deterministic synthetic cohort with the properties a test needs.

    `effect_daily` is a constant daily abnormal return injected over each event's
    window; `late_effect_daily` replaces it for the chronologically later half,
    which is how the decay fixture gets a real early effect and none late.
    `common_shock_sd` adds a shared per-date shock, which is what creates the
    within-date correlation `n_eff` is supposed to notice.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    days = _business_days(start, sessions)
    market = rng.normal(market_drift, market_sd, sessions)
    benchmark = _series_from_returns("BENCH", days, market, level=400.0)

    first_zero = 15
    span = sessions - horizon_max - first_zero - 2
    step = max(span // max(n_dates, 1), 1)
    zero_indices = [first_zero + i * step for i in range(n_dates)]

    events: list[EventRecord] = []
    for d_i, zero in enumerate(zero_indices):
        shock = rng.normal(0.0, common_shock_sd, horizon_max) if common_shock_sd else None
        late = d_i >= n_dates / 2
        eff = (late_effect_daily if (late and late_effect_daily is not None)
               else effect_daily)
        for k in range(per_date):
            idio = rng.normal(0.0, idio_sd, sessions) if idio_sd else np.zeros(sessions)
            returns = beta * market + idio
            for s in range(horizon_max):
                returns[zero + s] += eff
                if shock is not None:
                    returns[zero + s] += shock[s]
            ticker = f"S{d_i:03d}{k:02d}"
            events.append(EventRecord(
                event_id=ticker,
                ticker=ticker,
                known_at_utc=datetime.combine(
                    days[zero], datetime.min.time()).replace(tzinfo=timezone.utc),
                series=_series_from_returns(ticker, days, returns),
                liquidity_decile=(d_i + k) % 10 + 1,
                provenance="vendor_pit",
                regime=regimes[d_i % len(regimes)],
                covariates=(("market_cap_decile", float((d_i + k) % 10 + 1)),
                            ("liquidity_decile", float((d_i + k) % 10 + 1)),
                            ("realized_vol_decile", float((d_i * 3 + k) % 10 + 1))),
                terminal=None,
            ))
    return TradingCalendar(days), benchmark, tuple(events)
