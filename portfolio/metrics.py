"""Portfolio risk figures, computed deterministically from stored returns.

Spec L §3: portfolio beta to the broad benchmark over 60 and 250 sessions,
realized portfolio volatility, and the **maximum and average pairwise**
correlation among the largest positions. No vendor risk model, no model call —
Spec K §5 requires every scheduled job to be pure Python, and a statistic a
model produced is not a statistic.

Two design choices worth stating:

**Max and average pairwise, not a matrix.** Ten names over sixty sessions is a
45-cell correlation matrix estimated from 60 observations each; most of it is
noise, and rendering it invites reading structure into sampling error. The two
numbers that survive that sample size are the worst pair and the average pair,
and a book of six "different" names at 0.8 average pairwise correlation is one
position — which is the thing sector codes cannot say.

**Every figure carries its lookback and its ``n``.** A beta over 12 sessions
and a beta over 250 are different objects wearing the same label. Where the
sample is too short the figure is ``None`` with a reason in ``notes``, never a
number computed from whatever was available.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

#: Below this many overlapping observations a figure is not computed.
MIN_OBSERVATIONS = 20
#: Spec L §3 windows.
BETA_WINDOWS = (60, 250)
CORRELATION_LOOKBACK = 60
#: How many of the largest positions enter the pairwise correlation.
CORRELATION_TOP_N = 10
#: 252 trading days a year.
TRADING_DAYS_PER_YEAR = 252


def _tail(series, window: int) -> list[float]:
    values = [float(v) for v in series if v is not None]
    return values[-window:] if window else values


def _mean(values) -> float:
    return sum(values) / len(values)


def _variance(values) -> float:
    if len(values) < 2:
        return 0.0
    mu = _mean(values)
    return sum((v - mu) ** 2 for v in values) / (len(values) - 1)


def _covariance(left, right) -> float:
    if len(left) < 2:
        return 0.0
    lm, rm = _mean(left), _mean(right)
    return sum((a - lm) * (b - rm) for a, b in zip(left, right)) / (len(left) - 1)


def _is_constant(values) -> bool:
    """True when a series does not vary at all.

    Tested on the values rather than on the variance: summing squared
    deviations of identical floats can leave a residue on the order of 1e-36,
    which is not zero and would let a "correlation" of 0.0 out of the function
    below — a claim of independence the data does not support.
    """
    return not values or max(values) == min(values)


def correlation(left, right) -> float | None:
    """Pearson correlation, or ``None`` when either side does not vary."""
    if _is_constant(left) or _is_constant(right):
        return None
    lv, rv = _variance(left), _variance(right)
    if lv <= 0 or rv <= 0:
        return None
    return _covariance(left, right) / ((lv ** 0.5) * (rv ** 0.5))


def beta(portfolio_returns, benchmark_returns) -> float | None:
    """OLS beta of the portfolio on the benchmark over the overlap."""
    n = min(len(portfolio_returns), len(benchmark_returns))
    if n < MIN_OBSERVATIONS:
        return None
    port = list(portfolio_returns)[-n:]
    bench = list(benchmark_returns)[-n:]
    if _is_constant(bench):
        return None
    bench_var = _variance(bench)
    if bench_var <= 0:
        return None
    return _covariance(port, bench) / bench_var


def realized_volatility(returns, annualize: bool = True) -> float | None:
    """Sample standard deviation of returns, annualised by default."""
    values = [float(v) for v in returns if v is not None]
    if len(values) < MIN_OBSERVATIONS:
        return None
    sigma = _variance(values) ** 0.5
    return sigma * (TRADING_DAYS_PER_YEAR ** 0.5) if annualize else sigma


def portfolio_returns(returns_by_symbol: dict, weights: dict) -> list[float]:
    """Weight-combined daily returns over the sessions every name shares.

    Truncating to the shortest series rather than forward-filling: a name with
    a short history should shorten the window, not have its gaps invented.
    """
    usable = {
        symbol: [float(v) for v in series]
        for symbol, series in (returns_by_symbol or {}).items()
        if weights.get(symbol) and series
    }
    if not usable:
        return []
    length = min(len(series) for series in usable.values())
    if length == 0:
        return []
    total_weight = sum(abs(weights[symbol]) for symbol in usable)
    if total_weight <= 0:
        return []
    combined = []
    for index in range(-length, 0):
        combined.append(
            sum(weights[symbol] * usable[symbol][index] for symbol in usable) / total_weight
        )
    return combined


def pairwise_correlations(returns_by_symbol: dict, symbols, lookback: int = CORRELATION_LOOKBACK):
    """``(max, average, n_names, pairs_used)`` over the named symbols."""
    series = {
        symbol: _tail(returns_by_symbol.get(symbol) or [], lookback)
        for symbol in symbols
        if returns_by_symbol.get(symbol)
    }
    series = {s: v for s, v in series.items() if len(v) >= MIN_OBSERVATIONS}
    names = sorted(series)
    values: list[float] = []
    for i, left in enumerate(names):
        for right in names[i + 1:]:
            overlap = min(len(series[left]), len(series[right]))
            rho = correlation(series[left][-overlap:], series[right][-overlap:])
            if rho is not None:
                values.append(rho)
    if not values:
        return None, None, len(names), 0
    return max(values), sum(values) / len(values), len(names), len(values)


@dataclass(frozen=True)
class RiskMetrics:
    beta_60: float | None = None
    beta_60_n: int | None = None
    beta_250: float | None = None
    beta_250_n: int | None = None
    realized_volatility: float | None = None
    realized_volatility_n: int | None = None
    max_pairwise_correlation: float | None = None
    avg_pairwise_correlation: float | None = None
    correlation_lookback_sessions: int = CORRELATION_LOOKBACK
    correlation_names: int = 0
    notes: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        def rounded(value):
            return round(value, 6) if value is not None else None

        return {
            "beta_60": rounded(self.beta_60),
            "beta_60_n": self.beta_60_n,
            "beta_250": rounded(self.beta_250),
            "beta_250_n": self.beta_250_n,
            "realized_volatility": rounded(self.realized_volatility),
            "realized_volatility_n": self.realized_volatility_n,
            "max_pairwise_correlation": rounded(self.max_pairwise_correlation),
            "avg_pairwise_correlation": rounded(self.avg_pairwise_correlation),
            "correlation_lookback_sessions": self.correlation_lookback_sessions,
            "correlation_names": self.correlation_names,
            "notes": dict(self.notes),
        }


def compute_risk_metrics(
    returns_by_symbol: dict,
    weights: dict,
    benchmark_returns,
    *,
    correlation_lookback: int = CORRELATION_LOOKBACK,
    top_n: int = CORRELATION_TOP_N,
) -> RiskMetrics:
    """Every Spec L §3 risk figure, each with the sample it was computed over."""
    notes: dict[str, str] = {}
    combined = portfolio_returns(returns_by_symbol, weights)
    if not combined:
        notes["portfolio_returns"] = (
            "no overlapping return series for the weighted holdings; every "
            "figure below is null rather than computed from a partial book."
        )
        return RiskMetrics(correlation_lookback_sessions=correlation_lookback, notes=notes)

    bench = [float(v) for v in (benchmark_returns or []) if v is not None]
    values: dict = {}
    for window in BETA_WINDOWS:
        port_window = combined[-window:]
        bench_window = bench[-window:]
        n = min(len(port_window), len(bench_window))
        value = beta(port_window, bench_window)
        values[f"beta_{window}"] = value
        values[f"beta_{window}_n"] = n if value is not None else None
        if value is None:
            notes[f"beta_{window}"] = (
                f"only {n} overlapping sessions; {MIN_OBSERVATIONS} are required."
            )

    vol_window = combined[-max(BETA_WINDOWS[0], 1):]
    vol = realized_volatility(vol_window)
    if vol is None:
        notes["realized_volatility"] = (
            f"only {len(vol_window)} sessions; {MIN_OBSERVATIONS} are required."
        )

    largest = [
        symbol
        for symbol, _ in sorted(weights.items(), key=lambda kv: -abs(kv[1]))[:top_n]
    ]
    max_rho, avg_rho, names, pairs = pairwise_correlations(
        returns_by_symbol, largest, correlation_lookback
    )
    if max_rho is None:
        notes["pairwise_correlation"] = (
            f"{names} name(s) had at least {MIN_OBSERVATIONS} sessions in the "
            f"{correlation_lookback}-session lookback; at least two are needed."
        )

    return RiskMetrics(
        beta_60=values.get("beta_60"),
        beta_60_n=values.get("beta_60_n"),
        beta_250=values.get("beta_250"),
        beta_250_n=values.get("beta_250_n"),
        realized_volatility=vol,
        realized_volatility_n=len(vol_window) if vol is not None else None,
        max_pairwise_correlation=max_rho,
        avg_pairwise_correlation=avg_rho,
        correlation_lookback_sessions=correlation_lookback,
        correlation_names=names,
        notes=notes | ({"pairs_used": str(pairs)} if pairs else {}),
    )


def inputs_hash(payload) -> str:
    """A stable digest of what a snapshot was computed from.

    Spec L §3 stores "the hash of the inputs" so a snapshot is reproducible and
    a changed input is visible instead of silent.
    """
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
