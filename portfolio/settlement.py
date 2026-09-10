"""T+1 settlement on a cash account (Spec L §5.1).

The Robinhood Agentic account is a **cash** account by owner decision: no
margin of any kind. Proceeds from a Monday close are not redeployable until
Tuesday, so "how much cash is there" and "how much cash can this proposal
spend" are different questions and the ledger keeps them apart.

The settlement date is the next *trading* day, not the next calendar day — a
Friday sale settles Monday, and a sale the day before a holiday settles the day
after it. ``utils.market_hours`` already owns the NYSE closure table used by the
monitor and the scheduler, so this uses it rather than starting a second one.

What is deliberately *not* modelled: same-day-settling instruments, the
good-faith and free-riding rules that apply to a cash account, and any broker
extension of credit. All three would be a determination about what a regulator
permits, and this module only says which dollars have arrived.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from utils.market_hours import is_trading_day

#: Equities settle T+1 in the US as of the 2024 SEC rule change.
SETTLEMENT_DAYS = 1

#: Search bound for the next trading day. Long enough for any closure run in
#: the calendar, short enough that a bad calendar raises instead of looping.
_MAX_CALENDAR_SEARCH_DAYS = 14


def next_trading_day(day: date) -> date:
    """The first trading day strictly after ``day``."""
    candidate = day + timedelta(days=1)
    for _ in range(_MAX_CALENDAR_SEARCH_DAYS):
        if is_trading_day(candidate):
            return candidate
        candidate += timedelta(days=1)
    raise ValueError(
        f"no trading day found within {_MAX_CALENDAR_SEARCH_DAYS} days of {day}; "
        "the market-holiday table in utils/market_hours.py is probably stale."
    )


def settlement_date(trade_date: date, settlement_days: int = SETTLEMENT_DAYS) -> date:
    """When proceeds from a trade on ``trade_date`` become settled cash."""
    settled = trade_date
    for _ in range(max(0, int(settlement_days))):
        settled = next_trading_day(settled)
    return settled


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


@dataclass(frozen=True)
class SettlementView:
    """Cash split by what has arrived, as of one day."""

    settled: float
    unsettled: float
    #: ``settles_on`` -> amount, for the tranches not yet available.
    pending_by_date: dict
    as_of_date: date

    @property
    def available(self) -> float:
        """The only figure a cash-account proposal may spend."""
        return self.settled

    def earliest_settlement_for(self, amount: float) -> date | None:
        """The first date on which ``amount`` would be available, or ``None``.

        ``None`` means it never becomes available from the tranches known here
        — the answer is "not from this account's pending proceeds", which is a
        different refusal from "wait until Tuesday".
        """
        running = self.settled
        if running >= amount:
            return self.as_of_date
        for day in sorted(self.pending_by_date):
            running += self.pending_by_date[day]
            if running >= amount:
                return day
        return None


def settlement_view(
    *,
    settled_cash: float | None,
    unsettled_cash: float | None = None,
    pending_settlements=(),
    as_of_date: date,
) -> SettlementView:
    """Fold a ``cash_balances`` row into the settled/pending split.

    A null ``settled_cash`` is *unknown*, and unknown is treated as zero
    spendable here on purpose: this is the one place where the safe reading of
    "we do not know" is "do not spend it".
    """
    pending: dict[date, float] = {}
    for entry in pending_settlements or ():
        if isinstance(entry, dict):
            when = _as_date(entry.get("settles_on"))
            amount = entry.get("amount")
        else:
            when = _as_date(getattr(entry, "settles_on", None))
            amount = getattr(entry, "amount", None)
        if when is None or amount is None:
            continue
        if when <= as_of_date:
            # Already settled by this date; it is in `settled_cash` and adding
            # it again would double-count.
            continue
        pending[when] = pending.get(when, 0.0) + float(amount)

    unsettled_total = (
        float(unsettled_cash) if unsettled_cash is not None else sum(pending.values())
    )
    return SettlementView(
        settled=float(settled_cash or 0.0),
        unsettled=unsettled_total,
        pending_by_date=pending,
        as_of_date=as_of_date,
    )
