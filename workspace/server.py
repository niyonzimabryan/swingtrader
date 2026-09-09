#!/usr/bin/env python3
"""Entry point for the workspace Railway service.

    python -m workspace.server

Deliberately not importable from the bot: ``main.py`` starts the bot, this
starts the workspace, and the two share the database and nothing else.
"""

from __future__ import annotations

import os
import sys

from utils.logger import get_logger, setup_logging


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    from config.settings import Settings
    from workspace.app import create_app

    setup_logging()
    log = get_logger("workspace")
    settings = Settings()

    # Railway injects $PORT; the setting is the local-development default.
    port = int(os.environ.get("PORT") or settings.workspace_port)
    if not settings.workspace_api_enabled:
        log.warning(
            "workspace_api_disabled",
            detail="WORKSPACE_API_ENABLED is false: /health answers, every "
            "authenticated call returns 503.",
        )

    uvicorn.run(
        create_app(settings),
        host=settings.workspace_host,
        port=port,
        log_config=None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
