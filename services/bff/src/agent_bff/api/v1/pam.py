"""BFF user-facing PAM proxy — request elevation & view own history.

Proxy target: foundation-pam (``settings.foundation_pam_url``).

All endpoints require standard auth (``require_auth``), NOT super_admin.
Only surfaces the requesting user's own data; admin-level list/approve/revoke
stay in ``admin_pam.py``.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth
from agent_bff.api.v1.admin import _raise_for_upstream

router = APIRouter(tags=["PAM"])

_TIMEOUT = 15


def _headers(auth: tuple) -> dict[str, str]:
    tenant_id, user_id, access_token = auth
    return foundation_headers(tenant_id, user_id, access_token)


def _connect_502(exc: httpx.HTTPError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"pam upstream unreachable: {exc}",
    )


@router.post("/pam/elevations", status_code=201)
async def request_elevation(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    settings = request.app.state.settings
    body = await request.json() if await request.body() else {}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_pam_url}/elevations",
                json=body,
                headers=_headers(_auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.get("/pam/elevations/mine")
async def my_elevations(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_pam_url}/elevations/mine",
                headers=_headers(_auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()
