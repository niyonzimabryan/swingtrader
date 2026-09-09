"""The one module that talks to SEC over HTTP.

Two rules from the SEC's webmaster FAQ, verified in
``docs/research/2026-09-research-verification.md`` claim 13, are enforced here
and nowhere else:

* **"our current maximum access rate is 10 requests per second"** — every
  request goes through one throttle, so adding a caller cannot quietly double
  the rate. The ceiling is a hard constant; the configured rate may be lower
  but never higher.
* **"Please declare your user agent in request headers"**, in the form
  ``Sample Company Name AdminContact@<domain>.com``. The User-Agent comes from
  ``SEC_USER_AGENT``. There is no default and no fallback: a real contact
  address must never be committed to this repository, and inventing a
  plausible-looking one would be worse than sending none.

Retries cover the transport failures and the two status codes EDGAR uses for
back-pressure (429, 503). A 4xx that is not 429 is a bug in the caller and is
raised immediately rather than retried against a rate-limited host.

The client is constructed with an ``httpx`` transport so the whole path —
throttle, headers, retry, JSON decode — runs under test against recorded
fixtures with no network. See ``tests/fixtures/sec/``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from utils.logger import get_logger

log = get_logger("sec_client")

#: SEC's published maximum. Not configurable upwards.
SEC_MAX_REQUESTS_PER_SECOND = 10.0

DATA_SEC_BASE = "https://data.sec.gov"
WWW_SEC_BASE = "https://www.sec.gov"

RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


class SECClientError(RuntimeError):
    """The client refused to make a request, or the request failed for good."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class SECRateLimitError(SECClientError):
    """SEC kept returning back-pressure after every retry."""


def submissions_url(cik: str) -> str:
    return f"{DATA_SEC_BASE}/submissions/CIK{normalise_cik(cik)}.json"


def submissions_page_url(name: str) -> str:
    """An older submissions page referenced by ``filings.files[].name``."""
    return f"{DATA_SEC_BASE}/submissions/{name}"


def companyfacts_url(cik: str) -> str:
    return f"{DATA_SEC_BASE}/api/xbrl/companyfacts/CIK{normalise_cik(cik)}.json"


def company_tickers_url() -> str:
    return f"{WWW_SEC_BASE}/files/company_tickers.json"


def filing_index_url(cik: str, accession: str) -> str:
    """Human-readable filing index — stored as ``source_url`` on every row."""
    bare = accession.replace("-", "")
    return (
        f"{WWW_SEC_BASE}/Archives/edgar/data/"
        f"{int(normalise_cik(cik))}/{bare}/{accession}-index.htm"
    )


def fixture_name_for_url(url: str) -> str:
    """The file name a recorded response for ``url`` is stored under.

    Used by the offline replay paths (``tests/fixtures/sec/`` and
    ``scripts/sec_backfill.py --fixtures``) so a recording is findable from the
    URL that produced it, rather than by hand-maintained mapping.
    """
    path = httpx.URL(url).path
    leaf = path.rstrip("/").split("/")[-1]
    if "/api/xbrl/companyfacts/" in path:
        return f"companyfacts_{leaf}"
    if path.startswith("/submissions/") and leaf.upper().startswith("CIK") and "-" not in leaf:
        return f"submissions_{leaf}"
    return leaf


def normalise_cik(cik: str | int) -> str:
    """``320193`` / ``'CIK0000320193'`` / ``'0000320193'`` -> ``'0000320193'``."""
    text = str(cik).strip().upper()
    if text.startswith("CIK"):
        text = text[3:]
    text = text.lstrip("0") or "0"
    if not text.isdigit():
        raise SECClientError(f"not a CIK: {cik!r}")
    return text.zfill(10)


def validate_user_agent(user_agent: str) -> str:
    """SEC wants a name and a contact address. Refuse anything else."""
    value = (user_agent or "").strip()
    if not value:
        raise SECClientError(
            "SEC_USER_AGENT is unset. SEC requires a self-identifying "
            "User-Agent of the form 'Company Name contact@example.com' "
            "(https://www.sec.gov/os/webmaster-faq). Set it in .env; it is "
            "deliberately not defaulted in code so no real address is "
            "committed to the repository."
        )
    if "@" not in value or "." not in value.split("@")[-1]:
        raise SECClientError(
            f"SEC_USER_AGENT must contain a contact email address; got {value!r}."
        )
    return value


