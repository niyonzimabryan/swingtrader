"""
Token-bucket rate limiter per API service.
Tracks calls per window to respect free tier limits.
"""

import time
import threading
from collections import defaultdict
from utils.logger import get_logger

log = get_logger("rate_limiter")


class RateLimiter:
    """Simple token-bucket rate limiter."""

    def __init__(self):
        self._buckets: dict[str, dict] = {}
        self._lock = threading.Lock()

    def register(self, name: str, max_calls: int, window_seconds: int):
        """Register a rate limit for a service."""
        self._buckets[name] = {
            "max_calls": max_calls,
            "window_seconds": window_seconds,
            "calls": [],
        }

    def acquire(self, name: str, block: bool = True, timeout: float = 60) -> bool:
        """
        Acquire a rate limit token. If block=True, waits until available.
        Returns True if acquired, False if timed out.
        """
        if name not in self._buckets:
            return True  # No limit registered

        start = time.time()
        while True:
            with self._lock:
                bucket = self._buckets[name]
                now = time.time()
                cutoff = now - bucket["window_seconds"]
                # Prune old calls
                bucket["calls"] = [t for t in bucket["calls"] if t > cutoff]

                if len(bucket["calls"]) < bucket["max_calls"]:
                    bucket["calls"].append(now)
                    return True

            if not block:
                return False

            if time.time() - start > timeout:
                log.warning("rate_limit_timeout", service=name, timeout=timeout)
                return False

            # Wait before retrying
            time.sleep(0.5)

    def remaining(self, name: str) -> int:
        """Get remaining calls in current window."""
        if name not in self._buckets:
            return 999
        with self._lock:
            bucket = self._buckets[name]
            now = time.time()
            cutoff = now - bucket["window_seconds"]
            active = len([t for t in bucket["calls"] if t > cutoff])
            return max(0, bucket["max_calls"] - active)


# Global rate limiter instance
rate_limiter = RateLimiter()

# Register default limits
rate_limiter.register("finnhub", max_calls=55, window_seconds=60)  # 60/min limit, leave buffer
rate_limiter.register("fmp", max_calls=240, window_seconds=86400)  # 250/day limit
rate_limiter.register("alpha_vantage", max_calls=20, window_seconds=86400)  # 25/day limit
rate_limiter.register("telegram", max_calls=25, window_seconds=1)  # 30/sec limit
