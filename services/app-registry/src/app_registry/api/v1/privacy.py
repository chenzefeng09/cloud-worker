"""Privacy API — what app-registry holds about one user (GDPR Art. 15 / 17).

    GET  /privacy/apps[?user_id=usr_…]                 → UserAppsExport
    POST /privacy/apps/erase {"user_id"?, "dry_run"?}  → UserAppsErasure

Both act on the caller.  An ``X-Actor: ADMIN`` caller — the BFF sends it only
after enforcing a tenant-admin permission — may name another user; anyone else
naming a different user gets 403.  No role permission gates them
(``require_identified_caller``): access and erasure are the data subject's own
rights.  Every query stays inside the caller's tenant.

What erasure does to an App the user authored depends on whether others can
use it (``AppService.erase_user_apps``): one only they could see is deleted,
one shared with the tenant stays without naming them.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app_registry.api.authz_guard import require_identified_caller
from app_registry.api.dependencies import TDbSession, TTenantId, TUserId
from app_registry.errors import PrivacyForbiddenError, PrivacyInvalidSubjectError
from app_registry.schemas.app import (
    AppOut,
    AppReviewTrailEntry,
    AppVersionStatus,
    UserAppsEraseRequest,
    UserAppsErasure,
    UserAppsExport,
)
from app_registry.services.app_service import AppService

router = APIRouter(tags=["Privacy"])

_SUBJECT = Depends(require_identified_caller())
_USER_ID_RE = re.compile(r"^usr_[0-9A-Z]{26}$")


def _resolve_subject(request: Request, caller: str, requested: str | None) -> str:
    """The data subject: the caller, or ``requested`` for an ADMIN actor."""
    requested = (requested or "").strip() or None
    if requested is not None and requested != caller and (
        request.headers.get("X-Actor") != "ADMIN"
    ):
        raise PrivacyForbiddenError()
    subject = requested or caller
    if not _USER_ID_RE.match(subject):
        raise PrivacyInvalidSubjectError(subject)
    return subject


@router.get("/privacy/apps", response_model=UserAppsExport, dependencies=[_SUBJECT])
async def export_user_apps(
    request: Request,
    user_id: str | None = Query(default=None),
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    caller: str = TUserId,
) -> UserAppsExport:
    subject = _resolve_subject(request, caller, user_id)
    authored, trail = await AppService(db, tenant_id=tenant_id).export_user_apps(subject)
    return UserAppsExport(
        subject_user_id=subject,
        apps=[AppOut.model_validate(app) for app in authored],
        review_trail=[
            AppReviewTrailEntry(
                app_id=ver.app_id,
                version_id=ver.id,
                version=ver.version,
                status=AppVersionStatus(ver.status),
                created_at=ver.created_at if ver.created_by == subject else None,
                submitted_at=ver.submitted_at if ver.submitted_by == subject else None,
                reviewed_at=ver.reviewed_at if ver.reviewed_by == subject else None,
                review_note=ver.review_note if ver.reviewed_by == subject else None,
            )
            for ver in trail
        ],
    )


@router.post("/privacy/apps/erase", response_model=UserAppsErasure, dependencies=[_SUBJECT])
async def erase_user_apps(
    request: Request,
    body: UserAppsEraseRequest | None = None,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    caller: str = TUserId,
) -> UserAppsErasure:
    body = body or UserAppsEraseRequest()
    subject = _resolve_subject(request, caller, body.user_id)
    counts = await AppService(db, tenant_id=tenant_id).erase_user_apps(
        subject, dry_run=body.dry_run,
    )
    return UserAppsErasure(subject_user_id=subject, dry_run=body.dry_run, deleted=counts)
