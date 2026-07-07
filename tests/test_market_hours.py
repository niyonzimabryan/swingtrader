"""Spec B2 — holiday-aware market hours (shared helper)."""

import unittest
from datetime import date, datetime

from utils.market_hours import ET, is_market_holiday, is_market_open, is_trading_day


class MarketHoursTest(unittest.TestCase):
    def test_holiday_july_3_2026_closed(self):
        # July 4 2026 falls on a Saturday → observed holiday is Friday Jul 3.
        self.assertTrue(is_market_holiday(date(2026, 7, 3)))
        self.assertFalse(is_market_open(datetime(2026, 7, 3, 10, 0, tzinfo=ET)))

    def test_holiday_christmas_2026_closed(self):
        self.assertTrue(is_market_holiday(date(2026, 12, 25)))
        self.assertFalse(is_market_open(datetime(2026, 12, 25, 10, 0, tzinfo=ET)))

    def test_normal_tuesday_10am_open(self):
        # 2026-07-07 is a normal trading Tuesday.
        self.assertTrue(is_trading_day(date(2026, 7, 7)))
        self.assertTrue(is_market_open(datetime(2026, 7, 7, 10, 0, tzinfo=ET)))

    def test_weekend_closed(self):
        # 2026-07-11 is a Saturday.
        self.assertFalse(is_market_open(datetime(2026, 7, 11, 10, 0, tzinfo=ET)))

    def test_before_open_and_after_close(self):
        self.assertFalse(is_market_open(datetime(2026, 7, 7, 9, 0, tzinfo=ET)))   # pre-open
        self.assertFalse(is_market_open(datetime(2026, 7, 7, 16, 30, tzinfo=ET)))  # after close
        self.assertTrue(is_market_open(datetime(2026, 7, 7, 9, 30, tzinfo=ET)))    # exactly open

    def test_naive_datetime_treated_as_et(self):
        self.assertTrue(is_market_open(datetime(2026, 7, 7, 10, 0)))
        self.assertFalse(is_market_open(datetime(2026, 7, 3, 10, 0)))

    def test_position_monitor_is_market_hours_delegates(self):
        # The monitor's _is_market_hours must use the holiday-aware shared helper.
        from unittest.mock import patch
        from execution.position_monitor import PositionMonitor
        monitor = PositionMonitor(alpaca=object(), notification_manager=None, settings=object())
        with patch("execution.position_monitor.is_market_open", return_value=True) as m:
            self.assertTrue(monitor._is_market_hours())
        m.assert_called_once()


if __name__ == "__main__":
    unittest.main()
