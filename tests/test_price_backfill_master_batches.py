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

from scripts.price_backfill import (
    MASTER_TICKER_PARAM_MAX_CHARS,
    MASTER_TICKER_PARAM_MAX_COUNT,
    _master_batches,
)


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

    def test_a_batch_near_the_length_limit_is_not_split(self):
        """A group inside BOTH limits is sent as one request.

        Originally written with 38 four-character symbols (189 chars), back
        when the character cap was believed to be the only limit. The vendor
        then refused that batch on count. Rewritten to 27 six-character symbols
        — 188 characters and 27 tickers, inside both ceilings — because the
        original assertion was factually wrong about the API, not because it
        was inconvenient.
        """
        tickers = [f"SYM{n:03d}" for n in range(27)]
        self.assertEqual(len(",".join(tickers)), 188)
        self.assertLessEqual(len(tickers), MASTER_TICKER_PARAM_MAX_COUNT)

        self.assertEqual(list(_master_batches(tickers)), [tickers])



class MasterBatchCountTests(unittest.TestCase):
    """The second limit, which the vendor only reveals once length is satisfied.

        Too many tickers: ticker accepts at most 30 tickers per request (got 34).

    Observed live 2026-09-14 on the run immediately after the character fix.
    Batching on length alone averaged 47 tickers per request and was refused.
    """

    def test_no_batch_exceeds_the_count_limit(self):
        tickers = [f"T{n:03d}" for n in range(500)]

        for batch in _master_batches(tickers):
            self.assertLessEqual(len(batch), MASTER_TICKER_PARAM_MAX_COUNT)

    def test_both_limits_hold_simultaneously(self):
        tickers = [f"SYM{n:05d}" for n in range(500)]

        for batch in _master_batches(tickers):
            self.assertLessEqual(len(batch), MASTER_TICKER_PARAM_MAX_COUNT)
            self.assertLessEqual(len(",".join(batch)), MASTER_TICKER_PARAM_MAX_CHARS)

    def test_count_binds_for_short_symbols(self):
        """Thirty 5-char symbols join to 179 chars — under the length limit."""
        tickers = ["ABCDE"] * 120

        batches = list(_master_batches(tickers))

        self.assertEqual(len(batches[0]), MASTER_TICKER_PARAM_MAX_COUNT)
        self.assertLess(len(",".join(batches[0])), MASTER_TICKER_PARAM_MAX_CHARS)

    def test_length_binds_for_long_symbols(self):
        """Thirty 8-char symbols would join to 269 chars — over the limit."""
        tickers = ["ABCDEFGH"] * 120

        batches = list(_master_batches(tickers))

        self.assertLess(len(batches[0]), MASTER_TICKER_PARAM_MAX_COUNT)
        self.assertLessEqual(len(",".join(batches[0])), MASTER_TICKER_PARAM_MAX_CHARS)

    def test_length_batching_alone_would_have_been_refused(self):
        """The regression: 190 chars of 4-char symbols is ~38 tickers > 30."""
        tickers = [f"AB{n:02d}" for n in range(38)]
        self.assertLessEqual(len(",".join(tickers)), MASTER_TICKER_PARAM_MAX_CHARS)
        self.assertGreater(len(tickers), MASTER_TICKER_PARAM_MAX_COUNT)

        for batch in _master_batches(tickers):
            self.assertLessEqual(len(batch), MASTER_TICKER_PARAM_MAX_COUNT)


if __name__ == "__main__":
    unittest.main()
