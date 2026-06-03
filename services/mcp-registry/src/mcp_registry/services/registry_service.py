"""Business logic for the MCP server registry.

Transaction discipline mirrors session-orchestrator: never call
``commit()`` here — the caller's ``get_session`` opened the transaction
along with ``SET LOCAL app.tenant_id`` and our flushes piggyback on it.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_registry.domain.models import McpServer
from mcp_registry.errors import McpServerNameConflictError, McpServerNotFoundError


class RegistryService:
    def __init__(self, db: AsyncSession, *, tenant_id: str) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def list_servers(self) -> list[McpServer]:
        stmt = (
            select(McpServer)
            .where(McpServer.tenant_id == self._tenant_id)
            .order_by(McpServer.name.asc())
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def create(
        self,
        *,
        name: str,
        transport: str,
        endpoint: str | None,
        command: str | None,
        env: dict[str, str],
        scopes: list[str],
        enabled: bool,
    ) -> McpServer:
        server = McpServer(
            tenant_id=self._tenant_id,
            name=name,
            transport=transport,
            endpoint=endpoint,
            command=command,
            env=env or {},
            scopes=scopes or [],
            enabled=enabled,
        )
        self._db.add(server)
        try:
            await self._db.flush()
        except IntegrityError as exc:
            raise McpServerNameConflictError(name) from exc
        await self._db.refresh(server)
        return server

    async def get(self, server_id: str) -> McpServer:
        stmt = select(McpServer).where(
            McpServer.id == server_id,
            McpServer.tenant_id == self._tenant_id,
        )
        result = await self._db.execute(stmt)
        server = result.scalar_one_or_none()
        if server is None:
            raise McpServerNotFoundError(server_id)
        return server

    async def update(self, server_id: str, **changes: object) -> McpServer:
        server = await self.get(server_id)
        for k, v in changes.items():
            if v is not None and hasattr(server, k):
                setattr(server, k, v)
        await self._db.flush()
        await self._db.refresh(server)
        return server

    async def delete(self, server_id: str) -> None:
        server = await self.get(server_id)
        await self._db.delete(server)
        await self._db.flush()

    async def list_for_tenant(self, tenant_id: str) -> list[McpServer]:
        """Internal — list servers for an explicit tenant id.

        Caller is responsible for verifying the service-account scope before
        invoking this; the FastAPI dependency does that check.
        """
        stmt = (
            select(McpServer)
            .where(McpServer.tenant_id == tenant_id, McpServer.enabled.is_(True))
            .order_by(McpServer.name.asc())
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())
