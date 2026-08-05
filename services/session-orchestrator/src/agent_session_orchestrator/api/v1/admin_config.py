"""Admin platform configuration endpoints.

Guarded by ``X-Actor: ADMIN`` (set by the BFF admin proxy after verifying the
caller's tenant-admin permission).  All keys are tenant-scoped.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.api.dependencies import TDbSession, TTenantId
from agent_session_orchestrator.schemas.workspace import PlatformConfigOut
from agent_session_orchestrator.services.platform_config_service import (
    DEFAULTS,
    PlatformConfigService,
)

router = APIRouter(tags=["Admin Config"])


def _require_admin(request: Request) -> None:
    if request.headers.get("X-Actor") != "ADMIN":
        raise HTTPException(status_code=403, detail="admin access required")


class ConfigUpdate(BaseModel):
    value: Any


@router.get(
    "/admin/config",
    dependencies=[Depends(_require_admin)],
)
async def list_config(
    prefix: str = "workspace.",
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> dict[str, Any]:
    svc = PlatformConfigService(db, tenant_id=tenant_id)
    rows = await svc.list_by_prefix(prefix)
    items = [PlatformConfigOut.model_validate(r).model_dump(mode="json") for r in rows]

    existing_keys = {item["key"] for item in items}
    for key, default_value in DEFAULTS.items():
        if key.startswith(prefix) and key not in existing_keys:
            items.append({"key": key, "value": default_value, "updated_at": None, "updated_by": None})

    return {"items": items}


@router.put(
    "/admin/config/{key:path}",
    dependencies=[Depends(_require_admin)],
)
async def update_config(
    key: str,
    body: ConfigUpdate,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> dict[str, Any]:
    actor = request.headers.get("X-User-Id", "unknown")
    svc = PlatformConfigService(db, tenant_id=tenant_id)
    row = await svc.set(key, body.value, updated_by=actor)
    return PlatformConfigOut.model_validate(row).model_dump(mode="json")
