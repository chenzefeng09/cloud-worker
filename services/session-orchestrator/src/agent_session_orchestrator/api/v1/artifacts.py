"""Artifacts panel — cross-session listing of a user's session-generated outputs.

Two ``GET`` endpoints power the frontend's "Artifacts 专区":

* ``GET /artifacts/canvases`` — every live canvas document the caller owns,
  across every session (newest first).
* ``GET /artifacts/files?kind=output`` — every attachment (default: agent-run
  outputs) the caller owns, across every session (newest first).

Owner-scoping: mirror ``GET /sessions`` — ``user_id`` comes from the
``TUserId`` dependency (resolved from the gateway-injected ``X-User-Id``
header).  Tenant RLS already scopes to ``tenant_id``; the ``user_id``
predicate in the service is a strictly tighter filter on top (never crosses
tenants).  No ``X-Actor: ADMIN`` widening here — the Artifacts panel is a
self-service surface, never an admin replay path.

Auth: ``require_permission("session.read", "session")`` on both reads, same
guard the session list uses.  Errors are RFC 9457 problem+json with Chinese
``user_message`` where applicable (handled by the shared exception handlers).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.api.authz_guard import require_permission
from agent_session_orchestrator.api.dependencies import TDbSession, TTenantId, TUserId
from agent_session_orchestrator.schemas.canvas import CanvasSummaryOut
from agent_session_orchestrator.services.canvas_service import CanvasService
from agent_session_orchestrator.services.session_service import SessionService

router = APIRouter(tags=["Artifacts"])


@router.get(
    "/artifacts/canvases",
    dependencies=[Depends(require_permission("session.read", "session"))],
)
async def list_user_canvases(
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> dict[str, Any]:
    """List every live canvas owned by the caller, across all sessions.

    Returns ``{items: [CanvasSummaryOut, ...]}`` newest-first.  Soft-deleted
    canvases are excluded; the block content lives in ``CanvasVersion`` and is
    NOT included here (use ``GET /canvases/{id}`` for the full doc).
    """
    svc = CanvasService(db, tenant_id=tenant_id)
    canvases = await svc.list_canvases_for_user(user_id)
    return {
        "items": [
            CanvasSummaryOut.model_validate(c).model_dump(mode="json")
            for c in canvases
        ]
    }


@router.get(
    "/artifacts/files",
    dependencies=[Depends(require_permission("session.read", "session"))],
)
async def list_user_files(
    kind: str = Query("output"),
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> dict[str, Any]:
    """List every attachment owned by the caller, across all sessions.

    ``kind`` narrows to ``'input'`` (user-uploaded) or ``'output'``
    (agent-produced artifacts); defaults to ``'output'`` for the Artifacts
    panel.  Returns ``{items: [...]}`` newest-first.
    """
    svc = SessionService(db, tenant_id=tenant_id)
    atts = await svc.list_attachments_for_user(user_id, kind=kind)
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