@dataclass
class _Throttle:
    """Minimum spacing between requests, shared by every caller of a client."""

    min_interval_s: float
    clock: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], None] = time.sleep
    _last: float | None = None

    def wait(self) -> None:
        now = self.clock()
        if self._last is not None:
            elapsed = now - self._last
            if elapsed < self.min_interval_s:
                self.sleeper(self.min_interval_s - elapsed)
                now = self.clock()
        self._last = now


class SECClient:
    """Throttled, identified, retrying JSON reader for SEC's public endpoints.

    ``transport`` is an ``httpx`` transport; tests pass an
    ``httpx.MockTransport`` that serves recorded fixtures, which keeps the
    suite offline while still exercising this module.
    """

    def __init__(
        self,
        user_agent: str,
        *,
        max_requests_per_second: float = SEC_MAX_REQUESTS_PER_SECOND,
        timeout_s: float = 30.0,
        max_retries: int = 4,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if max_requests_per_second <= 0:
            raise SECClientError("max_requests_per_second must be positive")
        if max_requests_per_second > SEC_MAX_REQUESTS_PER_SECOND:
            raise SECClientError(
                f"max_requests_per_second={max_requests_per_second} exceeds SEC's "
                f"published maximum of {SEC_MAX_REQUESTS_PER_SECOND} requests/second."
            )

        self.user_agent = validate_user_agent(user_agent)
        self.max_retries = max(0, int(max_retries))
        self._sleeper = sleeper
        self._throttle = _Throttle(
            min_interval_s=1.0 / max_requests_per_second, clock=clock, sleeper=sleeper
        )
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout_s,
            headers={
                "User-Agent": self.user_agent,
                "Accept-Encoding": "gzip, deflate",
                "Accept": "application/json",
            },
            follow_redirects=True,
        )
        self.request_count = 0

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SECClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- fetching --------------------------------------------------------
    def get_json(self, url: str) -> Any:
        """Fetch and decode one JSON document, or raise."""
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle.wait()
            try:
                response = self._client.get(url)
            except httpx.HTTPError as exc:  # transport-level
                last_error = exc
                if attempt < self.max_retries:
                    self._backoff(attempt)
                continue

            self.request_count += 1

            if response.status_code in RETRYABLE_STATUS:
                last_error = SECRateLimitError(
                    f"{response.status_code} from {url} (attempt {attempt + 1})",
                    status_code=response.status_code,
                )
                if attempt < self.max_retries:
                    self._backoff(attempt, response.headers.get("Retry-After"))
                continue

            if response.status_code >= 400:
                raise SECClientError(
                    f"{response.status_code} from {url}",
                    status_code=response.status_code,
                )

            try:
                return response.json()
            except ValueError as exc:
                raise SECClientError(f"non-JSON response from {url}") from exc

        log.warning("sec_fetch_exhausted", url=url, attempts=self.max_retries + 1)
        if isinstance(last_error, SECClientError):
            raise last_error
        raise SECClientError(f"giving up on {url}: {last_error}") from last_error

    def get_json_or_none(self, url: str) -> Any | None:
        """As :meth:`get_json`, but a 404 is an answer, not a failure.

        Small filers have no ``companyfacts`` document at all. That is missing
        coverage to be reported, not an error to abort a backfill on.
        """
        try:
            return self.get_json(url)
        except SECClientError as exc:
            if exc.status_code == 404:
                return None
            raise

    def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        delay = min(2.0**attempt, 30.0)
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        self._sleeper(delay)


def client_from_settings(settings, *, transport: httpx.BaseTransport | None = None) -> SECClient:
    """Build a client from a ``Settings``-shaped object."""
    return SECClient(
        getattr(settings, "sec_user_agent", ""),
        max_requests_per_second=getattr(
            settings, "sec_max_requests_per_second", SEC_MAX_REQUESTS_PER_SECOND
        ),
        timeout_s=getattr(settings, "sec_request_timeout_s", 30.0),
        max_retries=getattr(settings, "sec_max_retries", 4),
        transport=transport,
    )
