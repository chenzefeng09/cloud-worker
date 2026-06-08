"""FastAPI dependencies — tenant context + DB session + internal auth."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Header, HTTPException, Request, status
from foundation_sdk.fastapi import get_tenant_id, get_user_id
from sqlalchemy.ext.asyncio import AsyncSession

from app_registry.config import Settings
from app_registry.domain.db import get_session
from app_registry.errors import InternalScopeRequiredError


async def current_tenant() -> str:
    tid = get_tenant_id()
    if not tid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Tenant-Id header required",
        )
    return tid


async def current_user() -> str:
    return get_user_id() or "SYSTEM"


async def db_session(
    tenant_id: str = Depends(current_tenant),
    user_id: str = Depends(current_user),
) -> AsyncIterator[AsyncSession]:
    async for session in get_session(tenant_id=tenant_id, user_id=user_id):
        yield session


async def internal_db_session(
    tenant_id: str,
    user_id: str = Depends(current_user),
) -> AsyncIterator[AsyncSession]:
    async for session in get_session(
        tenant_id=tenant_id or "SYSTEM", user_id=user_id, actor="SYSTEM",
    ):
        yield session


def require_internal_scope(
    request: Request,
    x_service_scopes: str | None = Header(default=None, alias="X-Service-Scopes"),
) -> None:
    required = Settings().internal_scope
    if not x_service_scopes:
        raise InternalScopeRequiredError()
    granted = {s.strip() for s in x_service_scopes.split(",") if s.strip()}
    if required not in granted:
        raise InternalScopeRequiredError()
    tenant_from_header = get_tenant_id()
    tenant_from_query = request.query_params.get("tenant_id")
    if not tenant_from_header or tenant_from_header != tenant_from_query:
        raise InternalScopeRequiredError()


TDbSession = Depends(db_session)
TTenantId = Depends(current_tenant)
TUserId = Depends(current_user)
TInternalAuth = Depends(require_internal_scope)
