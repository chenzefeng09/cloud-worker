"""Document upload, listing, and ingest endpoints."""

from __future__ import annotations

import math
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_ingest.api.authz_guard import require_permission
from knowledge_ingest.api.dependencies import (
    TBlobStore,
    TClearance,
    TDbSession,
    TEmbeddings,
    TTenantId,
    TUserId,
)
from knowledge_ingest.blob.base import BlobStore
from knowledge_ingest.domain.sensitivity import normalize_clearance, sensitivity_rank
from knowledge_ingest.embeddings.base import EmbeddingClient
from knowledge_ingest.schemas.common import ListPage
from knowledge_ingest.schemas.document import (
    DocumentOut,
    DocumentPatch,
    DocumentStatus,
    IngestResult,
    SearchRequest,
    SearchResponse,
    SearchScope,
)
from knowledge_ingest.services.ingest_service import _UNSET, IngestService

router = APIRouter(tags=["Documents"])


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


@router.get("/documents", response_model=ListPage,
            dependencies=[Depends(require_permission("document.read", "document"))])
async def list_documents(
    request: Request,
    include_memory: bool = False,
    folder_id: str | None = None,
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    embeddings: EmbeddingClient = TEmbeddings,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
    clearance: str | None = TClearance,
) -> ListPage:
    # ``include_memory`` defaults False so the ordinary library omits session
    # memory docs; the admin audit path passes ``include_memory=true`` to see
    # the full tenant set (all users' memory rows included).
    #
    # ``folder_id`` filters to one folder ("" / "root" → the root layer).  The
    # visibility gate uses ``X-User-Clearance`` when present (absent → complete
    # listing for the management/audit path).
    svc = _make_service(request, db, blob, embeddings, tenant_id, user_id)
    folder_filter = _parse_folder_filter(folder_id)
    rows = await svc.list_documents(
        include_memory=include_memory, folder_id=folder_filter, clearance=clearance,
    )
    items = []
    for doc, count in rows:
        out = DocumentOut.model_validate(doc).model_dump(mode="json")
        out["chunk_count"] = count
        items.append(out)
    return ListPage(items=items, total=len(items))


def _parse_folder_filter(folder_id: str | None) -> object:
    """Map the ``folder_id`` query param to a service filter value.

    Absent → no filter (``_UNSET``); ``""`` or ``"root"`` → the root layer
    (``None``); any other value → that folder id.
    """
    if folder_id is None:
        return _UNSET
    if folder_id == "" or folder_id.lower() == "root":
        return None
    return folder_id


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
    origin_session_id: str | None = Form(None),
    folder_id: str | None = Form(None),
    sensitivity: str | None = Form(None),
    file: UploadFile = File(...),
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    embeddings: EmbeddingClient = TEmbeddings,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> DocumentOut:
    payload = await file.read()

    # Quota check: request at least 1 GB unit; ceiling of actual size.
    quota_guard = request.app.state.quota_guard
    requested_gb = max(1, math.ceil(len(payload) / (1024 ** 3)))
    await quota_guard.check(tenant_id=tenant_id, dimension="storage_gb", requested=requested_gb)

    svc = _make_service(request, db, blob, embeddings, tenant_id, user_id)
    doc = await svc.create_document(
        title=title,
        filename=file.filename or "upload.bin",
        mime=mime,
        data=payload,
        origin_session_id=origin_session_id,
        folder_id=folder_id or None,
        sensitivity=sensitivity or None,
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
    user_id: str = TUserId,
) -> DocumentOut:
    svc = _make_service(request, db, blob, embeddings, tenant_id, user_id)
    doc, count = await svc.get_document(document_id)
    out = DocumentOut.model_validate(doc).model_dump(mode="json")
    out["chunk_count"] = count
    return DocumentOut(**out)


