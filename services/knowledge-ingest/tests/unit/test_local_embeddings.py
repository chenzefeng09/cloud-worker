"""Unit tests for LocalEmbeddingClient."""

from __future__ import annotations

import math

import pytest

from knowledge_ingest.embeddings.local import LocalEmbeddingClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# Basic contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deterministic_same_text_same_vector() -> None:
    """Same text must always produce the exact same vector."""
    client = LocalEmbeddingClient(dim=1536)
    v1 = await client.embed(["请假"])
    v2 = await client.embed(["请假"])
    assert v1 == v2


@pytest.mark.asyncio
async def test_correct_dimension_default() -> None:
    client = LocalEmbeddingClient()
    vecs = await client.embed(["hello world"])
    assert len(vecs) == 1
    assert len(vecs[0]) == 1536


@pytest.mark.asyncio
async def test_correct_dimension_custom() -> None:
    dim = 128
    client = LocalEmbeddingClient(dim=dim)
    vecs = await client.embed(["test", "another"])
    assert all(len(v) == dim for v in vecs)


@pytest.mark.asyncio
async def test_l2_normalized() -> None:
    """Each output vector should be L2-normalised (norm ≈ 1.0)."""
    client = LocalEmbeddingClient(dim=256)
    texts = ["年假申请流程", "报销规定", "hello", "12345", ""]
    vecs = await client.embed(texts)
    for v in vecs:
        norm = math.sqrt(sum(x * x for x in v))
        assert abs(norm - 1.0) < 1e-9, f"norm={norm} — vector is not unit-normalised"


@pytest.mark.asyncio
async def test_empty_input_returns_empty() -> None:
    client = LocalEmbeddingClient(dim=64)
    result = await client.embed([])
    assert result == []


@pytest.mark.asyncio
async def test_batch_order_preserved() -> None:
    """Return order must match input order."""
    client = LocalEmbeddingClient(dim=64)
    texts = ["alpha", "beta", "gamma"]
    vecs = await client.embed(texts)
    singles = [await client.embed([t]) for t in texts]
    for i, (v, s) in enumerate(zip(vecs, singles)):
        assert v == s[0], f"vector at position {i} does not match single-embed result"


# ---------------------------------------------------------------------------
# Relevance: overlapping vocabulary → higher cosine similarity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_vocabulary_overlap_raises_similarity() -> None:
    """A query sharing CJK tokens with a chunk must score higher than an
    unrelated chunk does.

    query:        "请假政策"
    related:      "年假/请假报销申请流程"   ← shares 请 假 with query
    unrelated:    "系统升级维护通知"         ← no overlap
    """
    client = LocalEmbeddingClient(dim=1536)

    query = "请假政策"
    related = "年假/请假报销申请流程"
    unrelated = "系统升级维护通知"

    vecs = await client.embed([query, related, unrelated])
    q_vec, rel_vec, unrel_vec = vecs

    sim_related = cosine(q_vec, rel_vec)
    sim_unrelated = cosine(q_vec, unrel_vec)

    assert sim_related > sim_unrelated, (
        f"Expected related ({sim_related:.4f}) > unrelated ({sim_unrelated:.4f}), "
        "but vocabulary-overlap heuristic did not rank them correctly."
    )


@pytest.mark.asyncio
async def test_identical_text_cosine_is_one() -> None:
    """Identical text should have cosine similarity of 1.0."""
    client = LocalEmbeddingClient(dim=512)
    text = "知识库检索测试"
    vecs = await client.embed([text, text])
    sim = cosine(vecs[0], vecs[1])
    assert abs(sim - 1.0) < 1e-9
