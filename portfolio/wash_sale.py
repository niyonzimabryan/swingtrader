"""The wash-sale *awareness* flag — informational, never a determination.

IRS Publication 550: a loss is disallowed if substantially identical stock or
securities are acquired within **30 days before or after** the sale. With the
sale day itself that is a 61-day window, and the off-by-one is the usual bug —
day 30 is inside it, day 31 is not.

Three things this module will not do, each of them deliberate:

1. **It does not decide.** "Substantially identical" is a facts-and-
   circumstances test, not a computation. A same-FIGI match is reported at
   ``high`` confidence and a same-issuer different-class or convertible at
   ``possible``; neither is a determination and both say so in the payload.
2. **It does not compute a tax.** The disallowed amount, the basis adjustment,
   and the holding-period tack-on are all outside this system.
3. **It does not claim completeness.** The check spans every Robinhood account
   the token can read, because the rule reaches across accounts. It cannot see
   a spouse's account or another broker's, and the payload says so on every
   result rather than in a docstring nobody reads.

An IRA purchase is flagged separately: there the loss is permanently
disallowed rather than deferred, and no basis adjustment recovers it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

#: 30 days before, the sale day, 30 days after.
WASH_SALE_DAYS_EITHER_SIDE = 30
WASH_SALE_WINDOW_DAYS = 2 * WASH_SALE_DAYS_EITHER_SIDE + 1

CONFIDENCE_HIGH = "high"
CONFIDENCE_POSSIBLE = "possible"

_DISCLAIMER = (
    "Informational only. 'Substantially identical' is a facts-and-circumstances "
    "test under IRS Publication 550, not a computation: this flag never emits a "
    "determination and this system does not give tax advice."
)
_COVERAGE = (
    "Covers every account this broker token can read. It cannot see a spouse's "
    "account or another broker's, and a purchase there would still trigger the "
    "rule."
)


@dataclass(frozen=True)
class RealizedLoss:
    """A realised loss the flag looks back at.

    ``figi`` is what makes a match ``high`` confidence. Where no FIGI is
    available the symbol is used and the match is reported at ``high`` only when
    the symbols are equal — a symbol is a weaker identifier than a FIGI (it is
    reused after a delisting), which is why the field exists at all.
    """

    symbol: str
    sale_date: date
    amount: float
    account: str = ""
    figi: str | None = None
    issuer_id: str | None = None
    security_class: str | None = None


def in_window(sale_date: date, purchase_date: date) -> bool:
    """True when ``purchase_date`` falls in the 61-day window around the sale.

    Inclusive at both ends: exactly 30 days either side is inside the window.
    """
    delta = abs((purchase_date - sale_date).days)
    return delta <= WASH_SALE_DAYS_EITHER_SIDE


def window_bounds(sale_date: date) -> tuple[date, date]:
    span = timedelta(days=WASH_SALE_DAYS_EITHER_SIDE)
    return sale_date - span, sale_date + span


def _match(loss: RealizedLoss, symbol: str, figi: str | None, issuer_id: str | None,
           security_class: str | None) -> tuple[str, str] | None:
    """``(confidence, why)`` if the securities may be substantially identical."""
    symbol = symbol.upper()
    if figi and loss.figi and figi == loss.figi:
        return CONFIDENCE_HIGH, "same FIGI"
    if not figi and not loss.figi and symbol == loss.symbol.upper():
        return CONFIDENCE_HIGH, "same ticker (no FIGI available on either side)"
    if figi and loss.figi and figi != loss.figi:
        # Distinct FIGIs can still be substantially identical (a convertible,
        # or a second share class), so fall through to the issuer test rather
        # than concluding "no".
        pass
    if issuer_id and loss.issuer_id and issuer_id == loss.issuer_id:
        if security_class and loss.security_class and security_class != loss.security_class:
            return CONFIDENCE_POSSIBLE, "same issuer, different security class"
        return CONFIDENCE_POSSIBLE, "same issuer"
    if symbol == loss.symbol.upper():
        return CONFIDENCE_HIGH, "same ticker"
    return None


@dataclass(frozen=True)
class WashSaleFlag:
    wash_sale_window: bool
    matches: tuple[dict, ...] = ()
    ira_purchase: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        payload = {
            "wash_sale_window": self.wash_sale_window,
            "is_determination": False,
            "window_days": WASH_SALE_WINDOW_DAYS,
            "matches": [dict(m) for m in self.matches],
            "notes": list(self.notes),
        }
        if self.ira_purchase:
            payload["ira_purchase"] = True
        return payload


def flag_wash_sale_window(
    *,
    symbol: str,
    purchase_date: date,
    realized_losses,
    figi: str | None = None,
    issuer_id: str | None = None,
    security_class: str | None = None,
    account_type: str = "cash",
) -> WashSaleFlag:
    """Flag a proposed buy that lands inside a realised loss's 61-day window.

    Returns a flag with ``wash_sale_window=False`` and the coverage note when
    nothing matches — an unflagged result is still an answer about scope, not a
    guarantee that no wash sale exists.
    """
    matches: list[dict] = []
    for loss in realized_losses or ():
        if not in_window(loss.sale_date, purchase_date):
            continue
        matched = _match(loss, symbol, figi, issuer_id, security_class)
        if matched is None:
            continue
        confidence, why = matched
        opens, closes = window_bounds(loss.sale_date)
        matches.append(
            {
                "symbol": loss.symbol.upper(),
                "sale_date": loss.sale_date.isoformat(),
                "realized_loss": float(loss.amount),
                "account": loss.account,
                "confidence": confidence,
                "basis": why,
                "days_from_sale": (purchase_date - loss.sale_date).days,
                "window_opens": opens.isoformat(),
                "window_closes": closes.isoformat(),
            }
        )

    notes = [_DISCLAIMER, _COVERAGE]
    ira = str(account_type or "").lower() == "ira"
    if matches and ira:
        notes.append(
            "The purchase is in an IRA: a loss disallowed by a purchase in an "
            "IRA is permanently disallowed rather than deferred, and no basis "
            "adjustment recovers it."
        )
    return WashSaleFlag(
        wash_sale_window=bool(matches),
        matches=tuple(sorted(matches, key=lambda m: (m["sale_date"], m["symbol"]))),
        ira_purchase=bool(matches and ira),
        notes=tuple(notes),
    )
