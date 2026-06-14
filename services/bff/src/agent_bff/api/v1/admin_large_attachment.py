"""BFF admin proxy — large-attachment entitlement (two-layer admission).

Proxy target: foundation lifecycle (``settings.foundation_lifecycle_url``).

Delegated to tenant admins (same pattern as skills / mcp / knowledge): every
endpoint is gated by ``require_admin_module_bff("session.attachment.large.create")``.
The ``session.attachment.large.create`` token is held by ``tenant_admin`` (via
the ``session.*`` wildcard) but NOT by ``member`` (whose session-attachment
grants are ``session.attachment.read`` / ``session.attachment.create`` only,
without ``.large.create``), so the gate admits tenant admins while excluding
members.  A super_admin (holding ``*``) passes both the same-tenant and the
cross-tenant lanes, preserving its existing cross-tenant reach.

A super_admin (or, in the same-tenant lane, a tenant_admin) acting on a tenant
supplies the optional ``tenant_id`` query parameter to override the forwarded
``X-Tenant-Id`` header — a tenant_admin passing a *foreign* tenant is 403'd by
the guard's cross-tenant lane before any upstream call.  Tenant scoping on the
lifecycle side comes from the FoundationContext (``X-Tenant-Id``) — never from
the request body — so the proxy only forwards ``foundation_headers`` + the
tenant override.

Headers / X-Actor
─────────────────
foundation-lifecycle has no per-user authz for this surface; it admits the
request on the static ``X-Foundation-Scopes: large_attachment.admin`` scope
grant (attached by ``_headers`` below) plus ``X-Tenant-Id`` isolation.  The BFF
guard is therefore the authoritative permission check.  ``_headers`` never
sends ``X-Actor`` (it builds plain ``foundation_headers`` + the scope), so no
ADMIN-bypass assertion is forwarded on the tenant-delegated path — consistent
with the skills / mcp / knowledge modules.

Upstream paths (foundation lifecycle ``large-attachment``):
    GET    /large-attachment/tenant-setting        → {large_attachment_enabled, …}
    PUT    /large-attachment/tenant-setting  {enabled}
    GET    /large-attachment/user-allows           → {items: [...]}
    PUT    /large-attachment/user-allows     {user_id, allowed}
    DELETE /large-attachment/user-allows/{user_id} → {deleted, user_id}
    GET    /large-attachment/subject-allows        → {items: [...]}
    PUT    /large-attachment/subject-allows  {subject_type, subject_id, allowed}
    DELETE /large-attachment/subject-allows/{subject_type}/{subject_id} → {deleted, subject_type, subject_id}

NOTE on the user-allows list shape: the lifecycle service currently wraps the
rows in ``{items: [...]}``.  Some foundation list endpoints instead return a
*bare array*; this proxy passes the upstream JSON through verbatim so the
frontend (which tolerates both shapes) keeps working if the upstream changes.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from agent_bff.api._http import foundation_headers
from agent_bff.api.v1.admin import _raise_for_upstream
from agent_bff.api.v1.admin_platform import require_admin_module_bff

# The tenant-delegated module guard.  ``admin_platform`` imports only from
# ``admin`` (not from this module), so importing it here introduces no cycle.
_LARGE_ATTACHMENT_GUARD = require_admin_module_bff("session.attachment.large.create")

router = APIRouter(tags=["Platform Admin"])

_SERVICE = "lifecycle"
_TIMEOUT = 15

# The admin large-attachment surface is scope-gated upstream
# (foundation-lifecycle requires ``large_attachment.admin`` on every
# tenant-setting / user-allows endpoint).  Attach it explicitly rather than
# widening SUPER_ADMIN_DEFAULT_SCOPES, so this reach stays scoped to this
# proxy instead of every super_admin call.
_ADMIN_SCOPES = ("large_attachment.admin",)


def _headers(request: Request, auth: tuple) -> dict[str, str]:
    """Headers for the scope-gated large-attachment admin calls.

    Attaches ``X-Foundation-Scopes: large_attachment.admin`` so the upstream
    (foundation-lifecycle) admits the request — the large-attachment admin
    surface is scope-gated on top of the platform.admin authz check the BFF
    already enforces via require_super_admin_bff.
    """
    tenant_id, user_id, access_token = auth
    target_tenant = request.query_params.get("tenant_id") or tenant_id
    return foundation_headers(
        target_tenant, user_id, access_token, scopes=_ADMIN_SCOPES,
    )


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
    _auth: tuple = Depends(_LARGE_ATTACHMENT_GUARD),
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
    _auth: tuple = Depends(_LARGE_ATTACHMENT_GUARD),
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
    _auth: tuple = Depends(_LARGE_ATTACHMENT_GUARD),
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
    _auth: tuple = Depends(_LARGE_ATTACHMENT_GUARD),
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
    _auth: tuple = Depends(_LARGE_ATTACHMENT_GUARD),
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


# ── subject layer ─────────────────────────────────────────────────────────────

@router.get("/admin/large-attachment/subject-allows")
async def list_subject_allows(
    request: Request,
    _auth: tuple = Depends(_LARGE_ATTACHMENT_GUARD),
) -> list | dict:
    """Subject allowlist (USER / TEAM / ORG_UNIT).

    Upstream returns ``{items: [...]}`` but a bare array is tolerated
    (passed through verbatim) for forward-compatibility.
    """
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_lifecycle_url}/large-attachment/subject-allows",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.put("/admin/large-attachment/subject-allows")
async def put_subject_allow(
    request: Request,
    _auth: tuple = Depends(_LARGE_ATTACHMENT_GUARD),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.put(
                f"{settings.foundation_lifecycle_url}/large-attachment/subject-allows",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.delete("/admin/large-attachment/subject-allows/{subject_type}/{subject_id}")
async def delete_subject_allow(
    subject_type: str,
    subject_id: str,
    request: Request,
    _auth: tuple = Depends(_LARGE_ATTACHMENT_GUARD),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.delete(
                f"{settings.foundation_lifecycle_url}/large-attachment/subject-allows/{subject_type}/{subject_id}",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json() if resp.content else {"deleted": True, "subject_type": subject_type, "subject_id": subject_id}
