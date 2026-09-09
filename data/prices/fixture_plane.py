"""`FixturePricePlane` — the whole interface, backed by committed CSVs.

It exists so the tests, the delisting audit and the universe job can run with no
network, no key and no vendor payload, and so that "does the plane satisfy the
contract" is answerable before a subscription is bought.

The fixtures live under `tests/fixtures/prices/` and are entirely synthetic
(`tests/fixtures/prices/generate_fixtures.py`). Two of them carry real delisted
tickers so the offline audit exercises the real `delisting_audit_list` rows; the
prices behind those tickers are invented, and nothing under `research/` or here
is a vendor series (Spec K §3.3).

The plane computes the split-adjusted and total-return closes from the raw
closes and the stored factors, rather than storing them in the CSV — which
means the fixtures cannot drift from the arithmetic they are supposed to
demonstrate.
"""

from __future__ import annotations

import csv
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Sequence

from data.prices.base import (
    CorporateActionRecord,
    DailyBar,
    MembershipInterval,
    PricePlane,
    PricePlaneConfigError,
    PricePlaneSchemaError,
    SecurityMasterRow,
)
from data.prices.derived import with_derived_series

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "prices"

SOURCE = "fixture"

SECURITY_COLUMNS = (
    "security_uid", "ticker", "name", "exchange", "venue", "ticker_valid_from",
    "ticker_valid_to", "listing_date", "delisting_date", "delisting_reason",
)
BAR_COLUMNS = (
    "security_uid", "ticker", "session_date", "raw_open", "raw_high", "raw_low",
    "raw_close", "volume", "split_factor", "dividend_cash",
)
ACTION_COLUMNS = ("security_uid", "ticker", "ex_date", "action_type", "value")
MEMBERSHIP_COLUMNS = (
    "universe_slug", "security_uid", "ticker", "member_from", "member_to", "known_at_utc",
)


