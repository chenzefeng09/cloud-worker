"""Embeddings — Protocol + foundation llm-gateway client + local fallback."""

from __future__ import annotations

from knowledge_ingest.embeddings.base import EmbeddingClient
from knowledge_ingest.embeddings.gateway import GatewayEmbeddingClient
from knowledge_ingest.embeddings.local import LocalEmbeddingClient

__all__ = ["EmbeddingClient", "GatewayEmbeddingClient", "LocalEmbeddingClient"]
