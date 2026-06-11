"""BFF admin proxy — data-protection (masking / residency / cross-border /
destruction / consent).

Proxy target: foundation data-protection (``settings.foundation_data_protection_url``).

All endpoints require super_admin (``require_super_admin_bff``).  A super_admin
acting on another tenant supplies the optional ``tenant_id`` query parameter to
override the forwarded ``X-Tenant-Id`` header.  Downstream is tenant-level and
receives X-Tenant-Id + X-User-Id.

Upstream paths (foundation data-protection):
    GET  /masking-rules
    GET  /data-residency
    POST /cross-border/check
    POST /destruction                         (201)
    GET  /destruction/{request_id}
    POST /destruction/{request_id}/approve    (approver from X-User-Id)
    GET  /consent                             (query: user_id optional)
    POST /consent                             (201)
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from agent_bff.api._http import foundation_headers
from agent_bff.api.v1.admin import _raise_for_upstream, require_super_admin_bff

router = APIRouter(tags=["Platform Admin"])

_SERVICE = "data-protection"
_TIMEOUT = 15


def _headers(request: Request, auth: tuple) -> dict[str, str]:
    tenant_id, user_id, access_token = auth
    target_tenant = request.query_params.get("tenant_id") or tenant_id
    return foundation_headers(target_tenant, user_id, access_token)


def _forward_params(request: Request) -> dict[str, str]:
    """Forwarded query params minus the BFF-private ``tenant_id`` override."""
    return {k: v for k, v in request.query_params.items() if k != "tenant_id"}


async def _body(request: Request) -> dict:
    return await request.json() if await request.body() else {}


def _connect_502(exc: httpx.HTTPError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"{_SERVICE} upstream unreachable: {exc}",
    )


# ── Masking / residency ─────────────────────────────────────────────────────────

@router.get("/admin/data-protection/masking-rules")
async def list_masking_rules(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_data_protection_url}/masking-rules",
                params=_forward_params(request),
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.get("/admin/data-protection/data-residency")
async def get_data_residency(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_data_protection_url}/data-residency",
                params=_forward_params(request),
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


# ── Cross-border ────────────────────────────────────────────────────────────────

@router.post("/admin/data-protection/cross-border/check")
async def cross_border_check(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_data_protection_url}/cross-border/check",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


# ── Destruction ─────────────────────────────────────────────────────────────────

@router.post("/admin/data-protection/destruction", status_code=201)
async def create_destruction(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_data_protection_url}/destruction",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.get("/admin/data-protection/destruction/{request_id}")
async def get_destruction(
    request_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_data_protection_url}/destruction/{request_id}",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/data-protection/destruction/{request_id}/approve")
async def approve_destruction(
    request_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Approve a destruction request; the approver is derived downstream from
    the forwarded ``X-User-Id`` header (empty body)."""
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_data_protection_url}/destruction/{request_id}/approve",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json() if resp.content else {"status": "approved"}


# ── Consent ─────────────────────────────────────────────────────────────────────

@router.get("/admin/data-protection/consent")
async def list_consent(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_data_protection_url}/consent",
                params=_forward_params(request),
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/data-protection/consent", status_code=201)
async def create_consent(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_data_protection_url}/consent",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()
