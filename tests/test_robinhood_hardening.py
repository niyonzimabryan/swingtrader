import unittest
from types import SimpleNamespace

from pydantic import ValidationError

from config.settings import Settings
from execution.brokers.factory import BrokerRouter
from execution.brokers.robinhood import RobinhoodMCPBroker, _extract_list


class SettingsValidationTests(unittest.TestCase):
    def test_invalid_robinhood_order_type_fails_fast(self):
        with self.assertRaises(ValidationError):
            Settings(robinhood_order_type="makret")

    def test_robinhood_order_type_is_normalized(self):
        settings = Settings(robinhood_order_type=" LIMIT ")
        self.assertEqual(settings.robinhood_order_type, "limit")


class BrokerRouterHardeningTests(unittest.TestCase):
    def test_unknown_attributes_do_not_silently_route_to_active_broker(self):
        active = SimpleNamespace(name="active", surprise="do-not-route")
        router = BrokerRouter(SimpleNamespace(execution_mode="live"), paper_broker=SimpleNamespace(), primary_broker=active)

        with self.assertRaises(AttributeError):
            getattr(router, "surprise")

    def test_declared_broker_methods_still_delegate(self):
        active = SimpleNamespace(name="active", get_orders=lambda status=None: [{"status": status or "all"}])
        router = BrokerRouter(SimpleNamespace(execution_mode="live"), paper_broker=SimpleNamespace(), primary_broker=active)

        self.assertEqual(router.get_orders(), [{"status": "all"}])


class RobinhoodPayloadParsingTests(unittest.TestCase):
    def test_account_info_prefers_selected_portfolio_over_unrelated_nested_values(self):
        broker = RobinhoodMCPBroker(
            SimpleNamespace(
                robinhood_account_number="RH123456",
                robinhood_mcp_url="https://example.invalid/mcp",
                token_encryption_key="",
            )
        )
        broker._call_tool_sync = lambda _name, _args: {
            "metadata": {"equity": 999999.0, "cash": 999999.0},
            "portfolio": {
                "account_number": "RH123456",
                "equity": 25.0,
                "cash": 7.5,
                "buying_power": 9.0,
            },
        }

        info = broker.get_account_info()

        self.assertEqual(info["equity"], 25.0)
        self.assertEqual(info["cash"], 7.5)
        self.assertEqual(info["buying_power"], 9.0)


if __name__ == "__main__":
    unittest.main()


class RobinhoodEnvelopeParsingTests(unittest.TestCase):
    """The live server's envelope, which the recorded-shape fixtures do not have.

    Observed against the production MCP endpoint on 2026-09-13: every response
    is wrapped as ``{"data": ..., "structured": ..., "guide": str, "text": str}``
    and the rows live at ``data.<key>``. Before the envelope fallback existed,
    ``_extract_list`` returned ``[]`` here — for all twelve read paths — while
    the fixture-backed tests passed, because those fixtures put the rows at the
    top level. These cases exist so that gap cannot reopen silently.
    """

    #: Field-for-field the shape production returned, with the account numbers
    #: replaced. Never commit a real account number to a fixture or a test.
    LIVE_ACCOUNTS_ENVELOPE = {
        "data": {
            "accounts": [
                {"account_number": "000000001", "type": "margin", "agentic_allowed": False},
                {"account_number": "000000002", "type": "cash", "nickname": "Trading", "agentic_allowed": False},
                {"account_number": "000000003", "type": "cash", "nickname": "Agentic", "agentic_allowed": True},
            ]
        },
        "guide": "Sort the list deterministically when presenting: ...",
        "structured": {"data": {"accounts": []}, "guide": "..."},
        "text": "| Account | Type |\n|---|---|\n",
    }

    def test_live_envelope_rows_are_found_under_data(self):
        rows = _extract_list(self.LIVE_ACCOUNTS_ENVELOPE, preferred_keys=("accounts", "results"))

        self.assertEqual(len(rows), 3)
        self.assertEqual([r["type"] for r in rows], ["margin", "cash", "cash"])

    def test_the_single_agentic_account_survives_extraction(self):
        rows = _extract_list(self.LIVE_ACCOUNTS_ENVELOPE, preferred_keys=("accounts", "results"))
        agentic = [r for r in rows if r.get("agentic_allowed") is True]

        self.assertEqual(len(agentic), 1)
        self.assertEqual(agentic[0]["nickname"], "Agentic")

    def test_top_level_shape_still_wins(self):
        """The recorded-shape fixtures must keep resolving exactly as before."""
        raw = {"accounts": [{"account_number": "000000001"}], "data": {"accounts": [{"account_number": "999999999"}]}}

        rows = _extract_list(raw, preferred_keys=("accounts", "results"))

        self.assertEqual(rows, [{"account_number": "000000001"}])

    def test_results_nesting_still_resolves(self):
        raw = {"results": [{"symbol": "SPY"}]}

        self.assertEqual(_extract_list(raw, preferred_keys=("positions", "results")), [{"symbol": "SPY"}])

    def test_envelope_walk_is_bounded_and_terminates(self):
        """A payload buried deeper than the bound is not found, and does not hang."""
        buried = {"a": {"b": {"c": {"d": {"positions": [{"symbol": "SPY"}]}}}}}

        self.assertEqual(_extract_list(buried, preferred_keys=("positions",)), [])

    def test_empty_envelope_is_still_empty(self):
        raw = {"data": {"accounts": []}, "guide": "...", "text": "..."}

        self.assertEqual(_extract_list(raw, preferred_keys=("accounts", "results")), [])
