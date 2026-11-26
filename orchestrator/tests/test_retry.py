"""Tests for the Anthropic exponential-backoff retry wrapper."""

from __future__ import annotations

import httpx
import pytest
from anthropic import (
    APIConnectionError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
)

from filings_orchestrator.classify.retry import (
    is_account_halt,
    is_credit_exhausted,
    is_retryable_error,
    with_retries,
)


def _make_rate_limit_error(message: str = "rate limited") -> RateLimitError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, request=request)
    return RateLimitError(message=message, response=response, body=None)


def _make_internal_error(message: str = "boom") -> InternalServerError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(500, request=request)
    return InternalServerError(message=message, response=response, body=None)


def _make_auth_error(message: str = "invalid key") -> AuthenticationError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(401, request=request)
    return AuthenticationError(message=message, response=response, body=None)


def _make_connection_error() -> APIConnectionError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return APIConnectionError(message="connect failed", request=request)


def test_returns_immediately_on_success() -> None:
    sleeps: list[float] = []
    result = with_retries(lambda: "ok", sleep=sleeps.append, rand=lambda: 0.5)
    assert result == "ok"
    assert sleeps == []


def test_retries_on_rate_limit_then_succeeds() -> None:
    attempts = {"n": 0}
    sleeps: list[float] = []

    def fn() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _make_rate_limit_error()
        return "classified"

    result = with_retries(fn, sleep=sleeps.append, rand=lambda: 0.5)
    assert result == "classified"
    # Two retries fired before the success on attempt 3.
    assert attempts["n"] == 3
    assert len(sleeps) == 2


def test_retries_on_internal_server_error() -> None:
    attempts = {"n": 0}

    def fn() -> str:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise _make_internal_error()
        return "ok"

    assert with_retries(fn, sleep=lambda _s: None, rand=lambda: 0.5) == "ok"


def test_retries_on_connection_error() -> None:
    attempts = {"n": 0}

    def fn() -> str:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise _make_connection_error()
        return "ok"

    assert with_retries(fn, sleep=lambda _s: None, rand=lambda: 0.5) == "ok"


def test_does_not_retry_on_authentication_error() -> None:
    """AuthenticationError is operator action, not transient. Must propagate
    on the first call without sleeping."""
    sleeps: list[float] = []

    def fn() -> str:
        raise _make_auth_error()

    with pytest.raises(AuthenticationError):
        with_retries(fn, sleep=sleeps.append, rand=lambda: 0.5)
    assert sleeps == []


def test_raises_after_max_attempts() -> None:
    attempts = {"n": 0}
    sleeps: list[float] = []

    def fn() -> str:
        attempts["n"] += 1
        raise _make_rate_limit_error()

    with pytest.raises(RateLimitError):
        with_retries(fn, sleep=sleeps.append, rand=lambda: 0.5)
    assert attempts["n"] == 5
    # Sleeps fire between attempts: 4 sleeps for 5 attempts.
    assert len(sleeps) == 4


def test_backoff_grows_exponentially_capped_at_max() -> None:
    """Initial 1s, doubling each retry, capped at max_delay. With rand=0.5 the
    jitter factor is exactly 1.0 so the values are deterministic for assertion."""
    sleeps: list[float] = []

    def fn() -> str:
        raise _make_rate_limit_error()

    with pytest.raises(RateLimitError):
        with_retries(
            fn,
            sleep=sleeps.append,
            rand=lambda: 0.5,
            initial_delay=1.0,
            max_delay=10.0,
        )
    # With jitter=1.0: waits are 1, 2, 4, 8 (cap not yet hit at attempt 5).
    assert sleeps == [1.0, 2.0, 4.0, 8.0]


def test_backoff_respects_max_delay() -> None:
    sleeps: list[float] = []

    def fn() -> str:
        raise _make_rate_limit_error()

    with pytest.raises(RateLimitError):
        with_retries(
            fn,
            sleep=sleeps.append,
            rand=lambda: 0.5,
            initial_delay=10.0,
            max_delay=5.0,
            max_attempts=4,
        )
    # initial=10 already exceeds max=5: every wait clamps to 5.
    assert sleeps == [5.0, 5.0, 5.0]


def _make_bad_request(message: str) -> BadRequestError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(400, request=request)
    return BadRequestError(message=message, response=response, body=None)


def _make_permission_error() -> PermissionDeniedError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(403, request=request)
    return PermissionDeniedError(message="not allowed", response=response, body=None)


@pytest.mark.parametrize(
    ("exc", "credit", "halt"),
    [
        (_make_bad_request("Your credit balance is too low to access the API."), True, True),
        (_make_auth_error(), False, True),
        (_make_permission_error(), False, True),
        # A 400 that is NOT about credit is a malformed request — the filing's problem.
        (_make_bad_request("messages.0: invalid role"), False, False),
        (_make_rate_limit_error(), False, False),
        (RuntimeError("schema rejected the model output"), False, False),
    ],
    ids=["credit", "auth", "permission", "malformed", "rate_limit", "content"],
)
def test_account_halt_classification(exc: BaseException, credit: bool, halt: bool) -> None:
    assert is_credit_exhausted(exc) is credit
    assert is_account_halt(exc) is halt


def test_account_halt_and_transient_are_disjoint() -> None:
    """A halt is not an outage: one waits on a human, the other resolves on its own, and the
    alert text and the dead-letter logic each depend on telling them apart."""
    for exc in (_make_bad_request("credit balance is too low"), _make_auth_error()):
        assert not is_retryable_error(exc)
