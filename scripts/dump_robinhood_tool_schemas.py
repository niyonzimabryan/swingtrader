"""Dump the Robinhood Trading MCP tool list with full JSON Schemas.

Robinhood publishes no developer documentation for its Trading MCP: no order-type
table, no `place_equity_order` parameter reference, no time-in-force or stop
semantics. The only authoritative source is the server's own `tools/list`
response. This script authenticates with the encrypted token store already used
by the broker (no interactive login; run `scripts/robinhood_auth.py` first if
the store is empty), fetches every tool, and writes the schemas to a JSON file.

Usage:
    python -m scripts.dump_robinhood_tool_schemas            # -> docs/robinhood/tool_schemas.json
    python -m scripts.dump_robinhood_tool_schemas --out path.json
    python -m scripts.dump_robinhood_tool_schemas --print place_equity_order review_equity_order

The output contains no account data and no tokens; it is the server's public
tool contract and is safe to commit. Spec L §5.1 (investment workspace) treats
this dump as the first Phase 1 checkpoint: it decides `can_place_attached_stop`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from config.settings import Settings
from execution.brokers.robinhood import RobinhoodMCPBroker

DEFAULT_OUT = Path("docs/robinhood/tool_schemas.json")
HIGHLIGHT = ("place_equity_order", "review_equity_order", "cancel_equity_order")


async def _fetch(broker: RobinhoodMCPBroker) -> list[dict]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    auth = broker._oauth_provider()
    headers = broker._headers(include_auth_token=auth is None)
    async with streamablehttp_client(broker.url, headers=headers, timeout=45, sse_read_timeout=45, auth=auth) as (
        read,
        write,
        _,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
    tools = []
    for t in result.tools:
        tools.append(
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.inputSchema,
                "outputSchema": getattr(t, "outputSchema", None),
                "annotations": getattr(t, "annotations", None) and t.annotations.model_dump(),
            }
        )
    return sorted(tools, key=lambda d: d["name"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Where to write the JSON dump")
    parser.add_argument("--print", nargs="*", default=None, metavar="TOOL", help="Also pretty-print these tools' schemas (default: the equity order tools)")
    args = parser.parse_args(argv)

    settings = Settings()
    broker = RobinhoodMCPBroker(settings)
    if not broker.configured:
        print("ROBINHOOD_MCP_URL is not configured.", file=sys.stderr)
        return 2

    try:
        tools = asyncio.run(_fetch(broker))
    except ImportError as exc:  # pragma: no cover - environment problem
        print(f"MCP SDK import failed ({exc}); requirements pin mcp<2.", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "server": broker.url,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "tool_count": len(tools),
        "tools": tools,
    }
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {len(tools)} tool schemas to {out}")

    to_print = args.print if args.print is not None else list(HIGHLIGHT)
    if to_print:
        by_name = {t["name"]: t for t in tools}
        for name in to_print:
            tool = by_name.get(name)
            print(f"\n=== {name} ===")
            if tool is None:
                print("  (not exposed by this server)")
                continue
            print(json.dumps(tool["inputSchema"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
