"""`get_quotes` and `get_tradability` against the shapes the live server sends.

The quote shapes below are the ones captured from production on 2026-09-14 and
declared by the committed schema dump in ``docs/robinhood/tool_schemas.json``:
the row carries ``quote`` and ``close`` and **no** top-level ``symbol`` or
``last_trade_price``. Prices arrive as decimal *strings*.

These tests assert the contract in the ``get_quotes`` docstring, not merely
that the result is non-empty. "Non-empty" is what a fix that returned the
server's row unchanged would also satisfy, and that fix is worse than the bug
it replaces: a caller would read ``last_trade_price`` off the row, get
``None``, and carry it forward as a price.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from execution.brokers import robinhood as robinhood_module
from execution.brokers.robinhood import RobinhoodMCPBroker


def _broker(response, *, account_number="RH123456"):
    """A broker whose only network method returns ``response``.

    Substituting ``_call_tool_sync`` is the established pattern in
    ``tests/test_robinhood_hardening.py``: every normalizer under test runs
    exactly as it does live.
    """
    broker = RobinhoodMCPBroker(
        SimpleNamespace(
            robinhood_account_number=account_number,
            robinhood_mcp_url="https://example.invalid/mcp",
            token_encryption_key="",
        )
    )
    broker.calls = []

    def _call(name, args):
        broker.calls.append((name, dict(args)))
        return response

    broker._call_tool_sync = _call
    return broker


def _live_quote_row(symbol, last_trade_price, **overrides):
    """One ``get_equity_quotes`` result row in the live nested shape."""
    row = {
        "quote": {
            "symbol": symbol,
            "last_trade_price": last_trade_price,
            "last_non_reg_trade_price": None,
            "venue_last_trade_time": "2026-09-14T20:00:00Z",
            "bid_price": "11.4500",
            "ask_price": "11.4700",
            "previous_close": "11.3900",
            "previous_close_date": "2026-09-12",
            "adjusted_previous_close": "11.3900",
            "has_traded": True,
            "state": "active",
        },
        "close": {
            "symbol": symbol,
            "date": "2026-09-12",
            "price": "11.3900",
            "interpolated": False,
            "source": "sip-close",
        },
    }
    row["quote"].update(overrides)
    return row


def _envelope(rows):
    """The live envelope: rows live at ``data.results``, not at the top level."""
    return {"data": {"results": rows}}


class QuoteContractTests(unittest.TestCase):
    def test_live_nested_row_resolves_symbol_and_price(self):
        broker = _broker(_envelope([_live_quote_row("F", "11.4600")]))

        quotes = broker.get_quotes(["f"])

        self.assertEqual(list(quotes), ["F"])
        self.assertEqual(quotes["F"]["symbol"], "F")
        self.assertEqual(quotes["F"]["last_price"], 11.46)

    def test_last_price_is_a_float_not_the_servers_decimal_string(self):
        broker = _broker(_envelope([_live_quote_row("F", "11.4600")]))

        last_price = broker.get_quotes(["F"])["F"]["last_price"]

        self.assertIsInstance(last_price, float)

    def test_returned_row_is_normalized_not_the_servers_row(self):
        """The trap: keying the raw row by symbol passes a "non-empty" test.

        It also hands the caller a mapping whose ``last_trade_price`` is
        missing, so ``.get`` yields ``None`` and a price becomes a silent wrong
        number. The normalized fields must be at the top of the value.
        """
        broker = _broker(_envelope([_live_quote_row("F", "11.4600")]))

        entry = broker.get_quotes(["F"])["F"]

        self.assertEqual(set(entry), {"symbol", "last_price", "raw"})
        self.assertNotIn("quote", entry)

    def test_raw_row_is_preserved_untouched(self):
        row = _live_quote_row("F", "11.4600")
        broker = _broker(_envelope([row]))

        self.assertEqual(broker.get_quotes(["F"])["F"]["raw"], row)

    def test_multiple_symbols_in_one_response(self):
        broker = _broker(
            _envelope(
                [
                    _live_quote_row("F", "11.4600"),
                    _live_quote_row("T", "27.0100"),
                    _live_quote_row("GM", "58.2500"),
                ]
            )
        )

        quotes = broker.get_quotes(["F", "T", "GM"])

        self.assertEqual(sorted(quotes), ["F", "GM", "T"])
        self.assertEqual(quotes["T"]["last_price"], 27.01)
        self.assertEqual(quotes["GM"]["last_price"], 58.25)

    def test_symbols_are_upper_cased_on_the_way_out(self):
        broker = _broker(_envelope([_live_quote_row("f", "11.4600")]))

        self.assertEqual(list(broker.get_quotes(["f"])), ["F"])

    def test_symbols_are_upper_cased_on_the_way_in(self):
        broker = _broker(_envelope([]))

        broker.get_quotes(["f", "gm"])

        self.assertEqual(broker.calls[0][1]["symbols"], ["F", "GM"])

    def test_a_symbol_that_did_not_come_back_is_absent_not_defaulted(self):
        broker = _broker(_envelope([_live_quote_row("F", "11.4600")]))

        quotes = broker.get_quotes(["F", "NOSUCH"])

        self.assertNotIn("NOSUCH", quotes)


class LegacyAndFlatShapeTests(unittest.TestCase):
    def test_flat_row_still_resolves(self):
        broker = _broker(
            {"results": [{"symbol": "F", "last_trade_price": "11.4600", "bid_price": "11.45"}]}
        )

        quotes = broker.get_quotes(["F"])

        self.assertEqual(quotes["F"]["last_price"], 11.46)

    def test_flat_row_with_last_price_spelling_still_resolves(self):
        broker = _broker({"quotes": [{"symbol": "F", "last_price": 11.46}]})

        self.assertEqual(broker.get_quotes(["F"])["F"]["last_price"], 11.46)

    def test_top_level_symbol_wins_over_the_nested_one(self):
        """A flat row that also carries sub-objects keeps resolving flat."""
        row = _live_quote_row("STALE", "1.0000")
        row["symbol"] = "F"
        row["last_trade_price"] = "11.4600"
        broker = _broker(_envelope([row]))

        quotes = broker.get_quotes(["F"])

        self.assertEqual(list(quotes), ["F"])
        self.assertEqual(quotes["F"]["last_price"], 11.46)

    def test_close_layer_supplies_the_symbol_when_the_quote_layer_has_none(self):
        row = {"close": {"symbol": "F", "date": "2026-09-12", "price": "11.3900"}}
        broker = _broker(_envelope([row]))

        quotes = broker.get_quotes(["F"])

        self.assertEqual(quotes["F"]["last_price"], 11.39)


class MissingPriceTests(unittest.TestCase):
    def test_has_traded_false_yields_none_not_a_meaningless_price(self):
        row = _live_quote_row("F", "11.4600", has_traded=False)
        broker = _broker(_envelope([row]))

        entry = broker.get_quotes(["F"])["F"]

        self.assertEqual(entry["symbol"], "F")
        self.assertIsNone(entry["last_price"])

    def test_zero_last_trade_price_falls_through_rather_than_reporting_zero(self):
        row = _live_quote_row("F", "0.0000")
        broker = _broker(_envelope([row]))

        # `close.price` is the only positive price left on the row.
        self.assertEqual(broker.get_quotes(["F"])["F"]["last_price"], 11.39)

    def test_no_resolvable_price_anywhere_is_none(self):
        row = {"quote": {"symbol": "F", "last_trade_price": "0.0000", "has_traded": True}}
        broker = _broker(_envelope([row]))

        entry = broker.get_quotes(["F"])["F"]

        self.assertIn("F", broker.get_quotes(["F"]))
        self.assertIsNone(entry["last_price"])

    def test_unparseable_price_is_none_not_an_exception(self):
        row = {"quote": {"symbol": "F", "last_trade_price": "n/a", "has_traded": True}}
        broker = _broker(_envelope([row]))

        self.assertIsNone(broker.get_quotes(["F"])["F"]["last_price"])


class UnkeyableRowTests(unittest.TestCase):
    def test_row_with_no_symbol_anywhere_is_dropped(self):
        broker = _broker(_envelope([{"quote": {"last_trade_price": "11.4600"}}]))

        with patch.object(robinhood_module, "log"):
            self.assertEqual(broker.get_quotes(["F"]), {})

    def test_the_drop_is_logged_not_silent(self):
        broker = _broker(_envelope([{"quote": {"last_trade_price": "11.4600"}}]))

        with patch.object(robinhood_module, "log") as fake_log:
            broker.get_quotes(["F"])

        fake_log.warning.assert_called_once()
        self.assertEqual(
            fake_log.warning.call_args.args[0], "robinhood_quote_row_without_symbol"
        )

    def test_an_unkeyable_row_does_not_discard_its_neighbours(self):
        broker = _broker(
            _envelope([{"quote": {"last_trade_price": "11.4600"}}, _live_quote_row("T", "27.0100")])
        )

        with patch.object(robinhood_module, "log"):
            quotes = broker.get_quotes(["F", "T"])

        self.assertEqual(list(quotes), ["T"])


class TradabilityTests(unittest.TestCase):
    """`get_tradability` is reached live: `review_order` calls it.

    The live response shape has not been observed. The field spelling used here
    is the one the committed schema dump declares (``tradeable``, a required
    boolean); the point of these tests is that *nothing* the adapter fails to
    read is allowed to read as permission.
    """

    def test_vendor_spelling_tradeable_is_read(self):
        broker = _broker({"data": {"results": [{"symbol": "F", "tradeable": True}]}})

        self.assertTrue(broker.get_tradability("F")["tradable"])

    def test_vendor_spelling_tradeable_false_refuses(self):
        broker = _broker({"data": {"results": [{"symbol": "F", "tradeable": False}]}})

        self.assertFalse(broker.get_tradability("F")["tradable"])

    def test_unreadable_row_fails_closed(self):
        """The fail-open this change removes: no readable field means no.

        Previously `_first_present` found none of the names it looked for and
        the `default=True` became the answer — a pre-trade check reporting
        permission on a response it had not understood.
        """
        broker = _broker({"data": {"results": [{"symbol": "F", "name": "Ford Motor Company"}]}})

        with patch.object(robinhood_module, "log"):
            result = broker.get_tradability("F")

        self.assertFalse(result["tradable"])
        self.assertIn("not tradable", result["error"])

    def test_unreadable_row_is_logged(self):
        broker = _broker({"data": {"results": [{"symbol": "F"}]}})

        with patch.object(robinhood_module, "log") as fake_log:
            broker.get_tradability("F")

        fake_log.warning.assert_called_once()
        self.assertEqual(
            fake_log.warning.call_args.args[0],
            "robinhood_tradability_unreadable_failing_closed",
        )

    def test_empty_response_fails_closed(self):
        broker = _broker({"data": {"results": []}})

        with patch.object(robinhood_module, "log"):
            self.assertFalse(broker.get_tradability("F")["tradable"])

    def test_legacy_tradable_spelling_still_resolves(self):
        broker = _broker({"results": [{"symbol": "F", "tradable": True}]})

        self.assertTrue(broker.get_tradability("F")["tradable"])

    def test_a_readable_row_records_no_error(self):
        broker = _broker({"data": {"results": [{"symbol": "F", "tradeable": True}]}})

        self.assertNotIn("error", broker.get_tradability("F"))

    def test_review_order_refuses_when_tradability_is_unreadable(self):
        """The reason the default matters, asserted end to end."""
        from execution.brokers.base import BrokerOrderRequest

        broker = _broker({"data": {"results": [{"symbol": "F"}]}})

        with patch.object(robinhood_module, "log"):
            review = broker.review_order(
                BrokerOrderRequest(
                    symbol="F", side="buy", order_type="limit", quantity=1, limit_price=11.46
                )
            )

        self.assertFalse(review.approved)
        self.assertTrue(any("not tradable" in err for err in review.errors))


if __name__ == "__main__":
    unittest.main()
