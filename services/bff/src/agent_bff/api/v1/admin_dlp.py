"""BFF admin proxy — DLP (data-loss prevention).

Proxy target: foundation-dlp (``settings.foundation_dlp_url``).

All endpoints require super_admin (``require_super_admin_bff``).  A super_admin
acting on another tenant supplies the optional ``tenant_id`` query parameter to
override the forwarded ``X-Tenant-Id`` header.

Upstream paths (foundation-dlp; the core router is mounted under ``/core``):
    POST /core/scan
    POST /core/classification:tag
    GET  /core/classification:get   (query: resource_type, resource_id)
    POST /core/check:read
    POST /core/check:download
    GET  /core/events               (query: check_type?, decision?, limit, before?)
    GET  /core/classifications      (query: sensitivity?, resource_type?, limit, before?)
    GET  /core/stats                (query: since?)

foundation-dlp lets only a SERVICE or ADMIN actor write classification labels,
so ``classification:tag`` goes out with ``X-Actor: ADMIN`` (every route here is
super_admin only).  ``scan`` deliberately goes out as an ordinary caller:
foundation-dlp then reports the matches and records the event but writes no
label.  The admin scans text they paste in, which says nothing about the
resource's real content, and a blank resource id becomes the ``adhoc-scan``
placeholder, which must never turn into a label.  Labels are set on purpose,
with ``classification:tag``.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from agent_bff.api._http import foundation_headers
from agent_bff.api.v1.admin import _raise_for_upstream, require_super_admin_bff

router = APIRouter(tags=["Platform Admin"])

_SERVICE = "dlp"
_TIMEOUT = 15


def _headers(request: Request, auth: tuple, *, actor: str | None = None) -> dict[str, str]:
    tenant_id, user_id, access_token = auth
    target_tenant = request.query_params.get("tenant_id") or tenant_id
    headers = foundation_headers(target_tenant, user_id, access_token)
    if actor is not None:
        headers["X-Actor"] = actor
    return headers


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


@router.post("/admin/dlp/scan")
async def scan_content(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_dlp_url}/core/scan",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/dlp/classification:tag", status_code=201)
async def tag_classification(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_dlp_url}/core/classification:tag",
                json=body,
                headers=_headers(request, _auth, actor="ADMIN"),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.get("/admin/dlp/classification:get")
async def get_classification(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Resolve a resource's classification (forwards resource_type/resource_id)."""
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_dlp_url}/core/classification:get",
                params=_forward_params(request),
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.get("/admin/dlp/events")
async def list_events(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Scan/decision history (forwards check_type/decision/limit/before)."""
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_dlp_url}/core/events",
                params=_forward_params(request),
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.get("/admin/dlp/classifications")
async def list_classifications(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Tagged-resource ledger (forwards sensitivity/resource_type/limit/before)."""
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_dlp_url}/core/classifications",
                params=_forward_params(request),
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.get("/admin/dlp/stats")
async def dlp_stats(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Governance overview counters (forwards optional ``since``)."""
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_dlp_url}/core/stats",
                params=_forward_params(request),
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/dlp/check:read")
async def check_read(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_dlp_url}/core/check:read",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/dlp/check:download")
async def check_download(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_dlp_url}/core/check:download",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()
