"""Tenant-admin data-subject requests — export / erase one member's data.

Prefix: /api/bff/v1 — gated by ``require_tenant_admin_bff`` (fail-closed authz
``/check``; the same tenant-admin guard as the session-replay surface).

    POST /admin/privacy/users/{user_id}/export
         → JSON download of everything the platform holds about the user
    POST /admin/privacy/users/{user_id}/erase   body {"confirm": "<user_id>"}
         → {"schedules", "platform", "memories_deleted", "llm_preferences", "bff",
            "account"} — ``llm_preferences`` is ``{"skipped": "not_supported"}``:
            the gateway only answers for the calling user

Both run the same fan-out as the self-service routes (``privacy.py``) with
``X-Actor: ADMIN`` and the subject named explicitly, so they stay inside the
admin's own tenant.  Every export and erasure — successful or not — emits an
audit event (``admin.privacy.user.exported`` / ``admin.privacy.user.erased``)
whose payload carries counts only, never exported content.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agent_bff.api.v1.privacy import (
    USER_ID_PATTERN,
    DataSubject,
    UpstreamError,
    build_export,
    emit_privacy_audit,
    erase_with_audit,
    export_download,
    export_failed_detail,
    today,
)
from agent_bff.api.v1.sessions import require_tenant_admin_bff

router = APIRouter(tags=["Admin Privacy"])

_EVENT_EXPORTED = "admin.privacy.user.exported"
_EVENT_ERASED = "admin.privacy.user.erased"

ADMIN_ACCOUNT_NOTE = (
    "The user's sign-in account is not closed by this request. "
    "Close it in user management if it is no longer needed."
)


class AdminEraseRequest(BaseModel):
    confirm: str


def _count(value: Any) -> int | None:
    return len(value) if isinstance(value, list) else None


def _export_counts(export: dict[str, Any]) -> dict[str, Any]:
    platform = export.get("platform")
    platform = platform if isinstance(platform, dict) else {}
    return {
        "sessions": _count(platform.get("sessions")),
        "workspaces": _count(platform.get("workspaces")),
        "memories": _count(export.get("memories")),
        "schedules": _count(export.get("schedules")),
        "api_keys": _count(export.get("api_keys")),
    }


@router.post("/admin/privacy/users/{user_id}/export")
async def admin_export_user_data(
    request: Request,
    user_id: str = Path(pattern=USER_ID_PATTERN),
    _auth: tuple = Depends(require_tenant_admin_bff),
) -> JSONResponse:
    """Export one member's data for a data-subject access request."""
    settings = request.app.state.settings
    subject = DataSubject.for_admin(_auth, user_id)
    try:
        export = await build_export(settings, subject)
    except UpstreamError as exc:
        await emit_privacy_audit(
            settings,
            subject,
            event_type=_EVENT_EXPORTED,
            outcome="FAILURE",
            payload={"via": "admin_dsar", "failed": exc.service},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=export_failed_detail(exc),
        ) from exc
    await emit_privacy_audit(
        settings,
        subject,
        event_type=_EVENT_EXPORTED,
        outcome="SUCCESS",
        payload={"via": "admin_dsar", "counts": _export_counts(export)},
    )
    payload = {"subject_user_id": user_id, **export}
    return export_download(payload, filename=f"user-data-{user_id}-{today()}.json")


@router.post("/admin/privacy/users/{user_id}/erase")
async def admin_erase_user_data(
    request: Request,
    body: AdminEraseRequest,
    user_id: str = Path(pattern=USER_ID_PATTERN),
    _auth: tuple = Depends(require_tenant_admin_bff),
) -> dict[str, Any]:
    """Erase one member's data (the sign-in account is left in place)."""
    if body.confirm != user_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "user_message": "Type the user's ID to confirm the erasure.",
                "code": "PRIVACY_CONFIRMATION_REQUIRED",
                "field": "confirm",
            },
        )
    return await erase_with_audit(
        request.app.state.settings,
        DataSubject.for_admin(_auth, user_id),
        event_type=_EVENT_ERASED,
        account_note=ADMIN_ACCOUNT_NOTE,
    )
