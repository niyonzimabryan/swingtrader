"""The price-plane adapter interface and the records it exchanges.

Spec N §4.3 stores **three** series per name, not two, plus the factors that map
between them:

  * **raw** OHLCV — what fills actually happened at;
  * **split-adjusted** — what signals, covariates and replay run on, so that an
    economically neutral 2-for-1 mid-hold does not fire a fixed stop;
  * **total-return** — split *and* dividend, what the benchmark comparison uses.

A `PricePlane` is the only thing that talks to a vendor. It returns plain frozen
dataclasses; persistence (`data.prices.store`) and the derived covariates
(`data.prices.derived`) work on those, never on a vendor payload. That is what
lets `FixturePricePlane` and `SharadarPricePlane` be substituted for each other
with no adapter-on-adapter layer.

Two rules every implementation obeys, and `test_adapter_schema_change_fails_loudly`
proves for the vendor one:

1. **Raise on an unexpected payload shape.** A renamed or missing vendor column
   is an error, never a silent `None`. A price plane that degrades quietly is
   worse than one that is down, because the cohort statistics it feeds look fine.
2. **Never write nulls.** Every field on `DailyBar` is required and finite.
"""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Sequence

#: Delisting reason categories, mirrored in `database.models.DELISTING_REASONS`.
#: Only `performance` carries a Shumway terminal return; `unknown` censors.
DELISTING_REASONS = ("performance", "merger_acquisition", "other", "unknown")

#: What kind of instrument a security-master row describes.
#:
#: The distinction is not cosmetic and it is not a vendor detail: an ETF is the
#: *benchmark* a cohort's abnormal returns are measured against (Spec N §5.2),
#: and a benchmark that is also a cohort member is a number measured against
#: itself. `data/prices/universes.py` therefore ranks equities only, and
#: `tests/test_price_plane.py` asserts a fund can never enter a liquidity-ranked
#: universe. `equity` is the default so that every row written before this
#: field existed reads as what it in fact was.
ASSET_CLASS_EQUITY = "equity"
ASSET_CLASS_FUND = "fund"
ASSET_CLASSES = (ASSET_CLASS_EQUITY, ASSET_CLASS_FUND)

#: The Shumway venue split: -30% NYSE/AMEX, -55% Nasdaq (Spec N §4.2).
VENUES = ("nyse_amex", "nasdaq", "other", "unknown")

#: Corporate-action types this plane recognises. Anything else is stored
#: verbatim and ignored by the derived series.
ACTION_SPLIT = "split"
ACTION_DIVIDEND = "dividend"
ACTION_DELISTING = "delisting"


class PricePlaneError(RuntimeError):
    """Base class for every failure this package raises deliberately."""


class PricePlaneSchemaError(PricePlaneError):
    """A vendor payload did not have the shape the adapter was written against.

    Raised on a missing or renamed column, a row of the wrong width, a value
    that will not parse, or a null in a field the plane must never store empty.
    """


class PricePlaneConfigError(PricePlaneError):
    """The plane is misconfigured — no API key, no fixture directory, flag off."""


def _finite(value: float, field: str, context: str) -> float:
    if value is None or not math.isfinite(value):
        raise PricePlaneSchemaError(f"{context}: {field} is {value!r}, which is not a finite number")
    return float(value)


