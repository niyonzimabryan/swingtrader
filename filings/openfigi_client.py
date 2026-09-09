"""The one module that talks to OpenFIGI — Spec O section 3.2.

CUSIP → FIGI → ticker. **One-way.** OpenFIGI accepts ``ID_CUSIP`` as an input
identifier but does not return CUSIP, ISIN or SEDOL as output, because those
are proprietary identifiers with redistribution restrictions (verification
claim 15). For ownership tables, which are CUSIP-keyed, that is exactly the
direction needed; a reverse lookup table cannot be built from this API and
nothing here pretends otherwise.

Rate limits, verbatim from https://www.openfigi.com/api/documentation and
recorded in verification claim 15:

* **without an API key** — 25 requests per minute, at most 10 jobs per request;
* **with an API key** — 25 requests per 6 seconds, at most 100 jobs.

Both pairs are constants here and the client picks by whether a key is set, so
raising the batch size without the key that earns it is not something a caller
can do by passing an argument.

The client is constructed with an ``httpx`` transport so the whole path runs
under test against fixtures with no network.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Sequence

import httpx

from utils.logger import get_logger

log = get_logger("openfigi_client")

OPENFIGI_BASE = "https://api.openfigi.com"
MAPPING_PATH = "/v3/mapping"

#: (requests, per_seconds, max_jobs) — keyless, then keyed.
KEYLESS_LIMITS = (25, 60.0, 10)
KEYED_LIMITS = (25, 6.0, 100)

RETRYABLE_STATUS = frozenset({429, 502, 503, 504})

#: The exchange codes a US-listed common share can carry. OpenFIGI returns
#: every venue a FIGI trades on, including foreign cross-listings that share a
#: ticker with an unrelated US name; restricting to the composite US venues is
#: what stops "VOD" on the LSE resolving a US 13F holding.
US_EXCHANGE_CODES = frozenset({"US", "UN", "UQ", "UA", "UP", "UR", "UW", "UV", "UF"})


class OpenFIGIError(RuntimeError):
    """The client refused to make a request, or the request failed for good."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class _Throttle:
    """A sliding window of request timestamps — the published limit shape."""

    max_requests: int
    window_s: float
    clock: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        self._times: list[float] = []

    def wait(self) -> None:
        while True:
            now = self.clock()
            self._times = [t for t in self._times if now - t < self.window_s]
            if len(self._times) < self.max_requests:
                self._times.append(now)
                return
            self.sleeper(self.window_s - (now - self._times[0]))


class OpenFIGIClient:
    """Batched, throttled CUSIP → FIGI mapping."""

    def __init__(
        self,
        api_key: str = "",
        *,
        timeout_s: float = 30.0,
        max_retries: int = 3,
        base_url: str = OPENFIGI_BASE,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.api_key = (api_key or "").strip()
        requests, window, jobs = KEYED_LIMITS if self.api_key else KEYLESS_LIMITS
        self.max_jobs = jobs
        self.max_retries = max(0, int(max_retries))
        self._sleeper = sleeper
        self._throttle = _Throttle(requests, window, clock=clock, sleeper=sleeper)

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-OPENFIGI-APIKEY"] = self.api_key
        self._client = httpx.Client(
            transport=transport, timeout=timeout_s, base_url=base_url, headers=headers
        )
        self.request_count = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OpenFIGIClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def map_jobs(self, jobs: Sequence[dict]) -> list[dict]:
        """POST one batch of mapping jobs; the result is positionally aligned."""
        batch = list(jobs)
        if not batch:
            return []
        if len(batch) > self.max_jobs:
            raise OpenFIGIError(
                f"{len(batch)} jobs exceeds OpenFIGI's limit of {self.max_jobs} "
                f"per request {'with' if self.api_key else 'without'} an API key."
            )

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle.wait()
            try:
                response = self._client.post(MAPPING_PATH, json=batch)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self.max_retries:
                    self._sleeper(min(2.0**attempt, 30.0))
                continue

            self.request_count += 1
            if response.status_code in RETRYABLE_STATUS:
                last_error = OpenFIGIError(
                    f"{response.status_code} from OpenFIGI (attempt {attempt + 1})",
                    status_code=response.status_code,
                )
                if attempt < self.max_retries:
                    self._sleeper(min(2.0**attempt, 30.0))
                continue
            if response.status_code >= 400:
                raise OpenFIGIError(
                    f"{response.status_code} from OpenFIGI", status_code=response.status_code
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise OpenFIGIError("non-JSON response from OpenFIGI") from exc
            if not isinstance(payload, list) or len(payload) != len(batch):
                raise OpenFIGIError(
                    "OpenFIGI returned "
                    f"{len(payload) if isinstance(payload, list) else type(payload).__name__}"
                    f" results for {len(batch)} jobs; the positional alignment this "
                    "parser depends on is gone."
                )
            return payload

        log.warning("openfigi_exhausted", attempts=self.max_retries + 1)
        if isinstance(last_error, OpenFIGIError):
            raise last_error
        raise OpenFIGIError(f"giving up on OpenFIGI: {last_error}") from last_error


def client_from_settings(settings, *, transport: httpx.BaseTransport | None = None):
    return OpenFIGIClient(
        getattr(settings, "openfigi_api_key", "") or "",
        timeout_s=getattr(settings, "openfigi_timeout_s", 30.0),
        transport=transport,
    )
