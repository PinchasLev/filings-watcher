"""Exponential-backoff retry wrapper for Anthropic API calls.

Per ADR 0021: classify-each-filing operations retry on 429 and 5xx with
exponential backoff (initial 1s, max 60s, ±20% jitter) up to 5 attempts.
Filings whose retries exhaust within a tick are skipped; the cursor does
not advance past them; the next tick re-attempts.

The wrapper does NOT retry on:
- AuthenticationError / PermissionDeniedError (operator action required)
- BadRequestError / UnprocessableEntityError (prompt/payload bug, not transient)
- NotFoundError (resource bug)

Retry-able shapes:
- RateLimitError (HTTP 429): Anthropic per-minute cap hit.
- InternalServerError (HTTP 5xx): transient upstream failure.
- APIConnectionError / APITimeoutError: transient network.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

from anthropic import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)

from filings_orchestrator.log_events import emit

_INITIAL_DELAY_SEC = 1.0
_MAX_DELAY_SEC = 60.0
_JITTER_FRACTION = 0.20
_MAX_ATTEMPTS = 5

_RETRYABLE_EXC: tuple[type[BaseException], ...] = (
    RateLimitError,
    InternalServerError,
    APIConnectionError,
    APITimeoutError,
)


def is_retryable_error(exc: BaseException) -> bool:
    """True if `exc` is a transient Anthropic failure (rate-limit, 5xx, network).

    `with_retries` already retries these in-call; when one still propagates, the
    condition is sustained (an outage), not a property of the input. Callers that
    bound re-attempts across runs — the classify reconciler's dead-letter counter
    (ADR 0030) — use this to count only *deterministic* failures toward
    abandonment, so a transient outage does not park otherwise-healthy filings.
    """
    return isinstance(exc, _RETRYABLE_EXC)


def with_retries[T](
    fn: Callable[[], T],
    *,
    log_context: dict[str, object] | None = None,
    max_attempts: int = _MAX_ATTEMPTS,
    initial_delay: float = _INITIAL_DELAY_SEC,
    max_delay: float = _MAX_DELAY_SEC,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
) -> T:
    """Call `fn()` with exponential backoff on retryable Anthropic errors.

    Emits a structured `rate_limited` event on every retry attempt for
    OTel correlation. Raises the final exception if all attempts exhaust.
    The `sleep` and `rand` parameters are seams for deterministic tests.
    """
    context = log_context or {}
    delay = initial_delay
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except _RETRYABLE_EXC as exc:
            last_exc = exc
            if attempt == max_attempts:
                break
            jitter = 1.0 + _JITTER_FRACTION * (2 * rand() - 1)
            wait = min(delay * jitter, max_delay)
            emit(
                "rate_limited",
                provider="anthropic",
                attempt=attempt,
                next_attempt_in_sec=round(wait, 3),
                error_class=type(exc).__name__,
                **context,
            )
            sleep(wait)
            delay = min(delay * 2, max_delay)
    assert last_exc is not None
    raise last_exc
