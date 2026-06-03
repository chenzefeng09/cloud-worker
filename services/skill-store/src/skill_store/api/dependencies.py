"""FastAPI dependencies — tenant context + DB session + blob store handle."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Header, HTTPException, Request, status
from foundation_sdk.fastapi import get_tenant_id, get_user_id
from sqlalchemy.ext.asyncio import AsyncSession

from skill_store.blob.base import BlobStore
from skill_store.config import Settings
from skill_store.domain.db import get_session
from skill_store.errors import InternalScopeRequiredError


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


def get_blob_store(request: Request) -> BlobStore:
    return request.app.state.blob_store


def require_internal_scope(
    x_service_scopes: str | None = Header(default=None, alias="X-Service-Scopes"),
) -> None:
    required = Settings().internal_scope
    if not x_service_scopes:
        raise InternalScopeRequiredError()
    granted = {s.strip() for s in x_service_scopes.split(",") if s.strip()}
    if required not in granted:
        raise InternalScopeRequiredError()


TDbSession = Depends(db_session)
TTenantId = Depends(current_tenant)
TUserId = Depends(current_user)
TBlobStore = Depends(get_blob_store)
TInternalAuth = Depends(require_internal_scope)
