"""Deterministic local embedding client — no external dependencies.

Produces L2-normalised float vectors of the configured dimension from text
using token-overlap-aware hashing: each CJK character / Latin word is hashed
individually, and its hash energy is distributed across ``dim`` buckets, so
texts that share vocabulary get higher cosine similarity without any ML model.

This is suitable for local smoke-testing and CI; it is NOT a semantic model
and should never be used in production.
"""

from __future__ import annotations

import hashlib
import math
import re


def _tokens(text: str) -> list[str]:
    """Split text into CJK characters + Latin tokens (lowercased)."""
    tokens: list[str] = []
    # CJK unified ideographs + extensions — treat each char as its own token.
    for char in text:
        cp = ord(char)
        if (
            0x4E00 <= cp <= 0x9FFF  # CJK Unified
            or 0x3400 <= cp <= 0x4DBF  # Extension A
            or 0x20000 <= cp <= 0x2A6DF  # Extension B
            or 0x2A700 <= cp <= 0x2B73F  # Extension C
            or 0xF900 <= cp <= 0xFAFF  # Compatibility
        ):
            tokens.append(char)
    # Also grab Latin/digit words (lower-cased).
    tokens.extend(w.lower() for w in re.findall(r"[A-Za-z0-9]+", text))
    # Fall back to the whole text if nothing was found.
    if not tokens:
        tokens = [text]
    return tokens


def _hash_int(s: str) -> int:
    """SHA-256 of *s* → 64-bit unsigned integer."""
    digest = hashlib.sha256(s.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _embed_one(text: str, dim: int) -> list[float]:
    """Produce a deterministic, L2-normalised float vector of length *dim*."""
    vec = [0.0] * dim

    # 1. Whole-text fingerprint (catches exact duplicates).
    h = _hash_int(text)
    # Spread the whole-text hash into 8 evenly-spaced buckets.
    for k in range(8):
        idx = (h ^ (k * 0x9E3779B97F4A7C15)) % dim
        # Use alternating sign to avoid all positive accumulation.
        sign = 1.0 if (h >> (k * 7)) & 1 else -1.0
        vec[idx] += sign * 1.0

    # 2. Per-token contribution — the key to vocabulary-overlap similarity.
    tokens = _tokens(text)
    weight = 1.0 / max(len(tokens), 1)
    for token in tokens:
        th = _hash_int(token)
        # Distribute each token across 4 buckets.
        for k in range(4):
            idx = (th ^ (k * 0xBF58476D1CE4E5B9)) % dim
            sign = 1.0 if (th >> (k * 11)) & 1 else -1.0
            vec[idx] += sign * weight

    # 3. L2-normalise.
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class LocalEmbeddingClient:
    """Offline deterministic embedding client.

    Matches the :class:`~knowledge_ingest.embeddings.base.EmbeddingClient`
    protocol exactly (async, list[list[float]]).  Vectors are always of
    length *dim* and L2-normalised, so cosine similarity is meaningful.
    """

    def __init__(self, dim: int = 1536) -> None:
        self._dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [_embed_one(t, self._dim) for t in texts]
