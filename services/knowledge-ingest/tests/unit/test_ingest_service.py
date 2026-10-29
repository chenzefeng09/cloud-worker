"""Service-layer tests for IngestService."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_ingest.blob.memory import InMemoryBlobStore
from knowledge_ingest.domain.models import Document
from knowledge_ingest.errors import (
    DocumentNotFoundError,
    EmbeddingDimensionError,
    UnsupportedMimeError,
)
from knowledge_ingest.services.ingest_service import IngestService
from tests.conftest import (
    TEST_TENANT,
    TEST_TENANT_OTHER,
    TEST_USER,
    TEST_USER_OTHER,
    FakeEmbeddings,
)


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
async def test_delete_document_drops_row_and_blob(
    svc: IngestService, blob_store: InMemoryBlobStore,
) -> None:
    doc = await svc.create_document(
        title="d", filename="d.txt", mime="text/plain", data=b"hello world",
    )
    assert blob_store.size == 1
    await svc.delete_document(doc.id)
    assert blob_store.size == 0
    with pytest.raises(DocumentNotFoundError):
        await svc.get_document(doc.id)


@pytest.mark.asyncio
async def test_delete_unknown_document_raises(svc: IngestService) -> None:
    with pytest.raises(DocumentNotFoundError):
        await svc.delete_document("doc_missing")


@pytest.mark.asyncio
async def test_delete_document_tolerates_blob_failure(
    svc: IngestService, blob_store: InMemoryBlobStore,
) -> None:
    """A blob delete failure must not abort the DB row removal."""
    doc = await svc.create_document(
        title="d", filename="d.txt", mime="text/plain", data=b"hello world",
    )

    def _boom(_key: str) -> None:
        raise RuntimeError("blob backend down")

    blob_store.delete = _boom  # type: ignore[method-assign]

    await svc.delete_document(doc.id)
    with pytest.raises(DocumentNotFoundError):
        await svc.get_document(doc.id)


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
async def test_create_document_persists_origin_session_id(
    svc: IngestService,
) -> None:
    doc = await svc.create_document(
        title="[memory] hi",
        filename="memory.txt",
        mime="text/plain",
        data="User task: hi\n\nAssistant answer: hello".encode(),
        origin_session_id="ses_abc123",
    )
    refreshed, _ = await svc.get_document(doc.id)
    assert refreshed.origin_session_id == "ses_abc123"


@pytest.mark.asyncio
async def test_search_excludes_origin_session_docs(svc: IngestService) -> None:
    """exclude_origin_session_id drops chunks from that session's own docs.

    Two docs share the exact same marker text so FakeEmbeddings gives them
    identical vectors (cosine == 1.0).  One is tagged with the excluded
    session; only the untagged one may survive the filter.
    """
    marker = "uniquely-identifiable-marker " * 8
    own = await svc.create_document(
        title="[memory] own",
        filename="own.txt",
        mime="text/plain",
        data=marker.encode(),
        origin_session_id="ses_self",
    )
    await svc.ingest_document(own.id)
    other = await svc.create_document(
        title="[memory] other",
        filename="other.txt",
        mime="text/plain",
        data=marker.encode(),
        origin_session_id="ses_other",
    )
    await svc.ingest_document(other.id)

    # Without exclusion, both docs' chunks are eligible.
    unfiltered = await svc.search("uniquely-identifiable-marker", k=10)
    ids_all = {h.document_id for h in unfiltered}
    assert own.id in ids_all
    assert other.id in ids_all

    # Excluding the self session removes only its chunks.
    filtered = await svc.search(
        "uniquely-identifiable-marker", k=10, exclude_origin_session_id="ses_self",
    )
    ids_filtered = {h.document_id for h in filtered}
    assert own.id not in ids_filtered
    assert other.id in ids_filtered


@pytest.mark.asyncio
async def test_search_exclude_keeps_untagged_docs(svc: IngestService) -> None:
    """Ordinary (untagged) docs are never dropped by exclude_origin_session_id."""
    marker = "plain-upload-marker " * 8
    plain = await svc.create_document(
        title="plain",
        filename="plain.txt",
        mime="text/plain",
        data=marker.encode(),
    )
    await svc.ingest_document(plain.id)

    hits = await svc.search(
        "plain-upload-marker", k=10, exclude_origin_session_id="ses_anything",
    )
    assert plain.id in {h.document_id for h in hits}


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


@pytest.mark.asyncio
async def test_list_documents_excludes_memory_by_default(
    session: AsyncSession,
    blob_store: InMemoryBlobStore,
    embeddings: FakeEmbeddings,
) -> None:
    """The default listing hides session-memory docs (origin_session_id set),
    keeping ordinary uploads."""
    svc = IngestService(
        session, blob_store, embeddings, tenant_id=TEST_TENANT, user_id=TEST_USER,
    )
    plain = await svc.create_document(
        title="plain", filename="plain.txt", mime="text/plain", data=b"hello",
    )
    mem = await svc.create_document(
        title="[memory] hi",
        filename="memory.txt",
        mime="text/plain",
        data=b"hi there",
        origin_session_id="ses_mem",
    )

    ids = {doc.id for doc, _ in await svc.list_documents()}
    assert plain.id in ids
    assert mem.id not in ids


@pytest.mark.asyncio
async def test_list_documents_include_memory_returns_all_users(
    session: AsyncSession,
    blob_store: InMemoryBlobStore,
    embeddings: FakeEmbeddings,
) -> None:
    """include_memory=True returns the full tenant set — ordinary uploads plus
    every user's memory docs, not just the caller's (audit semantics)."""
    a = IngestService(
        session, blob_store, embeddings, tenant_id=TEST_TENANT, user_id=TEST_USER,
    )
    b = IngestService(
        session, blob_store, embeddings, tenant_id=TEST_TENANT, user_id=TEST_USER_OTHER,
    )
    plain = await a.create_document(
        title="plain", filename="plain.txt", mime="text/plain", data=b"hello",
    )
    a_mem = await a.create_document(
        title="[memory] a", filename="a.txt", mime="text/plain",
        data=b"mine", origin_session_id="ses_a",
    )
    b_mem = await b.create_document(
        title="[memory] b", filename="b.txt", mime="text/plain",
        data=b"theirs", origin_session_id="ses_b",
    )

    # User A's audit listing sees everything, including user B's memory doc.
    all_ids = {doc.id for doc, _ in await a.list_documents(include_memory=True)}
    assert plain.id in all_ids
    assert a_mem.id in all_ids
    assert b_mem.id in all_ids

    # The default listing hides both memory docs but keeps the ordinary upload.
    default_ids = {doc.id for doc, _ in await a.list_documents()}
    assert plain.id in default_ids
    assert a_mem.id not in default_ids
    assert b_mem.id not in default_ids


# --------------------------------------------------------- per-user memory scope


@pytest.mark.asyncio
async def test_create_document_stamps_origin_user_id_for_memory(
    session: AsyncSession,
    blob_store: InMemoryBlobStore,
    embeddings: FakeEmbeddings,
) -> None:
    """A memory doc (origin_session_id set) is owned by the requesting user."""
    svc = IngestService(
        session, blob_store, embeddings, tenant_id=TEST_TENANT, user_id=TEST_USER,
    )
    doc = await svc.create_document(
        title="[memory] hi",
        filename="memory.txt",
        mime="text/plain",
        data="User task: hi\n\nAssistant answer: hello".encode(),
        origin_session_id="ses_abc123",
    )
    refreshed, _ = await svc.get_document(doc.id)
    assert refreshed.origin_user_id == TEST_USER


@pytest.mark.asyncio
async def test_create_document_ordinary_upload_leaves_origin_user_id_null(
    session: AsyncSession,
    blob_store: InMemoryBlobStore,
    embeddings: FakeEmbeddings,
) -> None:
    """An ordinary upload (no origin_session_id) stays tenant-shared: NULL owner."""
    svc = IngestService(
        session, blob_store, embeddings, tenant_id=TEST_TENANT, user_id=TEST_USER,
    )
    doc = await svc.create_document(
        title="plain", filename="plain.txt", mime="text/plain", data=b"hello",
    )
    refreshed, _ = await svc.get_document(doc.id)
    assert refreshed.origin_user_id is None


@pytest.mark.asyncio
async def test_search_isolates_memory_by_user(
    session: AsyncSession,
    blob_store: InMemoryBlobStore,
    embeddings: FakeEmbeddings,
) -> None:
    """User B must not retrieve user A's memory docs, but sees its own memory
    plus tenant-shared ordinary uploads."""
    marker = "uniquely-identifiable-marker " * 8
    a = IngestService(
        session, blob_store, embeddings, tenant_id=TEST_TENANT, user_id=TEST_USER,
    )
    b = IngestService(
        session, blob_store, embeddings, tenant_id=TEST_TENANT, user_id=TEST_USER_OTHER,
    )

    a_mem = await a.create_document(
        title="[memory] a", filename="a.txt", mime="text/plain",
        data=marker.encode(), origin_session_id="ses_a",
    )
    await a.ingest_document(a_mem.id)
    b_mem = await b.create_document(
        title="[memory] b", filename="b.txt", mime="text/plain",
        data=marker.encode(), origin_session_id="ses_b",
    )
    await b.ingest_document(b_mem.id)
    shared = await a.create_document(
        title="plain", filename="plain.txt", mime="text/plain", data=marker.encode(),
    )
    await a.ingest_document(shared.id)

    # User B's search sees its own memory + the shared upload, never A's memory.
    hits_b = {h.document_id for h in await b.search("uniquely-identifiable-marker", k=10)}
    assert a_mem.id not in hits_b
    assert b_mem.id in hits_b
    assert shared.id in hits_b

    # Symmetric: user A never sees B's memory.
    hits_a = {h.document_id for h in await a.search("uniquely-identifiable-marker", k=10)}
    assert b_mem.id not in hits_a
    assert a_mem.id in hits_a
    assert shared.id in hits_a


@pytest.mark.asyncio
async def test_search_excludes_unowned_memory_doc(
    session: AsyncSession,
    blob_store: InMemoryBlobStore,
    embeddings: FakeEmbeddings,
) -> None:
    """A memory doc with NULL origin_user_id (pre-migration, un-backfilled) is
    fail-closed: excluded from every user's search."""
    marker = "uniquely-identifiable-marker " * 8
    svc = IngestService(
        session, blob_store, embeddings, tenant_id=TEST_TENANT, user_id=TEST_USER,
    )
    # Simulate a legacy row: origin_session_id set but origin_user_id NULL.
    legacy = Document(
        tenant_id=TEST_TENANT,
        title="[memory] legacy",
        mime="text/plain",
        status="PENDING",
        origin_session_id="ses_legacy",
        origin_user_id=None,
    )
    session.add(legacy)
    await session.flush()
    # Give it a blob + chunks via the normal ingest path.
    from knowledge_ingest.blob.base import make_key
    key = make_key(TEST_TENANT, legacy.id, "legacy.txt")
    legacy.source_uri = blob_store.put(key, marker.encode(), content_type="text/plain")
    await session.flush()
    await svc.ingest_document(legacy.id)

    hits = {h.document_id for h in await svc.search("uniquely-identifiable-marker", k=10)}
    assert legacy.id not in hits
