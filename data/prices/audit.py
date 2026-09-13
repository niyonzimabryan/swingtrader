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
  * **missing** — the plane has no bars for the (resolved) name at all. That is
    the survivorship failure the audit is looking for in the first place, and
    it is reported separately from `stop` because the remedies differ: `stop`
    needs a synthesised return, `missing` needs a different vendor.
  * **too_short** — fewer bars than the window; nothing can be concluded.
  * **unresolved** — no vendor symbol could be found for this case at all, so
    the plane was never asked for bars. Distinct from `missing`: `missing`
    means "the vendor has the symbol and no bars"; `unresolved` means "we
    could not even ask" (Spec N §12 ruling). Sharadar keys many performance
    delistings by a post-event `Q`-suffixed symbol the case's historical
    ticker does not name — see `data/prices/delisting_audit_list.py`.
  * **out_of_window** — the case's delisting date predates the price plane's
    purchased history, so no vendor could answer regardless of symbol. Also
    distinct from `missing` for the same reason: the remedy is a longer
    history tier, not a different vendor or a corrected symbol.

Only `collapse`, `stop`, `missing` and `too_short` count as **testable**: the
resolver found a symbol and the window covers the delisting date, so the
plane was actually asked. `unresolved` and `out_of_window` cases never reach
`daily_bars` at all. `run_audit` refuses (raises `InsufficientTestableCasesError`)
when fewer than `min_testable` cases are testable — a check that can only ask
four of twenty questions is not the check Verification §24 describes.

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

The resolver
------------
Before any bars are requested, `resolve_case` finds the symbol to ask the
plane for, in order:

  1. `case.vendor_symbols[plane.source]`, if the case already carries one —
     recorded from prior manual or `--resolve` verification.
  2. `plane.security_master([case.ticker])` — the case's historical ticker,
     asked of the plane's own master. If the plane still lists it, it is used
     as-is; no company-name check is needed for an exact-ticker hit.
  3. If the plane declares `supports_company_name_search`, a best-effort
     search for the company by name (and, on Sharadar, `relatedtickers`) via
     `plane.find_by_company_name`. A candidate is accepted only if its name
     matches `case.company` under `normalize_company_name` — a strict,
     deterministic comparison, not a fuzzy one, because the cost of accepting
     the wrong company silently is exactly the defect this audit exists to
     catch.
  4. Otherwise (a plane with no name-search capability, e.g. `fixture`) the
     case's own ticker is used as-is: such a plane made no claim it could
     resolve anything, so a lookup miss says nothing about the case's real
     vendor symbol, and treating it as `unresolved` would just be noise.

Only step 3's failure — a plane that tried to search and found no accepted
candidate — produces `unresolved`. A plane with no search capability at all
falls through to `asis`, and classification proceeds exactly as it did before
this resolver existed (this preserves `FixturePricePlane`'s behaviour, since it
carries no `find_by_company_name`).

The result is written into `price_snapshots.delisting_audit_json`, so a cohort
can say which snapshot it ran on and what that snapshot's audit said.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Sequence

from data.prices.base import PricePlane, PricePlaneError, normalize_company_name
from data.prices.delisting_audit_list import DELISTING_AUDIT_LIST, DelistingCase

#: Sessions the terminal return is measured over.
DEFAULT_WINDOW_SESSIONS = 10

#: Below this, the series carried the collapse.
DEFAULT_COLLAPSE_THRESHOLD = -0.60

#: A ticker assumed always-listed, used to derive a plane's purchased history
#: start when nothing configures one explicitly (`resolve_history_start`).
DEFAULT_HISTORY_REFERENCE_TICKER = "AAPL"

#: Refuse the audit below this many testable cases (Spec N §12 ruling): a
#: check that can only ask a handful of its twenty questions is not a check.
DEFAULT_MIN_TESTABLE_CASES = 10

CLASSIFICATIONS = (
    "collapse", "stop", "missing", "too_short", "unresolved", "out_of_window",
)

#: How `resolved_symbol` was arrived at. `asis` means "no resolution evidence
#: either way" (see the module docstring's step 4) — it is not a claim the
#: symbol is correct, only that nothing here contradicts it.
RESOLUTIONS = ("explicit", "security_master", "name_match", "asis", "unresolved")

#: Not testable at all: the plane was never asked for bars.
UNTESTABLE_CLASSIFICATIONS = ("unresolved", "out_of_window")


class InsufficientTestableCasesError(PricePlaneError):
    """Fewer than `min_testable` cases could be asked of this plane/tier."""


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving one case's vendor symbol."""

    resolved_symbol: str | None
    resolution: str
    candidates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.resolution not in RESOLUTIONS:
            raise ValueError(f"unknown resolution {self.resolution!r}; expected one of {RESOLUTIONS}")
        if self.resolution == "unresolved" and self.resolved_symbol is not None:
            raise ValueError("an unresolved case must not carry a resolved_symbol")
        if self.resolution != "unresolved" and self.resolved_symbol is None:
            raise ValueError(f"resolution {self.resolution!r} must carry a resolved_symbol")


@dataclass(frozen=True)
class CaseResult:
    ticker: str
    company: str
    venue: str
    expected_delisting_date: str
    classification: str
    resolved_symbol: str | None
    resolution: str
    resolution_candidates: tuple[str, ...]
    resolution_note: str
    last_session: str | None
    sessions_available: int
    terminal_return: float | None
    last_close: float | None
    source_kind: str
    source_url: str
    source_verified: bool


