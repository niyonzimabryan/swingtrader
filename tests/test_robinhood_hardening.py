import unittest
from types import SimpleNamespace

from pydantic import ValidationError

from config.settings import Settings
from execution.brokers.factory import BrokerRouter
from execution.brokers.robinhood import RobinhoodMCPBroker


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
