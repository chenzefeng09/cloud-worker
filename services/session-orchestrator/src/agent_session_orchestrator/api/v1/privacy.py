"""Privacy API — GDPR data-owner endpoints (export, erasure, settings, retention).

All routes act on the caller (``X-User-Id``).  An ``X-Actor: ADMIN`` caller —
the BFF forwards it only after enforcing a tenant-admin permission, the same
signal ``require_permission`` honours — may name another data subject with
``user_id``; for anyone else naming a different user is a 403.  Every query
stays inside the caller's tenant (RLS).

    GET  /privacy/export[?user_id=usr_…]  → full JSON export (soft-deleted rows included)
    POST /privacy/erase   {"user_id"?, "dry_run"?} → hard delete + counts
    GET  /privacy/settings[?user_id=usr_…] → {"memory_enabled", "updated_at"}
    PUT  /privacy/settings[?user_id=usr_…] {"memory_enabled": bool} → same shape
    GET  /privacy/retention               → {"session_retention_days", "trash_grace_days"}
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.api.authz_guard import require_permission
from agent_session_orchestrator.api.dependencies import TDbSession, TTenantId, TUserId
from agent_session_orchestrator.services.privacy_service import (
    USER_ID_RE,
    PrivacyForbiddenError,
    PrivacyInvalidSubjectError,
    PrivacyService,
)

router = APIRouter(tags=["Privacy"])


class EraseRequest(BaseModel):
    user_id: str | None = None
    dry_run: bool = False


class PrivacySettingsUpdate(BaseModel):
    memory_enabled: bool


def _is_admin(request: Request) -> bool:
    return request.headers.get("X-Actor") == "ADMIN"


def _resolve_subject(request: Request, caller: str, requested: str | None) -> str:
    """The data subject: the caller, or ``requested`` for an ADMIN actor."""
    requested = (requested or "").strip() or None
    if requested is not None and requested != caller and not _is_admin(request):
        raise PrivacyForbiddenError()
    subject = requested or caller
    if not USER_ID_RE.match(subject):
        raise PrivacyInvalidSubjectError(f"invalid data subject user_id {subject!r}")
    return subject


@router.get(
    "/privacy/export",
    dependencies=[
        Depends(require_permission("session.read", "session")),
        Depends(require_permission("workspace.read", "workspace")),
    ],
)
async def export_user_data(
    request: Request,
    user_id: str | None = Query(default=None),
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    caller: str = TUserId,
) -> dict[str, Any]:
    subject = _resolve_subject(request, caller, user_id)
    return await PrivacyService(db, tenant_id=tenant_id).export_user_data(subject)


@router.post(
    "/privacy/erase",
    dependencies=[
        Depends(require_permission("session.delete", "session")),
        Depends(require_permission("workspace.manage", "workspace")),
    ],
)
async def erase_user_data(
    request: Request,
    body: EraseRequest | None = None,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    caller: str = TUserId,
) -> dict[str, Any]:
    body = body or EraseRequest()
    subject = _resolve_subject(request, caller, body.user_id)
    return await PrivacyService(db, tenant_id=tenant_id).erase_user_data(
        subject,
        dry_run=body.dry_run,
        blob_store=getattr(request.app.state, "blob_store", None),
        settings=request.app.state.settings,
        actor_user_id=caller,
        as_admin=_is_admin(request),
    )


@router.get(
    "/privacy/settings",
    dependencies=[Depends(require_permission("session.read", "session"))],
)
async def get_privacy_settings(
    request: Request,
    user_id: str | None = Query(default=None),
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    caller: str = TUserId,
) -> dict[str, Any]:
    subject = _resolve_subject(request, caller, user_id)
    return await PrivacyService(db, tenant_id=tenant_id).get_privacy_settings(subject)


@router.put(
    "/privacy/settings",
    dependencies=[Depends(require_permission("session.update", "session"))],
)
async def put_privacy_settings(
    request: Request,
    body: PrivacySettingsUpdate,
    user_id: str | None = Query(default=None),
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    caller: str = TUserId,
) -> dict[str, Any]:
    subject = _resolve_subject(request, caller, user_id)
    return await PrivacyService(db, tenant_id=tenant_id).set_privacy_settings(
        subject, memory_enabled=body.memory_enabled,
    )


@router.get(
    "/privacy/retention",
    dependencies=[Depends(require_permission("session.read", "session"))],
)
async def get_retention_policy(request: Request) -> dict[str, Any]:
    """The platform's storage-limitation policy, for display to data subjects.

    ``session_retention_days`` is how long a session may sit inactive before
    it is deleted (``null`` when the inactivity sweep is disabled, ``<= 0``).
    ``trash_grace_days`` is how long a deleted session, workspace or canvas is
    kept before its rows and stored files are physically purged (a negative
    value means the hard purge is disabled).
    """
    settings = request.app.state.settings
    retention = int(settings.session_retention_days)
    return {
        "session_retention_days": retention if retention > 0 else None,
        "trash_grace_days": int(settings.trash_grace_days),
    }
