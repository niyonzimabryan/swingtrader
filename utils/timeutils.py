"""Time helpers.

The SQLite schema stores DateTime columns as *naive* UTC (SQLAlchemy strips
tzinfo on write and returns naive datetimes on read). Mixing tz-aware and naive
datetimes raises ``TypeError`` on comparison/subtraction, and much of the code
subtracts DB-loaded datetimes (e.g. ``utcnow() - trade.entry_date``). To retire
the deprecated ``datetime.utcnow()`` without introducing naive-vs-aware bugs, we
standardize on a single naive-UTC source that matches the DB representation.

Use ``utcnow_naive()`` anywhere a value is stored in / compared against a DB
DateTime column or a wall-clock UTC string. For genuinely tz-aware needs (e.g.
the monitor watchdog's ``_last_tick``) keep using ``datetime.now(timezone.utc)``.
"""

from datetime import datetime, timezone


def utcnow_naive() -> datetime:
    """Current UTC time as a naive datetime (tzinfo stripped).

    Equivalent to the deprecated ``datetime.utcnow()`` but built on the
    non-deprecated ``datetime.now(timezone.utc)``.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
