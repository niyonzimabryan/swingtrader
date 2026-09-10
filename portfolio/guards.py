"""Refusals the ledger owes a proposal, written before the proposal exists.

``propose_order`` is Phase 6. These guards are Phase 1 because each of them is
a property of the *ledger*, and a guard written after the thing it guards has
shipped is a guard written under pressure to let the existing behaviour through.

Each returns a :class:`RiskRejection` — a reason the agent can read and explain
— rather than silently omitting the idea. Spec L §6.4: a proposal that breaches
a limit is created ``risk_rejected`` *and shown with the reason*, so the agent
can say why rather than quietly dropping a name from a list.

Nothing here places, reviews, or cancels an order, and nothing here imports a
broker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from portfolio.freshness import (
    DEFAULT_FRESHNESS_BUDGET_MINUTES,
    StaleLedger,
    age_minutes,
    require_fresh,
)
from portfolio.settlement import SettlementView, settlement_view

#: ``RiskRejection.code`` values. Stable strings: they are shown on the
#: approval card and are what a later phase filters and counts on.
STALE_LEDGER = "stale_ledger"
ACCOUNT_NOT_PLACEABLE = "account_not_agent_placeable"
UNSETTLED_CASH = "unsettled_cash"
UNKNOWN_COST_BASIS = "unknown_cost_basis"


class UnknownCostBasis(ValueError):
    """A consumer asked for a cost basis the broker never supplied.

    Raised rather than returning zero. ``test_unknown_basis_is_null_not_zero``
    exists because the zero is the plausible-looking answer: it renders, it
    sums, and it makes a 100% gain out of a position bought yesterday.
    """


@dataclass(frozen=True)
class RiskRejection:
    """Why a proposal cannot proceed, in a form that can be shown."""

    code: str
    reason: str
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"status": "risk_rejected", "code": self.code, "reason": self.reason, **({"detail": self.detail} if self.detail else {})}


def check_ledger_fresh(
    as_of_utc: datetime | None,
    now: datetime,
    *,
    budget_minutes: int = DEFAULT_FRESHNESS_BUDGET_MINUTES,
) -> RiskRejection | None:
    """``None`` when the ledger is fresh enough to size a position against.

    The non-raising face of :func:`portfolio.freshness.require_fresh`, for
    callers that collect every reason a proposal was refused rather than
    stopping at the first.
    """
    try:
        require_fresh(as_of_utc, now, budget_minutes=budget_minutes)
    except StaleLedger as exc:
        return RiskRejection(
            STALE_LEDGER,
            str(exc),
            {
                "as_of_utc": as_of_utc.isoformat() if as_of_utc else None,
                "age_minutes": (
                    round(age_minutes(as_of_utc, now), 2)
                    if age_minutes(as_of_utc, now) is not None
                    else None
                ),
                "freshness_budget_minutes": budget_minutes,
            },
        )
    return None


def check_account_placeable(account) -> RiskRejection | None:
    """Refuse a proposal aimed at a read-only account (Spec L §5.1).

    Reads span every account the token can see; placement is confined to the
    Agentic account. Robinhood enforces this too — the API rejects an account
    that is not ``agentic_allowed`` — but a proposal that fails at placement has
    already been shown to a human as though it were actionable.
    """
    if getattr(account, "agent_placeable", False):
        return None
    label = getattr(account, "label", "") or getattr(account, "external_account_id", "?")
    return RiskRejection(
        ACCOUNT_NOT_PLACEABLE,
        f"account {label!r} has agent_placeable=False: it is read-only to this "
        "workspace and no order may be proposed against it (Spec L §5.1).",
        {"account": label, "broker": getattr(account, "broker", "")},
    )


def check_settled_cash(
    required_cash: float,
    *,
    settled_cash: float | None,
    unsettled_cash: float | None = None,
    pending_settlements=(),
    on_date: date,
) -> RiskRejection | None:
    """Refuse a cash-account proposal that would need T+1 proceeds.

    The refusal carries the settlement date, because "not until Tuesday" is a
    thing the owner can act on and "insufficient funds" is not.
    """
    view: SettlementView = settlement_view(
        settled_cash=settled_cash,
        unsettled_cash=unsettled_cash,
        pending_settlements=pending_settlements,
        as_of_date=on_date,
    )
    if view.available >= required_cash:
        return None

    available_on = view.earliest_settlement_for(required_cash)
    if available_on is None:
        reason = (
            f"needs ${required_cash:,.2f}; ${view.available:,.2f} is settled and "
            f"${view.unsettled:,.2f} unsettled, which is not enough even once "
            "every pending tranche has settled."
        )
    else:
        reason = (
            f"needs ${required_cash:,.2f}; only ${view.available:,.2f} is settled "
            f"today. This is a cash account — proceeds settle T+1 — and the "
            f"amount is available on {available_on.isoformat()}."
        )
    return RiskRejection(
        UNSETTLED_CASH,
        reason,
        {
            "required_cash": float(required_cash),
            "settled_cash": view.available,
            "unsettled_cash": view.unsettled,
            "settlement_date": available_on.isoformat() if available_on else None,
            "pending_by_date": {
                day.isoformat(): amount for day, amount in sorted(view.pending_by_date.items())
            },
        },
    )


def require_cost_basis(value: float | None, *, symbol: str, what: str = "cost basis") -> float:
    """Return ``value``, or raise :class:`UnknownCostBasis` when it is ``None``.

    The single sanctioned way to consume a basis. ``0.0`` is a real basis (a
    gifted or fully written-down lot) and is returned as one; only ``None`` is
    unknown.
    """
    if value is None:
        raise UnknownCostBasis(
            f"{what} for {symbol} is unknown: the broker did not supply it. "
            "Null basis is unknown, never zero — show it as unknown rather "
            "than computing a return from it (Spec L §3)."
        )
    return float(value)


def basis_is_known(value: float | None) -> bool:
    return value is not None
