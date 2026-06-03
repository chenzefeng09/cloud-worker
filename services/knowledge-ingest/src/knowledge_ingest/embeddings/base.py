"""Embedding-client protocol."""

from __future__ import annotations

from typing import Protocol


class EmbeddingClient(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per text, in input order."""
        ...
