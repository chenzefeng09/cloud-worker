"""Session-memory endpoints — GDPR self-service and admin fulfilment.

Memory documents are the run-completion "[memory] …" docs the
session-orchestrator writes (``origin_session_id`` set, ``origin_user_id`` =
the owning user).  The ordinary library never shows them, so these routes are
the only way for a user to see and erase what the platform remembers.

Self-service (``X-Tenant-Id`` / ``X-User-Id``, same RBAC guard as the document
routes) — always confined to the caller's own memory docs:

    GET    /memories                          → {"items": [...]}
    DELETE /memories/{memory_id}              → 204 (not the caller's → 404)
    DELETE /memories?origin_session_id=ses_…  → {"deleted": n}
    DELETE /memories?all=true                 → {"deleted": n}

The gate is ``document.read`` on purpose: every tenant member holds it, and
erasing your own personal data must not require an admin-only grant
(GDPR Art. 17).  The owner filter, not the permission, is what confines a
caller to their own documents.

One widening: a session-scoped purge sent with ``X-Actor: ADMIN`` removes every
memory doc of that session regardless of owner (legacy un-attributed rows
included).  It lets the orchestrator clean up after an admin-initiated or
retention-driven session delete, where the caller is not the session owner.

Admin fulfilment (``X-Actor: ADMIN`` required; the BFF has already enforced a
tenant-admin permission) — scoped to the data subject in the path, still
bounded by ``X-Tenant-Id``:

    GET    /internal/privacy/users/{user_id}/memories → {"items": [...]}
    DELETE /internal/privacy/users/{user_id}/memories → {"deleted": n}

Deleting a memory doc removes its chunks (and embeddings) and its source blob,
exactly like ``DELETE /documents/{id}``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_ingest.api.authz_guard import require_permission
from knowledge_ingest.api.dependencies import (
    TBlobStore,
    TDbSession,
    TEmbeddings,
    TTenantId,
    TUserId,
)
from knowledge_ingest.api.v1.documents import _make_service
from knowledge_ingest.blob.base import BlobStore
from knowledge_ingest.domain.models import Document
from knowledge_ingest.embeddings.base import EmbeddingClient
from knowledge_ingest.schemas.memory import DeletedCount, MemoryList, MemoryOut

router = APIRouter(tags=["Memories"])

_USER_ID_PATTERN = r"^usr_[0-9A-Z]{26}$"

_require_member = require_permission("document.read", "document")


def _is_admin(request: Request) -> bool:
    return request.headers.get("X-Actor") == "ADMIN"


async def _require_admin_actor(request: Request) -> None:
    if not _is_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ADMIN actor required",
        )


def _memory_list(rows: list[tuple[Document, str]]) -> MemoryList:
    return MemoryList(
        items=[
            MemoryOut(
                id=doc.id,
                title=doc.title,
                content=content,
                origin_session_id=doc.origin_session_id,
                created_at=doc.uploaded_at,
            )
            for doc, content in rows
        ]
    )


# ── Self-service ──────────────────────────────────────────────────────────────


@router.get("/memories", response_model=MemoryList, dependencies=[Depends(_require_member)])
async def list_memories(
    request: Request,
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    embeddings: EmbeddingClient = TEmbeddings,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> MemoryList:
    """The caller's own memory docs, newest first, with their text."""
    svc = _make_service(request, db, blob, embeddings, tenant_id, user_id)
    return _memory_list(await svc.list_memories(owner_user_id=user_id))


@router.delete(
    "/memories/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(_require_member)],
)
async def delete_memory(
    memory_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    embeddings: EmbeddingClient = TEmbeddings,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> Response:
    svc = _make_service(request, db, blob, embeddings, tenant_id, user_id)
    await svc.delete_memory(memory_id, owner_user_id=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/memories", response_model=DeletedCount, dependencies=[Depends(_require_member)])
async def delete_memories(
    request: Request,
    origin_session_id: str | None = None,
    all_memories: bool = Query(default=False, alias="all"),
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    embeddings: EmbeddingClient = TEmbeddings,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> DeletedCount:
    """Bulk-delete the caller's memory docs: one session's, or all of them."""
    session_id = (origin_session_id or "").strip() or None
    if (session_id is not None) == all_memories:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="pass exactly one of origin_session_id=<session id> or all=true",
        )
    svc = _make_service(request, db, blob, embeddings, tenant_id, user_id)
    if session_id is not None:
        owner = None if _is_admin(request) else user_id
        deleted = await svc.delete_memories(owner_user_id=owner, origin_session_id=session_id)
    else:
        deleted = await svc.delete_memories(owner_user_id=user_id)
    return DeletedCount(deleted=deleted)


# ── Admin fulfilment (DSAR) ───────────────────────────────────────────────────


@router.get(
    "/internal/privacy/users/{subject_user_id}/memories",
    response_model=MemoryList,
    dependencies=[Depends(_require_admin_actor)],
)
async def admin_list_user_memories(
    request: Request,
    subject_user_id: str = Path(pattern=_USER_ID_PATTERN),
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    embeddings: EmbeddingClient = TEmbeddings,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> MemoryList:
    svc = _make_service(request, db, blob, embeddings, tenant_id, user_id)
    return _memory_list(await svc.list_memories(owner_user_id=subject_user_id))


@router.delete(
    "/internal/privacy/users/{subject_user_id}/memories",
    response_model=DeletedCount,
    dependencies=[Depends(_require_admin_actor)],
)
async def admin_delete_user_memories(
    request: Request,
    subject_user_id: str = Path(pattern=_USER_ID_PATTERN),
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    embeddings: EmbeddingClient = TEmbeddings,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> DeletedCount:
    svc = _make_service(request, db, blob, embeddings, tenant_id, user_id)
    return DeletedCount(deleted=await svc.delete_memories(owner_user_id=subject_user_id))
