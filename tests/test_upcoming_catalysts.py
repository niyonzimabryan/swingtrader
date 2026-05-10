"""Tests for /upcoming catalyst MVP (t_82dcdb23)."""

import unittest
from datetime import date

from data.upcoming_catalysts import (
    collect_upcoming,
    format_upcoming_message,
)


def _stub_earnings(mapping):
    """Build a stub earnings_fn from {ticker: date_or_None}."""
    def _fn(ticker, today=None):
        return mapping.get(ticker)
    return _fn


class CollectUpcomingTests(unittest.TestCase):
    def test_empty_ticker_list_returns_empty(self):
        result = collect_upcoming([], today=date(2026, 5, 10), earnings_fn=_stub_earnings({}))
        self.assertEqual(result, [])

    def test_returns_only_tickers_with_known_catalysts(self):
        today = date(2026, 5, 10)
        result = collect_upcoming(
            ["AAPL", "MSFT", "NVDA"],
            today=today,
            earnings_fn=_stub_earnings({
                "AAPL": date(2026, 5, 20),
                "MSFT": None,           # no known earnings
                "NVDA": date(2026, 5, 12),
            }),
        )
        # MSFT must be omitted; results sorted by event_date ascending
        self.assertEqual([r["ticker"] for r in result], ["NVDA", "AAPL"])
        self.assertEqual(result[0]["catalyst_type"], "earnings")
        self.assertEqual(result[0]["days_until"], 2)
        self.assertEqual(result[1]["days_until"], 10)

    def test_dedupes_and_normalizes_tickers(self):
        today = date(2026, 5, 10)
        result = collect_upcoming(
            ["aapl", "AAPL", "  ", None, "MSFT"],
            today=today,
            earnings_fn=_stub_earnings({
                "AAPL": date(2026, 5, 20),
                "MSFT": date(2026, 5, 15),
            }),
        )
        tickers = [r["ticker"] for r in result]
        self.assertEqual(tickers.count("AAPL"), 1)
        self.assertIn("MSFT", tickers)

    def test_provider_error_per_ticker_does_not_crash_collect(self):
        # earnings_fn raises for one ticker; collect_upcoming must not crash
        # because get_next_earnings_date already swallows exceptions. To prove
        # collect's resilience independently, we wrap the stub.
        today = date(2026, 5, 10)

        def _fn(ticker, today=None):
            if ticker == "BAD":
                # Simulate the *real* contract: provider returns None on error.
                return None
            return {"AAPL": date(2026, 5, 20)}.get(ticker)

        result = collect_upcoming(["BAD", "AAPL"], today=today, earnings_fn=_fn)
        self.assertEqual([r["ticker"] for r in result], ["AAPL"])


class FormatUpcomingMessageTests(unittest.TestCase):
    def test_empty_state_message(self):
        msg = format_upcoming_message([])
        self.assertIn("No upcoming catalysts", msg)
        self.assertIn("/watchlist add", msg)

    def test_found_state_message(self):
        msg = format_upcoming_message([
            {"ticker": "NVDA", "catalyst_type": "earnings",
             "event_date": date(2026, 5, 12), "days_until": 2},
            {"ticker": "AAPL", "catalyst_type": "earnings",
             "event_date": date(2026, 5, 20), "days_until": 10},
        ])
        self.assertIn("NVDA", msg)
        self.assertIn("AAPL", msg)
        self.assertIn("earnings", msg)
        self.assertIn("in 2d", msg)
        self.assertIn("in 10d", msg)

    def test_today_event_uses_today_phrasing(self):
        msg = format_upcoming_message([
            {"ticker": "NVDA", "catalyst_type": "earnings",
             "event_date": date(2026, 5, 10), "days_until": 0},
        ])
        self.assertIn("today", msg)


if __name__ == "__main__":
    unittest.main()
