"""Unit tests for GatewayEmbeddingClient (httpx mocked)."""

from __future__ import annotations

import httpx
import pytest

from knowledge_ingest.embeddings.gateway import GatewayEmbeddingClient


@pytest.mark.asyncio
async def test_gateway_embed_posts_to_correct_url() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read()
        return httpx.Response(
            200,
            json={"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://x")
    gateway = GatewayEmbeddingClient(
        base_url="http://llm/api/llm/v1", model="m1", client=client,
    )
    vecs = await gateway.embed(["a", "b"])
    assert vecs == [[0.1, 0.2], [0.3, 0.4]]
    assert seen["url"] == "http://llm/api/llm/v1/embeddings"


@pytest.mark.asyncio
async def test_gateway_empty_input_returns_empty() -> None:
    g = GatewayEmbeddingClient(base_url="http://x", model="m1")
    assert await g.embed([]) == []
