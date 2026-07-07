"""
Run a blocking (sync) callable on the event loop's executor under a hard timeout.

The Alpaca SDK is synchronous. Calling it directly from an async monitor loop
blocks the whole event loop if the broker stalls — the exact failure that hung
production on 2026-07-03. `call_with_timeout` offloads the call to a worker thread
and bounds it with `asyncio.wait_for`, so a stuck broker call raises
`asyncio.TimeoutError` (caught by the loop) instead of freezing forever.
"""

import asyncio
import functools
from typing import Any, Callable


async def call_with_timeout(
    fn: Callable[..., Any], *args: Any, timeout_s: float, **kwargs: Any
) -> Any:
    """
    Execute sync `fn(*args, **kwargs)` in the default executor, aborting the await
    after `timeout_s` seconds. Raises asyncio.TimeoutError on timeout; re-raises any
    exception the callable itself throws.
    """
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(None, functools.partial(fn, *args, **kwargs)),
        timeout=timeout_s,
    )
