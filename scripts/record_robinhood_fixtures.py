#!/usr/bin/env python3
"""Record real Robinhood MCP responses as test fixtures, with accounts masked.

    python -m scripts.record_robinhood_fixtures --list
    python -m scripts.record_robinhood_fixtures --out tests/fixtures/robinhood

Robinhood is not reachable from CI or from a cloud build environment, and
reaching it needs the owner's encrypted token store. So the fixtures committed
under ``tests/fixtures/robinhood/`` are **recorded-shape**: built from the
2026-09-08 ``tools/list`` schema dump and from the response handling already in
``execution/brokers/robinhood.py``, not from a live account. This script is how
they get replaced with real ones on a machine that has the token store, so the
shapes stop being an inference.

**Masking is not optional and not a flag.** Account numbers are rewritten
everywhere they appear — as values, inside nested objects, and inside any string
that contains one — before anything is written to disk. A fixture is a file in a
public repository; an account number in one is not recoverable by deleting the
file later.

Read tools only. This script never calls a place, review, or cancel tool, and
``tests/test_portfolio_import_graph.py`` asserts that no module in the
repository names one.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_OUT = Path("tests/fixtures/robinhood")

#: The read tools Spec L §5.1 names. Recorded in this order; a per-account tool
#: is recorded once per account the token can see.
ACCOUNT_TOOLS = (
    "get_portfolio",
    "get_equity_positions",
    "get_equity_tax_lots",
    "get_equity_orders",
    "get_option_positions",
    "get_realized_pnl",
    "get_pnl_trade_history",
)

#: Anything that looks like an account number, whatever key it sits under.
_ACCOUNT_LIKE_KEYS = re.compile(
    r"account(_number|_id)?$|^number$|^brokerage_account", re.IGNORECASE
)


def mask(value: str) -> str:
    return "****" + value[-4:] if len(value) > 4 else "****"


def scrub(payload, account_numbers: set[str]):
    """Replace every account number, and every key that names one, recursively."""
    replacements = {number: mask(number) for number in account_numbers if number}

    def scrub_text(text: str) -> str:
        for number, masked in replacements.items():
            text = text.replace(number, masked)
        return text

    def walk(node, key_hint: str = ""):
        if isinstance(node, dict):
            return {k: walk(v, k) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(item, key_hint) for item in node]
        if isinstance(node, str):
            masked = scrub_text(node)
            if masked == node and _ACCOUNT_LIKE_KEYS.search(key_hint or "") and node:
                # A key that names an account whose value we did not enumerate:
                # mask it anyway rather than trusting the enumeration.
                return mask(node)
            return masked
        return node

    return walk(payload)


def record(broker, out_dir: Path) -> list[Path]:
    accounts_raw = broker._call_tool_sync("get_accounts", {})
    numbers = set()
    for row in _iter_dicts(accounts_raw):
        for key in ("account_number", "account_id", "number", "id"):
            value = row.get(key)
            if isinstance(value, str) and value:
                numbers.add(value)

    written = [_write(out_dir, "get_accounts", scrub(accounts_raw, numbers))]
    for number in sorted(numbers):
        for tool in ACCOUNT_TOOLS:
            try:
                raw = broker._call_tool_sync(tool, {"account_number": number})
            except Exception as exc:
                print(f"  {tool} for {mask(number)} failed: {exc}", file=sys.stderr)
                continue
            written.append(_write(out_dir, f"{tool}__{mask(number)}", scrub(raw, numbers)))
    return written


def _iter_dicts(payload):
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                yield from (item for item in value if isinstance(item, dict))
            elif isinstance(value, dict):
                yield value
        yield payload
    elif isinstance(payload, list):
        yield from (item for item in payload if isinstance(item, dict))


def _write(out_dir: Path, name: str, payload) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name.replace('*', 'x')}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="fixture directory")
    parser.add_argument("--list", action="store_true", help="list the tools this records")
    args = parser.parse_args(argv)

    if args.list:
        print("get_accounts")
        for tool in ACCOUNT_TOOLS:
            print(tool)
        return 0

    from config.settings import Settings
    from database.token_store import is_configured
    from execution.brokers.robinhood import RobinhoodMCPBroker

    settings = Settings()
    if not is_configured(settings):
        print(
            "TOKEN_ENCRYPTION_KEY is not set: there is no token store to record "
            "against. Run this on the machine that holds it "
            "(docs/ROBINHOOD_TOKEN_STORE.md). The committed fixtures are "
            "recorded-shape until then.",
            file=sys.stderr,
        )
        return 2

    written = record(RobinhoodMCPBroker(settings), Path(args.out))
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
