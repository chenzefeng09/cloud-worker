"""Sandbox API endpoints.

Tenant isolation
----------------
``list_sandboxes`` and ``stop_sandbox`` filter on the caller's tenant.  The
tenant id is resolved by :func:`agent_scheduler.api.dependencies.current_tenant`
which raises 401 when the gateway-injected ``X-Tenant-Id`` header is missing,
so reaching the body of these handlers guarantees a non-empty tenant string.

``stop_sandbox`` additionally checks that the target container's
``agent-platform.tenant-id`` label matches the caller's tenant — otherwise a
caller who guessed (or scraped from logs) another tenant's container id
could stop a workload they don't own.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status

from agent_scheduler.api.authz_guard import require_permission
from agent_scheduler.api.dependencies import TRuntime, TTenantId
from agent_scheduler.errors import ContainerNotFoundError
from agent_scheduler.services.docker_runtime import DockerRuntime

router = APIRouter(tags=["Sandboxes"])


@router.get("/sandboxes",
            dependencies=[Depends(require_permission("sandbox.read", "sandbox"))])
async def list_sandboxes(
    runtime: DockerRuntime = TRuntime,
    tenant_id: str = TTenantId,
) -> dict[str, Any]:
    items = runtime.list_containers(tenant_id=tenant_id)
    return {"items": items}


@router.post("/sandboxes/{sandbox_id}/stop", status_code=204,
             dependencies=[Depends(require_permission("sandbox.stop", "sandbox"))])
async def stop_sandbox(
    sandbox_id: str,
    runtime: DockerRuntime = TRuntime,
    tenant_id: str = TTenantId,
) -> Response:
    try:
        owner = runtime.get_container_tenant(sandbox_id)
    except ContainerNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        ) from exc

    if owner != tenant_id:
        # Treat as 404 rather than 403 so the existence of another tenant's
        # container cannot be probed by guessing IDs.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sandbox not found")

    runtime.cancel_run(sandbox_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
