"""BFF admin proxy — large-attachment entitlement (two-layer admission).

Proxy target: foundation lifecycle (``settings.foundation_lifecycle_url``).

Mirrors ``admin_lifecycle.py``: all endpoints require super_admin
(``require_super_admin_bff``).  A super_admin acting on another tenant supplies
the optional ``tenant_id`` query parameter to override the forwarded
``X-Tenant-Id`` header.  Tenant scoping on the lifecycle side comes from the
FoundationContext (``X-Tenant-Id``) — never from the request body — so the
proxy only forwards ``foundation_headers`` + the tenant override.

Upstream paths (foundation lifecycle ``large-attachment``):
    GET    /large-attachment/tenant-setting        → {large_attachment_enabled, …}
    PUT    /large-attachment/tenant-setting  {enabled}
    GET    /large-attachment/user-allows           → {items: [...]}
    PUT    /large-attachment/user-allows     {user_id, allowed}
    DELETE /large-attachment/user-allows/{user_id} → {deleted, user_id}

NOTE on the user-allows list shape: the lifecycle service currently wraps the
rows in ``{items: [...]}``.  Some foundation list endpoints instead return a
*bare array*; this proxy passes the upstream JSON through verbatim so the
frontend (which tolerates both shapes) keeps working if the upstream changes.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from agent_bff.api._http import foundation_headers
from agent_bff.api.v1.admin import _raise_for_upstream, require_super_admin_bff

router = APIRouter(tags=["Platform Admin"])

_SERVICE = "lifecycle"
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


# ── tenant layer ─────────────────────────────────────────────────────────────

@router.get("/admin/large-attachment/tenant-setting")
async def get_tenant_setting(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_lifecycle_url}/large-attachment/tenant-setting",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.put("/admin/large-attachment/tenant-setting")
async def put_tenant_setting(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.put(
                f"{settings.foundation_lifecycle_url}/large-attachment/tenant-setting",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


# ── user layer ───────────────────────────────────────────────────────────────

@router.get("/admin/large-attachment/user-allows")
async def list_user_allows(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> list | dict:
    """User allowlist.

    Upstream currently returns ``{items: [...]}`` but a bare array is tolerated
    (passed through verbatim) for forward-compatibility with other foundation
    list endpoints that omit the envelope.
    """
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_lifecycle_url}/large-attachment/user-allows",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.put("/admin/large-attachment/user-allows")
async def put_user_allow(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.put(
                f"{settings.foundation_lifecycle_url}/large-attachment/user-allows",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.delete("/admin/large-attachment/user-allows/{user_id}")
async def delete_user_allow(
    user_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.delete(
                f"{settings.foundation_lifecycle_url}/large-attachment/user-allows/{user_id}",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json() if resp.content else {"deleted": True, "user_id": user_id}
