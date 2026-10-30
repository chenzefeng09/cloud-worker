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
    TDbSession,
    TTenantId,
    TUserId,
)
from agent_session_orchestrator.api.dlp_guard import (
    DlpDownloadDeniedError,
    check_download,
    scan_upload,
)
from agent_session_orchestrator.api.ownership import require_session_owner
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
             dependencies=[
                 Depends(require_permission("session.attachment.create", "session")),
                 Depends(require_session_owner),
             ])
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

    # ── DLP scan (mount point 2), before any bytes are stored ───────────────
    # Only text content is scanned — binary blobs (images, archives, etc.)
    # are skipped to avoid garbage hits and decode cost. A hit does NOT reject
    # the upload: DLP labels the attachment with the hit level, which
    # check:download enforces later. A scan that cannot complete follows
    # DLP_FAILURE_MODE (see dlp_guard): closed (default) raises 503, and the
    # request transaction drops the row before any blob exists; open stores
    # the file unscanned and logs an error.
    text_content = _maybe_text(data)
    if text_content is not None:
        await scan_upload(
            dlp_url=request.app.state.settings.foundation_dlp_url,
            tenant_id=tenant_id,
            user_id=user_id,
            content=text_content,
            resource_id=att.id,
            resource_type="attachment",
        )

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
            dependencies=[
                Depends(require_permission("session.attachment.read", "session")),
                Depends(require_session_owner),
            ])
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


@router.delete(
    "/sessions/{session_id}/attachments/{attachment_id}",
    status_code=204,
    dependencies=[
        Depends(require_permission("session.attachment.create", "session")),
        Depends(require_session_owner),
    ],
)
async def delete_attachment(
    session_id: str,
    attachment_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> Response:
    """Delete an attachment (Artifacts panel delete) — hard delete row + blob.

    Ownership + soft-deleted-session guards ride ``SessionService.get_attachment``
    (raises ``SessionNotFoundError`` → 404 for a missing / cross-tenant /
    soft-deleted-session attachment).  The S3 blob is removed best-effort before
    the row delete — a missing blob is not a blocker; the row is authoritative.
    """
    # ``_get_blob_store`` raises 503 when the blob store is unconfigured.  For a
    # delete the row is the system of record, so we fall back to ``None`` (skip
    # the blob step) rather than fail the whole request — the row still deletes.
    blob_store = getattr(request.app.state, "blob_store", None)
    svc = SessionService(db, tenant_id=tenant_id)
    await svc.delete_attachment(session_id, attachment_id, blob_store=blob_store)
    return Response(status_code=204)


@router.get("/sessions/{session_id}/files/{file_id}/content",
            dependencies=[
                Depends(require_permission("session.attachment.read", "session")),
                Depends(require_session_owner),
            ])
async def get_file_content(
    session_id: str,
    file_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
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
    # DLP compares the clearance it resolves for ``user_id`` with the
    # attachment's label; allowed=false raises DlpDownloadDeniedError → 403.
    # A check that cannot complete follows DLP_FAILURE_MODE (see dlp_guard):
    # closed (default) raises 503, open serves the file and logs an error.
    # The Attachment model has no dedicated resource-type column, so
    # resource_type is the constant "attachment"; resource_id is the
    # attachment id.
    try:
        await check_download(
            dlp_url=request.app.state.settings.foundation_dlp_url,
            tenant_id=tenant_id,
            user_id=user_id,
            resource_id=att.id,
            resource_type="attachment",
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
    except UnicodeDecodeError as exc:
        # The cut can split a multi-byte character (likely for CJK text);
        # drop that partial tail rather than skip the scan of a text file.
        if len(data) > len(sample) and exc.start >= len(sample) - 3:
            return sample[: exc.start].decode("utf-8")
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
