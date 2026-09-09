"""A live workspace service for integration tests.

The MCP round-trip has to go over real HTTP: streamable HTTP is the thing being
tested, and an ASGI-transport shortcut would skip the transport. So the app runs
under uvicorn on an ephemeral port **in this process**, on whichever engine the
run targets, and the official ``mcp`` client connects to it over loopback.

The name is deliberately not ``test_*``: ``unittest discover -p "test_*.py"``
would otherwise import it as a test module.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

import uvicorn

STARTUP_TIMEOUT_SECONDS = 20.0


class LiveWorkspace:
    """A running workspace app. ``base_url`` is where it answers."""

    def __init__(self, app):
        self._config = uvicorn.Config(
            app, host="127.0.0.1", port=0, log_config=None, lifespan="on"
        )
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> "LiveWorkspace":
        self._thread.start()
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while not self._server.started:
            if time.monotonic() > deadline:
                raise TimeoutError("workspace service did not start")
            if not self._thread.is_alive():
                raise RuntimeError("workspace service thread died during startup")
            time.sleep(0.01)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        return self

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=STARTUP_TIMEOUT_SECONDS)


@contextmanager
def running(app):
    live = LiveWorkspace(app).start()
    try:
        yield live
    finally:
        live.stop()


def build_app(database_url: str, **overrides):
    """A workspace app pointed at ``database_url``, with the flag on by default."""
    from config.settings import Settings

    from workspace.app import create_app

    settings = Settings()
    settings.database_url = database_url
    settings.workspace_api_enabled = True
    for key, value in overrides.items():
        setattr(settings, key, value)
    return create_app(settings), settings


def issue_token(label: str, scopes) -> str:
    from database.db import get_session
    from workspace import tokens

    with get_session() as session:
        return tokens.issue(session, label, scopes)


async def list_tools(base_url: str, token: str | None):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with streamablehttp_client(f"{base_url}/mcp", headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            return await session.list_tools()


async def call_tool(base_url: str, token: str | None, name: str, arguments=None):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with streamablehttp_client(f"{base_url}/mcp", headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            return await session.call_tool(name, arguments or {})
