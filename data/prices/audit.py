"""The Spec N §4.2 delisting audit: does the series collapse, or just stop?

Verification §24, stated as a test: take twenty known performance-related
delistings from 2015–2024, pull each name's final bars from the price plane, and
check whether the last observation reflects a collapse to near zero or simply
stops at the last quoted price.

  * **collapse** — the file carries the terminal decline. The stored series can
    be used as-is.
  * **stop** — the file ends at an ordinary quote. The terminal return has to be
    synthesised, and Shumway's -30% (NYSE/AMEX) / -55% (Nasdaq) is the
    literature default (`comparables.config.DELISTING_TERMINAL_RETURN`).
  * **missing** — the plane has no bars for the name at all. That is the
    survivorship failure the audit is looking for in the first place, and it is
    reported separately from `stop` because the remedies differ: `stop` needs a
    synthesised return, `missing` needs a different vendor.
  * **too_short** — fewer bars than the window; nothing can be concluded.

The classification rule
-----------------------
`terminal_return` is the return over the last `window` sessions of the stored
series, on the **split-adjusted** close (a reverse split during a collapse is
exactly the case where raw closes lie):

    terminal_return = split_adjusted_close[-1] / split_adjusted_close[-1-window] - 1

Below `collapse_threshold` it is a collapse; at or above, a stop. The threshold
is configuration, not a constant buried in a function, because the honest answer
to "how far down is a collapse" is a judgement and it should be visible and
movable. The default of -60% over 10 sessions is well below the -55% Nasdaq
correction, so a series that merely drifts to the correction level is *not*
counted as having carried the collapse.

The result is written into `price_snapshots.delisting_audit_json`, so a cohort
can say which snapshot it ran on and what that snapshot's audit said.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Sequence

from data.prices.base import PricePlane
from data.prices.delisting_audit_list import DELISTING_AUDIT_LIST, DelistingCase

#: Sessions the terminal return is measured over.
DEFAULT_WINDOW_SESSIONS = 10

#: Below this, the series carried the collapse.
DEFAULT_COLLAPSE_THRESHOLD = -0.60

CLASSIFICATIONS = ("collapse", "stop", "missing", "too_short")


@dataclass(frozen=True)
class CaseResult:
    ticker: str
    company: str
    venue: str
    expected_delisting_date: str
    classification: str
    last_session: str | None
    sessions_available: int
    terminal_return: float | None
    last_close: float | None
    source_kind: str
    source_url: str
    source_verified: bool


def classify_case(
    plane: PricePlane,
    case: DelistingCase,
    window: int = DEFAULT_WINDOW_SESSIONS,
    collapse_threshold: float = DEFAULT_COLLAPSE_THRESHOLD,
) -> CaseResult:
    bars = plane.daily_bars(case.ticker)

    def result(classification: str, terminal: float | None) -> CaseResult:
        return CaseResult(
            ticker=case.ticker,
            company=case.company,
            venue=case.venue,
            expected_delisting_date=case.delisting_date.isoformat(),
            classification=classification,
            last_session=bars[-1].session_date.isoformat() if bars else None,
            sessions_available=len(bars),
            terminal_return=terminal,
            last_close=bars[-1].split_adjusted_close if bars else None,
            source_kind=case.source_kind,
            source_url=case.source_url,
            source_verified=case.verified,
        )

    if not bars:
        return result("missing", None)
    if len(bars) < window + 1:
        return result("too_short", None)

    start = bars[-1 - window].split_adjusted_close
    if start <= 0:
        return result("too_short", None)
    terminal = bars[-1].split_adjusted_close / start - 1.0
    return result("collapse" if terminal < collapse_threshold else "stop", terminal)


def run_audit(
    plane: PricePlane,
    cases: Sequence[DelistingCase] = DELISTING_AUDIT_LIST,
    window: int = DEFAULT_WINDOW_SESSIONS,
    collapse_threshold: float = DEFAULT_COLLAPSE_THRESHOLD,
) -> dict:
    """Run the audit and return the JSON blob stored on the snapshot."""
    results = [classify_case(plane, case, window, collapse_threshold) for case in cases]
    counts = {name: 0 for name in CLASSIFICATIONS}
    for item in results:
        counts[item.classification] += 1

    classified = counts["collapse"] + counts["stop"]
    return {
        "spec": "N-4.2-delisting-returns",
        "source": plane.source,
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "window_sessions": window,
        "collapse_threshold": collapse_threshold,
        "n_cases": len(results),
        "counts": counts,
        "collapse_rate_of_classified": (
            counts["collapse"] / classified if classified else None
        ),
        # The headline: if the file mostly *stops*, its terminal returns have to
        # be synthesised and every cohort statistic built on it says so.
        "terminal_returns_must_be_synthesised": counts["stop"] > counts["collapse"],
        "sources_verified_against_primary_filing": all(item.source_verified for item in results),
        "cases": [asdict(item) for item in results],
    }
