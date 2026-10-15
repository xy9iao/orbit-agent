"""A polite shared HTTP client for the public job-board APIs.

Politeness rules (CLAUDE.md: respect rate limits):
- a descriptive User-Agent on every request;
- at most `max_concurrency` (4) requests in flight across all hosts;
- at least `min_interval` (1 s) between request starts to the same host;
- retries with exponential backoff on 429, 5xx and transport errors, honoring `Retry-After`;
  after `max_retries` retries a `SourceFetchError` is raised.

The clock and sleep are injectable so tests can assert on the waits without actually sleeping,
and an `httpx.AsyncClient` (or transport) can be injected so tests never touch the network.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from loguru import logger

USER_AGENT = "OrbitInternshipRadar/0.1 (+personal job alerts; contact via GitHub xy9iao/orbit)"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class SourceFetchError(RuntimeError):
    """A job-board request failed for good (non-retryable status, bad JSON, or retries exhausted)."""

    def __init__(self, message: str, *, url: str, status_code: int | None = None, attempts: int = 1):
        super().__init__(message)
        self.url = url
        self.status_code = status_code
        self.attempts = attempts


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """`Retry-After` is either delta-seconds or an HTTP date; returns seconds (>= 0) or None if absent/garbled."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    return max(0.0, (when - now).total_seconds())


class PoliteClient:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str = USER_AGENT,
        timeout: float = 20.0,
        max_concurrency: int = 4,
        min_interval: float = 1.0,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        max_backoff: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ):
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=True)
        self.user_agent = user_agent
        self.timeout = timeout
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.max_backoff = max_backoff
        self._clock = clock
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}

    async def __aenter__(self) -> PoliteClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _wait_for_host(self, host: str) -> None:
        """Hold the host's lock until `min_interval` has passed since its last request start, then stamp it."""
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            last = self._host_last.get(host)
            if last is not None:
                wait = last + self.min_interval - self._clock()
                if wait > 0:
                    await self._sleep(wait)
            self._host_last[host] = self._clock()

    async def _send(self, url: str, params: dict[str, Any] | None) -> httpx.Response:
        host = httpx.URL(url).host
        # Semaphore first, spacing second: the stamped time is then the real send time.
        async with self._semaphore:
            await self._wait_for_host(host)
            return await self._client.get(
                url,
                params=params,
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                timeout=self.timeout,
            )

    def _backoff(self, retry: int, retry_after: float | None) -> float:
        """Delay before retry number `retry` (1-based): Retry-After when given, else base * 2**(retry-1)."""
        if retry_after is not None:
            return retry_after
        return min(self.max_backoff, self.backoff_base * 2 ** (retry - 1))

    async def get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET with politeness and retries; returns a 2xx response or raises `SourceFetchError`."""
        attempts = self.max_retries + 1
        last_problem = ""
        for attempt in range(1, attempts + 1):
            retry_after: float | None = None
            try:
                resp = await self._send(url, params)
            except httpx.TransportError as e:
                last_problem = f"{type(e).__name__}: {e}"
                status = None
            else:
                status = resp.status_code
                if resp.is_success:
                    return resp
                if status not in RETRY_STATUSES:
                    msg = f"GET {url} returned HTTP {status}"
                    raise SourceFetchError(msg, url=url, status_code=status, attempts=attempt)
                last_problem = f"HTTP {status}"
                retry_after = parse_retry_after(resp.headers.get("Retry-After"))

            if attempt == attempts:
                break
            delay = self._backoff(attempt, retry_after)
            if delay > self.max_backoff:
                # Retrying earlier than the server asked would be rude; waiting longer would stall the poll.
                msg = (
                    f"GET {url} failed ({last_problem}); server asked to wait {delay:.0f}s, "
                    f"more than {self.max_backoff:.0f}s, giving up"
                )
                raise SourceFetchError(msg, url=url, status_code=status, attempts=attempt)
            logger.debug("radar http: {} failed ({}), retry {} in {:.1f}s", url, last_problem, attempt, delay)
            await self._sleep(delay)

        msg = f"GET {url} failed after {attempts} attempts ({self.max_retries} retries); last error: {last_problem}"
        raise SourceFetchError(msg, url=url, status_code=status, attempts=attempts)

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self.get(url, params)
        try:
            return resp.json()
        except ValueError as e:
            msg = f"GET {url} returned invalid JSON: {e}"
            raise SourceFetchError(msg, url=url, status_code=resp.status_code) from e
