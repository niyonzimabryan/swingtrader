"""
One-time interactive bootstrap for the Robinhood Agentic Trading MCP OAuth flow.

This is the DISCOVERY step for unattended live trading: it runs the real OAuth
handshake once, persists the result to the encrypted token store, and reports
exactly what Robinhood issued — crucially, whether a REFRESH TOKEN was granted
(which determines whether unattended auto-refresh is possible at all).

Usage:
  python -m scripts.robinhood_auth --gen-key      # print a TOKEN_ENCRYPTION_KEY
  python -m scripts.robinhood_auth                # run the OAuth handshake (local browser)
  python -m scripts.robinhood_auth --callback-file /tmp/rh-callback.txt
  python -m scripts.robinhood_auth --status       # show masked token-store status

Run this on a machine with a browser (Robinhood requires a desktop to open an
Agentic account / authorize). The encrypted token file lands next to the SQLite
DB; to use it in deployment, seed it onto the mounted persistent volume (e.g.
via your deployment shell) and set TOKEN_ENCRYPTION_KEY as a deployment secret.

NOTE: stop any deployed bot before seeding (Telegram allows one polling client).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from config.settings import Settings
from database.token_store import (
    EncryptedFileTokenStorage,
    build_oauth_provider,
    generate_key,
    is_configured,
)


def _build_provider(settings, port: int, scope: str, callback_file: str | None = None, timeout: float = 1800.0):
    async def redirect_handler(auth_url: str) -> None:
        print("\n=== AUTHORIZE URL (open this and approve) ===")
        print(auth_url)
        print("=== END AUTHORIZE URL ===\n", flush=True)
        if not callback_file:
            try:
                import webbrowser

                webbrowser.open(auth_url)
            except Exception:
                pass

    async def callback_handler() -> tuple[str, str | None]:
        if callback_file:
            return await _file_callback(callback_file, timeout)
        return await _await_callback(port)

    return build_oauth_provider(
        settings,
        port=port,
        scope=scope,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        timeout=timeout,
    )


async def _await_callback(port: int) -> tuple[str, str | None]:
    """Run a one-shot localhost HTTP server to capture ?code=&state= ."""
    import http.server

    captured: dict[str, str | None] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            qs = parse_qs(urlparse(self.path).query)
            captured["code"] = (qs.get("code") or [None])[0]
            captured["state"] = (qs.get("state") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>SwingTrader: authorization received. You can close this tab.</h2>")

        def log_message(self, *args):  # silence
            return

    def serve_once():
        with http.server.HTTPServer(("localhost", port), Handler) as httpd:
            httpd.handle_request()

    await asyncio.to_thread(serve_once)
    if not captured.get("code"):
        raise RuntimeError("No authorization code received on the callback.")
    return captured["code"], captured.get("state")


async def _file_callback(path: str, timeout: float) -> tuple[str, str | None]:
    """Headless/remote: poll a file for the pasted redirect URL (or raw code).

    The operator approves on any device, copies the resulting
    http://localhost:.../callback?code=...&state=... URL, and it is written to
    `path` (by a human or an orchestrator). We extract code+state from it.
    """
    p = Path(path)
    if p.exists():
        p.unlink()  # start clean so stale content isn't reused
    print(f"Waiting for the redirect URL at: {path}", flush=True)
    waited = 0.0
    while waited < timeout:
        if p.exists():
            raw = p.read_text().strip()
            if raw:
                qs = parse_qs(urlparse(raw).query)
                code = (qs.get("code") or [None])[0] or (raw if "code" not in raw and "/" not in raw else None)
                state = (qs.get("state") or [None])[0]
                if code:
                    return code, state
        await asyncio.sleep(2)
        waited += 2
    raise RuntimeError(f"Timed out after {timeout}s waiting for {path}")


async def run_oauth(settings, port: int, scope: str, callback_file: str | None) -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    provider, storage = _build_provider(settings, port, scope, callback_file=callback_file)

    print(f"Connecting to {settings.robinhood_mcp_url} (scope={scope}) ...", flush=True)
    async with streamablehttp_client(settings.robinhood_mcp_url, auth=provider) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"Authorized. Server exposes {len(tools.tools)} tools.")

    status = storage.status()
    print("\n=== Token store status ===")
    for k, v in status.items():
        print(f"  {k}: {v}")
    print("\n=== DISCOVERY RESULT ===")
    if status["has_refresh_token"]:
        print("  Refresh token ISSUED -> unattended auto-refresh is viable.")
    else:
        print("  NO refresh token -> unattended live will need periodic re-auth.")
        print(f"  Access token scope={status['scope']!r}, expires_at={status['expires_at']}.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Robinhood MCP OAuth bootstrap")
    parser.add_argument("--gen-key", action="store_true", help="Print a fresh TOKEN_ENCRYPTION_KEY and exit")
    parser.add_argument("--status", action="store_true", help="Print masked token-store status and exit")
    parser.add_argument("--callback-file", default=None, help="Headless/remote: poll this file for the pasted redirect URL instead of running a local callback server")
    parser.add_argument("--port", type=int, default=8765, help="Local callback port / registered redirect_uri port (default 8765)")
    parser.add_argument("--scope", default="internal", help="OAuth scope to request (default: internal)")
    args = parser.parse_args(argv)

    if args.gen_key:
        print(generate_key())
        return 0

    settings = Settings()

    if args.status:
        if not is_configured(settings):
            print("TOKEN_ENCRYPTION_KEY is not set; nothing to show.")
            return 1
        for k, v in EncryptedFileTokenStorage(settings).status().items():
            print(f"  {k}: {v}")
        return 0

    if not is_configured(settings):
        print(
            "TOKEN_ENCRYPTION_KEY is not set.\n"
            "Generate one and add it to your .env / deployment secrets:\n"
            "  python -m scripts.robinhood_auth --gen-key",
            file=sys.stderr,
        )
        return 1

    return asyncio.run(run_oauth(settings, args.port, args.scope, args.callback_file))


if __name__ == "__main__":
    raise SystemExit(main())
