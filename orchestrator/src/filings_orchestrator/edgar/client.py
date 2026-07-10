"""HTTP client for SEC EDGAR.

EDGAR requires every request to carry a descriptive User-Agent identifying
the requester (including a contact email). Requests without it are blocked
with HTTP 403. The client also rate-limits to stay within EDGAR's published
fair-use limit of 10 requests per second.

Reference: https://www.sec.gov/os/accessing-edgar-data
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock
from typing import Any

import httpx

# EDGAR documents its fair-use limit as 10 requests per second per IP.
# We target 9 to leave headroom for retries.
_DEFAULT_RATE_LIMIT_PER_SEC = 9


class RateLimiter:
    """Token-bucket rate limiter, thread-safe."""

    def __init__(self, max_per_second: int = _DEFAULT_RATE_LIMIT_PER_SEC) -> None:
        self._max = max_per_second
        self._timestamps: deque[float] = deque()
        self._lock = Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            cutoff = now - 1.0
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._max:
                sleep_for = 1.0 - (now - self._timestamps[0])
                if sleep_for > 0:
                    time.sleep(sleep_for)
                now = time.monotonic()
                cutoff = now - 1.0
                while self._timestamps and self._timestamps[0] < cutoff:
                    self._timestamps.popleft()
            self._timestamps.append(now)


class EdgarClient:
    """Synchronous EDGAR HTTP client with mandatory User-Agent and rate limiting.

    Two host families are used by EDGAR:
        - data.sec.gov: JSON APIs (submissions feed, company facts)
        - www.sec.gov:  static files (filing documents, daily indexes)
    Both require the same User-Agent and respect the same fair-use limit.
    """

    def __init__(
        self,
        user_agent: str,
        rate_limit_per_second: int = _DEFAULT_RATE_LIMIT_PER_SEC,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not user_agent or "@" not in user_agent:
            raise ValueError(
                "EDGAR requires a User-Agent containing a contact email "
                "(format: 'app-name you@example.com')."
            )
        self._limiter = RateLimiter(rate_limit_per_second)
        self._client = httpx.Client(
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json, text/html;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "Host": "data.sec.gov",
            },
            timeout=timeout_seconds,
        )

    def __enter__(self) -> EdgarClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get_json(self, url: str) -> dict[str, Any]:
        with self._for_host(url):
            self._limiter.acquire()
            response = self._client.get(url)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            return data

    def get_text(self, url: str) -> str:
        with self._for_host(url):
            self._limiter.acquire()
            response = self._client.get(url)
            response.raise_for_status()
            return response.text

    def get_bytes(self, url: str) -> tuple[bytes, str]:
        """Fetch raw bytes and the Content-Type header.

        Used where the content type must be known before decoding or parsing —
        e.g. filing documents and exhibits, which EDGAR may serve as PDFs or
        images that must never be handed to the HTML parser.
        """
        with self._for_host(url):
            self._limiter.acquire()
            response = self._client.get(url)
            response.raise_for_status()
            return response.content, response.headers.get("content-type", "")

    @contextmanager
    def _for_host(self, url: str) -> Iterator[None]:
        """Temporarily set the Host header to match the URL.

        httpx adds Host from the URL by default, but our default headers
        pin data.sec.gov; restore the correct host for www.sec.gov URLs.
        """
        host = httpx.URL(url).host
        previous = self._client.headers.get("Host")
        self._client.headers["Host"] = host
        try:
            yield
        finally:
            if previous is not None:
                self._client.headers["Host"] = previous
