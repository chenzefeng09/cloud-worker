"""BFF admin proxy — RBAC (roles / permissions / bindings).

Proxy target: foundation-authz (``settings.authz_url``).

All endpoints require platform.admin via ``require_super_admin_bff`` (fast BFF
403 + foundation re-check on arrival).  Tenant-scoped authz reads/writes honour
the optional ``tenant_id`` query parameter: when supplied it overrides the
forwarded ``X-Tenant-Id`` so a super_admin in tenant A can manage any tenant.

Upstream paths (foundation-authz REST mirror):
    GET    /roles                 → list (CursorPage {items, next_cursor})
    POST   /roles                 → create (201, Role)
    GET    /roles/{role_id}       → Role
    PATCH  /roles/{role_id}       → Role          (merge-patch semantics)
    DELETE /roles/{role_id}       → 204
    GET    /permissions           → bare array of Permission (filter.service)
    GET    /bindings              → list (CursorPage)
    POST   /bindings              → create (201, RoleBinding)
    DELETE /bindings/{binding_id} → 204
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from agent_bff.api._http import foundation_headers
from agent_bff.api.v1.admin import _raise_for_upstream, require_super_admin_bff

router = APIRouter(tags=["Platform Admin"])

_SERVICE = "authz"
_TIMEOUT = 15


def _headers(request: Request, auth: tuple) -> dict[str, str]:
    """foundation_headers with an optional ``tenant_id`` query override.

    super_admin in tenant A manages any tenant by passing ``?tenant_id=tnt_…``;
    the override is safe because the caller already passed platform.admin.
    """
    tenant_id, user_id, access_token = auth
    target_tenant = request.query_params.get("tenant_id") or tenant_id
    return foundation_headers(target_tenant, user_id, access_token)


def _params(request: Request) -> dict[str, str]:
    """Forward query params except the BFF-private ``tenant_id`` selector."""
    return {k: v for k, v in request.query_params.items() if k != "tenant_id"}


async def _body(request: Request) -> dict:
    return await request.json() if await request.body() else {}


def _connect_502(exc: httpx.HTTPError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"{_SERVICE} upstream unreachable: {exc}",
    )


# ── Roles ────────────────────────────────────────────────────────────────────

@router.get("/admin/authz/roles")
async def list_roles(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.authz_url}/roles",
                params=_params(request), headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/authz/roles", status_code=201)
async def create_role(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.authz_url}/roles",
                json=body, headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.get("/admin/authz/roles/{role_id}")
async def get_role(
    role_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.authz_url}/roles/{role_id}",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.patch("/admin/authz/roles/{role_id}")
async def update_role(
    role_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    # authz role PATCH uses application/merge-patch+json semantics upstream.
    headers = _headers(request, _auth)
    headers["Content-Type"] = "application/merge-patch+json"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.patch(
                f"{settings.authz_url}/roles/{role_id}",
                json=body, headers=headers,
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.delete("/admin/authz/roles/{role_id}")
async def delete_role(
    role_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.delete(
                f"{settings.authz_url}/roles/{role_id}",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return {"status": "deleted"}


# ── Permissions (catalogue) ──────────────────────────────────────────────────

@router.get("/admin/authz/permissions")
async def list_permissions(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> list | dict:
    """Permission catalogue.  Upstream returns a *bare array* (not {items})."""
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.authz_url}/permissions",
                params=_params(request), headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


# ── Bindings ─────────────────────────────────────────────────────────────────

@router.get("/admin/authz/bindings")
async def list_bindings(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.authz_url}/bindings",
                params=_params(request), headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/authz/bindings", status_code=201)
async def create_binding(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.authz_url}/bindings",
                json=body, headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.delete("/admin/authz/bindings/{binding_id}")
async def delete_binding(
    binding_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.delete(
                f"{settings.authz_url}/bindings/{binding_id}",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return {"status": "deleted"}
