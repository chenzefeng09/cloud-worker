"""Workspace API endpoints — CRUD + base-file management.

A workspace is a user-owned, reusable bundle of instructions / base files /
skills that sessions are created against; its context is injected into every
run of those sessions (see ``services/workspace_context.py`` and the
``create_run`` wiring in ``api/v1/runs.py``).

Owner scoping follows the sessions endpoints exactly: a normal caller only
sees their own workspaces; the admin path (``X-Actor: ADMIN``, the same bypass
``require_permission`` honours) may address any workspace in the tenant — never
wider.

Blob storage layout mirrors attachments::

    tenant/{tenant_id}/workspaces/{workspace_id}/files/{wsf_id}/{filename}

Lazy default workspace
----------------------
``GET /workspaces`` creates a default workspace ("Workspace", ``is_default=True``)
on first touch for a user with no live workspaces.  The create runs in its own
short transaction so a concurrent-request unique-index conflict
(``uq_workspace_default_per_user``) never poisons the request transaction — the
loser swallows the IntegrityError and re-queries.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, UploadFile, status
from sqlalchemy.exc import IntegrityError
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
    scan_upload,
)

# Shared helpers from the attachments module (same package): blob-store
# accessor (503 when unconfigured), RFC 6266 Content-Disposition builder
# (non-ASCII filenames), and the text-or-binary sniffer gating DLP scans.
from agent_session_orchestrator.api.v1.attachments import (
    _content_disposition,
    _get_blob_store,
    _maybe_text,
)
from agent_session_orchestrator.domain.db import get_session as acquire_db_session
from agent_session_orchestrator.errors import OrchestratorError
from agent_session_orchestrator.schemas.workspace import (
    WorkspaceCreate,
    WorkspaceFileOut,
    WorkspaceFileVersionOut,
    WorkspaceOut,
    WorkspaceUpdate,
)
from agent_session_orchestrator.services.clearance import (
    CLEARANCE_RANK,
    level_allows,
)
from agent_session_orchestrator.services.privacy_service import delete_unreferenced_blobs
from agent_session_orchestrator.services.workspace_service import WorkspaceService

router = APIRouter(tags=["Workspaces"])
logger = logging.getLogger(__name__)


def _owner_filter(request: Request, user_id: str) -> str | None:
    """IDOR guard: normal callers only address their own workspaces.

    The admin path (BFF, gated on a tenant_admin permission) forwards
    ``X-Actor: ADMIN`` — the same bypass ``require_permission`` honours — and
    may address every workspace in the tenant.  Never widens past the tenant.
    """
    is_admin = request.headers.get("X-Actor") == "ADMIN"
    return None if is_admin else user_id


async def _workspace_out(svc: WorkspaceService, workspace: Any) -> dict[str, Any]:
    """Serialize a Workspace row + its file count into the response shape."""
    count = await svc.count_files(workspace.id)
    out = WorkspaceOut.model_validate(workspace).model_copy(
        update={"file_count": count}
    )
    return out.model_dump(mode="json")


async def _lazy_create_default(tenant_id: str, user_id: str) -> None:
    """Create the user's default workspace in a separate short transaction.

    Runs outside the request transaction so a unique-index race (two first
    requests lazy-creating concurrently) rolls back only this small txn — the
    request txn stays healthy and simply re-queries.  The ``async for`` body
    deliberately contains no return/break: an early exit would GeneratorExit
    the ``get_session`` generator and roll the create back instead of
    committing it.
    """
    try:
        async for db in acquire_db_session(
            tenant_id=tenant_id, user_id=user_id, actor="USER"
        ):
            svc = WorkspaceService(db, tenant_id=tenant_id)
            await svc.ensure_default_workspace(user_id=user_id)
    except IntegrityError:
        # A concurrent request won the race — the caller re-queries and finds
        # the row the winner committed.
        logger.info(
            "default workspace lazy-create lost a concurrent race (benign)",
            extra={"tenant_id": tenant_id, "user_id": user_id},
        )
    except Exception:  # noqa: BLE001 — fail-open: listing still works without a default
        logger.warning(
            "default workspace lazy-create failed (fail-open)",
            extra={"tenant_id": tenant_id, "user_id": user_id},
            exc_info=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Workspace CRUD
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/workspaces",
            dependencies=[Depends(require_permission("workspace.read", "workspace"))])
async def list_workspaces(
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> dict[str, Any]:
    owner = _owner_filter(request, user_id)
    svc = WorkspaceService(db, tenant_id=tenant_id)
    items = await svc.list_workspaces(user_id=owner)

    # Lazy default: a normal user with zero live workspaces gets one created on
    # first list.  Admin listings (owner=None) never trigger the create.
    if owner is not None and not items:
        await _lazy_create_default(tenant_id, owner)
        items = await svc.list_workspaces(user_id=owner)

    return {"items": [await _workspace_out(svc, ws) for ws in items]}


@router.post("/workspaces", status_code=201,
             dependencies=[Depends(require_permission("workspace.manage", "workspace"))])
async def create_workspace(
    body: WorkspaceCreate,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
    clearance: str = TClearance,
) -> dict[str, Any]:
    # A workspace above the caller's clearance would let them mint a bucket for
    # bytes they are not cleared to see.  Schema already validated the tier.
    if not level_allows(clearance, body.sensitivity_level):
        raise OrchestratorError(
            "workspace sensitivity_level exceeds caller clearance",
            code="AGENT.WORKSPACE.CLEARANCE_DENIED",
            status=403,
            user_message="The workspace sensitivity level cannot exceed your clearance level.",
        )
    svc = WorkspaceService(db, tenant_id=tenant_id)
    workspace = await svc.create_workspace(
        user_id=user_id,
        name=body.name,
        description=body.description,
        instructions=body.instructions,
        skills=body.skills or [],
        meta=body.meta or {},
        sensitivity_level=body.sensitivity_level,
    )
    return await _workspace_out(svc, workspace)


@router.get("/workspaces/{workspace_id}",
            dependencies=[Depends(require_permission("workspace.read", "workspace"))])
async def get_workspace(
    workspace_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> dict[str, Any]:
    owner = _owner_filter(request, user_id)
    svc = WorkspaceService(db, tenant_id=tenant_id)
    workspace = await svc.get_workspace(workspace_id, user_id=owner)
    return await _workspace_out(svc, workspace)


@router.patch("/workspaces/{workspace_id}",
              dependencies=[Depends(require_permission("workspace.manage", "workspace"))])
async def update_workspace(
    workspace_id: str,
    body: WorkspaceUpdate,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
    clearance: str = TClearance,
) -> dict[str, Any]:
    owner = _owner_filter(request, user_id)
    svc = WorkspaceService(db, tenant_id=tenant_id)
    updates = body.model_dump(exclude_unset=True)
    # Raising the workspace tier above the caller's clearance is forbidden for
    # the same reason as create: no minting buckets for bytes you can't see.
    # An explicit null is rejected by the service layer (INVALID_LEVEL 422); we
    # only run the clearance comparison for a concrete level.
    new_level = updates.get("sensitivity_level")
    if new_level is not None and not level_allows(clearance, new_level):
        raise OrchestratorError(
            "workspace sensitivity_level exceeds caller clearance",
            code="AGENT.WORKSPACE.CLEARANCE_DENIED",
            status=403,
            user_message="The workspace sensitivity level cannot exceed your clearance level.",
        )
    workspace = await svc.update_workspace(
        workspace_id, user_id=owner, updates=updates
    )
    return await _workspace_out(svc, workspace)


@router.delete("/workspaces/{workspace_id}", status_code=204,
               dependencies=[Depends(require_permission("workspace.manage", "workspace"))])
async def delete_workspace(
    workspace_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> Response:
    owner = _owner_filter(request, user_id)
    svc = WorkspaceService(db, tenant_id=tenant_id)
    # is_default=True raises WorkspaceDefaultDeleteError → 409 problem+json via
    # the shared OrchestratorError handler.
    await svc.soft_delete_workspace(workspace_id, user_id=owner)
    return Response(status_code=204)


# ─────────────────────────────────────────────────────────────────────────────
# Workspace files
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/workspaces/{workspace_id}/files", status_code=201,
             dependencies=[Depends(require_permission("workspace.manage", "workspace"))])
async def upload_workspace_file(
    workspace_id: str,
    file: UploadFile,
    request: Request,
    description: str | None = Form(None),
    sensitivity_level: str = Form("L2"),
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
    clearance: str = TClearance,
) -> dict[str, Any]:
    """Upload a base file into the workspace.

    ``multipart/form-data`` with one ``file`` field and an optional
    ``description`` form field.  Enforces ``workspace_file_max_bytes``
    (default 20 MB) with 413.
    """
    # Format-check the tier (form fields bypass the Pydantic schema).  Then
    # clearance: a file above the caller's clearance is 403; the service then
    # enforces file_level <= workspace_level (422).
    if sensitivity_level not in CLEARANCE_RANK:
        raise OrchestratorError(
            f"invalid sensitivity_level {sensitivity_level!r}",
            code="AGENT.WORKSPACE.INVALID_LEVEL",
            status=422,
            user_message="Invalid sensitivity level; it must be one of L1 to L5.",
        )
    if not level_allows(clearance, sensitivity_level):
        raise OrchestratorError(
            "file sensitivity_level exceeds caller clearance",
            code="AGENT.WORKSPACE.CLEARANCE_DENIED",
            status=403,
            user_message="The file sensitivity level cannot exceed your clearance level.",
        )

    blob_store = _get_blob_store(request)

    data = await file.read()
    size_bytes = len(data)
    max_bytes = request.app.state.settings.workspace_file_max_bytes
    if size_bytes > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"file exceeds workspace file size limit ({max_bytes} bytes)",
        )
    filename = file.filename or "upload.bin"
    mime = file.content_type or "application/octet-stream"

    owner = _owner_filter(request, user_id)
    svc = WorkspaceService(db, tenant_id=tenant_id)

    # Create the row first (validates workspace ownership) so the blob key can
    # embed the generated file id — same two-step as attachment upload.
    wsf = await svc.create_file(
        workspace_id=workspace_id,
        owner=owner,
        user_id=user_id,
        filename=filename,
        mime=mime,
        size_bytes=size_bytes,
        blob_key="",  # filled in below
        description=description,
        sensitivity_level=sensitivity_level,
    )

    blob_key = (
        f"tenant/{tenant_id}/workspaces/{workspace_id}/files/{wsf.id}/{filename}"
    )

    # DLP scan before any bytes are stored — text content only, same gating
    # and DLP_FAILURE_MODE handling as attachment upload (closed: 503 and the
    # row is rolled back; open: stored unscanned with an error log).
    text_content = _maybe_text(data)
    if text_content is not None:
        await scan_upload(
            dlp_url=request.app.state.settings.foundation_dlp_url,
            tenant_id=tenant_id,
            user_id=user_id,
            content=text_content,
            resource_id=wsf.id,
            resource_type="workspace_file",
        )

    try:
        await asyncio.to_thread(blob_store.put, blob_key, data, content_type=mime)
    except Exception as exc:
        logger.error("S3 put failed", extra={"key": blob_key, "err": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="blob storage upload failed",
        ) from exc

    wsf.blob_key = blob_key
    await db.flush()

    return WorkspaceFileOut.model_validate(wsf).model_dump(mode="json")


@router.get("/workspaces/{workspace_id}/files",
            dependencies=[Depends(require_permission("workspace.read", "workspace"))])
async def list_workspace_files(
    workspace_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> dict[str, Any]:
    owner = _owner_filter(request, user_id)
    svc = WorkspaceService(db, tenant_id=tenant_id)
    files = await svc.list_files(workspace_id, user_id=owner)
    return {
        "items": [
            WorkspaceFileOut.model_validate(f).model_dump(mode="json")
            for f in files
        ]
    }


@router.delete("/workspaces/{workspace_id}/files/{file_id}", status_code=204,
               dependencies=[Depends(require_permission("workspace.manage", "workspace"))])
async def delete_workspace_file(
    workspace_id: str,
    file_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> Response:
    owner = _owner_filter(request, user_id)
    svc = WorkspaceService(db, tenant_id=tenant_id)
    blob_keys = await svc.delete_file(workspace_id, file_id, user_id=owner)

    # Best-effort blob delete: the row delete is authoritative; an orphaned
    # blob only costs storage and never blocks the API.  Deliberately does NOT
    # use _get_blob_store (503) — the delete must succeed without a blob store.
    # Bytes another row still uses (a restore shares keys) are kept.
    await delete_unreferenced_blobs(
        db, tenant_id=tenant_id, keys=blob_keys,
        blob_store=getattr(request.app.state, "blob_store", None),
    )
    return Response(status_code=204)


@router.get("/workspaces/{workspace_id}/files/{file_id}/content",
            dependencies=[Depends(require_permission("workspace.read", "workspace"))])
async def get_workspace_file_content(
    workspace_id: str,
    file_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
    clearance: str = TClearance,
) -> Response:
    """Download/preview the raw bytes for a workspace file.

    Same DLP ``check:download`` + RFC 6266 Content-Disposition discipline as
    the session-attachment content endpoint.
    """
    blob_store = _get_blob_store(request)
    owner = _owner_filter(request, user_id)
    svc = WorkspaceService(db, tenant_id=tenant_id)
    wsf = await svc.get_file(workspace_id, file_id, user_id=owner)

    # Defence-in-depth alongside the DLP check below: never serve bytes for a
    # file above the caller's clearance, regardless of what DLP decides.
    if not level_allows(clearance, wsf.sensitivity_level):
        raise OrchestratorError(
            "file sensitivity_level exceeds caller clearance",
            code="AGENT.WORKSPACE.CLEARANCE_DENIED",
            status=403,
            user_message="This file's sensitivity level exceeds your clearance level, so it cannot be downloaded.",
        )

    # DLP decides with the clearance it resolves for ``user_id`` itself; a
    # check that cannot complete follows DLP_FAILURE_MODE (closed: 503).
    try:
        await check_download(
            dlp_url=request.app.state.settings.foundation_dlp_url,
            tenant_id=tenant_id,
            user_id=user_id,
            resource_id=wsf.id,
            resource_type="workspace_file",
            destination="laptop",
        )
    except DlpDownloadDeniedError as exc:
        raise HTTPException(status_code=403, detail=f"DLP: {exc.reason}") from exc

    try:
        data = await asyncio.to_thread(blob_store.get, wsf.blob_key)
    except Exception as exc:
        logger.error("S3 get failed", extra={"key": wsf.blob_key, "err": str(exc)})
        raise HTTPException(status_code=502, detail="blob storage fetch failed") from exc

    return Response(
        content=data,
        media_type=wsf.mime or "application/octet-stream",
        headers={
            "Content-Disposition": _content_disposition(wsf.filename),
            "Content-Length": str(len(data)),
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# File versions
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/workspaces/{workspace_id}/files/{file_id}/versions",
            dependencies=[Depends(require_permission("workspace.read", "workspace"))])
async def list_file_versions(
    workspace_id: str,
    file_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> dict[str, Any]:
    owner = _owner_filter(request, user_id)
    svc = WorkspaceService(db, tenant_id=tenant_id)
    versions = await svc.list_file_versions(workspace_id, file_id, user_id=owner)
    return {
        "items": [
            WorkspaceFileVersionOut.model_validate(v).model_dump(mode="json")
            for v in versions
        ]
    }


@router.post("/workspaces/{workspace_id}/files/{file_id}/versions/{version_id}/restore",
             dependencies=[Depends(require_permission("workspace.manage", "workspace"))])
async def restore_file_version(
    workspace_id: str,
    file_id: str,
    version_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> dict[str, Any]:
    owner = _owner_filter(request, user_id)
    svc = WorkspaceService(db, tenant_id=tenant_id)
    wsf = await svc.restore_file_version(
        workspace_id, file_id, version_id,
        user_id=owner, restored_by=f"user:{user_id}",
    )
    return WorkspaceFileOut.model_validate(wsf).model_dump(mode="json")


# ─────────────────────────────────────────────────────────────────────────────
# Internal writeback (called by run-completion, not user-facing)
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/internal/workspaces/{workspace_id}/writeback")
async def process_writeback(
    workspace_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> dict[str, Any]:
    """Process writeback files from a completed run.

    Called internally by the run-completion handler; not user-facing.
    Body: ``{"run_id": str, "user_id": str, "files": [{filename, blob_key, size_bytes, mime}]}``
    """
    body = await request.json()
    run_id = body.get("run_id", "unknown")
    user_id = body.get("user_id", "unknown")
    files = body.get("files") or []
    if not files:
        return {"items": []}

    from agent_session_orchestrator.services.platform_config_service import PlatformConfigService
    cfg_svc = PlatformConfigService(db, tenant_id=tenant_id)
    max_versions = int(await cfg_svc.get("workspace.max_versions_per_file", 20))

    svc = WorkspaceService(db, tenant_id=tenant_id)
    results = await svc.process_writeback(
        workspace_id=workspace_id,
        run_id=run_id,
        user_id=user_id,
        writeback_files=files,
        max_versions=max_versions,
    )

    all_evicted: list[str] = []
    for r in results:
        all_evicted.extend(r.pop("evicted_blobs", []))

    # An evicted version's bytes can still back the current file (a restore
    # shares keys), so only unreferenced ones are deleted.  Best-effort.
    await delete_unreferenced_blobs(
        db, tenant_id=tenant_id, keys=all_evicted,
        blob_store=getattr(request.app.state, "blob_store", None),
    )

    return {"items": results}
