"""``regime_v1`` — a deterministic, versioned market-regime classifier.

Spec O section 4.2. **Deterministic is the deliberate choice, not the simple
one.** Hidden-Markov regime models overfit without regularisation, collapse
states onto tiny-variance regions, and produce short-lived regimes that
underperform buy-and-hold in practice; the recent statistical-jump-model
literature (verification section 28) reads as evidence that *persistence*
matters more than statistical sophistication, and a fixed threshold rule has
persistence for free. It has one more property that matters more here than
out-of-sample Sharpe: **yesterday's label never changes.** A fitted model's
state assignments move when it is refit, which breaks the point-in-time
guarantee everything else in this system is built around.

**Every input is on the never-revised allowlist** (``macro.series``): the S&P
500 index level, the VIX close, and the Treasury par yield curve. So a label
computed for 2019 today is the label a person could have computed in 2019.
Inflation, employment and credit-spread composites are revised and are
therefore ``regime_v2``'s business, once their vintages are stored.

**No model, anywhere in this path.** This module imports the standard library
and ``macro.series``, and nothing else — checked by ``test_no_llm_in_regime``
as an import-graph assertion rather than trusted as a convention. A model may
narrate a regime; it may not assign one.

**Versioning.** The thresholds are a frozen dataclass with a fingerprint over
their values, pinned to a literal below. Changing a threshold changes the
fingerprint, which fails ``test_regime_classifier_versioned`` and tells you
what to do instead: copy this module to ``regime_v2.py`` and change it there.
Editing the pinned literal to make the test pass is silently relabelling
history — the exact thing the version exists to prevent — and the failure
message says so.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from typing import Sequence

from macro import series as series_registry

VERSION = "regime_v1"

#: The series this classifier reads. Every one must be on the never-revised
#: allowlist — ``test_regime_v1_inputs_unrevised``.
INPUT_SERIES: tuple[str, ...] = ("SP500", "VIXCLS", "DGS10", "DGS3MO")

LABEL_CRISIS = "crisis"
LABEL_STRESS = "stress"
LABEL_LATE_CYCLE = "late_cycle"
LABEL_EXPANSION = "expansion"
LABEL_CONTRACTION = "contraction"
LABEL_INSUFFICIENT = "insufficient_data"

#: The fixed, small label set. Order is severity, worst first.
LABELS: tuple[str, ...] = (
    LABEL_CRISIS,
    LABEL_STRESS,
    LABEL_CONTRACTION,
    LABEL_LATE_CYCLE,
    LABEL_EXPANSION,
    LABEL_INSUFFICIENT,
)


@dataclass(frozen=True)
class Thresholds:
    """Every number the classifier compares against, in one place.

    They are judgement calls — verification section 28 looked for a canonical
    published threshold set and did not find one, so these are ours and are
    documented as such. What matters is not that they are optimal (they are
    not tuned, deliberately: a tuned threshold is a fitted parameter wearing a
    constant's clothes) but that they are fixed, visible, and versioned.
    """

    #: Sessions in the trend moving average.
    trend_window: int = 200
    #: Sessions in the drawdown lookback.
    drawdown_window: int = 252
    #: Sessions in the realized-volatility window.
    realized_vol_window: int = 20
    #: Trading sessions per year, for annualising realized volatility.
    sessions_per_year: int = 252

    #: Drawdown at or beyond which the regime is `crisis`, in percent.
    crisis_drawdown_pct: float = -20.0
    #: VIX at or above which the regime is `crisis`.
    crisis_vix: float = 30.0
    #: Drawdown at or beyond which the regime is at least `stress`.
    stress_drawdown_pct: float = -10.0
    #: VIX at or above which the regime is at least `stress`.
    stress_vix: float = 22.0
    #: Annualised realized volatility at or above which it is at least `stress`.
    stress_realized_vol_pct: float = 25.0
    #: 10y minus 3m, in basis points, below which a rising market is late cycle.
    inversion_bps: float = 0.0


THRESHOLDS = Thresholds()

#: Pinned fingerprint of :data:`THRESHOLDS`. See the module docstring: if this
#: no longer matches, the fix is a new module, not a new literal.
FROZEN_THRESHOLD_FINGERPRINT = "2fdd08290d658f0ad704ca95cb1d44d1e8743806bf76a2c86aeef86916cb6c5d"


class RegimeVersionViolation(RuntimeError):
    """A threshold changed without a version bump."""


def thresholds_fingerprint(thresholds: Thresholds = THRESHOLDS) -> str:
    blob = json.dumps(asdict(thresholds), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def assert_thresholds_unchanged() -> None:
    """Raise unless the checked-in thresholds are the ones this version means.

    ``test_regime_classifier_versioned`` calls this.
    """
    actual = thresholds_fingerprint()
    if actual != FROZEN_THRESHOLD_FINGERPRINT:
        raise RegimeVersionViolation(
            f"{VERSION}'s thresholds changed: fingerprint {actual} != pinned "
            f"{FROZEN_THRESHOLD_FINGERPRINT}.\n"
            "Spec O section 4.2: 'changing a threshold creates regime_v2 and "
            "does not silently relabel history.' Copy macro/regime_v1.py to "
            "macro/regime_v2.py, change it there, and label new observations "
            "with the new version. Do NOT update the pinned fingerprint to "
            "make this pass — every regime label ever computed under "
            f"{VERSION} would then silently mean something else."
        )


# --- inputs -----------------------------------------------------------------


@dataclass(frozen=True)
class RegimeInputs:
    """The four measured quantities the rules compare. All never-revised."""

    as_of: date
    index_level: float | None = None
    trend_ma: float | None = None
    drawdown_pct: float | None = None
    realized_vol_pct: float | None = None
    vix: float | None = None
    curve_10y_3m_bps: float | None = None
    #: Series that had no value at ``as_of`` — an absent print, not a zero.
    missing: tuple[str, ...] = ()

    @property
    def above_trend(self) -> bool | None:
        if self.index_level is None or self.trend_ma is None:
            return None
        return self.index_level > self.trend_ma

    def as_dict(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "index_level": self.index_level,
            "trend_ma": self.trend_ma,
            "drawdown_pct": self.drawdown_pct,
            "realized_vol_pct": self.realized_vol_pct,
            "vix": self.vix,
            "curve_10y_3m_bps": self.curve_10y_3m_bps,
            "above_trend": self.above_trend,
            "missing": list(self.missing),
        }


def _tail(values: Sequence[float], window: int) -> list[float]:
    return list(values[-window:]) if window > 0 else []


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def realized_volatility_pct(
    closes: Sequence[float], *, window: int, sessions_per_year: int
) -> float | None:
    """Annualised standard deviation of daily log returns, in percent.

    Population (not sample) standard deviation, and the choice is recorded
    rather than left to whichever helper was handy: the two differ by
    sqrt(n/(n-1)), about 2.6% at a 20-session window, which is enough to move
    a value across the 25% threshold.
    """
    if len(closes) < window + 1:
        return None
    tail = _tail(closes, window + 1)
    returns = [
        math.log(tail[i] / tail[i - 1])
        for i in range(1, len(tail))
        if tail[i] > 0 and tail[i - 1] > 0
    ]
    if len(returns) < window:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    return math.sqrt(variance) * math.sqrt(sessions_per_year) * 100.0


def drawdown_pct(closes: Sequence[float], *, window: int) -> float | None:
    """Percent below the highest close in the trailing window. Never positive."""
    if not closes:
        return None
    tail = _tail(closes, window)
    peak = max(tail)
    if peak <= 0:
        return None
    return (tail[-1] / peak - 1.0) * 100.0


def build_inputs(
    *,
    as_of: date,
    index_closes: Sequence[float] = (),
    vix: float | None = None,
    dgs10: float | None = None,
    dgs3mo: float | None = None,
    thresholds: Thresholds = THRESHOLDS,
) -> RegimeInputs:
    """Compute the four quantities from never-revised series.

    ``index_closes`` is the S&P 500 level series **up to and including**
    ``as_of``, oldest first. The caller is responsible for it being
    point-in-time; ``macro.api`` reads it from the ledger with a
    ``known_at_utc <= as_of`` filter, so that responsibility has one owner.
    """
    missing: list[str] = []
    closes = [float(c) for c in index_closes if c is not None]
    if not closes:
        missing.append("SP500")
    if vix is None:
        missing.append("VIXCLS")
    if dgs10 is None:
        missing.append("DGS10")
    if dgs3mo is None:
        missing.append("DGS3MO")

    trend = None
    if len(closes) >= thresholds.trend_window:
        trend = _mean(_tail(closes, thresholds.trend_window))

    curve = None
    if dgs10 is not None and dgs3mo is not None:
        curve = (float(dgs10) - float(dgs3mo)) * 100.0

    return RegimeInputs(
        as_of=as_of,
        index_level=closes[-1] if closes else None,
        trend_ma=trend,
        drawdown_pct=drawdown_pct(closes, window=thresholds.drawdown_window),
        realized_vol_pct=realized_volatility_pct(
            closes,
            window=thresholds.realized_vol_window,
            sessions_per_year=thresholds.sessions_per_year,
        ),
        vix=None if vix is None else float(vix),
        curve_10y_3m_bps=curve,
        missing=tuple(missing),
    )


# --- the classifier ---------------------------------------------------------


@dataclass(frozen=True)
class RegimeLabel:
    """A label, the version that produced it, and why it fired."""

    label: str
    version: str
    thresholds_fingerprint: str
    reasons: tuple[str, ...]
    inputs: RegimeInputs

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "version": self.version,
            "thresholds_fingerprint": self.thresholds_fingerprint,
            "reasons": list(self.reasons),
            "inputs": self.inputs.as_dict(),
        }


def classify(inputs: RegimeInputs, thresholds: Thresholds = THRESHOLDS) -> RegimeLabel:
    """Assign a label. Total, ordered, and free of hidden state.

    The rules, in the order they are checked — first match wins, and severity
    beats direction so a market above its 200-session average during a
    volatility shock is not called ``expansion``:

    1. ``crisis``      — drawdown at or beyond -20%, or VIX at or above 30.
    2. ``stress``      — drawdown at or beyond -10%, or VIX at or above 22, or
       annualised 20-session realized volatility at or above 25%.
    3. ``late_cycle``  — above the 200-session average with 10y-3m inverted.
    4. ``expansion``   — above the 200-session average, curve not inverted.
    5. ``contraction`` — below the 200-session average.

    Anything the inputs cannot decide is ``insufficient_data``. It is a real
    answer: a regime guessed from a missing VIX is worse than no regime,
    because a covariate that is silently wrong on the days data was missing is
    a covariate that is wrong exactly when markets were disorderly.
    """
    reasons: list[str] = []
    drawdown = inputs.drawdown_pct
    vix = inputs.vix
    vol = inputs.realized_vol_pct
    curve = inputs.curve_10y_3m_bps
    above = inputs.above_trend

    if drawdown is not None and drawdown <= thresholds.crisis_drawdown_pct:
        reasons.append(f"drawdown {drawdown:.1f}% <= {thresholds.crisis_drawdown_pct}%")
    if vix is not None and vix >= thresholds.crisis_vix:
        reasons.append(f"vix {vix:.2f} >= {thresholds.crisis_vix}")
    if reasons:
        return _label(LABEL_CRISIS, reasons, inputs, thresholds)

    if drawdown is not None and drawdown <= thresholds.stress_drawdown_pct:
        reasons.append(f"drawdown {drawdown:.1f}% <= {thresholds.stress_drawdown_pct}%")
    if vix is not None and vix >= thresholds.stress_vix:
        reasons.append(f"vix {vix:.2f} >= {thresholds.stress_vix}")
    if vol is not None and vol >= thresholds.stress_realized_vol_pct:
        reasons.append(
            f"realized vol {vol:.1f}% >= {thresholds.stress_realized_vol_pct}%"
        )
    if reasons:
        return _label(LABEL_STRESS, reasons, inputs, thresholds)

    if above is None:
        return _label(
            LABEL_INSUFFICIENT,
            [f"no trend: {', '.join(inputs.missing) or 'series shorter than the window'}"],
            inputs,
            thresholds,
        )

    if above:
        if curve is not None and curve < thresholds.inversion_bps:
            return _label(
                LABEL_LATE_CYCLE,
                [f"above {thresholds.trend_window}-session average",
                 f"10y-3m {curve:.0f}bp < {thresholds.inversion_bps:.0f}bp"],
                inputs,
                thresholds,
            )
        if curve is None:
            return _label(
                LABEL_INSUFFICIENT,
                ["above trend but the yield curve is missing, and the "
                 "expansion/late-cycle split is exactly that comparison"],
                inputs,
                thresholds,
            )
        return _label(
            LABEL_EXPANSION,
            [f"above {thresholds.trend_window}-session average",
             f"10y-3m {curve:.0f}bp >= {thresholds.inversion_bps:.0f}bp"],
            inputs,
            thresholds,
        )

    return _label(
        LABEL_CONTRACTION,
        [f"below {thresholds.trend_window}-session average, no stress trigger"],
        inputs,
        thresholds,
    )


def _label(
    label: str, reasons: Sequence[str], inputs: RegimeInputs, thresholds: Thresholds
) -> RegimeLabel:
    return RegimeLabel(
        label=label,
        version=VERSION,
        thresholds_fingerprint=thresholds_fingerprint(thresholds),
        reasons=tuple(reasons),
        inputs=inputs,
    )


def input_series_are_unrevised() -> tuple[bool, tuple[str, ...]]:
    """``(all on the allowlist, the offenders)`` — ``test_regime_v1_inputs_unrevised``."""
    offenders = tuple(
        series_id
        for series_id in INPUT_SERIES
        if series_id not in series_registry.NEVER_REVISED
    )
    return (not offenders), offenders
