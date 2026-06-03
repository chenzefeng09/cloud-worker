"""HTTP embedding client backed by saas-foundation llm-gateway.

The gateway exposes an OpenAI-compatible ``POST /v1/embeddings`` endpoint;
we use the SDK's standard ``httpx.AsyncClient`` rather than the
foundation-sdk because the contract is openly OpenAI-shaped and pulling
in the SDK's typed client for one call is overkill.
"""

from __future__ import annotations

import httpx


class GatewayEmbeddingClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._client = client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            resp = await client.post(
                f"{self._base_url}/embeddings",
                json={"model": self._model, "input": texts},
            )
            resp.raise_for_status()
            payload = resp.json()
            return [item["embedding"] for item in payload["data"]]
        finally:
            if owns_client:
                await client.aclose()
