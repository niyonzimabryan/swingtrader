"""Replay the recorded-shape Robinhood fixtures through the real adapter.

The point of this helper is that it does **not** reimplement the adapter. It
substitutes ``_call_tool_sync`` — the one method that touches the network — and
lets every normalizer in ``execution/brokers/robinhood.py`` run exactly as it
does live. A test that built ``HoldingRecord``s by hand would prove the sync
works and say nothing about whether the adapter parses what Robinhood sends.

The fixtures are recorded-*shape*, not recorded from a live account; see
``tests/fixtures/robinhood/README.md`` for exactly what that does and does not
establish. ``scripts/record_robinhood_fixtures.py`` replaces them with real
responses on a machine that has the token store.

The name is deliberately not ``test_*``: ``unittest discover -p "test_*.py"``
would otherwise import it as a test module.
"""

from __future__ import annotations

import json
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "robinhood"

#: Masked account numbers as they appear in ``get_accounts.json``.
AGENTIC_ACCOUNT = "****4021"
PRIMARY_ACCOUNT = "****7788"

_SUFFIX = {AGENTIC_ACCOUNT: "agentic", PRIMARY_ACCOUNT: "primary"}


def load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


class FixtureTransport:
    """Serves recorded payloads and records every call it was asked for.

    A tool with no fixture for an account returns an empty envelope rather than
    raising: that is what the live server does for an account with no options or
    no lots, and a test that could not express "this account holds nothing"
    would push every empty case out of coverage.
    """

    def __init__(self, *, missing: tuple[str, ...] = (), failing: dict | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.missing = set(missing)
        self.failing = dict(failing or {})

    def __call__(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, dict(arguments)))
        if name in self.failing:
            raise RuntimeError(self.failing[name])
        if name in self.missing:
            return {"results": []}
        if name == "get_accounts":
            return load("get_accounts")
        account = arguments.get("account_number", "")
        suffix = _SUFFIX.get(account)
        if suffix is None:
            return {"results": []}
        path = FIXTURE_DIR / f"{name}__{suffix}.json"
        if not path.exists():
            return {"results": []}
        return json.loads(path.read_text(encoding="utf-8"))

    def tools_called(self) -> set[str]:
        return {name for name, _ in self.calls}


def fixture_broker(*, missing: tuple[str, ...] = (), failing: dict | None = None):
    """A ``RobinhoodMCPBroker`` whose transport is the fixture set."""
    from types import SimpleNamespace

    from execution.brokers.robinhood import RobinhoodMCPBroker

    settings = SimpleNamespace(
        robinhood_mcp_url="https://agent.robinhood.com/mcp/trading",
        robinhood_account_number=AGENTIC_ACCOUNT,
        robinhood_mcp_auth_token="",
        robinhood_mcp_headers_json="",
        token_encryption_key="",
        allow_live_trading=False,
    )
    broker = RobinhoodMCPBroker(settings)
    transport = FixtureTransport(missing=missing, failing=failing)
    broker._call_tool_sync = transport
    broker.transport = transport
    return broker
