"""Canvas v2 API endpoints — the system of record (design §5.1).

Routes (mounted under ``settings.api_prefix`` = ``/api/session/v1``):

    POST   /sessions/{sid}/canvases                 create (Phase-A materialize)
    GET    /sessions/{sid}/canvases                 list (no blocks)
    GET    /canvases/{cid}                           detail (current blocks)
    GET    /canvases/{cid}/versions                  version list (metadata)
    GET    /canvases/{cid}/versions/{n}              a specific version
    POST   /canvases/{cid}/versions                  manual-edit version
    POST   /canvases/{cid}/suggestions               create suggestion (BFF internal)
    POST   /canvases/{cid}/suggestions/{sgid}/accept accept (single txn)
    POST   /canvases/{cid}/suggestions/{sgid}/reject reject
    POST   /canvases/{cid}/revert                    revert to a version

Auth + RLS mirror the messages endpoints: every route carries a
``require_permission`` guard on the session permission scopes, and the DB
session is tenant-bound (RLS) via ``TDbSession``.  Ownership of a canvas is
enforced in the service by validating the owning session (soft-deleted /
cross-tenant sessions raise NOT_FOUND).  Errors are RFC 9457 problem+json with
``AGENT.SESSION.CANVAS.*`` codes and Chinese ``user_message`` (see errors.py).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.api.authz_guard import require_permission
from agent_session_orchestrator.api.dependencies import TDbSession, TTenantId, TUserId
from agent_session_orchestrator.schemas.canvas import (
    CanvasCreate,
    CanvasOut,
    CanvasSummaryOut,
    ManualVersionCreate,
    RevertRequest,
    SuggestionCreate,
    SuggestionOut,
    VersionOut,
    VersionSummaryOut,
)
from agent_session_orchestrator.schemas.common import CursorPage
from agent_session_orchestrator.services.canvas_service import CanvasService

router = APIRouter(tags=["Canvas"])


def _content_type_of(blocks: list) -> str:
    """Derive the wire ``content_type`` from a canvas' current blocks.

    An executable canvas is exactly one ``html`` block (see
    ``canvas_blocks.single_html_block``); everything else is prose markdown.
    """
    if len(blocks) == 1 and isinstance(blocks[0], dict) and blocks[0].get("type") == "html":
        return "html"
    return "markdown"


async def _canvas_out(svc: CanvasService, canvas) -> CanvasOut:  # type: ignore[no-untyped-def]
    """Serialise a canvas with its current version's blocks inline."""
    blocks = await svc.get_current_blocks(canvas)
    return CanvasOut(
        id=canvas.id,
        session_id=canvas.session_id,
        kind=canvas.kind,
        content_type=_content_type_of(blocks),
        title=canvas.title,
        current_version=canvas.current_version,
        source_message_id=canvas.source_message_id,
        source_run_id=canvas.source_run_id,
        blocks=blocks,
        created_at=canvas.created_at,
        updated_at=canvas.updated_at,
    )


# ── create / list ────────────────────────────────────────────────────────────