@dataclass(frozen=True)
class SecurityMasterRow:
    """One security-master row: a stable id, a ticker validity interval, a fate.

    `security_uid` is stable across ticker changes — Sharadar's `permaticker`,
    or `{source}:{ticker}` for a source that has no such id. `delisting_reason`
    is one of `DELISTING_REASONS`; a vendor that does not publish a reason gets
    `unknown`, which is honest and which the §4.4 censoring rule then treats as
    censored rather than matured.

    `asset_class` is one of `ASSET_CLASSES` and defaults to `equity`, which is
    what every row written before the field existed was. It is what keeps the
    benchmark ETF out of "liquid US equities": see `ASSET_CLASSES` above.
    """

    security_uid: str
    ticker: str
    source: str
    name: str | None = None
    exchange: str | None = None
    venue: str = "unknown"
    ticker_valid_from: date | None = None
    ticker_valid_to: date | None = None
    listing_date: date | None = None
    delisting_date: date | None = None
    delisting_reason: str = "unknown"
    asset_class: str = ASSET_CLASS_EQUITY

    def __post_init__(self) -> None:
        if not self.security_uid:
            raise PricePlaneSchemaError("security_uid must not be empty")
        if not self.ticker:
            raise PricePlaneSchemaError(f"{self.security_uid}: ticker must not be empty")
        if self.asset_class not in ASSET_CLASSES:
            raise PricePlaneSchemaError(
                f"{self.ticker}: unknown asset class {self.asset_class!r}; "
                f"expected one of {ASSET_CLASSES}"
            )
        if self.delisting_reason not in DELISTING_REASONS:
            raise PricePlaneSchemaError(
                f"{self.ticker}: unknown delisting reason {self.delisting_reason!r}; "
                f"expected one of {DELISTING_REASONS}"
            )
        if self.venue not in VENUES:
            raise PricePlaneSchemaError(
                f"{self.ticker}: unknown venue {self.venue!r}; expected one of {VENUES}"
            )
        if self.delisting_date is not None and self.delisting_reason == "unknown":
            # Allowed, and deliberately not an error: it is exactly the state a
            # vendor with no reason field leaves us in. The audit reports it.
            pass


@dataclass(frozen=True)
class DailyBar:
    """One daily session with all three series and the factors between them.

    `split_factor` is the share multiplier whose **ex-date is this session**
    (2.0 for a 2-for-1), and `dividend_cash` the cash per share with the same
    ex-date. Both are 1.0 / 0.0 on an ordinary day. Storing the factor beside
    the bar rather than only in the actions table is what makes the series
    reconstructible from stored rows alone (`derived.reconstruct_*`).
    """

    security_uid: str
    ticker: str
    session_date: date
    raw_open: float
    raw_high: float
    raw_low: float
    raw_close: float
    volume: float
    split_factor: float
    dividend_cash: float
    split_adjusted_close: float
    total_return_close: float
    source: str

    def __post_init__(self) -> None:
        context = f"{self.ticker} {self.session_date}"
        for field in (
            "raw_open", "raw_high", "raw_low", "raw_close", "volume",
            "split_factor", "dividend_cash", "split_adjusted_close", "total_return_close",
        ):
            _finite(getattr(self, field), field, context)
        if self.raw_close <= 0:
            raise PricePlaneSchemaError(f"{context}: raw_close must be positive, got {self.raw_close}")
        if self.split_factor <= 0:
            raise PricePlaneSchemaError(f"{context}: split_factor must be positive")
        if self.volume < 0:
            raise PricePlaneSchemaError(f"{context}: volume must not be negative")

    @property
    def dollar_volume(self) -> float:
        """Raw close x raw volume — invariant to splits, which is the point."""
        return self.raw_close * self.volume


@dataclass(frozen=True)
class CorporateActionRecord:
    """A split, dividend or delisting stored with its ex-date (Spec N §4.3)."""

    security_uid: str
    ticker: str
    ex_date: date
    action_type: str
    value: float | None
    source: str

    def __post_init__(self) -> None:
        if not self.action_type:
            raise PricePlaneSchemaError(f"{self.ticker} {self.ex_date}: empty action type")


@dataclass(frozen=True)
class MembershipInterval:
    """Half-open point-in-time membership: `member_from <= d < member_to`.

    `member_to is None` means "still a member". `known_at_utc` is when the fact
    could first have been acted on; for a retrospective reconstruction such as
    `sp500_wikipedia_v1` it is the effective date, and the source's provenance
    class (`archival_reconstructed`) is what records that this is a
    reconstruction rather than an observation.
    """

    universe_slug: str
    security_uid: str
    ticker: str
    member_from: date
    member_to: date | None
    source: str
    known_at_utc: datetime

    def __post_init__(self) -> None:
        if self.member_to is not None and self.member_to <= self.member_from:
            raise PricePlaneSchemaError(
                f"{self.ticker}: member_to {self.member_to} is not after member_from {self.member_from}"
            )
        if self.known_at_utc.tzinfo is None:
            raise PricePlaneSchemaError(f"{self.ticker}: known_at_utc must be timezone-aware")

    def covers(self, day: date) -> bool:
        return self.member_from <= day and (self.member_to is None or day < self.member_to)


