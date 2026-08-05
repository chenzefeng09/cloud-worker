"""Folder CRUD endpoints for the knowledge library.

Folders form a tenant-scoped tree (``parent_id`` self-reference, NULL == root).
Writes reuse the ``document.*`` RBAC family (folder management is a tenant-admin
operation in v1, same audience as uploads); reads use ``document.read`` so
ordinary users can browse the tree.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_ingest.api.authz_guard import require_permission
from knowledge_ingest.api.dependencies import (
    TBlobStore,
    TDbSession,
    TEmbeddings,
    TTenantId,
    TUserId,
)
from knowledge_ingest.blob.base import BlobStore
from knowledge_ingest.embeddings.base import EmbeddingClient
from knowledge_ingest.schemas.document import (
    FolderCreate,
    FolderList,
    FolderOut,
    FolderUpdate,
)
from knowledge_ingest.services.ingest_service import _UNSET, IngestService

router = APIRouter(tags=["Folders"])


def _make_service(
    request: Request,
    db: AsyncSession,
    blob: BlobStore,
    embeddings: EmbeddingClient,
    tenant_id: str,
    user_id: str,
) -> IngestService:
    settings = request.app.state.settings
    return IngestService(
        db,
        blob,
        embeddings,
        tenant_id=tenant_id,
        user_id=user_id,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        search_max_distance=settings.knowledge_search_max_distance,
    )


@router.get("/folders", response_model=FolderList,
            dependencies=[Depends(require_permission("document.read", "document"))])
async def list_folders(
    request: Request,
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    embeddings: EmbeddingClient = TEmbeddings,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> FolderList:
    """Flat list of the tenant's folders (the client assembles the tree)."""
    svc = _make_service(request, db, blob, embeddings, tenant_id, user_id)
    folders = await svc.list_folders()
    items = [FolderOut.model_validate(f) for f in folders]
    return FolderList(items=items, total=len(items))


@router.post("/folders", response_model=FolderOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_permission("document.create", "document"))])
async def create_folder(
    request: Request,
    body: FolderCreate = Body(...),
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    embeddings: EmbeddingClient = TEmbeddings,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> FolderOut:
    svc = _make_service(request, db, blob, embeddings, tenant_id, user_id)
    folder = await svc.create_folder(
        name=body.name,
        parent_id=body.parent_id,
        default_sensitivity=body.default_sensitivity.value,
    )
    return FolderOut.model_validate(folder)


@router.patch("/folders/{folder_id}", response_model=FolderOut,
              dependencies=[Depends(require_permission("document.update", "document"))])
async def patch_folder(
    folder_id: str,
    request: Request,
    body: FolderUpdate = Body(...),
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    embeddings: EmbeddingClient = TEmbeddings,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> FolderOut:
    svc = _make_service(request, db, blob, embeddings, tenant_id, user_id)
    fields = body.model_fields_set
    folder = await svc.update_folder(
        folder_id,
        name=(body.name if "name" in fields else _UNSET),
        parent_id=(body.parent_id if "parent_id" in fields else _UNSET),
        default_sensitivity=(
            body.default_sensitivity.value
            if "default_sensitivity" in fields and body.default_sensitivity is not None
            else _UNSET
        ),
    )
    return FolderOut.model_validate(folder)


@router.delete("/folders/{folder_id}", status_code=status.HTTP_204_NO_CONTENT,
               dependencies=[Depends(require_permission("document.delete", "document"))])
async def delete_folder(
    folder_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    embeddings: EmbeddingClient = TEmbeddings,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> Response:
    """Delete a folder — 409 if it holds any sub-folder or document."""
    svc = _make_service(request, db, blob, embeddings, tenant_id, user_id)
    await svc.delete_folder(folder_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