def _read(path: Path, expected: Sequence[str]) -> list[dict[str, str]]:
    if not path.exists():
        raise PricePlaneConfigError(f"fixture file missing: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        header = tuple(reader.fieldnames or ())
        if header != tuple(expected):
            raise PricePlaneSchemaError(
                f"{path.name}: header is {header}, expected {tuple(expected)}"
            )
        return list(reader)


def _date(value: str, context: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise PricePlaneSchemaError(f"{context}: {value!r} is not an ISO date") from exc


def _required_date(value: str, context: str) -> date:
    parsed = _date(value, context)
    if parsed is None:
        raise PricePlaneSchemaError(f"{context}: a date is required, got an empty cell")
    return parsed


def _float(value: str, context: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise PricePlaneSchemaError(f"{context}: {value!r} is not a number") from exc


class FixturePricePlane(PricePlane):
    """A `PricePlane` over four CSVs. Deterministic, offline, committed."""

    source = SOURCE

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root is not None else DEFAULT_FIXTURE_ROOT
        if not self.root.is_dir():
            raise PricePlaneConfigError(f"fixture directory not found: {self.root}")
        self._bars: dict[str, tuple[DailyBar, ...]] | None = None
        self._securities: tuple[SecurityMasterRow, ...] | None = None
        self._actions: tuple[CorporateActionRecord, ...] | None = None
        self._membership: tuple[MembershipInterval, ...] | None = None

    # -- loading ---------------------------------------------------------- #

    def _load_securities(self) -> tuple[SecurityMasterRow, ...]:
        if self._securities is None:
            rows = _read(self.root / "securities.csv", SECURITY_COLUMNS)
            self._securities = tuple(
                SecurityMasterRow(
                    security_uid=row["security_uid"],
                    ticker=row["ticker"],
                    source=self.source,
                    name=row["name"] or None,
                    exchange=row["exchange"] or None,
                    venue=row["venue"] or "unknown",
                    ticker_valid_from=_date(row["ticker_valid_from"], row["ticker"]),
                    ticker_valid_to=_date(row["ticker_valid_to"], row["ticker"]),
                    listing_date=_date(row["listing_date"], row["ticker"]),
                    delisting_date=_date(row["delisting_date"], row["ticker"]),
                    delisting_reason=row["delisting_reason"] or "unknown",
                )
                for row in rows
            )
        return self._securities

    def _load_bars(self) -> dict[str, tuple[DailyBar, ...]]:
        if self._bars is None:
            rows = _read(self.root / "bars.csv", BAR_COLUMNS)
            grouped: dict[str, list[DailyBar]] = {}
            for row in rows:
                context = f"{row['ticker']} {row['session_date']}"
                bar = DailyBar(
                    security_uid=row["security_uid"],
                    ticker=row["ticker"],
                    session_date=_required_date(row["session_date"], context),
                    raw_open=_float(row["raw_open"], context),
                    raw_high=_float(row["raw_high"], context),
                    raw_low=_float(row["raw_low"], context),
                    raw_close=_float(row["raw_close"], context),
                    volume=_float(row["volume"], context),
                    split_factor=_float(row["split_factor"], context),
                    dividend_cash=_float(row["dividend_cash"], context),
                    # Filled below from the raw closes and the factors.
                    split_adjusted_close=_float(row["raw_close"], context),
                    total_return_close=_float(row["raw_close"], context),
                    source=self.source,
                )
                grouped.setdefault(row["ticker"], []).append(bar)
            self._bars = {
                ticker: with_derived_series(sorted(bars, key=lambda b: b.session_date))
                for ticker, bars in grouped.items()
            }
        return self._bars

    def _load_actions(self) -> tuple[CorporateActionRecord, ...]:
        if self._actions is None:
            rows = _read(self.root / "corporate_actions.csv", ACTION_COLUMNS)
            self._actions = tuple(
                CorporateActionRecord(
                    security_uid=row["security_uid"],
                    ticker=row["ticker"],
                    ex_date=_required_date(row["ex_date"], row["ticker"]),
                    action_type=row["action_type"],
                    value=_float(row["value"], row["ticker"]) if row["value"] else None,
                    source=self.source,
                )
                for row in rows
            )
        return self._actions

    def _load_membership(self) -> tuple[MembershipInterval, ...]:
        if self._membership is None:
            rows = _read(self.root / "universe_membership.csv", MEMBERSHIP_COLUMNS)
            out = []
            for row in rows:
                known = row["known_at_utc"]
                try:
                    known_at = datetime.fromisoformat(known)
                except ValueError as exc:
                    raise PricePlaneSchemaError(
                        f"{row['ticker']}: {known!r} is not an ISO timestamp"
                    ) from exc
                if known_at.tzinfo is None:
                    known_at = known_at.replace(tzinfo=timezone.utc)
                out.append(MembershipInterval(
                    universe_slug=row["universe_slug"],
                    security_uid=row["security_uid"],
                    ticker=row["ticker"],
                    member_from=_required_date(row["member_from"], row["ticker"]),
                    member_to=_date(row["member_to"], row["ticker"]),
                    source=self.source,
                    known_at_utc=known_at,
                ))
            self._membership = tuple(out)
        return self._membership

    # -- interface -------------------------------------------------------- #

    def daily_bars(
        self, ticker: str, start: date | None = None, end: date | None = None
    ) -> tuple[DailyBar, ...]:
        bars = self._load_bars().get(ticker, ())
        return tuple(
            bar for bar in bars
            if (start is None or bar.session_date >= start)
            and (end is None or bar.session_date <= end)
        )

    def corporate_actions(
        self, ticker: str, start: date | None = None, end: date | None = None
    ) -> tuple[CorporateActionRecord, ...]:
        return tuple(sorted(
            (
                action for action in self._load_actions()
                if action.ticker == ticker
                and (start is None or action.ex_date >= start)
                and (end is None or action.ex_date <= end)
            ),
            key=lambda a: (a.ex_date, a.action_type),
        ))

    def security_master(
        self, tickers: Sequence[str] | None = None
    ) -> tuple[SecurityMasterRow, ...]:
        rows = self._load_securities()
        if tickers is not None:
            wanted = set(tickers)
            rows = tuple(row for row in rows if row.ticker in wanted)
        return tuple(sorted(rows, key=lambda r: (r.ticker, r.security_uid)))

    def index_membership(self, universe_slug: str) -> tuple[MembershipInterval, ...]:
        rows = tuple(
            interval for interval in self._load_membership()
            if interval.universe_slug == universe_slug
        )
        if not rows:
            raise PricePlaneConfigError(
                f"{self.source}: no membership history for {universe_slug!r}; "
                f"known universes: {self.known_universes()}"
            )
        return tuple(sorted(rows, key=lambda i: (i.member_from, i.ticker)))

    def known_universes(self) -> tuple[str, ...]:
        return tuple(sorted({i.universe_slug for i in self._load_membership()}))

    def tickers(self) -> tuple[str, ...]:
        return tuple(sorted(self._load_bars()))


#: The close of a US session, in UTC, used to stamp `known_at_utc` on rows whose
#: knowledge date is "after this session's close". Mirrors
#: `comparables.config.SESSION_OPEN_UTC`, which is the other end of the day.
SESSION_CLOSE_UTC = time(21, 0, tzinfo=timezone.utc)


def session_close_utc(day: date) -> datetime:
    return datetime.combine(day, SESSION_CLOSE_UTC)
