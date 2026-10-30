"""Session-memory routes (GDPR listing / erasure) and ``POST /search``."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_ingest.blob.base import make_key
from knowledge_ingest.blob.memory import InMemoryBlobStore
from knowledge_ingest.domain.models import Chunk, Document
from knowledge_ingest.errors import DocumentNotFoundError
from knowledge_ingest.services.ingest_service import IngestService
from tests.conftest import (
    TEST_TENANT,
    TEST_TENANT_OTHER,
    TEST_USER,
    TEST_USER_OTHER,
    FakeEmbeddings,
)

BASE = "/api/knowledge/v1"
ADMIN_ID = "usr_01J5TEST000000000000ADMIN1"


def _upload(
    client: TestClient,
    headers: dict[str, str],
    *,
    text: str,
    session_id: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Upload + ingest a document; a ``session_id`` makes it a memory doc."""
    data = {"title": title or ("[memory] note" if session_id else "upload"), "mime": "text/plain"}
    if session_id:
        data["origin_session_id"] = session_id
    resp = client.post(
        f"{BASE}/documents",
        headers=headers,
        data=data,
        files={"file": ("memory.txt", text.encode(), "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    doc = resp.json()
    ingest = client.post(f"{BASE}/documents/{doc['id']}/ingest", headers=headers)
    assert ingest.status_code == 200, ingest.text
    return doc


def _memory_ids(client: TestClient, headers: dict[str, str]) -> set[str]:
    resp = client.get(f"{BASE}/memories", headers=headers)
    assert resp.status_code == 200, resp.text
    return {item["id"] for item in resp.json()["items"]}


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"X-Tenant-Id": TEST_TENANT, "X-User-Id": ADMIN_ID, "X-Actor": "ADMIN"}


@pytest.fixture
def authz_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    denied = MagicMock(spec=httpx.Response)
    denied.status_code = 200
    denied.json = MagicMock(return_value={"allowed": False})

    class _Client:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def post(self, *a: Any, **kw: Any) -> MagicMock:
            return denied

    monkeypatch.setattr("knowledge_ingest.api.authz_guard.httpx.AsyncClient", _Client)


# ── GET /memories ─────────────────────────────────────────────────────────────


def test_list_memories_returns_only_the_callers_memory_docs(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    other_user_headers: dict[str, str],
    other_tenant_headers: dict[str, str],
) -> None:
    text = "User task: plan a trip\n\nAssistant answer: Lisbon in May"
    mine = _upload(app_client, tenant_headers, text=text, session_id="ses_a1")
    mine_too = _upload(app_client, tenant_headers, text="second memory", session_id="ses_a2")
    _upload(app_client, tenant_headers, text="an ordinary upload")
    _upload(app_client, other_user_headers, text="someone else", session_id="ses_b1")
    _upload(app_client, other_tenant_headers, text="other tenant", session_id="ses_c1")

    resp = app_client.get(f"{BASE}/memories", headers=tenant_headers)

    assert resp.status_code == 200, resp.text
    items = {item["id"]: item for item in resp.json()["items"]}
    assert set(items) == {mine["id"], mine_too["id"]}
    first = items[mine["id"]]
    assert first["content"] == text
    assert first["title"] == "[memory] note"
    assert first["origin_session_id"] == "ses_a1"
    assert first["created_at"]
    assert set(first) == {"id", "title", "content", "origin_session_id", "created_at"}


def test_list_memories_requires_tenant_and_user(app_client: TestClient) -> None:
    assert app_client.get(f"{BASE}/memories").status_code in (401, 403)
    resp = app_client.get(f"{BASE}/memories", headers={"X-Tenant-Id": TEST_TENANT})
    assert resp.status_code == 403


def test_memory_routes_enforce_rbac(
    app_client: TestClient, tenant_headers: dict[str, str], authz_denied: None,
) -> None:
    assert app_client.get(f"{BASE}/memories", headers=tenant_headers).status_code == 403
    assert app_client.delete(
        f"{BASE}/memories", params={"all": "true"}, headers=tenant_headers,
    ).status_code == 403
    assert app_client.delete(
        f"{BASE}/memories/doc_x", headers=tenant_headers,
    ).status_code == 403


# ── DELETE /memories/{id} ─────────────────────────────────────────────────────


def test_delete_memory_removes_document_chunks_and_blob(
    app_client: TestClient, tenant_headers: dict[str, str], blob_store: InMemoryBlobStore,
) -> None:
    marker = "uniquely-identifiable-marker " * 8
    doc = _upload(app_client, tenant_headers, text=marker, session_id="ses_del")
    assert blob_store.size == 1

    resp = app_client.delete(f"{BASE}/memories/{doc['id']}", headers=tenant_headers)

    assert resp.status_code == 204, resp.text
    assert blob_store.size == 0
    assert _memory_ids(app_client, tenant_headers) == set()
    detail = app_client.get(f"{BASE}/documents/{doc['id']}", headers=tenant_headers)
    assert detail.status_code == 404
    # Chunks/embeddings went with it: nothing is retrievable any more.
    hits = app_client.post(
        f"{BASE}/search",
        json={"q": "uniquely-identifiable-marker", "k": 10, "scope": "memory"},
        headers=tenant_headers,
    ).json()["hits"]
    assert hits == []


def test_delete_memory_of_another_user_or_an_upload_is_404(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    other_user_headers: dict[str, str],
) -> None:
    theirs = _upload(app_client, other_user_headers, text="their memory", session_id="ses_b")
    upload = _upload(app_client, tenant_headers, text="shared upload")

    assert app_client.delete(
        f"{BASE}/memories/{theirs['id']}", headers=tenant_headers,
    ).status_code == 404
    assert app_client.delete(
        f"{BASE}/memories/{upload['id']}", headers=tenant_headers,
    ).status_code == 404
    assert app_client.delete(
        f"{BASE}/memories/doc_missing", headers=tenant_headers,
    ).status_code == 404

    assert _memory_ids(app_client, other_user_headers) == {theirs["id"]}
    still_there = app_client.get(f"{BASE}/documents/{upload['id']}", headers=tenant_headers)
    assert still_there.status_code == 200


# ── DELETE /memories?… ────────────────────────────────────────────────────────


def test_delete_memories_by_session_is_owner_scoped(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    other_user_headers: dict[str, str],
) -> None:
    a1 = _upload(app_client, tenant_headers, text="one", session_id="ses_x")
    a2 = _upload(app_client, tenant_headers, text="two", session_id="ses_x")
    keep = _upload(app_client, tenant_headers, text="three", session_id="ses_y")
    theirs = _upload(app_client, other_user_headers, text="four", session_id="ses_x")

    resp = app_client.delete(
        f"{BASE}/memories", params={"origin_session_id": "ses_x"}, headers=tenant_headers,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 2}
    remaining = _memory_ids(app_client, tenant_headers)
    assert remaining == {keep["id"]}
    assert a1["id"] not in remaining and a2["id"] not in remaining
    assert _memory_ids(app_client, other_user_headers) == {theirs["id"]}


def test_admin_session_purge_removes_every_owners_memory(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    other_user_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    _upload(app_client, tenant_headers, text="one", session_id="ses_x")
    _upload(app_client, other_user_headers, text="two", session_id="ses_x")
    keep = _upload(app_client, tenant_headers, text="three", session_id="ses_y")

    resp = app_client.delete(
        f"{BASE}/memories", params={"origin_session_id": "ses_x"}, headers=admin_headers,
    )

    assert resp.json() == {"deleted": 2}
    assert _memory_ids(app_client, tenant_headers) == {keep["id"]}
    assert _memory_ids(app_client, other_user_headers) == set()


def test_delete_all_memories_spares_uploads_and_other_users(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    other_user_headers: dict[str, str],
) -> None:
    _upload(app_client, tenant_headers, text="one", session_id="ses_1")
    _upload(app_client, tenant_headers, text="two", session_id="ses_2")
    upload = _upload(app_client, tenant_headers, text="ordinary upload")
    theirs = _upload(app_client, other_user_headers, text="theirs", session_id="ses_3")

    resp = app_client.delete(f"{BASE}/memories", params={"all": "true"}, headers=tenant_headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 2}
    assert _memory_ids(app_client, tenant_headers) == set()
    assert _memory_ids(app_client, other_user_headers) == {theirs["id"]}
    listing = app_client.get(f"{BASE}/documents", headers=tenant_headers).json()
    assert {d["id"] for d in listing["items"]} == {upload["id"]}

    # Re-running is a no-op, not an error.
    again = app_client.delete(f"{BASE}/memories", params={"all": "true"}, headers=tenant_headers)
    assert again.json() == {"deleted": 0}


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"all": "false"},
        {"origin_session_id": "   "},
        {"origin_session_id": "ses_x", "all": "true"},
    ],
)
def test_delete_memories_needs_exactly_one_selector(
    app_client: TestClient, tenant_headers: dict[str, str], params: dict[str, str],
) -> None:
    _upload(app_client, tenant_headers, text="keep me", session_id="ses_x")
    resp = app_client.delete(f"{BASE}/memories", params=params, headers=tenant_headers)
    assert resp.status_code == 422, resp.text
    assert len(_memory_ids(app_client, tenant_headers)) == 1