@router.post(
    "/sessions/{session_id}/canvases",
    status_code=201,
    response_model=CanvasOut,
    dependencies=[Depends(require_permission("session.message.create", "session"))],
)
async def create_canvas(
    session_id: str,
    body: CanvasCreate,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> CanvasOut:
    svc = CanvasService(db, tenant_id=tenant_id)
    canvas = await svc.create_canvas(
        session_id,
        user_id=user_id,
        source_message_id=body.source_message_id,
        content=body.content,
        kind=body.kind,
        title=body.title,
        content_type=body.content_type,
    )
    return await _canvas_out(svc, canvas)


@router.get(
    "/sessions/{session_id}/canvases",
    response_model=CursorPage,
    dependencies=[Depends(require_permission("session.message.read", "session"))],
)
async def list_canvases(
    session_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> CursorPage:
    svc = CanvasService(db, tenant_id=tenant_id)
    canvases = await svc.list_canvases(session_id)
    return CursorPage(
        items=[
            CanvasSummaryOut.model_validate(c).model_dump(mode="json") for c in canvases
        ],
        next_cursor=None,
    )


# ── detail / versions ────────────────────────────────────────────────────────


@router.get(
    "/canvases/{canvas_id}",
    response_model=CanvasOut,
    dependencies=[Depends(require_permission("session.message.read", "session"))],
)
async def get_canvas(
    canvas_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> CanvasOut:
    svc = CanvasService(db, tenant_id=tenant_id)
    canvas = await svc.get_canvas(canvas_id)
    return await _canvas_out(svc, canvas)


@router.delete(
    "/canvases/{canvas_id}",
    status_code=204,
    dependencies=[Depends(require_permission("session.message.create", "session"))],
)
async def delete_canvas(
    canvas_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> Response:
    """Soft-delete a canvas (Artifacts panel delete).

    Sets ``deleted_at``; the row + versions stay for the retention sweep's
    physical purge.  Raises ``404 CanvasNotFoundError`` via ``_get_canvas`` for
    a missing / soft-deleted / cross-tenant canvas.
    """
    svc = CanvasService(db, tenant_id=tenant_id)
    await svc.soft_delete(canvas_id)
    return Response(status_code=204)


@router.get(
    "/canvases/{canvas_id}/versions",
    response_model=CursorPage,
    dependencies=[Depends(require_permission("session.message.read", "session"))],
)
async def list_versions(
    canvas_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> CursorPage:
    svc = CanvasService(db, tenant_id=tenant_id)
    versions = await svc.list_versions(canvas_id)
    return CursorPage(
        items=[
            VersionSummaryOut.model_validate(v).model_dump(mode="json") for v in versions
        ],
        next_cursor=None,
    )


@router.get(
    "/canvases/{canvas_id}/versions/{version}",
    response_model=VersionOut,
    dependencies=[Depends(require_permission("session.message.read", "session"))],
)
async def get_version(
    canvas_id: str,
    version: int,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> VersionOut:
    svc = CanvasService(db, tenant_id=tenant_id)
    row = await svc.get_version(canvas_id, version)
    return VersionOut.model_validate(row)


@router.post(
    "/canvases/{canvas_id}/versions",
    status_code=201,
    response_model=CanvasOut,
    dependencies=[Depends(require_permission("session.message.create", "session"))],
)
async def create_manual_version(
    canvas_id: str,
    body: ManualVersionCreate,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> CanvasOut:
    svc = CanvasService(db, tenant_id=tenant_id)
    canvas = await svc.create_manual_version(
        canvas_id,
        blocks=[b.model_dump() for b in body.blocks],
        base_version=body.base_version,
    )
    return await _canvas_out(svc, canvas)


@router.post(
    "/canvases/{canvas_id}/revert",
    status_code=201,
    response_model=CanvasOut,
    dependencies=[Depends(require_permission("session.message.create", "session"))],
)
async def revert_canvas(
    canvas_id: str,
    body: RevertRequest,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> CanvasOut:
    svc = CanvasService(db, tenant_id=tenant_id)
    canvas = await svc.revert(canvas_id, to_version=body.to_version)
    return await _canvas_out(svc, canvas)


# ── suggestions ──────────────────────────────────────────────────────────────


@router.post(
    "/canvases/{canvas_id}/suggestions",
    status_code=201,
    response_model=SuggestionOut,
    dependencies=[Depends(require_permission("session.message.create", "session"))],
)
async def create_suggestion(
    canvas_id: str,
    body: SuggestionCreate,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> SuggestionOut:
    svc = CanvasService(db, tenant_id=tenant_id)
    suggestion = await svc.create_suggestion(
        canvas_id,
        base_version=body.base_version,
        instruction=body.instruction,
        diffs=[d.model_dump() for d in body.diffs],
        change_summary=body.change_summary,
    )
    return SuggestionOut.model_validate(suggestion)


@router.post(
    "/canvases/{canvas_id}/suggestions/{suggestion_id}/accept",
    response_model=CanvasOut,
    dependencies=[Depends(require_permission("session.message.create", "session"))],
)
async def accept_suggestion(
    canvas_id: str,
    suggestion_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> CanvasOut:
    svc = CanvasService(db, tenant_id=tenant_id)
    canvas = await svc.accept_suggestion(canvas_id, suggestion_id)
    return await _canvas_out(svc, canvas)


@router.post(
    "/canvases/{canvas_id}/suggestions/{suggestion_id}/reject",
    response_model=SuggestionOut,
    dependencies=[Depends(require_permission("session.message.create", "session"))],
)
async def reject_suggestion(
    canvas_id: str,
    suggestion_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> SuggestionOut:
    svc = CanvasService(db, tenant_id=tenant_id)
    suggestion = await svc.reject_suggestion(canvas_id, suggestion_id)
    return SuggestionOut.model_validate(suggestion)
