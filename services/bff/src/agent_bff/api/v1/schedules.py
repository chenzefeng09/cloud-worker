"""Schedules proxy — forwards CronJob CRUD to the cron-runner service.

Proxies:
  GET    /schedules              → cron-runner GET    /cron-jobs
  POST   /schedules              → cron-runner POST   /cron-jobs
  PATCH  /schedules/{id}         → cron-runner PATCH  /cron-jobs/{id}
  DELETE /schedules/{id}         → cron-runner DELETE /cron-jobs/{id}
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth

router = APIRouter(tags=["Schedules"])


def _cron_runner_url(request: Request) -> str:
    return request.app.state.settings.cron_runner_url.rstrip("/")


def _raise_for_upstream(resp: httpx.Response) -> None:
    """Surface upstream 4xx/5xx as HTTPException."""
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@router.get("/schedules")
async def list_schedules(
    request: Request,
    _auth: tuple = Depends(require_auth()),
):  # type: ignore[no-untyped-def]
    """List the calling tenant's CronJobs from cron-runner."""
    tenant_id, user_id, access_token = _auth
    url = _cron_runner_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{url}/cron-jobs",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        resp.raise_for_status()
        return resp.json()


@router.post("/schedules", status_code=201)
async def create_schedule(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """Create a new CronJob in cron-runner."""
    tenant_id, user_id, access_token = _auth
    url = _cron_runner_url(request)
    body = await request.json()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{url}/cron-jobs",
            json=body,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        _raise_for_upstream(resp)
        return resp.json()


@router.patch("/schedules/{schedule_id}")
async def update_schedule(
    schedule_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """Update a CronJob's fields in cron-runner."""
    tenant_id, user_id, access_token = _auth
    url = _cron_runner_url(request)
    body = await request.json()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.patch(
            f"{url}/cron-jobs/{schedule_id}",
            json=body,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        _raise_for_upstream(resp)
        return resp.json()


@router.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(
    schedule_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> Response:
    """Delete a CronJob from cron-runner."""
    tenant_id, user_id, access_token = _auth
    url = _cron_runner_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(
            f"{url}/cron-jobs/{schedule_id}",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        _raise_for_upstream(resp)
        return Response(status_code=204)
