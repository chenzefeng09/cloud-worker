"""Pool acquire / release endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from foundation_sdk.fastapi import get_tenant_id
from pydantic import BaseModel

from sandbox_pool.errors import PoolError
from sandbox_pool.pool import Pool

router = APIRouter(tags=["Sandboxes"])


class AcquireRequest(BaseModel):
    template: str = "base"


class AcquireResponse(BaseModel):
    sandbox_id: str
    template: str
    cold_spawned: bool


def _pool(request: Request) -> Pool:
    return request.app.state.pool


def _require_tenant() -> str:
    tid = get_tenant_id()
    if not tid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Tenant-Id header required",
        )
    return tid


@router.post(
    "/sandboxes/acquire",
    status_code=status.HTTP_201_CREATED,
    response_model=AcquireResponse,
)
async def acquire(body: AcquireRequest, request: Request) -> AcquireResponse:
    tenant_id = _require_tenant()
    pool = _pool(request)
    try:
        entry = await pool.acquire(tenant_id=tenant_id, template=body.template)
    except PoolError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    # Skeleton: we don't track cold-vs-warm yet; return False.  B-M2
    # follow-up exposes this via PoolEntry.spawn_kind.
    return AcquireResponse(
        sandbox_id=entry.sandbox_id,
        template=entry.template,
        cold_spawned=False,
    )


@router.delete(
    "/sandboxes/{sandbox_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def release(sandbox_id: str, request: Request) -> Response:
    _require_tenant()
    pool = _pool(request)
    try:
        await pool.release(sandbox_id)
    except PoolError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sandboxes/_pool")
async def pool_snapshot(request: Request) -> dict:
    """Return pool occupancy.  Wired to the readiness probe + metrics."""
    return _pool(request).snapshot()
