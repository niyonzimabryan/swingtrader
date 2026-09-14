"""The vendor's `ticker` parameter limit is on characters, not on count.

Sharadar refuses a `ticker` query parameter longer than 200 characters:

    Invalid ticker parameter: ticker exceeds maximum length of 200 characters.
    Use fewer tickers, date filters, or bulk download (years=) for large
    universes.

Observed live against the production API on 2026-09-14, when a whole-market
`--bulk years=10` run died on its first master lookup having written zero bars.
The previous batching used a fixed count of 200 tickers, which joins to roughly
a thousand characters. A ten-name `--tickers` slice stayed under the limit by
accident, which is exactly why this went unnoticed.

These cases are about the *joined* length, because that is what the vendor
measures.
"""

import unittest

from scripts.price_backfill import MASTER_TICKER_PARAM_MAX_CHARS, _master_batches


class MasterBatchLengthTests(unittest.TestCase):
    def test_every_batch_joins_within_the_vendor_limit(self):
        tickers = [f"TICK{n}" for n in range(1000)]

        for batch in _master_batches(tickers):
            self.assertLessEqual(len(",".join(batch)), MASTER_TICKER_PARAM_MAX_CHARS)

    def test_no_ticker_is_lost_or_reordered(self):
        tickers = ["A", "BB", "CCC", "DDDD", "EEEEE"] * 200

        flattened = [t for batch in _master_batches(tickers) for t in batch]

        self.assertEqual(flattened, tickers)

    def test_a_fixed_count_of_200_would_have_exceeded_the_limit(self):
        """The regression itself: 200 four-character symbols is ~1000 chars."""
        tickers = [f"TIC{n:01d}" for n in range(200)]

        self.assertGreater(len(",".join(tickers)), 200)
        for batch in _master_batches(tickers):
            self.assertLessEqual(len(",".join(batch)), MASTER_TICKER_PARAM_MAX_CHARS)

    def test_batches_are_packed_not_one_per_request(self):
        """Correctness is not enough: one ticker per request would also pass."""
        tickers = [f"AB{n:02d}" for n in range(100)]

        batches = list(_master_batches(tickers))

        self.assertLess(len(batches), 10)

    def test_a_single_oversized_ticker_is_yielded_alone(self):
        absurd = "X" * (MASTER_TICKER_PARAM_MAX_CHARS + 50)

        batches = list(_master_batches([absurd, "AAPL"]))

        self.assertEqual(batches[0], [absurd])
        self.assertEqual(batches[1], ["AAPL"])

    def test_empty_input_yields_nothing(self):
        self.assertEqual(list(_master_batches([])), [])

    def test_a_batch_exactly_at_the_limit_is_not_split(self):
        # 38 four-char symbols + 37 commas = 189 characters.
        tickers = [f"T{n:03d}" for n in range(38)]
        self.assertEqual(len(",".join(tickers)), 189)

        self.assertEqual(list(_master_batches(tickers)), [tickers])


if __name__ == "__main__":
    unittest.main()
