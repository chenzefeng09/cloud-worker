"""FastAPI dependencies — tenant context + DB session."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from cron_runner.domain.db import get_session


async def current_tenant(
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> str:
    if not x_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Tenant-Id header required",
        )
    return x_tenant_id


async def current_user(
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> str:
    return x_user_id or "SYSTEM"


async def db_session(
    tenant_id: str = Depends(current_tenant),
    user_id: str = Depends(current_user),
) -> AsyncIterator[AsyncSession]:
    async for session in get_session(tenant_id=tenant_id, user_id=user_id):
        yield session


TTenantId = Depends(current_tenant)
TUserId = Depends(current_user)
TDbSession = Depends(db_session)
