"""``get_equity_tax_lots`` is per symbol, and its rows carry no symbol.

Both facts are in the tool's own schema (`docs/robinhood/tool_schemas.json`):
``symbol`` is required in the inputSchema ("one symbol per call"), and the
outputSchema puts rows at ``data.tax_lots`` with ``symbol`` once at
``data.symbol``, not on each row.

The adapter called it once per account with no symbol, which the vendor refused
with ``invalid params: missing properties: ["symbol"]``. That refusal is why no
portfolio sync had ever completed — invisible until #103 stopped the exception
group's constant message from erasing it (2026-09-15).
"""

import unittest
from types import SimpleNamespace

from execution.brokers.robinhood import RobinhoodMCPBroker, _tax_lot_next_cursor


def _broker(transport):
    settings = SimpleNamespace(
        robinhood_mcp_url="https://agent.robinhood.com/mcp/trading",
        robinhood_account_number="000000001",
        robinhood_mcp_auth_token="",
        robinhood_mcp_headers_json="",
        token_encryption_key="",
        allow_live_trading=False,
    )
    broker = RobinhoodMCPBroker(settings)
    broker._call_tool_sync = transport
    return broker


class RecordingTransport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        handler = self.responses.get(name)
        if callable(handler):
            return handler(arguments)
        return handler if handler is not None else {"data": {"results": []}}


def _envelope(**data):
    return {"data": data, "structured": {"data": data}, "guide": "", "text": ""}


class TaxLotCallShapeTests(unittest.TestCase):
    def test_no_symbol_makes_no_call(self):
        transport = RecordingTransport({})
        broker = _broker(transport)

        self.assertEqual(broker.get_tax_lots("000000001"), [])
        self.assertEqual(transport.calls, [])

    def test_the_symbol_is_sent_and_uppercased(self):
        transport = RecordingTransport({"get_equity_tax_lots": _envelope(symbol="AAPL", tax_lots=[], next=None)})
        broker = _broker(transport)

        broker.get_tax_lots("000000001", "aapl")

        self.assertEqual(transport.calls, [("get_equity_tax_lots", {"account_number": "000000001", "symbol": "AAPL"})])

    def test_rows_get_the_symbol_injected(self):
        """Rows carry no symbol; without injection `_tax_lot` drops every one."""
        transport = RecordingTransport({
            "get_equity_tax_lots": _envelope(
                symbol="AAPL", next=None,
                tax_lots=[{"open_lot_id": "L1", "quantity": "1", "cost_per_share": "100"}],
            )
        })
        broker = _broker(transport)

        rows = broker.get_tax_lots("000000001", "AAPL")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "AAPL")

    def test_pagination_follows_next_and_stops(self):
        pages = {
            None: _envelope(symbol="AAPL", next="c2", tax_lots=[{"open_lot_id": "L1", "quantity": "1"}]),
            "c2": _envelope(symbol="AAPL", next=None, tax_lots=[{"open_lot_id": "L2", "quantity": "1"}]),
        }
        transport = RecordingTransport({"get_equity_tax_lots": lambda a: pages[a.get("cursor")]})
        broker = _broker(transport)

        rows = broker.get_tax_lots("000000001", "AAPL")

        self.assertEqual([r["open_lot_id"] for r in rows], ["L1", "L2"])
        self.assertEqual(transport.calls[1][1]["cursor"], "c2")
        self.assertEqual(len(transport.calls), 2)

    def test_next_cursor_is_read_from_the_envelope(self):
        self.assertEqual(_tax_lot_next_cursor(_envelope(next="abc")), "abc")
        self.assertIsNone(_tax_lot_next_cursor(_envelope(next=None)))
        self.assertIsNone(_tax_lot_next_cursor({"data": {"next": "   "}}))
        self.assertIsNone(_tax_lot_next_cursor("not a dict"))


class SnapshotTests(unittest.TestCase):
    """The account snapshot asks per held equity — and with nothing held, asks nothing."""

    def _snapshot_with(self, positions):
        transport = RecordingTransport({
            "get_equity_positions": _envelope(results=positions),
            "get_option_positions": _envelope(results=[]),
            "get_equity_tax_lots": lambda a: _envelope(
                symbol=a["symbol"], next=None,
                tax_lots=[{"open_lot_id": f"{a['symbol']}-1", "quantity": "1", "cost_per_share": "10"}],
            ),
            "get_portfolio": _envelope(total_value="25", cash="25", buying_power={"buying_power": "25"}),
            "get_pnl_trade_history": _envelope(results=[]),
            "get_equity_orders": _envelope(results=[]),
        })
        broker = _broker(transport)
        record = SimpleNamespace(booking_method="STRICT", agent_placeable=True, external_account_id="000000001", label="x", broker="robinhood")
        from datetime import datetime
        snap = broker._account_snapshot(record, "000000001", datetime(2026, 9, 15))
        return snap, transport

    def test_an_empty_account_never_calls_tax_lots(self):
        """Exactly the production case that had failed every sync: zero positions."""
        snap, transport = self._snapshot_with([])

        self.assertNotIn("get_equity_tax_lots", transport.tools_called() if hasattr(transport, "tools_called") else {n for n, _ in transport.calls})
        self.assertEqual(snap.tax_lots, ())

    def test_one_call_per_held_equity_symbol(self):
        snap, transport = self._snapshot_with([
            {"symbol": "AAPL", "quantity": "1", "average_buy_price": "10"},
            {"symbol": "MSFT", "quantity": "2", "average_buy_price": "20"},
        ])

        lot_calls = [a for n, a in transport.calls if n == "get_equity_tax_lots"]
        self.assertEqual(sorted(a["symbol"] for a in lot_calls), ["AAPL", "MSFT"])
        self.assertEqual(sorted(lot.symbol for lot in snap.tax_lots), ["AAPL", "MSFT"])


if __name__ == "__main__":
    unittest.main()
