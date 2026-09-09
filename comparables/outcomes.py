"""Outcome measurement (Spec N §5) — four numbers, always all four.

Everything in this module follows the §5.0 definitions literally, and the
hand-calculated fixture in `docs/investment-workspace/comparables-fixture.md`
is the authority when they disagree.

  * Event clock: session 0 is the first session whose open is at or after
    `known_at_utc`; entry is the open of session 0; horizon `h` ends at the
    close of session `h-1`.
  * `R_i(h)  = close(h-1)/entry - 1`  on the total-return series.
  * `CAR(h)  = sum over s < h of (r_i,s - r_m,s)` — a sum of daily excess
    returns, which is not `R_i(h) - R_m(h)` and is not meant to be.
  * Calendar-time portfolio: equal-weighted across every event whose window
    covers the session; the headline is `alpha x h` from
    `p_t - r_f,t = alpha + beta (r_m,t - r_f,t) + eps_t`.
  * Policy return: `backtest/simulator.py` unmodified, net of a half-spread by
    liquidity decile plus adverse slippage, with gross shown separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Sequence

from backtest.simulator import TradeResult, simulate_trade

from comparables import config

# --------------------------------------------------------------------------- #
# Price and event records
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Bar:
    """One daily bar. Duck-type compatible with `backtest.simulator`."""

    date: date
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class CorporateAction:
    """A split or dividend factor stored with its ex-date (Spec N §4.3)."""

    ex_date: date
    kind: str          # "split" | "dividend"
    factor: float      # 2.0 for a 2-for-1 split


@dataclass(frozen=True)
class PriceSeries:
    """Three series per name, plus the factors that map between them (§4.3).

    * `raw` — what fills happened at.
    * `split_adjusted` — what signals, covariates and replay run on.
    * `total_return` — split + dividend, what the benchmark comparison uses.
    """

    ticker: str
    raw: tuple[Bar, ...]
    split_adjusted: tuple[Bar, ...]
    total_return: tuple[Bar, ...]
    actions: tuple[CorporateAction, ...] = ()

    def __post_init__(self) -> None:
        n = len(self.raw)
        if not (n == len(self.split_adjusted) == len(self.total_return)):
            raise ValueError(f"{self.ticker}: the three series must be the same length")
        if n == 0:
            raise ValueError(f"{self.ticker}: empty price series")

    def index_of(self, day: date) -> int:
        for i, bar in enumerate(self.split_adjusted):
            if bar.date == day:
                return i
        raise KeyError(f"{self.ticker}: no bar on {day}")

    @property
    def last_date(self) -> date:
        return self.split_adjusted[-1].date


@dataclass(frozen=True)
class TerminalOutcome:
    """How a name's history ends (Spec N §4.4).

    `resolved=True` (a performance delisting, an acquisition with deal terms, a
    to-zero name) is **matured**: the terminal return applies on `date` and is
    carried flat through every remaining horizon.  `resolved=False` (a halt with
    no resolution, a data gap) is **censored** and never enters a mean.
    """

    date: date
    reason: str
    terminal_return: float
    resolved: bool

    @classmethod
    def performance_delisting(cls, day: date, venue: str) -> "TerminalOutcome":
        key = f"performance_{venue}"
        if key not in config.DELISTING_TERMINAL_RETURN:
            raise ValueError(f"no configured terminal return for {key!r}")
        return cls(day, key, config.DELISTING_TERMINAL_RETURN[key], True)

    @classmethod
    def unknown_end(cls, day: date) -> "TerminalOutcome":
        return cls(day, "unknown_end", 0.0, False)


@dataclass(frozen=True)
class EventRecord:
    """One event in a cohort. Everything the outcome layer needs, nothing more."""

    event_id: str
    ticker: str
    known_at_utc: datetime
    series: PriceSeries
    liquidity_decile: int
    provenance: str                                   # observed_live|vendor_pit|archival_reconstructed
    regime: str
    covariates: tuple[tuple[str, float], ...] = ()
    terminal: TerminalOutcome | None = None

    def __post_init__(self) -> None:
        if self.provenance not in PROVENANCE_CLASSES:
            raise ValueError(f"unknown provenance class {self.provenance!r}")
        if not 1 <= self.liquidity_decile <= 10:
            raise ValueError("liquidity_decile is 1..10")
        if self.known_at_utc.tzinfo is None:
            raise ValueError("known_at_utc must be timezone-aware")

    @property
    def covariate_map(self) -> dict[str, float]:
        return dict(self.covariates)


PROVENANCE_CLASSES = ("observed_live", "vendor_pit", "archival_reconstructed")


@dataclass(frozen=True)
class TradingCalendar:
    """The sessions, in order. Holidays and weekends are simply absent."""

    sessions: tuple[date, ...]

    def __post_init__(self) -> None:
        if list(self.sessions) != sorted(set(self.sessions)):
            raise ValueError("sessions must be unique and ascending")

    def index_of(self, day: date) -> int:
        return self.sessions.index(day)

    def session_zero(self, known_at_utc: datetime) -> int:
        """First session whose open is at or after `known_at_utc` (§5.0)."""
        for i, day in enumerate(self.sessions):
            opening = datetime.combine(day, config.SESSION_OPEN_UTC)
            if opening >= known_at_utc:
                return i
        raise ValueError(f"no session opens at or after {known_at_utc}")

    def window(self, zero_index: int, horizon: int) -> tuple[date, ...]:
        """The `horizon` sessions of the event window, session 0 first."""
        if horizon < 1:
            raise ValueError("horizon must be >= 1")
        end = zero_index + horizon
        if end > len(self.sessions):
            raise ValueError("window runs past the end of the calendar")
        return self.sessions[zero_index:end]


# --------------------------------------------------------------------------- #
# Daily returns
# --------------------------------------------------------------------------- #


def benchmark_daily_returns(
    benchmark: PriceSeries, calendar: TradingCalendar, zero_index: int, horizon: int
) -> tuple[float, ...]:
    """`r_m,s` over the same sessions the event is measured on.

    Session 0 is open-to-close, because that is when the position exists;
    later sessions are close-to-close.
    """
    days = calendar.window(zero_index, horizon)
    bars = [benchmark.total_return[benchmark.index_of(d)] for d in days]
    out = [bars[0].close / bars[0].open - 1.0]
    for i in range(1, len(bars)):
        out.append(bars[i].close / bars[i - 1].close - 1.0)
    return tuple(out)


def benchmark_return(
    benchmark: PriceSeries, calendar: TradingCalendar, zero_index: int, horizon: int
) -> float:
    """`R_m(h)`, compounded over the identical sessions."""
    total = 1.0
    for r in benchmark_daily_returns(benchmark, calendar, zero_index, horizon):
        total *= 1.0 + r
    return total - 1.0


def entry_price(event: EventRecord, calendar: TradingCalendar) -> float:
    """The open of session 0 on the total-return series."""
    zero = calendar.session_zero(event.known_at_utc)
    return event.series.total_return[event.series.index_of(calendar.sessions[zero])].open


def event_daily_returns(
    event: EventRecord, calendar: TradingCalendar, horizon: int
) -> tuple[float, ...]:
    """`r_i,s` for `s = 0 .. horizon-1`, with the terminal outcome applied.

    Session 0 is measured from the entry open. After a resolved terminal
    outcome the daily return is the terminal return on its date and zero
    thereafter — carried flat, per §4.4.
    """
    zero = calendar.session_zero(event.known_at_utc)
    days = calendar.window(zero, horizon)
    series = event.series.total_return
    terminal = event.terminal

    out: list[float] = []
    prev: float | None = None
    for i, day in enumerate(days):
        if terminal is not None and terminal.resolved and day >= terminal.date:
            out.append(terminal.terminal_return if day == terminal.date else 0.0)
            continue
        bar = series[event.series.index_of(day)]
        if i == 0:
            out.append(bar.close / bar.open - 1.0)
        else:
            out.append(bar.close / prev - 1.0)
        prev = bar.close
    return tuple(out)


def event_return(event: EventRecord, calendar: TradingCalendar, horizon: int) -> float:
    """`R_i(h)` — the compounded event return over the window."""
    total = 1.0
    for r in event_daily_returns(event, calendar, horizon):
        total *= 1.0 + r
    return total - 1.0


def car(
    event: EventRecord,
    benchmark: PriceSeries,
    calendar: TradingCalendar,
    horizon: int,
) -> float:
    """`CAR_i(h)` — the sum of daily excess returns."""
    zero = calendar.session_zero(event.known_at_utc)
    ri = event_daily_returns(event, calendar, horizon)
    rm = benchmark_daily_returns(benchmark, calendar, zero, horizon)
    return sum(a - b for a, b in zip(ri, rm))


# --------------------------------------------------------------------------- #
# Maturity and censoring (§4.4)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MaturityStatus:
    event_id: str
    matured: bool
    reason: str        # "matured" | "unknown_end" | "horizon_not_matured" | "data_gap"


def maturity(
    event: EventRecord, calendar: TradingCalendar, horizon: int
) -> MaturityStatus:
    """Resolved-by-terminal-outcome is matured; everything else is censored."""
    zero = calendar.session_zero(event.known_at_utc)
    if event.terminal is not None and not event.terminal.resolved:
        return MaturityStatus(event.event_id, False, event.terminal.reason)
    if zero + horizon > len(calendar.sessions):
        return MaturityStatus(event.event_id, False, "horizon_not_matured")
    try:
        days = calendar.window(zero, horizon)
    except ValueError:
        return MaturityStatus(event.event_id, False, "horizon_not_matured")
    terminal_date = event.terminal.date if event.terminal is not None else None
    for day in days:
        if terminal_date is not None and day >= terminal_date:
            break
        try:
            event.series.index_of(day)
        except KeyError:
            return MaturityStatus(event.event_id, False, "data_gap")
    return MaturityStatus(event.event_id, True, "matured")


def delisting_rate(events: Sequence[EventRecord]) -> float:
    """Share of cohort members whose history ends in a resolved delisting."""
    if not events:
        return 0.0
    n = sum(
        1 for e in events
        if e.terminal is not None and e.terminal.resolved
        and e.terminal.reason.startswith("performance")
    )
    return n / len(events)


# --------------------------------------------------------------------------- #
# Calendar-time portfolio (§5.0, §6.2)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CalendarTimeSeries:
    """The equal-weighted portfolio's daily series over the cohort's span."""

    sessions: tuple[date, ...]
    holdings: tuple[int, ...]
    portfolio: tuple[float, ...]       # p_t
    benchmark: tuple[float, ...]       # r_m,t
    risk_free: tuple[float, ...]       # r_f,t

    @property
    def abnormal(self) -> tuple[float, ...]:
        return tuple(p - m for p, m in zip(self.portfolio, self.benchmark))


@dataclass(frozen=True)
class CalendarTimeResult:
    """`alpha x h`, the headline, with the regression it came from."""

    horizon: int
    n_sessions: int
    alpha_daily: float
    beta: float
    beta_estimated: bool
    alpha_times_h: float
    mean_abnormal_daily: float


def calendar_time_series(
    events: Sequence[EventRecord],
    benchmark: PriceSeries,
    calendar: TradingCalendar,
    horizon: int,
    risk_free_daily: float = 0.0,
) -> CalendarTimeSeries:
    """Equal-weighted portfolio of every event inside its window on each date."""
    per_session: dict[date, list[float]] = {}
    for event in events:
        zero = calendar.session_zero(event.known_at_utc)
        days = calendar.window(zero, horizon)
        for day, r in zip(days, event_daily_returns(event, calendar, horizon)):
            per_session.setdefault(day, []).append(r)

    days = tuple(sorted(per_session))
    if not days:
        raise ValueError("no sessions in the calendar-time portfolio")
    span_start = calendar.index_of(days[0])
    span_len = calendar.index_of(days[-1]) - span_start + 1
    rm_all = benchmark_daily_returns(benchmark, calendar, span_start, span_len)
    rm_by_day = dict(zip(calendar.sessions[span_start:span_start + span_len], rm_all))

    portfolio = tuple(sum(v) / len(v) for v in (per_session[d] for d in days))
    return CalendarTimeSeries(
        sessions=days,
        holdings=tuple(len(per_session[d]) for d in days),
        portfolio=portfolio,
        benchmark=tuple(rm_by_day[d] for d in days),
        risk_free=tuple(risk_free_daily for _ in days),
    )


def calendar_time_alpha(series: CalendarTimeSeries, horizon: int) -> CalendarTimeResult:
    """OLS of the portfolio's excess return on the benchmark's.

    Statistical choice the spec left open: with fewer than
    `MIN_CALENDAR_SESSIONS_FOR_BETA` sessions, or with no variance in the
    benchmark series, beta is not identified. The engine then sets **beta = 1**
    (market neutral) rather than estimating it, so alpha degrades to the mean
    daily abnormal return. Setting beta = 0 instead would have quietly turned
    the "abnormal" headline into the raw return. `beta_estimated` is reported so
    the fallback is never invisible.
    """
    y = [p - f for p, f in zip(series.portfolio, series.risk_free)]
    x = [m - f for m, f in zip(series.benchmark, series.risk_free)]
    n = len(y)
    if n == 0:
        raise ValueError("empty calendar-time series")

    mean_y = sum(y) / n
    mean_x = sum(x) / n
    var_x = sum((v - mean_x) ** 2 for v in x) / n

    if n >= config.MIN_CALENDAR_SESSIONS_FOR_BETA and var_x > 0.0:
        cov = sum((y[i] - mean_y) * (x[i] - mean_x) for i in range(n)) / n
        beta = cov / var_x
        estimated = True
    else:
        beta = 1.0
        estimated = False

    alpha = mean_y - beta * mean_x
    abnormal = series.abnormal
    return CalendarTimeResult(
        horizon=horizon,
        n_sessions=n,
        alpha_daily=alpha,
        beta=beta,
        beta_estimated=estimated,
        alpha_times_h=alpha * horizon,
        mean_abnormal_daily=sum(abnormal) / n,
    )


# --------------------------------------------------------------------------- #
# Policy-simulated return (§5.3) — the honest one
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PolicySpec:
    """A named execution policy, as fractions of the entry reference."""

    slug: str
    stop_frac: float           # 0.94 -> stop at 94% of the entry reference
    target1_frac: float
    target2_frac: float
    max_holding_days: int      # calendar days, mirroring order_monitor
    direction: str = "long"


@dataclass(frozen=True)
class CostModel:
    """Half-spread by liquidity decile plus adverse slippage (§5.3).

    Both are adverse per-fill price adjustments in basis points, which is
    exactly what `backtest.simulator` already models, so they are applied by
    running the simulator at `slippage_bps = slippage + half_spread`. Gross is
    the same replay at zero.
    """

    half_spread_bps_by_decile: tuple[tuple[int, float], ...]
    baseline_slippage_bps: float
    sensitivity_slippage_bps: tuple[float, ...]
    commission_bps: float = 0.0

    @classmethod
    def default(cls) -> "CostModel":
        return cls(
            half_spread_bps_by_decile=tuple(
                sorted(config.HALF_SPREAD_BPS_BY_DECILE.items())
            ),
            baseline_slippage_bps=config.BASELINE_SLIPPAGE_BPS,
            sensitivity_slippage_bps=tuple(config.SENSITIVITY_SLIPPAGE_BPS),
        )

    def half_spread_bps(self, liquidity_decile: int) -> float:
        table = dict(self.half_spread_bps_by_decile)
        if liquidity_decile not in table:
            raise KeyError(f"no half-spread configured for decile {liquidity_decile}")
        return table[liquidity_decile]


@dataclass(frozen=True)
class PolicyOutcome:
    """One event replayed through the simulator, gross and net."""

    event_id: str
    half_spread_bps: float
    gross_pct: float
    net_pct: float
    net_by_slippage_bps: tuple[tuple[float, float], ...]
    rule_fired: str
    exit_date: date
    entry_date: date
    stopped_out: bool


def policy_bars(event: EventRecord, calendar: TradingCalendar) -> tuple[Bar, ...]:
    """Replay bars: the split-adjusted series, truncated at a resolved terminal.

    Replay runs on the split-adjusted series because the simulator holds fixed
    stop and target prices with no corporate-action input; on raw prices an
    economically neutral 2-for-1 split mid-hold would fire the stop (§4.3).
    """
    bars = list(event.series.split_adjusted)
    terminal = event.terminal
    if terminal is not None and terminal.resolved:
        bars = [b for b in bars if b.date < terminal.date]
        if not bars:
            raise ValueError(f"{event.ticker}: no bars before the terminal date")
        value = round(bars[-1].close * (1.0 + terminal.terminal_return), 2)
        bars.append(Bar(terminal.date, value, value, value, value))
    return tuple(bars)


def _replay(
    event: EventRecord,
    calendar: TradingCalendar,
    policy: PolicySpec,
    slippage_bps: float,
) -> TradeResult:
    bars = policy_bars(event, calendar)
    zero_day = calendar.sessions[calendar.session_zero(event.known_at_utc)]
    pos = next(i for i, b in enumerate(bars) if b.date == zero_day)
    if pos == 0:
        raise ValueError(
            f"{event.ticker}: no bar before session 0; the simulator enters at "
            f"the T+1 open, so the signal bar must exist"
        )
    reference = bars[pos].open
    return simulate_trade(
        bars,
        pos - 1,
        policy.direction,
        reference * policy.stop_frac,
        reference * policy.target1_frac,
        reference * policy.target2_frac,
        policy.max_holding_days,
        slippage_bps=slippage_bps,
    )


def policy_outcome(
    event: EventRecord,
    calendar: TradingCalendar,
    policy: PolicySpec,
    costs: CostModel,
) -> PolicyOutcome:
    """The §5.3 number: net of costs, with gross shown separately."""
    hs = costs.half_spread_bps(event.liquidity_decile)
    gross = _replay(event, calendar, policy, 0.0)
    net = _replay(event, calendar, policy, costs.baseline_slippage_bps + hs)
    sensitivity = tuple(
        (bps, _replay(event, calendar, policy, bps + hs).pnl_pct)
        for bps in costs.sensitivity_slippage_bps
    )
    return PolicyOutcome(
        event_id=event.event_id,
        half_spread_bps=hs,
        gross_pct=gross.pnl_pct,
        net_pct=net.pnl_pct,
        net_by_slippage_bps=((costs.baseline_slippage_bps, net.pnl_pct),) + sensitivity,
        rule_fired=net.rule_fired,
        exit_date=net.exit_date,
        entry_date=net.entry_date,
        stopped_out=net.rule_fired in ("stop", "t1_then_stop"),
    )


@dataclass(frozen=True)
class PolicyAggregate:
    """Cohort-level policy statistics. The headline is `mean_net_pct`."""

    policy_slug: str
    n: int
    mean_gross_pct: float
    mean_net_pct: float
    mean_net_by_slippage_bps: tuple[tuple[float, float], ...]
    stopped_out_count: int
    outcomes: tuple[PolicyOutcome, ...]


def policy_aggregate(
    events: Sequence[EventRecord],
    calendar: TradingCalendar,
    policy: PolicySpec,
    costs: CostModel,
) -> PolicyAggregate:
    outcomes = tuple(policy_outcome(e, calendar, policy, costs) for e in events)
    n = len(outcomes)
    if n == 0:
        raise ValueError("no events to replay")
    levels = [bps for bps, _ in outcomes[0].net_by_slippage_bps]
    means = tuple(
        (bps, sum(dict(o.net_by_slippage_bps)[bps] for o in outcomes) / n)
        for bps in levels
    )
    return PolicyAggregate(
        policy_slug=policy.slug,
        n=n,
        mean_gross_pct=sum(o.gross_pct for o in outcomes) / n,
        mean_net_pct=sum(o.net_pct for o in outcomes) / n,
        mean_net_by_slippage_bps=means,
        stopped_out_count=sum(1 for o in outcomes if o.stopped_out),
        outcomes=outcomes,
    )


# --------------------------------------------------------------------------- #
# Cohort-level assembly
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HorizonOutcomes:
    """Per-event outcomes at one horizon, plus the cross-sectional means."""

    horizon: int
    event_ids: tuple[str, ...]
    event_dates: tuple[date, ...]
    raw: tuple[float, ...]
    benchmark: tuple[float, ...]
    car: tuple[float, ...]
    mean_raw: float
    mean_benchmark: float
    mean_car: float
    n_matured: int
    n_censored: int
    censored_reasons: tuple[tuple[str, str], ...]


def horizon_outcomes(
    events: Sequence[EventRecord],
    benchmark: PriceSeries,
    calendar: TradingCalendar,
    horizon: int,
) -> HorizonOutcomes:
    """Matured events enter the means; censored ones are counted, never dropped."""
    ids: list[str] = []
    dates: list[date] = []
    raw: list[float] = []
    bench: list[float] = []
    cars: list[float] = []
    censored: list[tuple[str, str]] = []

    for event in events:
        status = maturity(event, calendar, horizon)
        if not status.matured:
            censored.append((event.event_id, status.reason))
            continue
        zero = calendar.session_zero(event.known_at_utc)
        ids.append(event.event_id)
        dates.append(calendar.sessions[zero])
        raw.append(event_return(event, calendar, horizon))
        bench.append(benchmark_return(benchmark, calendar, zero, horizon))
        cars.append(car(event, benchmark, calendar, horizon))

    n = len(ids)
    mean = (lambda xs: sum(xs) / n if n else 0.0)
    return HorizonOutcomes(
        horizon=horizon,
        event_ids=tuple(ids),
        event_dates=tuple(dates),
        raw=tuple(raw),
        benchmark=tuple(bench),
        car=tuple(cars),
        mean_raw=mean(raw),
        mean_benchmark=mean(bench),
        mean_car=mean(cars),
        n_matured=n,
        n_censored=len(censored),
        censored_reasons=tuple(censored),
    )


def provenance_mix(events: Sequence[EventRecord]) -> dict[str, int]:
    mix = {k: 0 for k in PROVENANCE_CLASSES}
    for e in events:
        mix[e.provenance] += 1
    return mix


def distinct_event_dates(
    events: Sequence[EventRecord], calendar: TradingCalendar
) -> tuple[date, ...]:
    return tuple(sorted({calendar.sessions[calendar.session_zero(e.known_at_utc)]
                         for e in events}))
