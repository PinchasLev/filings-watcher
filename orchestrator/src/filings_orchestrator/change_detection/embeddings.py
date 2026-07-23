"""Embedding provider for change-detection block vectors (ADR 0042, PR 3).

Turns risk-factor block text into vectors so a later diff can compare blocks by
meaning rather than by keywords. Uses Voyage (Anthropic's partnered embedding
provider); the finance-tuned model separates SEC-filing prose better than a
general one. The `Embedder` protocol keeps the CLI and tests decoupled from the
HTTP client — a fake embedder drops in for tests, and a different provider is a
new class, not a rewrite.
"""

from __future__ import annotations

from typing import Protocol

import httpx

# The finance-domain-tuned Voyage model — tuned for financial/legal prose, which is
# exactly what SEC risk factors are. Overridable via config so a general model can
# be A/B'd (embeddings key by model_id, so both can coexist in the store).
DEFAULT_MODEL = "voyage-finance-2"

_VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"


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
    ) -> None:
        self.model_id = model
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=timeout)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self._client.post(
            _VOYAGE_URL,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={"input": texts, "model": self.model_id, "input_type": "document"},
        )
        response.raise_for_status()
        payload = response.json()
        # Voyage tags each item with its input index; sort to restore input order.
        items = sorted(payload["data"], key=lambda d: int(d["index"]))
        return [[float(x) for x in item["embedding"]] for item in items]
