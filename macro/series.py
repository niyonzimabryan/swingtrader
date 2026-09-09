"""The macro series registry, and the allowlist that keeps ``regime_v1`` honest.

Spec O section 4.2:

> ``regime_v1`` uses only inputs that are **never revised** — price trend and
> drawdown, realized volatility, VIX level, and the Treasury yield curve — so
> it is point-in-time by construction and Spec N's regime split does not wait
> on this plane's vintage work.

"Never revised" is a claim about the publisher, not a convenience. A price
index level and a closing volatility index are observations of a market that
happened; the Treasury par yield curve is published once from that day's
quotes. GDP, CPI, payrolls and the NBER recession indicator are *estimates*
that get restated, sometimes years later and sometimes by a lot — and the
restatement is systematically in the direction of the eventual truth, which is
precisely the shape of lookahead that flatters a backtest.

Two consequences encoded here rather than remembered:

1. ``regime_v1``'s inputs must all be in :data:`NEVER_REVISED`, checked by
   ``test_regime_v1_inputs_unrevised``.
2. ``USREC`` carries ``vintage_only=True``. The NBER indicator is announced
   with a long lag *and* revised, so its current series says a recession
   started in a month when nobody knew it for another year. Requesting it
   without a vintage raises (``test_usrec_only_via_vintage``).

This module imports only the standard library. ``macro.regime_v1`` imports this
and nothing else, which is what makes the no-model rule structural.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SeriesSpec:
    """One FRED/ALFRED series and what may be done with it."""

    series_id: str
    label: str
    #: True when the publisher never restates a value once released.
    never_revised: bool
    #: True when the series may only ever be read through an ALFRED vintage.
    vintage_only: bool = False
    units: str = ""
    why_revised: str = ""


#: Prices and quotes. Observations of a market, not estimates of one.
NEVER_REVISED_SPECS = (
    SeriesSpec("SP500", "S&P 500 index level", True, units="index"),
    SeriesSpec("VIXCLS", "CBOE Volatility Index, close", True, units="index"),
    SeriesSpec("DGS1MO", "1-month Treasury constant maturity", True, units="percent"),
    SeriesSpec("DGS3MO", "3-month Treasury constant maturity", True, units="percent"),
    SeriesSpec("DGS1", "1-year Treasury constant maturity", True, units="percent"),
    SeriesSpec("DGS2", "2-year Treasury constant maturity", True, units="percent"),
    SeriesSpec("DGS5", "5-year Treasury constant maturity", True, units="percent"),
    SeriesSpec("DGS10", "10-year Treasury constant maturity", True, units="percent"),
    SeriesSpec("DGS30", "30-year Treasury constant maturity", True, units="percent"),
)

#: Estimates. Readable only as of a vintage, and never an input to regime_v1.
REVISED_SPECS = (
    SeriesSpec(
        "USREC", "NBER recession indicator", False, vintage_only=True, units="0/1",
        why_revised=(
            "NBER announces a turning point months to years after it happened "
            "and revises it. The current series therefore marks a recession "
            "starting on a date nobody could have known it started."
        ),
    ),
    SeriesSpec(
        "GDPC1", "Real GDP", False, units="billions chained USD",
        why_revised="Advance, second and third estimates, then annual revisions.",
    ),
    SeriesSpec(
        "CPIAUCSL", "CPI, all urban consumers", False, units="index",
        why_revised="Seasonal factors are re-estimated annually and restate history.",
    ),
    SeriesSpec(
        "PAYEMS", "Nonfarm payrolls", False, units="thousands",
        why_revised="Two monthly revisions plus an annual benchmark revision.",
    ),
    SeriesSpec(
        "UNRATE", "Unemployment rate", False, units="percent",
        why_revised="Seasonal adjustment is re-estimated annually.",
    ),
    SeriesSpec(
        "DFF", "Federal funds effective rate", False, units="percent",
        why_revised=(
            "The rate itself is a published quote, but the FRED series carries "
            "revisions to the calculation methodology; treated as revised until "
            "that is verified."
        ),
    ),
    SeriesSpec(
        "BAMLC0A0CM", "ICE BofA US Corporate Index OAS", False, units="percent",
        why_revised=(
            "Index-level composite; constituents and the option-adjustment model "
            "are restated. Spec O section 4.2 defers it to regime_v2."
        ),
    ),
)

ALL_SPECS = NEVER_REVISED_SPECS + REVISED_SPECS
BY_ID = {spec.series_id: spec for spec in ALL_SPECS}

#: The allowlist ``regime_v1``'s inputs are checked against.
NEVER_REVISED = frozenset(spec.series_id for spec in NEVER_REVISED_SPECS)
VINTAGE_ONLY = frozenset(spec.series_id for spec in ALL_SPECS if spec.vintage_only)


class SeriesNotRegistered(KeyError):
    """A series id nothing in this repository has classified."""


class VintageRequired(RuntimeError):
    """A vintage-only series was asked for as a current value."""


def spec_for(series_id: str) -> SeriesSpec:
    key = (series_id or "").strip().upper()
    if key not in BY_ID:
        raise SeriesNotRegistered(
            f"{series_id!r} is not in the macro series registry. Add a SeriesSpec "
            "saying whether the publisher revises it — an unclassified series "
            "cannot be checked against the regime_v1 allowlist, and the point of "
            "the allowlist is that nothing gets in by omission."
        )
    return BY_ID[key]


def is_never_revised(series_id: str) -> bool:
    return spec_for(series_id).never_revised


def require_vintage(series_id: str) -> None:
    """Raise when ``series_id`` may only be read as of a vintage.

    ``test_usrec_only_via_vintage``. The message is long on purpose: the person
    who hits it is about to label 2008 a recession in March 2008.
    """
    spec = spec_for(series_id)
    if spec.vintage_only:
        raise VintageRequired(
            f"{spec.series_id} ({spec.label}) may only be read as of an ALFRED "
            f"vintage. {spec.why_revised} Call the vintage path with an "
            "`as_of` date instead of the current-value path."
        )
