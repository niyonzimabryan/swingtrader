"""Dividends, reconstructed — because the broker does not expose them.

Verified 2026-09-08 against the Robinhood MCP's own ``tools/list`` schema: **no
tool exposes dividends received, cash movements, deposits, fees, or corporate
actions on the account.** ``get_equity_fundamentals`` carries security-level
dividend metadata — the declared rate for the instrument — not a receipt for
this account.

So a dividend line in this ledger is not an observation. It is a market-data
dividend feed multiplied by a quantity this ledger believes was held on the
record date, and **every row it produces carries ``reconstructed=true``**. That
flag is the point of the module. A reconstructed cash flow is wrong in ways a
receipt is not:

- it uses the holding as of the record date **that this ledger recorded**, and
  the ledger syncs hourly, so an intraday trade on the record date is invisible;
- it does not know about withholding, ADR fees, or a dividend reinvestment;
- it cannot see a special dividend the feed has not published yet.

None of that makes the reconstruction useless — a total-return figure without
dividends is wrong by more — but it does make labelling it non-negotiable. When
Robinhood exposes receipts, these rows are replaced by observations and the
flag goes away.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

#: Set on every row this module produces. There is no code path that clears it.
RECONSTRUCTED = True


@dataclass(frozen=True)
class DividendDeclaration:
    """One dividend as a market-data feed publishes it, per share."""

    symbol: str
    ex_date: date
    amount_per_share: float
    pay_date: date | None = None
    record_date: date | None = None
    currency: str = "USD"
    source: str = ""
    special: bool = False


@dataclass(frozen=True)
class DividendCashFlow:
    """A dividend this ledger believes was received. Never an observation."""

    symbol: str
    ex_date: date
    pay_date: date | None
    quantity: float
    amount_per_share: float
    gross_amount: float
    account: str
    currency: str
    source: str
    reconstructed: bool = RECONSTRUCTED
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "ex_date": self.ex_date.isoformat(),
            "pay_date": self.pay_date.isoformat() if self.pay_date else None,
            "quantity": self.quantity,
            "amount_per_share": self.amount_per_share,
            "gross_amount": round(self.gross_amount, 4),
            "account": self.account,
            "currency": self.currency,
            "source": self.source,
            "reconstructed": self.reconstructed,
            "warnings": list(self.warnings),
        }


_BASE_WARNINGS = (
    "reconstructed: derived from a market-data dividend feed applied to the "
    "holdings this ledger recorded, not from a broker receipt — Robinhood "
    "exposes no dividend, cash-movement, or corporate-action tool.",
    "gross of withholding, ADR fees, and any reinvestment.",
)


def reconstruct_dividends(
    declarations,
    *,
    quantity_on,
    accounts=("",),
) -> tuple[DividendCashFlow, ...]:
    """Apply a dividend feed to held quantities.

    ``quantity_on(symbol, account, on_date)`` returns the quantity this ledger
    recorded for that account on that date, or ``None`` when the ledger has no
    row covering the date. ``None`` produces **no cash flow at all** rather than
    a zero: a dividend we cannot size is unknown, and a zero in a total-return
    series is a claim that none was paid.
    """
    flows: list[DividendCashFlow] = []
    for declaration in declarations or ():
        on_date = declaration.record_date or declaration.ex_date
        for account in accounts:
            quantity = quantity_on(declaration.symbol, account, on_date)
            if quantity is None or quantity == 0:
                continue
            warnings = list(_BASE_WARNINGS)
            if declaration.record_date is None:
                warnings.append(
                    "no record date in the feed: the ex-date holding was used, "
                    "which differs when the position changed between them."
                )
            if declaration.special:
                warnings.append("special dividend: feeds publish these late and revise them.")
            flows.append(
                DividendCashFlow(
                    symbol=declaration.symbol.upper(),
                    ex_date=declaration.ex_date,
                    pay_date=declaration.pay_date,
                    quantity=float(quantity),
                    amount_per_share=float(declaration.amount_per_share),
                    gross_amount=float(quantity) * float(declaration.amount_per_share),
                    account=account,
                    currency=declaration.currency,
                    source=declaration.source,
                    warnings=tuple(warnings),
                )
            )
    return tuple(sorted(flows, key=lambda f: (f.ex_date, f.symbol, f.account)))


def total_reconstructed(flows) -> dict:
    """A summary that keeps the flag attached to the number it qualifies."""
    total = sum(flow.gross_amount for flow in flows)
    return {
        "gross_amount": round(total, 4),
        "count": len(tuple(flows)),
        "reconstructed": RECONSTRUCTED,
        "note": _BASE_WARNINGS[0],
    }
