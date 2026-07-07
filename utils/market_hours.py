"""
Shared US-equity market-hours helper.

Single source of truth for "is the US stock market open right now?" — used by the
position monitor, the reliability watchdog, and the scheduler so a market holiday
(e.g. 2026-07-03) can never be mistaken for a normal trading day.

Deliberately dependency-free: a small static NYSE full-closure table beats pulling
in `pandas_market_calendars` for this repo. Early closes (1 PM ET half-days) are
intentionally out of scope — they still count as open.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 30
MARKET_CLOSE_HOUR = 16
MARKET_CLOSE_MINUTE = 0

# NYSE full-closure dates for 2026–2027 (observed dates). Update annually.
US_MARKET_HOLIDAYS: frozenset[date] = frozenset({
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # Martin Luther King Jr. Day
    date(2026, 2, 16),   # Washington's Birthday
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed — Jul 4 is a Saturday)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving Day
    date(2026, 12, 25),  # Christmas Day
    # 2027
    date(2027, 1, 1),    # New Year's Day
    date(2027, 1, 18),   # Martin Luther King Jr. Day
    date(2027, 2, 15),   # Washington's Birthday
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),   # Memorial Day
    date(2027, 6, 18),   # Juneteenth (observed — Jun 19 is a Saturday)
    date(2027, 7, 5),    # Independence Day (observed — Jul 4 is a Sunday)
    date(2027, 9, 6),    # Labor Day
    date(2027, 11, 25),  # Thanksgiving Day
    date(2027, 12, 24),  # Christmas Day (observed — Dec 25 is a Saturday)
})


def is_market_holiday(day: date) -> bool:
    """True if `day` is a full NYSE closure (weekends not included here)."""
    return day in US_MARKET_HOLIDAYS


def is_trading_day(day: date) -> bool:
    """True if `day` is a weekday and not a US market holiday."""
    return day.weekday() < 5 and not is_market_holiday(day)


def is_market_open(now: datetime | None = None) -> bool:
    """
    True if the US equity market is in regular trading hours (9:30–16:00 ET)
    on a trading day. `now` may be naive (assumed ET) or tz-aware (converted);
    defaults to the current time in ET.
    """
    if now is None:
        now = datetime.now(ET)
    elif now.tzinfo is not None:
        now = now.astimezone(ET)
    else:
        now = now.replace(tzinfo=ET)

    if not is_trading_day(now.date()):
        return False

    market_open = now.replace(
        hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0, microsecond=0
    )
    market_close = now.replace(
        hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0, microsecond=0
    )
    return market_open <= now <= market_close