class PricePlane(ABC):
    """A source of point-in-time prices, actions, security master and membership.

    Every method returns records ordered deterministically, so that two runs
    against the same source produce byte-identical stored rows — the property
    `test_liquid_universe_rule_reproducible` leans on.
    """

    #: Written into every row this plane produces.
    source: str = "unset"

    @abstractmethod
    def daily_bars(
        self,
        ticker: str,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[DailyBar, ...]:
        """Daily bars for one name, ascending by session date.

        Delisted names are included: a plane that drops them is survivorship
        bias by construction (Spec N §4.2). An unknown ticker returns `()`.
        """

    @abstractmethod
    def corporate_actions(
        self,
        ticker: str,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[CorporateActionRecord, ...]:
        """Splits, dividends and delistings with their ex-dates, ascending."""

    @abstractmethod
    def security_master(
        self,
        tickers: Sequence[str] | None = None,
        *,
        asset_class: str | None = ASSET_CLASS_EQUITY,
    ) -> tuple[SecurityMasterRow, ...]:
        """Security-master rows, including delisted names with their reason.

        `asset_class` names which instrument class to look up. It defaults to
        `equity` rather than to `None` on purpose: a caller that has not thought
        about funds gets the behaviour it had before funds existed, and a
        universe job cannot pick up ETFs by forgetting an argument. `None`
        means "whatever class the source says this name is", which is the
        auto-detect path a backfill uses when the operator has not said.
        """

    @abstractmethod
    def index_membership(self, universe_slug: str) -> tuple[MembershipInterval, ...]:
        """Constituent history for an index this plane can supply.

        Raises `PricePlaneConfigError` for a slug the plane does not carry, so a
        caller cannot silently receive an empty universe and build a cohort on it.
        """

    def known_universes(self) -> tuple[str, ...]:
        return ()

    #: True on a plane whose `find_by_company_name` performs a real search.
    #: The delisting audit's resolver (`data/prices/audit.py`) needs to tell
    #: "this plane cannot search by name" from "it searched and found
    #: nothing" — the two are different facts and only one of them means the
    #: audit should give up on a case (Spec N §12 ruling).
    supports_company_name_search: bool = False

    def find_by_company_name(
        self, company: str, *, original_ticker: str | None = None
    ) -> tuple[dict, ...]:
        """Best-effort candidate vendor rows for a company under an unknown symbol.

        The default implementation performs no search and returns `()`; a
        plane that overrides `supports_company_name_search = True` must also
        override this. Returns raw vendor-shaped dicts (at least `ticker` and
        `name`) rather than `SecurityMasterRow` — candidate search does not
        need, and should not pay for, whatever a plane's `security_master`
        derives per row (Sharadar's delisting reason costs one extra call per
        delisted name).
        """
        return ()


_CORP_SUFFIX_RE = re.compile(
    r"\b(INCORPORATED|INC|CORPORATION|CORP|COMPANY|CO|LIMITED|LTD|"
    r"HOLDINGS?|HOLDCO|GROUP|TRUST|LLC|LP|PLC)\b\.?"
)
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9 ]")


def normalize_company_name(name: str) -> str:
    """A strict, deterministic key for comparing two company names.

    Uppercases, drops punctuation, and strips the common corporate-suffix
    words (`Inc`, `Corp`, `Holdings`, ...) so that "RadioShack Corporation"
    and "RADIOSHACK CORP" collapse to the same key. It is deliberately strict
    rather than fuzzy: the delisting audit's resolver (`data/prices/audit.py`)
    uses this as the sole acceptance test for a name-matched candidate, and a
    fuzzy match would accept the wrong company silently (Spec N §12 ruling).
    """
    upper = _NON_ALNUM_RE.sub(" ", name.upper())
    upper = _CORP_SUFFIX_RE.sub(" ", upper)
    return " ".join(upper.split())


def members_as_of(
    intervals: Iterable[MembershipInterval],
    day: date,
) -> tuple[MembershipInterval, ...]:
    """The point-in-time filter, in one place so nobody re-derives it wrong.

    A name that joined *after* `day` is not a member as of `day`, and a name
    that left before it is not either. This is the whole of Spec N §4.2's
    "membership is evaluated as of the event date, not from today's list".
    """
    return tuple(sorted(
        (i for i in intervals if i.covers(day)),
        key=lambda i: (i.ticker, i.security_uid, i.member_from),
    ))
