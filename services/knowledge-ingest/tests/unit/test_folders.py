"""Service-layer tests for folders, sensitivity, scope and the distance gate."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_ingest.blob.memory import InMemoryBlobStore
from knowledge_ingest.errors import (
    FolderCycleError,
    FolderNameConflictError,
    FolderNotEmptyError,
    FolderNotFoundError,
    InvalidSensitivityError,
)
from knowledge_ingest.services.ingest_service import IngestService
from tests.conftest import (
    TEST_TENANT,
    TEST_USER,
    TEST_USER_OTHER,
    FakeEmbeddings,
)


def _svc(
    session: AsyncSession,
    blob_store: InMemoryBlobStore,
    embeddings: FakeEmbeddings,
    *,
    user_id: str = TEST_USER,
    search_max_distance: float = 0.6,
) -> IngestService:
    return IngestService(
        session,
        blob_store,
        embeddings,
        tenant_id=TEST_TENANT,
        user_id=user_id,
        chunk_size=64,
        chunk_overlap=8,
        search_max_distance=search_max_distance,
    )


# ------------------------------------------------------------- folder CRUD


@pytest.mark.asyncio
async def test_create_and_list_folders(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings)
    f = await svc.create_folder(name="产品", default_sensitivity="L2")
    assert f.name == "产品"
    assert f.default_sensitivity == "L2"
    assert f.created_by_user_id == TEST_USER
    listed = await svc.list_folders()
    assert [x.id for x in listed] == [f.id]


@pytest.mark.asyncio
async def test_create_folder_same_parent_duplicate_name_conflicts(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings)
    await svc.create_folder(name="dup")
    with pytest.raises(FolderNameConflictError):
        await svc.create_folder(name="dup")


@pytest.mark.asyncio
async def test_create_folder_same_name_different_parent_ok(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings)
    root = await svc.create_folder(name="root")
    child_a = await svc.create_folder(name="same", parent_id=root.id)
    # Same name under a *different* parent (the real root) is allowed.
    child_b = await svc.create_folder(name="same")
    assert child_a.id != child_b.id


@pytest.mark.asyncio
async def test_create_folder_unknown_parent_404(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings)
    with pytest.raises(FolderNotFoundError):
        await svc.create_folder(name="x", parent_id="fld_missing")


@pytest.mark.asyncio
async def test_create_folder_invalid_sensitivity(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings)
    with pytest.raises(InvalidSensitivityError):
        await svc.create_folder(name="x", default_sensitivity="L9")


@pytest.mark.asyncio
async def test_update_folder_rename_and_conflict(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings)
    a = await svc.create_folder(name="a")
    b = await svc.create_folder(name="b")
    renamed = await svc.update_folder(a.id, name="a2")
    assert renamed.name == "a2"
    # Renaming b to a2 (now a sibling name) collides.
    with pytest.raises(FolderNameConflictError):
        await svc.update_folder(b.id, name="a2")


@pytest.mark.asyncio
async def test_update_folder_move_and_cycle_guards(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings)
    parent = await svc.create_folder(name="parent")
    child = await svc.create_folder(name="child", parent_id=parent.id)
    grand = await svc.create_folder(name="grand", parent_id=child.id)

    # Move child under itself → cycle.
    with pytest.raises(FolderCycleError):
        await svc.update_folder(child.id, parent_id=child.id)
    # Move parent under its own grandchild → cycle.
    with pytest.raises(FolderCycleError):
        await svc.update_folder(parent.id, parent_id=grand.id)
    # Legit move: grand → root.
    moved = await svc.update_folder(grand.id, parent_id=None)
    assert moved.parent_id is None


@pytest.mark.asyncio
async def test_update_folder_default_sensitivity(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings)
    f = await svc.create_folder(name="f", default_sensitivity="L1")
    updated = await svc.update_folder(f.id, default_sensitivity="L4")
    assert updated.default_sensitivity == "L4"


@pytest.mark.asyncio
async def test_delete_folder_empty_ok_nonempty_409(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings)
    parent = await svc.create_folder(name="parent")
    child = await svc.create_folder(name="child", parent_id=parent.id)

    # Parent has a sub-folder → not empty.
    with pytest.raises(FolderNotEmptyError):
        await svc.delete_folder(parent.id)

    # Put a document in the child → not empty either.
    await svc.create_document(
        title="d", filename="d.txt", mime="text/plain", data=b"hello", folder_id=child.id,
    )
    with pytest.raises(FolderNotEmptyError):
        await svc.delete_folder(child.id)

    # Unknown folder → 404.
    with pytest.raises(FolderNotFoundError):
        await svc.delete_folder("fld_missing")


@pytest.mark.asyncio
async def test_delete_folder_becomes_empty_ok(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings)
    f = await svc.create_folder(name="tmp")
    await svc.delete_folder(f.id)
    assert await svc.list_folders() == []


# ------------------------------------------------------ document sensitivity


@pytest.mark.asyncio
async def test_upload_inherits_folder_default_sensitivity(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings)
    folder = await svc.create_folder(name="secret", default_sensitivity="L3")
    doc = await svc.create_document(
        title="d", filename="d.txt", mime="text/plain", data=b"hi", folder_id=folder.id,
    )
    assert doc.sensitivity == "L3"
    assert doc.folder_id == folder.id
    assert doc.uploaded_by_user_id == TEST_USER


@pytest.mark.asyncio
async def test_upload_explicit_sensitivity_overrides_folder_default(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings)
    folder = await svc.create_folder(name="secret", default_sensitivity="L3")
    # Explicit lower value is respected on upload (design: default only fills
    # when omitted).
    doc = await svc.create_document(
        title="d", filename="d.txt", mime="text/plain", data=b"hi",
        folder_id=folder.id, sensitivity="L1",
    )
    assert doc.sensitivity == "L1"


@pytest.mark.asyncio
async def test_upload_unknown_folder_404(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings)
    with pytest.raises(FolderNotFoundError):
        await svc.create_document(
            title="d", filename="d.txt", mime="text/plain", data=b"hi",
            folder_id="fld_missing",
        )


@pytest.mark.asyncio
async def test_move_elevates_only(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings)
    high = await svc.create_folder(name="high", default_sensitivity="L4")
    low = await svc.create_folder(name="low", default_sensitivity="L1")

    doc = await svc.create_document(
        title="d", filename="d.txt", mime="text/plain", data=b"hi",
    )  # root → L1
    assert doc.sensitivity == "L1"

    # Move into the L4 folder → elevated.
    moved, _ = await svc.update_document(doc.id, folder_id=high.id)
    assert moved.sensitivity == "L4"

    # Move into the L1 folder → NOT lowered (only elevate).
    moved2, _ = await svc.update_document(doc.id, folder_id=low.id)
    assert moved2.sensitivity == "L4"


@pytest.mark.asyncio
async def test_explicit_patch_can_downgrade(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings)
    doc = await svc.create_document(
        title="d", filename="d.txt", mime="text/plain", data=b"hi", sensitivity="L4",
    )
    downgraded, _ = await svc.update_document(doc.id, sensitivity="L2")
    assert downgraded.sensitivity == "L2"


@pytest.mark.asyncio
async def test_move_to_root_clears_folder(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings)
    folder = await svc.create_folder(name="f", default_sensitivity="L2")
    doc = await svc.create_document(
        title="d", filename="d.txt", mime="text/plain", data=b"hi", folder_id=folder.id,
    )
    moved, _ = await svc.update_document(doc.id, folder_id=None)
    assert moved.folder_id is None
    assert moved.sensitivity == "L2"  # unchanged by move-to-root


# --------------------------------------------------------------- search gates


async def _ingest(svc: IngestService, *, title: str, marker: str, **kw: object) -> str:
    doc = await svc.create_document(
        title=title, filename="d.txt", mime="text/plain",
        data=(f"{marker} " * 8).encode(), **kw,  # type: ignore[arg-type]
    )
    await svc.ingest_document(doc.id)
    return doc.id


@pytest.mark.asyncio
async def test_search_distance_threshold_drops_unrelated(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings)
    doc_id = await _ingest(svc, title="fruit", marker="banana-token")

    # Exact token → distance 0 → within the 0.6 gate.
    near = await svc.search("banana-token", k=5)
    assert doc_id in {h.document_id for h in near}

    # Disjoint token → distance ≈ 1.0 → dropped.
    far = await svc.search("coconut-token", k=5)
    assert far == []


@pytest.mark.asyncio
async def test_search_tight_threshold_drops_partial(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    # A very tight gate keeps only near-identical directions.
    svc = _svc(session, blob_store, embeddings, search_max_distance=0.01)
    doc_id = await _ingest(svc, title="fruit", marker="banana-token")
    assert doc_id in {h.document_id for h in await svc.search("banana-token", k=5)}
    # Same doc, but a two-token query only partially overlaps → beyond 0.01.
    assert await svc.search("banana-token apple-token", k=5) == []


@pytest.mark.asyncio
async def test_search_clearance_filters_knowledge(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings)
    doc_id = await _ingest(svc, title="secret", marker="topsecret-token", sensitivity="L3")

    # L1 clearance cannot see an L3 knowledge doc.
    assert await svc.search("topsecret-token", k=5, scope="knowledge", clearance="L1") == []
    # L3 clearance can.
    hits = await svc.search("topsecret-token", k=5, scope="knowledge", clearance="L3")
    assert doc_id in {h.document_id for h in hits}


@pytest.mark.asyncio
async def test_search_scope_separates_knowledge_and_memory(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings, user_id=TEST_USER)
    marker = "sharedmarker-token"
    know_id = await _ingest(svc, title="know", marker=marker)
    mem_id = await _ingest(svc, title="[memory] m", marker=marker, origin_session_id="ses_x")

    know_hits = await svc.search(marker, k=10, scope="knowledge", clearance="L5")
    assert know_id in {h.document_id for h in know_hits}
    assert mem_id not in {h.document_id for h in know_hits}
    assert all(h.scope == "knowledge" for h in know_hits)

    mem_hits = await svc.search(marker, k=10, scope="memory")
    assert mem_id in {h.document_id for h in mem_hits}
    assert know_id not in {h.document_id for h in mem_hits}
    assert all(h.scope == "memory" for h in mem_hits)

    all_hits = await svc.search(marker, k=10, scope="all", clearance="L5")
    ids = {h.document_id for h in all_hits}
    assert know_id in ids and mem_id in ids


@pytest.mark.asyncio
async def test_search_hit_carries_name_and_folder_path(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings)
    top = await svc.create_folder(name="产品", default_sensitivity="L1")
    sub = await svc.create_folder(name="合同模板", parent_id=top.id, default_sensitivity="L1")
    doc = await svc.create_document(
        title="模板A", filename="d.txt", mime="text/plain",
        data=(b"pathmarker-token " * 8), folder_id=sub.id,
    )
    await svc.ingest_document(doc.id)

    hits = await svc.search("pathmarker-token", k=5)
    assert hits
    hit = next(h for h in hits if h.document_id == doc.id)
    assert hit.document_name == "模板A"
    assert hit.folder_path == "产品/合同模板"
    assert hit.scope == "knowledge"


# ------------------------------------------------------ list folder/clearance


@pytest.mark.asyncio
async def test_list_documents_folder_and_clearance_filters(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings)
    folder = await svc.create_folder(name="f", default_sensitivity="L1")
    in_folder = await svc.create_document(
        title="in", filename="a.txt", mime="text/plain", data=b"hi", folder_id=folder.id,
    )
    at_root_high = await svc.create_document(
        title="root-high", filename="b.txt", mime="text/plain", data=b"hi", sensitivity="L4",
    )

    # folder_id filter.
    ids_in = {d.id for d, _ in await svc.list_documents(folder_id=folder.id)}
    assert ids_in == {in_folder.id}
    ids_root = {d.id for d, _ in await svc.list_documents(folder_id=None)}
    assert at_root_high.id in ids_root
    assert in_folder.id not in ids_root

    # No clearance → complete listing (both visible).
    ids_all = {d.id for d, _ in await svc.list_documents()}
    assert {in_folder.id, at_root_high.id} <= ids_all

    # L1 clearance hides the L4 doc.
    ids_l1 = {d.id for d, _ in await svc.list_documents(clearance="L1")}
    assert in_folder.id in ids_l1
    assert at_root_high.id not in ids_l1


@pytest.mark.asyncio
async def test_search_memory_stays_isolated_per_user(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    """scope=all still isolates memory by owner while sharing knowledge."""
    a = _svc(session, blob_store, embeddings, user_id=TEST_USER)
    b = _svc(session, blob_store, embeddings, user_id=TEST_USER_OTHER)
    marker = "isolation-token"
    a_mem = await _ingest(a, title="[memory] a", marker=marker, origin_session_id="ses_a")
    b_mem = await _ingest(b, title="[memory] b", marker=marker, origin_session_id="ses_b")

    ids_b = {h.document_id for h in await b.search(marker, k=10, scope="all", clearance="L5")}
    assert b_mem in ids_b
    assert a_mem not in ids_b
