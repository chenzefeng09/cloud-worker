"""Per-resource ownership guards (IDOR protection).

``require_permission`` answers "may this user perform ``session.read`` at
all?" — it says nothing about *which* session.  Without these guards any
member of a tenant who learned a session or canvas id could read its
transcript, attachments and live stream.

Every route addressing ``/sessions/{session_id}/...`` or
``/canvases/{canvas_id}/...`` depends on one of the guards below.  A caller
who does not own the resource gets the same 404 as for a missing one, so the
endpoint is not an existence oracle.

Two callers legitimately bypass the check:

* the BFF admin replay path, which forwards ``X-Actor: ADMIN`` only after
  enforcing a tenant-admin permission itself (same signal
  ``require_permission`` honours);
* service-to-service calls without an end-user identity (``SYSTEM``) — the
  reconcile sweeper and scheduler callbacks.  The tenant boundary still
  applies to them.
"""

from __future__ import annotations

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.api.dependencies import TDbSession, TTenantId, TUserId
from agent_session_orchestrator.domain.models import CanvasDocument, Session
from agent_session_orchestrator.errors import CanvasNotFoundError, SessionNotFoundError


def _bypasses_owner_check(request: Request, user_id: str) -> bool:
    return request.headers.get("X-Actor") == "ADMIN" or user_id == "SYSTEM"


async def require_session_owner(
    session_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> None:
    if _bypasses_owner_check(request, user_id):
        return
    owner = (
        await db.execute(
            select(Session.user_id).where(
                Session.id == session_id,
                Session.tenant_id == tenant_id,
                Session.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if owner is None or owner != user_id:
        raise SessionNotFoundError(session_id)


async def require_canvas_owner(
    canvas_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> None:
    if _bypasses_owner_check(request, user_id):
        return
    owner = (
        await db.execute(
            select(CanvasDocument.user_id).where(
                CanvasDocument.id == canvas_id,
                CanvasDocument.tenant_id == tenant_id,
                CanvasDocument.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if owner is None or owner != user_id:
        raise CanvasNotFoundError(canvas_id)
