"""Form 4 — insider transactions, with the transaction codes kept apart.

Spec O section 3.1 rule 3, and the reason this module exists at all:

> Form 4 must distinguish by **transaction code**: ``P`` (open-market purchase)
> and ``S`` (sale) are the informative ones; ``A`` (award), ``M`` (option
> exercise), ``F`` (tax withholding) and ``G`` (gift) are never pooled with
> them, and the 10b5-1 plan checkbox flags planned sales. Conflating them is
> the most common insider-data error and inverts the signal.

**The separation is structural, not a convention.** Each code category gets its
own ``fact_type`` — ``insider_open_market_purchase`` is a different fact from
``insider_award`` — so pooling them is not something a caller can do by
forgetting a filter. :func:`net_open_market_shares` refuses any row that is not
``P``/``S``, with the reason, rather than quietly summing what it was handed.
A vesting award is not a vote of confidence, and a sale to cover withholding
tax on that award is not a vote of no confidence; a "net insider activity"
number that adds them to open-market trades is worse than no number.

**The Rule 10b5-1 flag is detected, not assumed.** Verification claim 14 could
not confirm the field-level representation of the checkbox added to Form 4 in
2023, so this parser looks for any of the documented candidate elements *and*
falls back to the footnote text, and records which mechanism fired
(``plan_10b5_1_basis``). When none fires the value is ``None`` — **unknown**,
not ``False``. Encoding "we could not tell" as "not a plan" is what would make
a planned sale read as a discretionary one, which is the exact inversion this
module exists to prevent. Confirming the element name against a real filing is
one REPL session and is listed as an owner action.

Parsing is stdlib ``xml.etree``: the ownership schema is small and flat, and
Phase 3a's argument against adding ``edgartools`` (one throttle, in one client
module) still holds.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Sequence

from filings import client as sec_client
from filings.errors import AdapterSchemaError
from filings.observations import (
    PRECISION_SECOND,
    PROVENANCE_VENDOR_PIT,
    TRUST_PRIMARY_REGULATOR,
    Observation,
)
from utils.logger import get_logger

log = get_logger("form4")

SOURCE_FORM4 = "sec_form4"

#: A Form 4 XML document is a few tens of kilobytes. A hundredfold headroom
#: still refuses an entity-expansion bomb before the parser sees it.
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024

# --- transaction codes (SEC Form 4 general instructions, table I/II) --------

CODE_PURCHASE = "P"
CODE_SALE = "S"
CODE_AWARD = "A"
CODE_EXERCISE = "M"
CODE_TAX_WITHHOLDING = "F"
CODE_GIFT = "G"

CATEGORY_OPEN_MARKET = "open_market"
CATEGORY_AWARD = "award"
CATEGORY_EXERCISE = "exercise"
CATEGORY_TAX = "tax_withholding"
CATEGORY_GIFT = "gift"
CATEGORY_OTHER = "other"

#: Code -> (category, human description). The codes SEC documents; anything
#: outside it is an adapter-visible surprise, not a silent ``other``.
TRANSACTION_CODES: dict[str, tuple[str, str]] = {
    "P": (CATEGORY_OPEN_MARKET, "Open-market or private purchase"),
    "S": (CATEGORY_OPEN_MARKET, "Open-market or private sale"),
    "A": (CATEGORY_AWARD, "Grant, award or other acquisition"),
    "D": (CATEGORY_AWARD, "Disposition to the issuer"),
    "F": (CATEGORY_TAX, "Payment of exercise price or tax by delivering shares"),
    "M": (CATEGORY_EXERCISE, "Exercise or conversion of a derivative security"),
    "C": (CATEGORY_EXERCISE, "Conversion of a derivative security"),
    "X": (CATEGORY_EXERCISE, "Exercise of an in-the-money derivative security"),
    "G": (CATEGORY_GIFT, "Bona fide gift"),
    "V": (CATEGORY_OTHER, "Transaction voluntarily reported earlier than required"),
    "I": (CATEGORY_OTHER, "Discretionary transaction"),
    "J": (CATEGORY_OTHER, "Other acquisition or disposition"),
    "K": (CATEGORY_OTHER, "Equity swap or similar instrument"),
    "L": (CATEGORY_OTHER, "Small acquisition"),
    "U": (CATEGORY_OTHER, "Disposition pursuant to a tender of shares"),
    "W": (CATEGORY_OTHER, "Acquisition or disposition by will or laws of descent"),
    "Z": (CATEGORY_OTHER, "Deposit into or withdrawal from a voting trust"),
    "E": (CATEGORY_OTHER, "Expiration of a short derivative position"),
    "H": (CATEGORY_OTHER, "Expiration (or cancellation) of a long derivative position"),
    "O": (CATEGORY_OTHER, "Exercise of an out-of-the-money derivative security"),
}

#: The only two codes that reflect a discretionary decision to buy or sell at
#: the market price. Everything else is compensation mechanics.
OPEN_MARKET_CODES = frozenset({CODE_PURCHASE, CODE_SALE})
#: Named in Spec O section 3.1 as the codes that must never be pooled with
#: ``P``/``S``. They are the frequent ones; the rule is the whole complement.
NEVER_POOLED_WITH_OPEN_MARKET = frozenset(
    code for code in TRANSACTION_CODES if code not in OPEN_MARKET_CODES
)

#: fact_type per category, so two categories cannot land in one bucket.
FACT_TYPE_BY_KEY: dict[str, str] = {
    "open_market_purchase": "insider_open_market_purchase",
    "open_market_sale": "insider_open_market_sale",
    CATEGORY_AWARD: "insider_award",
    CATEGORY_EXERCISE: "insider_derivative_exercise",
    CATEGORY_TAX: "insider_tax_withholding",
    CATEGORY_GIFT: "insider_gift",
    CATEGORY_OTHER: "insider_other_transaction",
}

OPEN_MARKET_FACT_TYPES = frozenset(
    {FACT_TYPE_BY_KEY["open_market_purchase"], FACT_TYPE_BY_KEY["open_market_sale"]}
)
ALL_FACT_TYPES = frozenset(FACT_TYPE_BY_KEY.values())

WARN_10B5_1_UNKNOWN = "rule_10b5_1_flag_unknown"
WARN_10B5_1_FROM_FOOTNOTE = "rule_10b5_1_from_footnote_text"
WARN_NO_PRICE = "transaction_price_absent"


class PoolingRefused(RuntimeError):
    """An aggregation was handed codes that may not be summed together."""


# --- XML helpers ------------------------------------------------------------


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _child_value(parent: ET.Element | None, path: str) -> str:
    """``<x><value>v</value></x>`` and bare ``<x>v</x>`` both give ``v``."""
    if parent is None:
        return ""
    node = parent.find(path)
    if node is None:
        return ""
    inner = node.find("value")
    return _text(inner if inner is not None else node)


def _decimal(raw: str, *, where: str) -> float | None:
    value = (raw or "").strip().replace(",", "")
    if not value:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise AdapterSchemaError(f"{where}: expected a number, got {raw!r}") from exc


def _iso_date(raw: str, *, where: str) -> date | None:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise AdapterSchemaError(f"{where}: unparseable date {raw!r}") from exc


def _truthy(raw: str) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "y", "yes"}


# --- the document -----------------------------------------------------------


@dataclass(frozen=True)
class ReportingOwner:
    cik: str
    name: str
    is_director: bool = False
    is_officer: bool = False
    is_ten_percent_owner: bool = False
    officer_title: str = ""

    @property
    def role(self) -> str:
        roles = []
        if self.is_director:
            roles.append("director")
        if self.is_officer:
            roles.append(f"officer:{self.officer_title}" if self.officer_title else "officer")
        if self.is_ten_percent_owner:
            roles.append("ten_percent_owner")
        return ",".join(roles) or "unspecified"

    @property
    def is_operating_insider(self) -> bool:
        """Spec O section 3.1: operating insiders are the informative subset."""
        return self.is_director or self.is_officer


@dataclass(frozen=True)
class Transaction:
    code: str
    security_title: str
    transaction_date: date
    shares: float | None
    price_per_share: float | None
    acquired_disposed: str
    shares_owned_after: float | None
    direct_or_indirect: str
    is_derivative: bool
    plan_10b5_1: bool | None
    plan_10b5_1_basis: str | None
    footnote_ids: tuple[str, ...] = ()

    @property
    def category(self) -> str:
        return TRANSACTION_CODES[self.code][0]

    @property
    def description(self) -> str:
        return TRANSACTION_CODES[self.code][1]

    @property
    def is_open_market(self) -> bool:
        return self.code in OPEN_MARKET_CODES

    @property
    def fact_key(self) -> str:
        if self.code == CODE_PURCHASE:
            return "open_market_purchase"
        if self.code == CODE_SALE:
            return "open_market_sale"
        return self.category

    @property
    def fact_type(self) -> str:
        return FACT_TYPE_BY_KEY[self.fact_key]

    @property
    def signed_shares(self) -> float | None:
        """Positive for acquisitions, negative for dispositions."""
        if self.shares is None:
            return None
        return self.shares if self.acquired_disposed == "A" else -self.shares

    @property
    def notional(self) -> float | None:
        if self.shares is None or self.price_per_share is None:
            return None
        return self.shares * self.price_per_share


@dataclass(frozen=True)
class Form4Document:
    issuer_cik: str
    issuer_name: str
    issuer_symbol: str
    period_of_report: date | None
    document_type: str
    owners: tuple[ReportingOwner, ...]
    transactions: tuple[Transaction, ...]
    footnotes: dict = field(default_factory=dict)

    @property
    def is_amendment(self) -> bool:
        return self.document_type.upper().endswith("/A")


def _reporting_owners(root: ET.Element) -> tuple[ReportingOwner, ...]:
    out = []
    for node in root.findall("reportingOwner"):
        ident = node.find("reportingOwnerId")
        rel = node.find("reportingOwnerRelationship")
        cik_raw = _child_value(ident, "rptOwnerCik") if ident is not None else ""
        if not cik_raw:
            raise AdapterSchemaError("form4: reportingOwner without rptOwnerCik")
        out.append(
            ReportingOwner(
                cik=sec_client.normalise_cik(cik_raw),
                name=_child_value(ident, "rptOwnerName"),
                is_director=_truthy(_child_value(rel, "isDirector")),
                is_officer=_truthy(_child_value(rel, "isOfficer")),
                is_ten_percent_owner=_truthy(_child_value(rel, "isTenPercentOwner")),
                officer_title=_child_value(rel, "officerTitle"),
            )
        )
    if not out:
        raise AdapterSchemaError("form4: no reportingOwner block")
    return tuple(out)


def _footnotes(root: ET.Element) -> dict:
    block = root.find("footnotes")
    if block is None:
        return {}
    return {
        str(node.get("id") or f"F{index}"): _text(node)
        for index, node in enumerate(block.findall("footnote"), start=1)
    }


#: Element names that have been proposed for the Rule 10b5-1 checkbox. The
#: parser accepts any of them and records which one fired, because verification
#: claim 14 left the field-level name unconfirmed.
CANDIDATE_10B5_1_TAGS = ("aff10b5one", "rule10b5-1flag", "rule10b5-1plan", "rule10b51flag")


def _flag_10b5_1_from_elements(*scopes: ET.Element | None) -> tuple[bool | None, str | None]:
    for scope in scopes:
        if scope is None:
            continue
        for node in scope.iter():
            tag = node.tag.split("}")[-1].lower()
            if tag not in CANDIDATE_10B5_1_TAGS:
                continue
            inner = node.find("value")
            raw = _text(inner if inner is not None else node)
            if raw == "":
                continue
            return _truthy(raw), f"element:{node.tag.split('}')[-1]}"
    return None, None


def _flag_10b5_1_from_footnotes(
    footnote_ids: Sequence[str], footnotes: dict
) -> tuple[bool | None, str | None]:
    for fid in footnote_ids:
        text = (footnotes.get(fid) or "").lower()
        if "10b5-1" in text:
            return True, f"footnote:{fid}"
    return None, None


def _footnote_ids(node: ET.Element) -> tuple[str, ...]:
    return tuple(
        str(ref.get("id"))
        for ref in node.iter()
        if ref.tag.split("}")[-1] == "footnoteId" and ref.get("id")
    )


def _transaction(
    node: ET.Element, *, is_derivative: bool, root: ET.Element, footnotes: dict
) -> Transaction:
    coding = node.find("transactionCoding")
    code = _child_value(coding, "transactionCode").strip().upper()
    if not code:
        raise AdapterSchemaError("form4: transaction without a transactionCode")
    if code not in TRANSACTION_CODES:
        raise AdapterSchemaError(
            f"form4: unknown transactionCode {code!r}. The code table is the "
            "basis of the pooling rule, so an unrecognised code is a schema "
            "change to look at, not an 'other' to absorb."
        )

    amounts = node.find("transactionAmounts")
    when = _iso_date(
        _child_value(node, "transactionDate"), where="form4.transactionDate"
    )
    if when is None:
        raise AdapterSchemaError("form4: transaction without a transactionDate")

    ids = _footnote_ids(node)
    flag, basis = _flag_10b5_1_from_elements(node, coding, root)
    if flag is None:
        flag, basis = _flag_10b5_1_from_footnotes(ids, footnotes)

    post = node.find("postTransactionAmounts")
    ownership = node.find("ownershipNature")
    return Transaction(
        code=code,
        security_title=_child_value(node, "securityTitle"),
        transaction_date=when,
        shares=_decimal(
            _child_value(amounts, "transactionShares"), where="form4.transactionShares"
        ),
        price_per_share=_decimal(
            _child_value(amounts, "transactionPricePerShare"),
            where="form4.transactionPricePerShare",
        ),
        acquired_disposed=_child_value(amounts, "transactionAcquiredDisposedCode")
        .strip()
        .upper()
        or "A",
        shares_owned_after=_decimal(
            _child_value(post, "sharesOwnedFollowingTransaction"),
            where="form4.sharesOwnedFollowingTransaction",
        ),
        direct_or_indirect=_child_value(ownership, "directOrIndirectOwnership")
        .strip()
        .upper(),
        is_derivative=is_derivative,
        plan_10b5_1=flag,
        plan_10b5_1_basis=basis,
        footnote_ids=ids,
    )


def parse_form4(xml_text: str | bytes, *, form_type: str = "") -> Form4Document:
    """Parse an EDGAR ownership document into typed transactions.

    ``form_type`` is EDGAR's own form label from the submissions index (``4``
    or ``4/A``) and wins over the document's ``documentType`` — the index is
    what the regulator classified the submission as.
    """
    raw = xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise AdapterSchemaError(
            f"form4: document is {len(raw)} bytes, over the "
            f"{MAX_DOCUMENT_BYTES}-byte ceiling."
        )
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise AdapterSchemaError(f"form4: not well-formed XML: {exc}") from exc

    if root.tag.split("}")[-1] != "ownershipDocument":
        raise AdapterSchemaError(
            f"form4: root element is {root.tag!r}, expected 'ownershipDocument'"
        )

    issuer = root.find("issuer")
    if issuer is None:
        raise AdapterSchemaError("form4: no issuer block")
    issuer_cik_raw = _child_value(issuer, "issuerCik")
    if not issuer_cik_raw:
        raise AdapterSchemaError("form4: issuer without issuerCik")

    footnotes = _footnotes(root)
    transactions: list[Transaction] = []
    for table, derivative in (("nonDerivativeTable", False), ("derivativeTable", True)):
        block = root.find(table)
        if block is None:
            continue
        tag = "derivativeTransaction" if derivative else "nonDerivativeTransaction"
        for node in block.findall(tag):
            transactions.append(
                _transaction(
                    node, is_derivative=derivative, root=root, footnotes=footnotes
                )
            )

    return Form4Document(
        issuer_cik=sec_client.normalise_cik(issuer_cik_raw),
        issuer_name=_child_value(issuer, "issuerName"),
        issuer_symbol=_child_value(issuer, "issuerTradingSymbol").strip().upper(),
        period_of_report=_iso_date(
            _child_value(root, "periodOfReport"), where="form4.periodOfReport"
        ),
        document_type=(form_type or _child_value(root, "documentType") or "4").strip(),
        owners=_reporting_owners(root),
        transactions=tuple(transactions),
        footnotes=footnotes,
    )


# --- observations -----------------------------------------------------------


def form4_observations(
    document: Form4Document,
    *,
    accession: str,
    acceptance: datetime,
    source_url: str,
    ticker_at_time: str | None = None,
    extra_warnings: Sequence[str] = (),
) -> list[Observation]:
    """One observation per (owner, transaction), typed by transaction code.

    ``valid_at`` is the transaction date at midnight UTC — when the trade
    happened. ``known_at_utc`` is the filing's ``acceptanceDateTime``, which is
    up to two business days later and is the only time at which anyone outside
    the company could have known. Using the transaction date as availability
    would hand a backtest two days of the insider's own information.
    """
    out: list[Observation] = []
    for owner in document.owners:
        for transaction in document.transactions:
            warnings = list(extra_warnings)
            if transaction.plan_10b5_1 is None:
                warnings.append(WARN_10B5_1_UNKNOWN)
            elif (transaction.plan_10b5_1_basis or "").startswith("footnote:"):
                warnings.append(WARN_10B5_1_FROM_FOOTNOTE)
            if transaction.price_per_share is None:
                warnings.append(WARN_NO_PRICE)

            out.append(
                Observation(
                    source=SOURCE_FORM4,
                    entity_cik=document.issuer_cik,
                    ticker_at_time=ticker_at_time,
                    fact_type=transaction.fact_type,
                    valid_at=datetime(
                        transaction.transaction_date.year,
                        transaction.transaction_date.month,
                        transaction.transaction_date.day,
                        tzinfo=timezone.utc,
                    ),
                    known_at_utc=acceptance,
                    known_at_source="acceptanceDateTime",
                    precision=PRECISION_SECOND,
                    provenance_class=PROVENANCE_VENDOR_PIT,
                    replay_eligible=True,
                    value_numeric=transaction.signed_shares,
                    value_text=f"{transaction.code}:{owner.cik}",
                    unit="shares",
                    accession=accession,
                    source_url=source_url,
                    source_trust=TRUST_PRIMARY_REGULATOR,
                    payload={
                        "transaction_code": transaction.code,
                        "transaction_category": transaction.category,
                        "transaction_description": transaction.description,
                        "is_open_market": transaction.is_open_market,
                        "is_derivative": transaction.is_derivative,
                        "security_title": transaction.security_title,
                        "acquired_disposed": transaction.acquired_disposed,
                        "shares": transaction.shares,
                        "price_per_share": transaction.price_per_share,
                        "notional": transaction.notional,
                        "shares_owned_after": transaction.shares_owned_after,
                        "direct_or_indirect": transaction.direct_or_indirect,
                        "plan_10b5_1": transaction.plan_10b5_1,
                        "plan_10b5_1_basis": transaction.plan_10b5_1_basis,
                        "owner_cik": owner.cik,
                        "owner_name": owner.name,
                        "owner_role": owner.role,
                        "owner_is_operating_insider": owner.is_operating_insider,
                        "issuer_symbol_at_filing": document.issuer_symbol,
                        "form_type": document.document_type,
                        "is_amendment": document.is_amendment,
                        "period_of_report": (
                            document.period_of_report.isoformat()
                            if document.period_of_report
                            else None
                        ),
                    },
                    quality_warnings=tuple(warnings),
                )
            )
    return out


# --- aggregation, with the pooling rule enforced ---------------------------


@dataclass(frozen=True)
class OpenMarketTotals:
    purchased_shares: float = 0.0
    sold_shares: float = 0.0
    purchase_notional: float = 0.0
    sale_notional: float = 0.0
    distinct_insiders: int = 0
    planned_sales: int = 0
    planned_sale_shares: float = 0.0
    unknown_plan_flag: int = 0

    @property
    def net_shares(self) -> float:
        return self.purchased_shares - self.sold_shares


def net_open_market_shares(rows: Iterable[Any]) -> OpenMarketTotals:
    """Aggregate ``P``/``S`` only, and raise on anything else.

    ``test_form4_codes_never_pooled``. Handing this function an award row is a
    caller bug, and the honest response is to stop rather than to filter it out
    silently — a filter would make "insiders bought 40,000 shares" true of a
    set the caller believed contained awards, and nobody would ever find out.

    A 10b5-1 sale is counted in ``sold_shares`` *and* reported separately: it
    was scheduled in advance, so reading it as a fresh negative judgement is
    the second-most-common insider-data error after pooling.
    """
    purchased = sold = purchase_notional = sale_notional = 0.0
    planned = unknown = 0
    planned_shares = 0.0
    insiders: set[str] = set()

    for row in rows:
        payload = _row_payload(row)
        fact_type = getattr(row, "fact_type", None)
        code = str(payload.get("transaction_code") or "").upper()

        if fact_type not in OPEN_MARKET_FACT_TYPES or code not in OPEN_MARKET_CODES:
            raise PoolingRefused(
                f"net_open_market_shares was handed fact_type={fact_type!r} "
                f"code={code!r}. Spec O section 3.1: {sorted(OPEN_MARKET_CODES)} are "
                f"never pooled with {sorted(NEVER_POOLED_WITH_OPEN_MARKET)} — an "
                "award, an option exercise, a tax-withholding sale and a gift are "
                "compensation mechanics, not decisions to buy or sell at the market "
                "price. Filter to the open-market fact types before aggregating."
            )

        shares = abs(float(payload.get("shares") or 0.0))
        notional = float(payload.get("notional") or 0.0)
        owner = str(payload.get("owner_cik") or "")
        if owner:
            insiders.add(owner)
        if payload.get("plan_10b5_1") is None:
            unknown += 1

        if code == CODE_PURCHASE:
            purchased += shares
            purchase_notional += notional
        else:
            sold += shares
            sale_notional += notional
            if payload.get("plan_10b5_1") is True:
                planned += 1
                planned_shares += shares

    return OpenMarketTotals(
        purchased_shares=purchased,
        sold_shares=sold,
        purchase_notional=purchase_notional,
        sale_notional=sale_notional,
        distinct_insiders=len(insiders),
        planned_sales=planned,
        planned_sale_shares=planned_shares,
        unknown_plan_flag=unknown,
    )


@dataclass(frozen=True)
class InsiderCluster:
    """Several *distinct* insiders buying on the open market inside a window.

    Spec O section 3.3: the insider subset with the most support in the
    literature, and even so it is one covariate feeding a Spec N cohort, not a
    recommendation. Counting one insider's three tranches as a cluster is the
    obvious way to manufacture one, so the count is of distinct owner CIKs.
    """

    entity_cik: str
    window_start: date
    window_end: date
    distinct_insiders: int
    operating_insiders: int
    total_shares: float
    total_notional: float
    known_at_utc: datetime


def insider_purchase_clusters(
    rows: Sequence[Any], *, window_days: int = 30, min_insiders: int = 2
) -> list[InsiderCluster]:
    """Windows in which ``min_insiders`` distinct insiders made ``P`` trades.

    The window is anchored on ``known_at_utc``, not the transaction date: a
    cluster is only a cluster once it is *visible*, and three insiders who
    bought on the same day but whose filings landed a week apart were not
    knowable together until the last one landed.
    """
    purchases = [
        row
        for row in rows
        if getattr(row, "fact_type", None) == FACT_TYPE_BY_KEY["open_market_purchase"]
    ]
    purchases.sort(key=lambda r: (r.known_at_utc, r.id or 0))

    clusters: list[InsiderCluster] = []
    for index, anchor in enumerate(purchases):
        window: list[Any] = []
        for candidate in purchases[index:]:
            delta = candidate.known_at_utc - anchor.known_at_utc
            if delta.days > window_days:
                break
            window.append(candidate)

        owners = {str(_row_payload(r).get("owner_cik") or "") for r in window}
        owners.discard("")
        if len(owners) < min_insiders:
            continue
        operating = {
            str(_row_payload(r).get("owner_cik") or "")
            for r in window
            if _row_payload(r).get("owner_is_operating_insider")
        }
        operating.discard("")
        clusters.append(
            InsiderCluster(
                entity_cik=anchor.entity_cik,
                window_start=anchor.known_at_utc.date(),
                window_end=window[-1].known_at_utc.date(),
                distinct_insiders=len(owners),
                operating_insiders=len(operating),
                total_shares=sum(
                    abs(float(_row_payload(r).get("shares") or 0.0)) for r in window
                ),
                total_notional=sum(
                    float(_row_payload(r).get("notional") or 0.0) for r in window
                ),
                known_at_utc=window[-1].known_at_utc,
            )
        )
    return clusters


def _row_payload(row: Any) -> dict:
    payload = getattr(row, "payload", None)
    if isinstance(payload, dict):
        return payload
    return {}
