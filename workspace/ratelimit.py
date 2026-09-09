"""Per-token rate limits (Spec K §4.1): 60 read and 10 write calls per minute.

"A runaway agent loop must cost time, not money." The limit is per token and per
kind, in a fixed one-minute window, held in process memory.

In-process is the right scope *today* and the wrong scope later: the workspace
runs as a single Railway replica (``numReplicas = 1``), so one process sees
every call. If the service is ever scaled out, this becomes a per-replica limit
and has to move to Redis — the same Redis that Spec K §4 says to add when
stream resumability is needed, and not before.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

WINDOW_SECONDS = 60.0

DEFAULT_READ_PER_MINUTE = 60
DEFAULT_WRITE_PER_MINUTE = 10


@dataclass(frozen=True)
class RateLimitExceeded(Exception):
    """Raised when a token has spent its allowance for the current window."""

    kind: str
    limit: int
    retry_after: int

    def __str__(self) -> str:  # pragma: no cover - message only
        return (
            f"rate limit exceeded: {self.limit} {self.kind} calls per minute "
            f"per token; retry in {self.retry_after}s"
        )


class RateLimiter:
    """Fixed-window counters keyed by ``(token_id, kind)``."""

    def __init__(
        self,
        read_per_minute: int = DEFAULT_READ_PER_MINUTE,
        write_per_minute: int = DEFAULT_WRITE_PER_MINUTE,
        clock=time.monotonic,
    ):
        self._limits = {"read": read_per_minute, "write": write_per_minute}
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: dict[tuple[int, str], tuple[float, int]] = {}

    def limit_for(self, kind: str) -> int:
        return self._limits[kind]

    def check(self, token_id: int, kind: str) -> None:
        """Count one call. Raises :class:`RateLimitExceeded` when over."""
        limit = self._limits[kind]
        now = self._clock()
        key = (token_id, kind)
        with self._lock:
            started_at, used = self._windows.get(key, (now, 0))
            if now - started_at >= WINDOW_SECONDS:
                started_at, used = now, 0
            if used >= limit:
                retry_after = max(
                    1, int(math.ceil(WINDOW_SECONDS - (now - started_at)))
                )
                self._windows[key] = (started_at, used)
                raise RateLimitExceeded(kind=kind, limit=limit, retry_after=retry_after)
            self._windows[key] = (started_at, used + 1)

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()
