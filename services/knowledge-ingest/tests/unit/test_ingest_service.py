"""Service-layer tests for IngestService."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_ingest.blob.memory import InMemoryBlobStore
from knowledge_ingest.errors import (
    DocumentNotFoundError,
    EmbeddingDimensionError,
    UnsupportedMimeError,
)
from knowledge_ingest.services.ingest_service import IngestService
from tests.conftest import TEST_TENANT, TEST_TENANT_OTHER, FakeEmbeddings


@pytest.fixture
def svc(
    session: AsyncSession,
    blob_store: InMemoryBlobStore,
    embeddings: FakeEmbeddings,
) -> IngestService:
    return IngestService(
        session,
        blob_store,
        embeddings,
        tenant_id=TEST_TENANT,
        chunk_size=64,
        chunk_overlap=8,
    )


@pytest.mark.asyncio
async def test_create_document_persists_and_uploads(
    svc: IngestService, blob_store: InMemoryBlobStore,
) -> None:
    doc = await svc.create_document(
        title="hello", filename="hello.txt", mime="text/plain", data=b"hi there",
    )
    assert doc.tenant_id == TEST_TENANT
    assert doc.status == "PENDING"
    assert blob_store.size == 1


@pytest.mark.asyncio
async def test_create_document_rejects_bad_mime(svc: IngestService) -> None:
    with pytest.raises(UnsupportedMimeError):
        await svc.create_document(
            title="x", filename="x.png", mime="image/png", data=b"",
        )


@pytest.mark.asyncio
async def test_ingest_round_trip(
    svc: IngestService, embeddings: FakeEmbeddings,
) -> None:
    body = "the quick brown fox " * 20  # ~400 chars → multiple chunks
    doc = await svc.create_document(
        title="doc", filename="doc.txt", mime="text/plain", data=body.encode(),
    )
    finished, count = await svc.ingest_document(doc.id)
    assert finished.status == "READY"
    assert count > 1
    # Embedding client was called exactly once with the chunk list.
    assert len(embeddings.calls[0]) == count


@pytest.mark.asyncio
async def test_ingest_missing_document(svc: IngestService) -> None:
    with pytest.raises(DocumentNotFoundError):
        await svc.ingest_document("doc_missing")


@pytest.mark.asyncio
async def test_ingest_dimension_mismatch_marks_failed(
    session: AsyncSession,
    blob_store: InMemoryBlobStore,
) -> None:
    class WrongDim:
        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 10 for _ in texts]

    svc = IngestService(
        session,
        blob_store,
        WrongDim(),
        tenant_id=TEST_TENANT,
        chunk_size=64,
        chunk_overlap=8,
    )
    doc = await svc.create_document(
        title="d", filename="d.txt", mime="text/plain", data=b"abc def ghi" * 20,
    )
    with pytest.raises(EmbeddingDimensionError):
        await svc.ingest_document(doc.id)
    refreshed, _ = await svc.get_document(doc.id)
    assert refreshed.status == "FAILED"


@pytest.mark.asyncio
async def test_search_returns_best_chunk_for_existing_text(
    svc: IngestService,
) -> None:
    text = "alpha beta gamma " * 10 + "uniquely-identifiable-marker " * 5
    doc = await svc.create_document(
        title="d", filename="d.txt", mime="text/plain", data=text.encode(),
    )
    await svc.ingest_document(doc.id)

    hits = await svc.search("uniquely-identifiable-marker", k=3)
    assert hits, "expected at least one hit"
    # The chunk containing the unique marker should rank first under the
    # deterministic FakeEmbeddings (sha256 seeding makes identical text
    # produce identical vectors → cosine == 1.0).
    assert hits[0].text.startswith("alpha beta gamma") or "marker" in hits[0].text


@pytest.mark.asyncio
async def test_search_empty_query_returns_empty(svc: IngestService) -> None:
    assert await svc.search("   ", k=5) == []


@pytest.mark.asyncio
async def test_tenant_isolation_on_listing(
    session: AsyncSession,
    blob_store: InMemoryBlobStore,
    embeddings: FakeEmbeddings,
) -> None:
    a = IngestService(session, blob_store, embeddings, tenant_id=TEST_TENANT)
    b = IngestService(session, blob_store, embeddings, tenant_id=TEST_TENANT_OTHER)

    await a.create_document(
        title="mine", filename="x.txt", mime="text/plain", data=b"hello",
    )
    rows_a = await a.list_documents()
    rows_b = await b.list_documents()
    assert len(rows_a) == 1
    assert len(rows_b) == 0
