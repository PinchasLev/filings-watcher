"""Embedding provider for change-detection block vectors (ADR 0042, PR 3).

Turns risk-factor block text into vectors so a later diff can compare blocks by
meaning rather than by keywords. Uses Voyage (Anthropic's partnered embedding
provider); the finance-tuned model separates SEC-filing prose better than a
general one. The `Embedder` protocol keeps the CLI and tests decoupled from the
HTTP client — a fake embedder drops in for tests, and a different provider is a
new class, not a rewrite.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx

# The finance-domain-tuned Voyage model — tuned for financial/legal prose, which is
# exactly what SEC risk factors are. Overridable via config so a general model can
# be A/B'd (embeddings key by model_id, so both can coexist in the store).
DEFAULT_MODEL = "voyage-finance-2"

_VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"

# Voyage returns 429 when a per-minute rate/token limit is hit, and 5xx on transient
# upstream failures. Both are worth retrying — the difference between the embed
# reconciler draining a backlog and dying on a busy minute.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 529})
_RETRYABLE_NETWORK: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.PoolTimeout,
)


class Embedder(Protocol):
    """Anything that turns a batch of texts into vectors, tagged by its model id."""

    model_id: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class VoyageEmbedder:
    """Embed text via the Voyage API. One `embed` call is one batched request.

    Both sides of a change-detection diff are documents (this year's block vs last
    year's), so every input is embedded with input_type="document" — the symmetric,
    corpus-to-corpus setting, not the asymmetric query/document retrieval setting.
    """

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        client: httpx.Client | None = None,
        timeout: float = 60.0,
        max_attempts: int = 5,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model_id = model
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=timeout)
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._sleep = sleep

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = self._post(texts)
        # Voyage tags each item with its input index; sort to restore input order.
        items = sorted(payload["data"], key=lambda d: int(d["index"]))
        return [[float(x) for x in item["embedding"]] for item in items]

    def _post(self, texts: list[str]) -> dict[str, Any]:
        """POST one batch with exponential backoff on rate-limit / transient errors.

        Voyage's per-minute rate and token limits surface as 429; retrying with
        backoff is what lets the embed reconciler drain a backlog instead of failing
        the tick on a busy minute. Non-retryable errors (auth, bad request) raise
        immediately; the final retryable failure propagates and the reconciler leaves
        the batch for the next run.
        """
        last_exc: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                response = self._client.post(
                    _VOYAGE_URL,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"input": texts, "model": self.model_id, "input_type": "document"},
                )
                response.raise_for_status()
                result: dict[str, Any] = response.json()
                return result
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in _RETRYABLE_STATUS:
                    raise
                last_exc = exc
            except _RETRYABLE_NETWORK as exc:
                last_exc = exc
            if attempt < self._max_attempts - 1:
                self._sleep(min(self._base_delay * 2**attempt, self._max_delay))
        assert last_exc is not None  # loop ran at least once
        raise last_exc
