"""`sp500_wikipedia_v1` — S&P 500 membership history from `fja05680/sp500`.

The file is a per-change-date snapshot of the index: one row per date, one
comma-separated list of constituent tickers. Converting it into the half-open
intervals `universe_membership` stores is the whole job.

**Read the provenance before trusting it** (verification §22). This is a
hand-maintained Wikipedia scrape: the original list came from Andreas Clenow's
*Trading Evolved* and runs 1996–2019; everything after that is the maintainer
reconciling Wikipedia's "Selected Changes" section against Google searches,
updated "every couple of months". It is MIT-licensed and covers more history
than any affordable vendor, and it is *not* an authoritative membership record.

So its provenance class is **`archival_reconstructed`**, not `vendor_pit`, and a
cohort that leans on it inherits that tier (Spec N §4.1/§8). `known_at_utc` is
set to the close of the effective date, which is the earliest moment the change
could have been acted on — the honest reading, given that the change dates
themselves were researched after the fact.

Two truncation effects, both structural and both worth knowing:

* Every name in the first row gets `member_from` = the first snapshot date, so
  membership durations before 1996-01-02 are unknowable from this file.
* A name can leave and rejoin; that produces two intervals, not one, which is
  why the natural key includes `member_from`.
"""

from __future__ import annotations

import csv
from datetime import date, datetime, time, timezone
from pathlib import Path

from data.prices.base import MembershipInterval, PricePlaneSchemaError

UNIVERSE_SLUG = "sp500_wikipedia_v1"
SOURCE = "fja05680/sp500@a2430f2"

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The committed copy. MIT-licensed, attribution in `docs/DATA_LICENSES.md` and
#: in the `.LICENSE` file beside it.
DEFAULT_CSV = REPO_ROOT / "data" / "prices" / "sp500" / "sp500_historical_components_fja05680.csv"

#: The upstream commit the committed copy was taken from, so a refresh is a
#: diff rather than a mystery.
UPSTREAM_COMMIT = "a2430f2af0c79ddf0748e91de11bdeb1616ab5a7"
UPSTREAM_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/"
    f"{UPSTREAM_COMMIT}/S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv"
)

EXPECTED_HEADER = ("date", "tickers")

SESSION_CLOSE_UTC = time(21, 0, tzinfo=timezone.utc)


def read_snapshots(path: Path | str | None = None) -> tuple[tuple[date, tuple[str, ...]], ...]:
    """`((effective_date, tickers), ...)` ascending, from the committed CSV."""
    csv_path = Path(path) if path is not None else DEFAULT_CSV
    if not csv_path.exists():
        raise PricePlaneSchemaError(
            f"{csv_path} is missing. It is committed in this repo; if you are "
            f"refreshing it, take it from {UPSTREAM_URL}"
        )
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = tuple(next(reader, ()))
        if header != EXPECTED_HEADER:
            raise PricePlaneSchemaError(
                f"{csv_path.name}: header is {header}, expected {EXPECTED_HEADER}"
            )
        snapshots = []
        for line, row in enumerate(reader, start=2):
            if len(row) != 2:
                raise PricePlaneSchemaError(f"{csv_path.name}:{line}: {len(row)} fields, expected 2")
            try:
                effective = date.fromisoformat(row[0].strip())
            except ValueError as exc:
                raise PricePlaneSchemaError(
                    f"{csv_path.name}:{line}: {row[0]!r} is not an ISO date"
                ) from exc
            tickers = tuple(sorted({t.strip() for t in row[1].split(",") if t.strip()}))
            if not tickers:
                raise PricePlaneSchemaError(f"{csv_path.name}:{line}: empty constituent list")
            snapshots.append((effective, tickers))
    if not snapshots:
        raise PricePlaneSchemaError(f"{csv_path.name}: no rows")
    snapshots.sort(key=lambda pair: pair[0])
    return tuple(snapshots)


def membership_intervals(path: Path | str | None = None) -> tuple[MembershipInterval, ...]:
    """Half-open membership intervals from the per-date constituent snapshots.

    A ticker present in snapshot *k* but not *k-1* joins on snapshot *k*'s date;
    a ticker present in *k-1* but not *k* leaves on snapshot *k*'s date, which
    is the exclusive `member_to`. Names still present in the final snapshot get
    `member_to = None`.
    """
    snapshots = read_snapshots(path)
    open_from: dict[str, date] = {}
    intervals: list[MembershipInterval] = []
    previous: frozenset[str] = frozenset()

    for effective, tickers in snapshots:
        current = frozenset(tickers)
        for ticker in sorted(current - previous):
            open_from[ticker] = effective
        for ticker in sorted(previous - current):
            member_from = open_from.pop(ticker, None)
            if member_from is None:
                continue
            intervals.append(_interval(ticker, member_from, effective))
        previous = current

    for ticker, member_from in sorted(open_from.items()):
        intervals.append(_interval(ticker, member_from, None))

    intervals.sort(key=lambda i: (i.member_from, i.ticker))
    return tuple(intervals)


def _interval(ticker: str, member_from: date, member_to: date | None) -> MembershipInterval:
    return MembershipInterval(
        universe_slug=UNIVERSE_SLUG,
        # This source has no security id of its own; the ticker is all it knows.
        # A later phase that resolves tickers to permatickers rewrites the uid;
        # the prefix is what makes such a row identifiable as unresolved.
        security_uid=f"ticker:{ticker}",
        ticker=ticker,
        member_from=member_from,
        member_to=member_to,
        source=SOURCE,
        known_at_utc=datetime.combine(member_from, SESSION_CLOSE_UTC),
    )