# ── /internal/privacy/users/{user_id}/memories ────────────────────────────────


def test_internal_routes_require_the_admin_actor(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    url = f"{BASE}/internal/privacy/users/{TEST_USER}/memories"
    assert app_client.get(url, headers=tenant_headers).status_code == 403
    assert app_client.delete(url, headers=tenant_headers).status_code == 403


def test_internal_routes_validate_the_subject_id(
    app_client: TestClient, admin_headers: dict[str, str],
) -> None:
    url = f"{BASE}/internal/privacy/users/not-a-user/memories"
    assert app_client.get(url, headers=admin_headers).status_code == 422
    assert app_client.delete(url, headers=admin_headers).status_code == 422


def test_internal_list_and_erase_a_subjects_memories(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    other_user_headers: dict[str, str],
    other_tenant_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    text = "User task: a\n\nAssistant answer: b"
    mine = _upload(app_client, tenant_headers, text=text, session_id="ses_1")
    upload = _upload(app_client, tenant_headers, text="ordinary upload")
    theirs = _upload(app_client, other_user_headers, text="theirs", session_id="ses_2")
    elsewhere = _upload(app_client, other_tenant_headers, text="elsewhere", session_id="ses_3")
    url = f"{BASE}/internal/privacy/users/{TEST_USER}/memories"

    listing = app_client.get(url, headers=admin_headers)
    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    assert [i["id"] for i in items] == [mine["id"]]
    assert items[0]["content"] == text

    erased = app_client.delete(url, headers=admin_headers)
    assert erased.status_code == 200, erased.text
    assert erased.json() == {"deleted": 1}
    assert _memory_ids(app_client, tenant_headers) == set()
    assert _memory_ids(app_client, other_user_headers) == {theirs["id"]}
    assert _memory_ids(app_client, other_tenant_headers) == {elsewhere["id"]}
    assert app_client.get(
        f"{BASE}/documents/{upload['id']}", headers=tenant_headers,
    ).status_code == 200


# ── POST /search ──────────────────────────────────────────────────────────────


def test_post_search_matches_get_search(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    marker = "uniquely-identifiable-marker " * 8
    upload = _upload(app_client, tenant_headers, text=marker)
    memory = _upload(app_client, tenant_headers, text=marker, session_id="ses_self")

    for params in (
        {"q": "uniquely-identifiable-marker", "k": 10},
        {"q": "uniquely-identifiable-marker", "k": 10, "scope": "memory"},
        {"q": "uniquely-identifiable-marker", "k": 10, "scope": "knowledge"},
        {"q": "uniquely-identifiable-marker", "k": 1},
        {"q": "uniquely-identifiable-marker", "k": 10, "exclude_origin_session_id": "ses_self"},
    ):
        got = app_client.get(f"{BASE}/search", params=params, headers=tenant_headers)
        posted = app_client.post(f"{BASE}/search", json=params, headers=tenant_headers)
        assert got.status_code == posted.status_code == 200, posted.text
        assert posted.json() == got.json(), params

    everything = app_client.post(
        f"{BASE}/search", json={"q": "uniquely-identifiable-marker", "k": 10},
        headers=tenant_headers,
    ).json()["hits"]
    assert {h["document_id"] for h in everything} == {upload["id"], memory["id"]}
    excluded = app_client.post(
        f"{BASE}/search",
        json={"q": "uniquely-identifiable-marker", "exclude_origin_session_id": "ses_self"},
        headers=tenant_headers,
    ).json()["hits"]
    assert {h["document_id"] for h in excluded} == {upload["id"]}


def test_post_search_validates_its_body(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    assert app_client.post(f"{BASE}/search", json={}, headers=tenant_headers).status_code == 422
    assert app_client.post(
        f"{BASE}/search", json={"q": "x", "scope": "everything"}, headers=tenant_headers,
    ).status_code == 422
    empty = app_client.post(f"{BASE}/search", json={"q": "   "}, headers=tenant_headers)
    assert empty.status_code == 200
    assert empty.json() == {"hits": []}


def test_post_search_enforces_rbac(
    app_client: TestClient, tenant_headers: dict[str, str], authz_denied: None,
) -> None:
    resp = app_client.post(f"{BASE}/search", json={"q": "x"}, headers=tenant_headers)
    assert resp.status_code == 403


# ── Service layer ─────────────────────────────────────────────────────────────


def _svc(
    session: AsyncSession,
    blob_store: InMemoryBlobStore,
    embeddings: FakeEmbeddings,
    *,
    tenant_id: str = TEST_TENANT,
    user_id: str = TEST_USER,
) -> IngestService:
    return IngestService(
        session, blob_store, embeddings,
        tenant_id=tenant_id, user_id=user_id, chunk_size=64, chunk_overlap=8,
    )


async def _memory(svc: IngestService, text: str, session_id: str) -> Document:
    doc = await svc.create_document(
        title="[memory] x", filename="memory.txt", mime="text/plain",
        data=text.encode(), origin_session_id=session_id,
    )
    await svc.ingest_document(doc.id)
    return doc


async def _chunk_count(session: AsyncSession, document_id: str) -> int:
    stmt = select(func.count(Chunk.id)).where(Chunk.document_id == document_id)
    return int((await session.execute(stmt)).scalar_one())


@pytest.mark.asyncio
async def test_delete_memory_drops_chunks(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings)
    doc = await _memory(svc, "alpha beta gamma " * 20, "ses_1")
    assert await _chunk_count(session, doc.id) > 1

    await svc.delete_memory(doc.id, owner_user_id=TEST_USER)

    assert await _chunk_count(session, doc.id) == 0
    assert blob_store.size == 0
    with pytest.raises(DocumentNotFoundError):
        await svc.get_document(doc.id)


@pytest.mark.asyncio
async def test_delete_memory_refuses_other_owners(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    theirs = await _memory(_svc(session, blob_store, embeddings, user_id=TEST_USER_OTHER),
                           "their text", "ses_2")
    with pytest.raises(DocumentNotFoundError):
        await _svc(session, blob_store, embeddings).delete_memory(
            theirs.id, owner_user_id=TEST_USER,
        )
    assert await _chunk_count(session, theirs.id) > 0


@pytest.mark.asyncio
async def test_delete_memories_requires_a_scope(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    with pytest.raises(ValueError):
        await _svc(session, blob_store, embeddings).delete_memories()


@pytest.mark.asyncio
async def test_session_purge_reaches_unattributed_legacy_rows(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    """A legacy memory doc (NULL owner) survives owner-scoped erasure but not a
    session-scoped purge."""
    svc = _svc(session, blob_store, embeddings)
    legacy = Document(
        tenant_id=TEST_TENANT, title="[memory] legacy", mime="text/plain",
        status="PENDING", origin_session_id="ses_legacy", origin_user_id=None,
    )
    session.add(legacy)
    await session.flush()
    key = make_key(TEST_TENANT, legacy.id, "legacy.txt")
    legacy.source_uri = blob_store.put(key, b"legacy text", content_type="text/plain")
    await session.flush()
    await svc.ingest_document(legacy.id)

    assert await svc.delete_memories(owner_user_id=TEST_USER) == 0
    assert await svc.delete_memories(origin_session_id="ses_legacy") == 1
    assert await _chunk_count(session, legacy.id) == 0
    assert blob_store.size == 0


@pytest.mark.asyncio
async def test_memory_purge_is_tenant_bound(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    other_tenant = _svc(session, blob_store, embeddings, tenant_id=TEST_TENANT_OTHER)
    elsewhere = await _memory(other_tenant, "elsewhere", "ses_shared")

    svc = _svc(session, blob_store, embeddings)
    assert await svc.delete_memories(origin_session_id="ses_shared") == 0
    assert await svc.delete_memories(owner_user_id=TEST_USER) == 0
    assert [d.id for d, _ in await other_tenant.list_memories(owner_user_id=TEST_USER)] == [
        elsewhere.id
    ]


@pytest.mark.asyncio
async def test_list_memories_falls_back_to_chunk_text(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    svc = _svc(session, blob_store, embeddings)
    doc = await _memory(svc, "short memory text", "ses_1")

    def _unreadable(_key: str) -> bytes:
        raise RuntimeError("blob backend down")

    blob_store.get = _unreadable  # type: ignore[method-assign]

    [(listed, content)] = await svc.list_memories(owner_user_id=TEST_USER)
    assert listed.id == doc.id
    assert content == "short memory text"


@pytest.mark.asyncio
async def test_list_memories_is_newest_first(
    session: AsyncSession, blob_store: InMemoryBlobStore, embeddings: FakeEmbeddings,
) -> None:
    from datetime import UTC, datetime, timedelta

    svc = _svc(session, blob_store, embeddings)
    older = await _memory(svc, "older", "ses_1")
    newer = await _memory(svc, "newer", "ses_2")
    older.uploaded_at = datetime.now(UTC) - timedelta(days=1)
    await session.flush()

    rows = await svc.list_memories(owner_user_id=TEST_USER)
    assert [d.id for d, _ in rows] == [newer.id, older.id]
