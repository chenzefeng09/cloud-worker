"""Session proxy endpoints — proxy to session-orchestrator.

All downstream calls carry:
* ``Authorization: Bearer <access_token>`` — the OIDC token from login
* ``X-Tenant-Id`` / ``X-User-Id`` — defence-in-depth alongside the JWT

session-orchestrator's gateway will JWKS-verify the bearer token and
populate its own tenant context; the headers are belt + braces so the
service can still operate behind a gateway that doesn't unwrap the JWT.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Request, Response

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth

router = APIRouter(tags=["Sessions"])


async def _orchestrator_url(request: Request) -> str:
    return request.app.state.settings.session_orchestrator_url


@router.get("/sessions")
async def list_sessions(
    request: Request,
    cursor: str | None = None,
    limit: int = 50,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    params = {"limit": limit}
    if cursor:
        params["cursor"] = cursor

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{orch_url}/sessions",
            params=params,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        return resp.json()


@router.post("/sessions", status_code=201)
async def create_session(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    tenant_id, user_id, access_token = _auth
    body = await request.json() if await request.body() else {}

    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{orch_url}/sessions",
            json=body,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        return resp.json()


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{orch_url}/sessions/{session_id}",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        return resp.json()


@router.patch("/sessions/{session_id}")
async def update_session(
    session_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    tenant_id, user_id, access_token = _auth
    body = await request.json() if await request.body() else {}
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.patch(
            f"{orch_url}/sessions/{session_id}",
            json=body,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        return resp.json()


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> Response:
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        await client.delete(
            f"{orch_url}/sessions/{session_id}",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    return Response(status_code=204)