@router.get(
    "/documents/{document_id}/content",
    response_class=Response,
    dependencies=[Depends(require_permission("document.read", "document"))],
)
async def get_document_content(
    document_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    embeddings: EmbeddingClient = TEmbeddings,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
    clearance: str | None = TClearance,
) -> Response:
    """Stream the source blob bytes for a knowledge document.

    Preview/download counterpart to ``GET /documents/{id}``.  Recovers the
    blob key from ``doc.source_uri`` the same way ``IngestService`` does on
    delete and returns the raw bytes with an RFC 6266 ``Content-Disposition``
    header so non-ASCII filenames (e.g. Chinese) don't blow up latin-1 header
    encoding.

    Single-doc ops (get/patch/delete) currently enforce RBAC + RLS but NOT the
    sensitivity visibility gate — so this endpoint closes that gap on the
    download path: when the caller's clearance (defaulted fail-closed to L1)
    ranks below the document's sensitivity, we 403 before touching the blob.
    """
    svc = _make_service(request, db, blob, embeddings, tenant_id, user_id)
    doc, _ = await svc.get_document(document_id)

    # Clearance gate — fail-closed (absent/unknown → L1), mirroring the listing
    # filter at ingest_service.py:277 (`allowed_levels_for(clearance)`).
    if sensitivity_rank(doc.sensitivity) > sensitivity_rank(
        normalize_clearance(clearance or "L1")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="document sensitivity exceeds caller clearance",
        )

    if doc.source_uri is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="document has no source blob",
        )

    # Recover the blob key exactly like IngestService.delete_document:
    # `s3://bucket/<key>` → take everything after the 3rd slash; `memory://<key>`
    # → strip the `memory://` prefix.
    if doc.source_uri.startswith("s3://"):
        key = doc.source_uri.split("/", 3)[-1]
    else:
        key = doc.source_uri.replace("memory://", "")
    raw = blob.get(key)

    # RFC 6266 Content-Disposition — ASCII filename plus a percent-encoded
    # `filename*` so non-ASCII filenames don't crash latin-1 header encoding
    # (the file-content-non-ascii-500 lesson). If the name is pure ASCII the
    # two fields collapse to the same value and remain readable to legacy
    # clients; non-ASCII clients use `filename*`.
    name = doc.title or f"{document_id}.bin"
    try:
        name.encode("ascii")
        disposition = f'attachment; filename="{name}"'
    except UnicodeEncodeError:
        ascii_fallback = (
            name.encode("ascii", "replace").decode("ascii").replace('"', "_")
        )
        disposition = (
            f'attachment; filename="{ascii_fallback}"; '
            f"filename*=UTF-8''{quote(name, safe='')}"
        )

    return Response(
        content=raw,
        media_type=doc.mime or "application/octet-stream",
        headers={"Content-Disposition": disposition},
    )



@router.patch(
    "/documents/{document_id}",
    response_model=DocumentOut,
    dependencies=[Depends(require_permission("document.update", "document"))],
)
async def patch_document(
    document_id: str,
    request: Request,
    patch: DocumentPatch = Body(...),
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    embeddings: EmbeddingClient = TEmbeddings,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> DocumentOut:
    """Move a document and/or change its sensitivity.

    Only the fields present in the body are applied; moving into a folder
    without an explicit sensitivity elevates only (never lowers), while an
    explicit ``sensitivity`` is the sole way to downgrade.
    """
    svc = _make_service(request, db, blob, embeddings, tenant_id, user_id)
    fields = patch.model_fields_set
    doc, count = await svc.update_document(
        document_id,
        folder_id=(patch.folder_id if "folder_id" in fields else _UNSET),
        sensitivity=(
            patch.sensitivity.value
            if "sensitivity" in fields and patch.sensitivity is not None
            else _UNSET
        ),
    )
    out = DocumentOut.model_validate(doc).model_dump(mode="json")
    out["chunk_count"] = count
    return DocumentOut(**out)


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("document.delete", "document"))],
)
async def delete_document(
    document_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    embeddings: EmbeddingClient = TEmbeddings,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> Response:
    """Delete a document — its chunks (cascade) and source blob (best-effort)."""
    svc = _make_service(request, db, blob, embeddings, tenant_id, user_id)
    await svc.delete_document(document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
    user_id: str = TUserId,
) -> IngestResult:
    svc = _make_service(request, db, blob, embeddings, tenant_id, user_id)
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
    scope: SearchScope = SearchScope.ALL,
    workspace_id: str | None = None,
    exclude_origin_session_id: str | None = None,
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    embeddings: EmbeddingClient = TEmbeddings,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
    clearance: str | None = TClearance,
) -> SearchResponse:
    # ``scope`` defaults to ``all`` for backward compatibility. Clearance is
    # normalised to L1 when the header is absent (fail-closed retrieval), so an
    # unauthenticated-for-clearance caller only sees L1 knowledge.
    svc = _make_service(request, db, blob, embeddings, tenant_id, user_id)
    return await _run_search(
        svc,
        SearchRequest(
            q=q,
            k=k,
            scope=scope,
            workspace_id=workspace_id,
            exclude_origin_session_id=exclude_origin_session_id,
        ),
        clearance=clearance,
    )


@router.post("/search", response_model=SearchResponse,
             dependencies=[Depends(require_permission("document.search", "document"))])
async def search_post(
    request: Request,
    body: SearchRequest = Body(...),
    db: AsyncSession = TDbSession,
    blob: BlobStore = TBlobStore,
    embeddings: EmbeddingClient = TEmbeddings,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
    clearance: str | None = TClearance,
) -> SearchResponse:
    """``GET /search`` with the query in the body instead of the URL.

    Same fields, defaults, gates and response.  Prefer this form: a prompt in
    ``?q=`` ends up in access logs and traces.
    """
    svc = _make_service(request, db, blob, embeddings, tenant_id, user_id)
    return await _run_search(svc, body, clearance=clearance)


async def _run_search(
    svc: IngestService, params: SearchRequest, *, clearance: str | None,
) -> SearchResponse:
    hits = await svc.search(
        params.q,
        k=params.k,
        scope=params.scope.value,
        clearance=clearance or "L1",
        exclude_origin_session_id=params.exclude_origin_session_id,
        workspace_id=params.workspace_id,
    )
    return SearchResponse(hits=hits)
