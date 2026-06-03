"""Fleet proxy endpoints — sandbox-pool snapshot and scheduler runs.

Proxies to:
* sandbox-pool     GET /sandboxes/_pool
* agent-scheduler  GET /sandboxes  (running containers for tenant)
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Request

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth

router = APIRouter(tags=["Fleet"])


@router.get("/fleet/pool")
async def pool_snapshot(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """Return sandbox pool occupancy from sandbox-pool service."""
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.sandbox_pool_url
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{url}/sandboxes/_pool",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        resp.raise_for_status()
        return resp.json()


@router.get("/fleet/sandboxes")
async def list_sandboxes(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """List running sandbox containers for the tenant from agent-scheduler."""
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.agent_scheduler_url
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{url}/sandboxes",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        resp.raise_for_status()
        return resp.json()
