"""BFF admin proxy — user management actions (invite / freeze / reset).

Proxy target: foundation-identity (``settings.foundation_identity_url``).

These are the *tenant-scoped* identity ``/users`` routes (NOT the cross-tenant
``/platform/users`` listing already proxied in admin.py).  Because they are
RLS-scoped by ``X-Tenant-Id``, a super_admin acting on another tenant supplies
the optional ``tenant_id`` query parameter to override the forwarded tenant.

Upstream paths (foundation-identity):
    POST   /users                       → 201  (invite — body: email, role, …)
    POST   /users/{user_id}/freeze      → 204  (body: reason)
    POST   /users/{user_id}/unfreeze    → 204
    POST   /users/{user_id}/password:reset → 204

204-returning upstream actions are normalised to ``{"status": "ok"}`` so the
SPA always receives a JSON body.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from agent_bff.api._http import foundation_headers
from agent_bff.api.v1.admin import _raise_for_upstream, require_super_admin_bff

router = APIRouter(tags=["Platform Admin"])

_SERVICE = "identity"
_TIMEOUT = 15


def _headers(request: Request, auth: tuple) -> dict[str, str]:
    tenant_id, user_id, access_token = auth
    target_tenant = request.query_params.get("tenant_id") or tenant_id
    return foundation_headers(target_tenant, user_id, access_token)


async def _body(request: Request) -> dict:
    return await request.json() if await request.body() else {}


def _connect_502(exc: httpx.HTTPError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"{_SERVICE} upstream unreachable: {exc}",
    )


@router.post("/admin/users", status_code=201)
async def invite_user(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Invite a user into the target tenant (upstream POST /users)."""
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_identity_url}/users",
                json=body, headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json() if resp.content else {"status": "invited"}


@router.post("/admin/users/{user_id}/freeze")
async def freeze_user(
    user_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_identity_url}/users/{user_id}/freeze",
                json=body, headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return {"status": "frozen"}


@router.post("/admin/users/{user_id}/unfreeze")
async def unfreeze_user(
    user_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_identity_url}/users/{user_id}/unfreeze",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return {"status": "unfrozen"}


@router.post("/admin/users/{user_id}/password:reset")
async def reset_user_password(
    user_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_identity_url}/users/{user_id}/password:reset",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return {"status": "password_reset"}
