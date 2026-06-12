"""Attachment upload and listing endpoints.

Storage layout
--------------
``POST /sessions/{session_id}/attachments``  — multipart upload, stores bytes
in S3/MinIO, writes an ``attachment`` row, returns metadata.

``GET  /sessions/{session_id}/attachments``  — list attachments for a session.
    Optional ?kind=input|output filter.

``GET  /sessions/{session_id}/files/{file_id}/content``  — download/preview
    file content from S3.  Returns raw bytes with original Content-Type.

Blob key schema::

    tenant/{tenant_id}/sessions/{session_id}/attachments/{att_id}/{filename}

The key is designed to be unique and human-readable so ops can inspect the
bucket without additional tooling.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.api.authz_guard import require_permission
from agent_session_orchestrator.api.dependencies import (
    TClearance,
    TDbSession,
    TTenantId,
    TUserId,
)
from agent_session_orchestrator.api.dlp_guard import (
    DlpDownloadDeniedError,
    check_download,
    scan_and_tag,
)
from agent_session_orchestrator.services.session_service import SessionService

router = APIRouter(tags=["Attachments"])
logger = logging.getLogger(__name__)


def _get_blob_store(request: Request):  # type: ignore[no-untyped-def]
    """Retrieve the S3BlobStore from app state; raise 503 if unconfigured."""
    blob = getattr(request.app.state, "blob_store", None)
    if blob is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="blob store not configured",
        )
    return blob


@router.post("/sessions/{session_id}/attachments", status_code=201,
             dependencies=[Depends(require_permission("session.attachment.create", "session"))])
async def upload_attachment(
    session_id: str,
    file: UploadFile,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> dict[str, Any]:
    """Upload a file and attach it to the session.

    Accepts ``multipart/form-data`` with one ``file`` field.
    Returns the attachment metadata row.
    """
    blob_store = _get_blob_store(request)

    data = await file.read()
    size_bytes = len(data)
    filename = file.filename or "upload.bin"
    mime = file.content_type or "application/octet-stream"

    svc = SessionService(db, tenant_id=tenant_id)

    # We need the attachment id for the blob key; generate it via the service
    # create path which also validates session ownership.
    # Use a temporary key placeholder; we update after the row id is known.
    att = await svc.create_attachment(
        session_id=session_id,
        user_id=user_id,
        filename=filename,
        mime=mime,
        size_bytes=size_bytes,
        blob_key="",  # filled in below
    )

    blob_key = f"tenant/{tenant_id}/sessions/{session_id}/attachments/{att.id}/{filename}"

    # Upload to S3 (synchronous boto3 call — acceptable in dev; in high-QPS
    # prod this should run in a thread pool via asyncio.to_thread).
    try:
        import asyncio
        await asyncio.to_thread(blob_store.put, blob_key, data, content_type=mime)
    except Exception as exc:
        logger.error("S3 put failed", extra={"key": blob_key, "err": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="blob storage upload failed",
        ) from exc

    # Persist the real blob_key.
    att.blob_key = blob_key
    await db.flush()

    # ── DLP scan (mount point 2): fail-open ─────────────────────────────────
    # Only text content is scanned — binary blobs (images, archives, etc.)
    # are skipped to avoid garbage hits and decode cost. A hit does NOT reject
    # the upload; scan_and_tag auto-classifies the attachment instead. Any DLP
    # error is logged and swallowed inside scan_and_tag.
    text_content = _maybe_text(data)
    if text_content is not None:
        await scan_and_tag(
            dlp_url=request.app.state.settings.foundation_dlp_url,
            tenant_id=tenant_id,
            content=text_content,
            resource_id=att.id,
            resource_type="attachment",
        )

    return {
        "id": att.id,
        "session_id": session_id,
        "filename": att.filename,
        "mime": att.mime,
        "size_bytes": att.size_bytes,
        "blob_key": att.blob_key,
        "created_at": att.created_at.isoformat(),
    }


@router.get("/sessions/{session_id}/attachments",
            dependencies=[Depends(require_permission("session.attachment.read", "session"))])
async def list_attachments(
    session_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    kind: str | None = None,
) -> dict[str, Any]:
    """List attachments for a session.

    Optional ``kind`` query param: ``input`` (user-uploaded) or ``output``
    (agent artifacts).  Omit to list all.
    """
    svc = SessionService(db, tenant_id=tenant_id)
    atts = await svc.list_attachments(session_id, kind=kind)
    return {
        "items": [
            {
                "id": a.id,
                "session_id": a.session_id,
                "filename": a.filename,
                "mime": a.mime,
                "size_bytes": a.size_bytes,
                "blob_key": a.blob_key,
                "kind": a.kind,
                "run_id": a.run_id,
                "created_at": a.created_at.isoformat(),
            }
            for a in atts
        ]
    }


@router.get("/sessions/{session_id}/files/{file_id}/content",
            dependencies=[Depends(require_permission("session.attachment.read", "session"))])
async def get_file_content(
    session_id: str,
    file_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
    clearance: str = TClearance,
) -> Response:
    """Download or preview the raw bytes for a session file.

    Fetches the object from S3 and returns it with the original Content-Type.
    Works for both user-uploaded inputs and agent-produced artifacts.
    """
    blob_store = _get_blob_store(request)
    svc = SessionService(db, tenant_id=tenant_id)
    try:
        att = await svc.get_attachment(session_id, file_id)
    except Exception:
        raise HTTPException(status_code=404, detail="file not found") from None

    # ── DLP check:download (mount point 1) ──────────────────────────────────
    # fail-OPEN on DLP error (network/timeout/non-200 → WARN, download
    # proceeds; see dlp_guard.check_download), but an explicit allowed=false
    # from DLP raises DlpDownloadDeniedError → 403. The Attachment model has no
    # dedicated resource-type column, so resource_type is the constant
    # "attachment"; resource_id is the attachment id.
    #
    # ``clearance`` is the caller's real data clearance, resolved from the
    # ``X-User-Clearance`` header (injected by the BFF) via the
    # ``current_clearance`` dependency; it falls back to "L2" when absent.
    try:
        await check_download(
            dlp_url=request.app.state.settings.foundation_dlp_url,
            tenant_id=tenant_id,
            user_id=user_id,
            resource_id=att.id,
            resource_type="attachment",
            user_clearance=clearance,
            destination="laptop",
        )
    except DlpDownloadDeniedError as exc:
        raise HTTPException(status_code=403, detail=f"DLP: {exc.reason}") from exc

    try:
        import asyncio
        data = await asyncio.to_thread(blob_store.get, att.blob_key)
    except Exception as exc:
        logger.error("S3 get failed", extra={"key": att.blob_key, "err": str(exc)})
        raise HTTPException(status_code=502, detail="blob storage fetch failed") from exc

    return Response(
        content=data,
        media_type=att.mime or "application/octet-stream",
        headers={
            "Content-Disposition": _content_disposition(att.filename),
            "Content-Length": str(len(data)),
        },
    )


# Cap how much of a text upload we hand to DLP scan to bound payload size /
# latency on large files. DLP only needs a representative sample for pattern
# matching; whole-file scanning of multi-MB uploads is not worth the cost.
_DLP_SCAN_MAX_BYTES = 256 * 1024


def _maybe_text(data: bytes) -> str | None:
    """Return ``data`` decoded as UTF-8 text if it looks textual, else ``None``.

    Used to gate DLP scanning so binary blobs (images, archives, PDFs) are
    skipped — they would only produce decode errors and noise. Truncates to
    ``_DLP_SCAN_MAX_BYTES`` before decoding to bound the scan payload.
    """
    if not data:
        return None
    sample = data[:_DLP_SCAN_MAX_BYTES]
    # A NUL byte is a strong signal of binary content.
    if b"\x00" in sample:
        return None
    try:
        return sample.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _content_disposition(filename: str, disposition: str = "inline") -> str:
    """Build an RFC 6266 Content-Disposition header.

    HTTP header values are encoded as latin-1, so a non-ASCII ``filename``
    (e.g. Chinese characters) would raise ``UnicodeEncodeError`` during
    response serialization and surface as a 500. Emit an ASCII-safe
    ``filename`` fallback plus a percent-encoded ``filename*`` (UTF-8) for
    clients that support it.
    """
    name = filename or "download"
    ascii_fallback = name.encode("ascii", "replace").decode("ascii").replace('"', "'")
    quoted = quote(name, safe="")
    return (
        f"{disposition}; filename=\"{ascii_fallback}\"; "
        f"filename*=UTF-8''{quoted}"
    )