def resolve_case(plane: PricePlane, case: DelistingCase) -> Resolution:
    """Find the vendor symbol to ask `plane` for, without ever guessing.

    See the module docstring for the four-step order. Never calls
    `plane.daily_bars` — that is `classify_case`'s job, only once a symbol is
    in hand.
    """
    explicit = case.vendor_symbols.get(plane.source)
    if explicit:
        return Resolution(explicit, "explicit", candidates=(explicit,))

    if plane.security_master([case.ticker]):
        return Resolution(case.ticker, "security_master", candidates=(case.ticker,))

    if getattr(plane, "supports_company_name_search", False):
        raw_candidates = plane.find_by_company_name(case.company, original_ticker=case.ticker)
        target = normalize_company_name(case.company)
        accepted = [
            row for row in raw_candidates
            if normalize_company_name(str(row.get("name") or "")) == target
        ]
        candidate_tickers = tuple(sorted({str(row["ticker"]) for row in raw_candidates}))
        if len(accepted) == 1:
            return Resolution(
                str(accepted[0]["ticker"]), "name_match", candidates=candidate_tickers,
            )
        return Resolution(None, "unresolved", candidates=candidate_tickers)

    return Resolution(case.ticker, "asis")


def resolve_history_start(
    plane: PricePlane, reference_ticker: str = DEFAULT_HISTORY_REFERENCE_TICKER,
) -> date | None:
    """The plane's purchased history start, or `None` if it cannot be told.

    Derived from the first bar of a known always-listed ticker (default
    `AAPL`) rather than asked of the plane directly — no `PricePlane` method
    reports a purchased tier, because the tier is a subscription fact, not a
    data fact. A plane with no bars for the reference ticker (a fixture with a
    small synthetic universe) reports `None`: every case is then treated as
    within-window rather than refused for a fact this plane cannot supply.
    """
    bars = plane.daily_bars(reference_ticker)
    return bars[0].session_date if bars else None


def classify_case(
    plane: PricePlane,
    case: DelistingCase,
    window: int = DEFAULT_WINDOW_SESSIONS,
    collapse_threshold: float = DEFAULT_COLLAPSE_THRESHOLD,
    history_start: date | None = None,
) -> CaseResult:
    def result(
        classification: str,
        terminal: float | None = None,
        bars: Sequence = (),
        resolution: Resolution | None = None,
    ) -> CaseResult:
        resolution = resolution or Resolution(None, "unresolved")
        return CaseResult(
            ticker=case.ticker,
            company=case.company,
            venue=case.venue,
            expected_delisting_date=case.delisting_date.isoformat(),
            classification=classification,
            resolved_symbol=resolution.resolved_symbol,
            resolution=resolution.resolution,
            resolution_candidates=resolution.candidates,
            resolution_note=case.resolution_note,
            last_session=bars[-1].session_date.isoformat() if bars else None,
            sessions_available=len(bars),
            terminal_return=terminal,
            last_close=bars[-1].split_adjusted_close if bars else None,
            source_kind=case.source_kind,
            source_url=case.source_url,
            source_verified=case.verified,
        )

    if history_start is not None and case.delisting_date < history_start:
        return result("out_of_window")

    resolution = resolve_case(plane, case)
    if resolution.resolved_symbol is None:
        return result("unresolved", resolution=resolution)

    bars = plane.daily_bars(resolution.resolved_symbol)
    if not bars:
        return result("missing", resolution=resolution)
    if len(bars) < window + 1:
        return result("too_short", bars=bars, resolution=resolution)

    start = bars[-1 - window].split_adjusted_close
    if start <= 0:
        return result("too_short", bars=bars, resolution=resolution)
    terminal = bars[-1].split_adjusted_close / start - 1.0
    classification = "collapse" if terminal < collapse_threshold else "stop"
    return result(classification, terminal, bars=bars, resolution=resolution)


def run_audit(
    plane: PricePlane,
    cases: Sequence[DelistingCase] = DELISTING_AUDIT_LIST,
    window: int = DEFAULT_WINDOW_SESSIONS,
    collapse_threshold: float = DEFAULT_COLLAPSE_THRESHOLD,
    min_testable: int = DEFAULT_MIN_TESTABLE_CASES,
    history_start: date | None = None,
) -> dict:
    """Run the audit and return the JSON blob stored on the snapshot.

    Raises `InsufficientTestableCasesError` — a subclass of `PricePlaneError`,
    so a caller already catching that (the CLI does) needs no new branch —
    when fewer than `min_testable` cases were testable. Nothing is returned or
    stored in that case: a refusal must not look like a thin answer.
    """
    if history_start is None:
        history_start = resolve_history_start(plane)

    results = [
        classify_case(plane, case, window, collapse_threshold, history_start=history_start)
        for case in cases
    ]
    counts = {name: 0 for name in CLASSIFICATIONS}
    for item in results:
        counts[item.classification] += 1

    n_cases = len(results)
    n_testable = n_cases - sum(counts[name] for name in UNTESTABLE_CLASSIFICATIONS)
    if n_testable < min_testable:
        raise InsufficientTestableCasesError(
            f"only {n_testable} of {n_cases} cases are testable under this plane/tier "
            f"(minimum {min_testable}): {counts['unresolved']} unresolved, "
            f"{counts['out_of_window']} out of window. A check that thin is not a check "
            "— resolve more cases (`--resolve`) or buy a longer history tier."
        )

    classified = counts["collapse"] + counts["stop"]
    return {
        "spec": "N-4.2-delisting-returns",
        "source": plane.source,
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "window_sessions": window,
        "collapse_threshold": collapse_threshold,
        "history_start": history_start.isoformat() if history_start else None,
        "min_testable_cases": min_testable,
        "n_cases": n_cases,
        "n_testable": n_testable,
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
