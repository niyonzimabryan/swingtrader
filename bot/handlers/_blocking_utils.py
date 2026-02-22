"""
Helpers for running blocking/synchronous work from async bot handlers.
"""

import asyncio
from collections.abc import Callable
from typing import TypeVar

from utils.logger import get_logger

log = get_logger("bot_blocking")

T = TypeVar("T")


class BlockingCallTimeout(Exception):
    """Raised when a blocking handler task exceeds its timeout."""

    def __init__(self, operation: str, timeout_s: float):
        super().__init__(f"{operation} timed out after {timeout_s:.0f}s")
        self.operation = operation
        self.timeout_s = timeout_s


async def run_blocking(
    operation: str,
    fn: Callable[[], T],
    timeout_s: float,
) -> T:
    """
    Run a blocking callable in the default executor with a hard timeout.

    This keeps the Telegram event loop responsive while sync I/O runs.
    """
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(None, fn), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        log.warning("blocking_call_timeout", operation=operation, timeout_s=timeout_s)
        raise BlockingCallTimeout(operation, timeout_s) from exc
