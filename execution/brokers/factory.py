"""Broker construction and runtime routing."""

from __future__ import annotations

from execution.alpaca_client import AlpacaClient
from execution.brokers.alpaca import AlpacaBroker
from execution.brokers.robinhood import RobinhoodMCPBroker


DELEGATED_BROKER_ATTRS = {
    "cancel_order",
    "close_position",
    "get_account_info",
    "get_order_status",
    "get_orders",
    "get_positions_detail",
    "get_quotes",
    "get_tradability",
    "review_order",
    "place_order",
    "submit_limit_cover",
    "submit_limit_sell",
    "submit_stop_loss",
}


class BrokerRouter:
    """Delegates broker calls to paper or primary broker based on EXECUTION_MODE."""

    name = "router"

    def __init__(self, settings, paper_broker, primary_broker):
        self.settings = settings
        self.paper_broker = paper_broker
        self.primary_broker = primary_broker

    @property
    def active(self):
        mode = str(getattr(self.settings, "execution_mode", "paper")).lower()
        if mode == "paper":
            return self.paper_broker
        return self.primary_broker

    @property
    def supports_fractional(self) -> bool:
        return getattr(self.active, "supports_fractional", False)

    @property
    def supports_order_review(self) -> bool:
        return getattr(self.active, "supports_order_review", False)

    @property
    def live_trading(self) -> bool:
        return getattr(self.active, "live_trading", False)

    def set_primary(self, broker):
        self.primary_broker = broker

    def __getattr__(self, item):
        if item.startswith("_") or item not in DELEGATED_BROKER_ATTRS:
            raise AttributeError(f"{type(self).__name__!s} has no attribute {item!r}")
        return getattr(self.active, item)


def create_brokers(settings):
    alpaca_client = AlpacaClient(settings.alpaca_api_key, settings.alpaca_secret_key)
    paper_broker = AlpacaBroker(alpaca_client)
    primary = _build_primary(settings, paper_broker)
    return alpaca_client, paper_broker, primary, BrokerRouter(settings, paper_broker, primary)


def rebuild_primary_broker(settings, paper_broker):
    return _build_primary(settings, paper_broker)


def _build_primary(settings, paper_broker):
    broker_name = str(getattr(settings, "broker_primary", "alpaca")).strip().lower()
    if broker_name == "robinhood":
        return RobinhoodMCPBroker(settings)
    return paper_broker
