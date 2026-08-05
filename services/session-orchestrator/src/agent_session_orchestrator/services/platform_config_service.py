"""Platform configuration service — key-value settings scoped per tenant.

Transaction discipline mirrors WorkspaceService: never calls commit().
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.domain.models import PlatformConfig

DEFAULTS: dict[str, Any] = {
    "workspace.version_retention_days": 90,
    "workspace.max_versions_per_file": 20,
    "workspace.cleanup_cron": "0 3 * * *",
}


class PlatformConfigService:
    def __init__(self, db: AsyncSession, *, tenant_id: str) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def get(self, key: str, default: Any = None) -> Any:
        stmt = select(PlatformConfig).where(
            PlatformConfig.tenant_id == self._tenant_id,
            PlatformConfig.key == key,
        )
        result = await self._db.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row.value
        if default is not None:
            return default
        return DEFAULTS.get(key)

    async def set(self, key: str, value: Any, *, updated_by: str | None = None) -> PlatformConfig:
        stmt = select(PlatformConfig).where(
            PlatformConfig.tenant_id == self._tenant_id,
            PlatformConfig.key == key,
        )
        result = await self._db.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            row.value = value
            row.updated_at = dt.datetime.now(tz=dt.UTC)
            row.updated_by = updated_by
        else:
            row = PlatformConfig(
                tenant_id=self._tenant_id,
                key=key,
                value=value,
                updated_by=updated_by,
            )
            self._db.add(row)
        await self._db.flush()
        return row

    async def list_by_prefix(self, prefix: str) -> list[PlatformConfig]:
        stmt = (
            select(PlatformConfig)
            .where(
                PlatformConfig.tenant_id == self._tenant_id,
                PlatformConfig.key.startswith(prefix),
            )
            .order_by(PlatformConfig.key)
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())
