"""MCP server CRUD endpoints (tenant-scoped) + internal cross-tenant read."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_registry.api.authz_guard import require_permission
from mcp_registry.api.dependencies import (
    TDbSession,
    TInternalAuth,
    TTenantId,
    internal_db_session,
)
from mcp_registry.schemas.common import ListPage
from mcp_registry.schemas.mcp_server import McpServerCreate, McpServerOut, McpServerUpdate
from mcp_registry.services.registry_service import RegistryService

router = APIRouter(tags=["MCPServers"])


@router.get("/mcp-servers", response_model=ListPage,
            dependencies=[Depends(require_permission("mcp_server.read", "mcp_server"))])
async def list_servers(
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> ListPage:
    svc = RegistryService(db, tenant_id=tenant_id)
    items = await svc.list_servers()
    payload = [McpServerOut.model_validate(s).model_dump(mode="json") for s in items]
    return ListPage(items=payload, total=len(payload))


@router.post(
    "/mcp-servers",
    status_code=status.HTTP_201_CREATED,
    response_model=McpServerOut,
    dependencies=[Depends(require_permission("mcp_server.create", "mcp_server"))],
)
async def create_server(
    body: McpServerCreate,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> McpServerOut:
    svc = RegistryService(db, tenant_id=tenant_id)
    server = await svc.create(
        name=body.name,
        transport=body.transport.value,
        endpoint=body.endpoint,
        command=body.command,
        env=body.env,
        scopes=body.scopes,
        enabled=body.enabled,
    )
    return McpServerOut.model_validate(server)


@router.get("/mcp-servers/{server_id}", response_model=McpServerOut,
            dependencies=[Depends(require_permission("mcp_server.read", "mcp_server"))])
async def get_server(
    server_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> McpServerOut:
    svc = RegistryService(db, tenant_id=tenant_id)
    server = await svc.get(server_id)
    return McpServerOut.model_validate(server)


@router.patch("/mcp-servers/{server_id}", response_model=McpServerOut,
              dependencies=[Depends(require_permission("mcp_server.update", "mcp_server"))])
async def update_server(
    server_id: str,
    body: McpServerUpdate,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> McpServerOut:
    svc = RegistryService(db, tenant_id=tenant_id)
    changes = body.model_dump(exclude_none=True)
    if "transport" in changes:
        changes["transport"] = changes["transport"].value if hasattr(
            changes["transport"], "value",
        ) else changes["transport"]
    server = await svc.update(server_id, **changes)
    return McpServerOut.model_validate(server)


@router.delete("/mcp-servers/{server_id}", status_code=204,
               dependencies=[Depends(require_permission("mcp_server.delete", "mcp_server"))])
async def delete_server(
    server_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> Response:
    svc = RegistryService(db, tenant_id=tenant_id)
    await svc.delete(server_id)
    return Response(status_code=204)


@router.get(
    "/internal/mcp-servers",
    response_model=ListPage,
    dependencies=[TInternalAuth],
)
async def internal_list(
    tenant_id: str,
    db: AsyncSession = Depends(internal_db_session),
) -> ListPage:
    """For mcp_tenant_injector — enabled MCP servers for ``tenant_id``."""
    svc = RegistryService(db, tenant_id=tenant_id)
    items = await svc.list_for_tenant(tenant_id)
    payload = [McpServerOut.model_validate(s).model_dump(mode="json") for s in items]
    return ListPage(items=payload, total=len(payload))
