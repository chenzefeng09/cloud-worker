"""Document upload, listing, and ingest endpoints."""

from __future__ import annotations

import math

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_ingest.api.authz_guard import require_permission
from knowledge_ingest.api.dependencies import (
    TBlobStore,
    TDbSession,
    TEmbeddings,
    TTenantId,
)
from knowledge_ingest.blob.base import BlobStore
from knowledge_ingest.embeddings.base import EmbeddingClient
from knowledge_ingest.schemas.common import ListPage
from knowledge_ingest.schemas.document import (
    DocumentOut,
    DocumentStatus,
    IngestResult,
    SearchResponse,
)
from knowledge_ingest.services.ingest_service import IngestService

router = APIRouter(tags=["Documents"])


def _make_service(
    request: Request,
    db: AsyncSession,
    blob: BlobStore,
    embeddings: EmbeddingClient,
    tenant_id: str,
) -> IngestService:
    settings = request.app.state.settings
    return IngestService(
        db,
        blob,
        embeddings,
        tenant_id=tenant_id,
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
    )


@router.get("/documents", response_model=ListPage,
            dependencies=[Depends(require_permission("document.read", "document"))])
async def list_documents(
    request: Request,
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    embeddings: EmbeddingClient = TEmbeddings,
    tenant_id: str = TTenantId,
) -> ListPage:
    svc = _make_service(request, db, blob, embeddings, tenant_id)
    rows = await svc.list_documents()
    items = []
    for doc, count in rows:
        out = DocumentOut.model_validate(doc).model_dump(mode="json")
        out["chunk_count"] = count
        items.append(out)
    return ListPage(items=items, total=len(items))


@router.post(
    "/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("document.create", "document"))],
)
async def upload_document(
    request: Request,
    title: str = Form(...),
    mime: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    embeddings: EmbeddingClient = TEmbeddings,
    tenant_id: str = TTenantId,
) -> DocumentOut:
    payload = await file.read()

    # Quota check: request at least 1 GB unit; ceiling of actual size.
    quota_guard = request.app.state.quota_guard
    requested_gb = max(1, math.ceil(len(payload) / (1024 ** 3)))
    await quota_guard.check(tenant_id=tenant_id, dimension="storage_gb", requested=requested_gb)

    svc = _make_service(request, db, blob, embeddings, tenant_id)
    doc = await svc.create_document(
        title=title,
        filename=file.filename or "upload.bin",
        mime=mime,
        data=payload,
    )

    # Record actual usage as a float GB value (best-effort).
    actual_gb = len(payload) / (1024 ** 3)
    await quota_guard.record(
        tenant_id=tenant_id,
        dimension="storage_gb",
        amount=actual_gb,
        description=f"upload document {doc.id}",
    )

    out = DocumentOut.model_validate(doc).model_dump(mode="json")
    out["chunk_count"] = 0
    return DocumentOut(**out)


@router.get("/documents/{document_id}", response_model=DocumentOut,
            dependencies=[Depends(require_permission("document.read", "document"))])
async def get_document(
    document_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    embeddings: EmbeddingClient = TEmbeddings,
    tenant_id: str = TTenantId,
) -> DocumentOut:
    svc = _make_service(request, db, blob, embeddings, tenant_id)
    doc, count = await svc.get_document(document_id)
    out = DocumentOut.model_validate(doc).model_dump(mode="json")
    out["chunk_count"] = count
    return DocumentOut(**out)


@router.post(
    "/documents/{document_id}/ingest",
    response_model=IngestResult,
    dependencies=[Depends(require_permission("document.update", "document"))],
)
async def ingest_document(
    document_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    embeddings: EmbeddingClient = TEmbeddings,
    tenant_id: str = TTenantId,
) -> IngestResult:
    svc = _make_service(request, db, blob, embeddings, tenant_id)
    doc, count = await svc.ingest_document(document_id)
    return IngestResult(
        document_id=doc.id,
        status=DocumentStatus(doc.status),
        chunks_created=count,
    )


@router.get("/search", response_model=SearchResponse,
            dependencies=[Depends(require_permission("document.search", "document"))])
async def search(
    request: Request,
    q: str,
    k: int = 5,
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    embeddings: EmbeddingClient = TEmbeddings,
    tenant_id: str = TTenantId,
) -> SearchResponse:
    svc = _make_service(request, db, blob, embeddings, tenant_id)
    hits = await svc.search(q, k=k)
    return SearchResponse(hits=hits)
