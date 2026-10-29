"""BFF admin proxy — compliance (ROPA / DPIA / DSAR / reports).

Proxy target: foundation-compliance (``settings.foundation_compliance_url``).

All endpoints require super_admin (``require_super_admin_bff``).  A super_admin
acting on another tenant supplies the optional ``tenant_id`` query parameter to
override the forwarded ``X-Tenant-Id`` header.

Upstream paths (foundation-compliance):
    GET/POST  /ropa
    POST      /dpia
    POST      /dpia/{dpia_id}/complete
    POST      /dsar
    GET       /dsar/{dsar_id}
    GET/POST  /reports
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from agent_bff.api._http import foundation_headers
from agent_bff.api.v1.admin import _raise_for_upstream, require_super_admin_bff

router = APIRouter(tags=["Platform Admin"])

_SERVICE = "compliance"
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


# ── ROPA ──────────────────────────────────────────────────────────────────────

@router.get("/admin/compliance/ropa")
async def list_ropa(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_compliance_url}/ropa",
                params=_forward_params(request),
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/compliance/ropa", status_code=201)
async def create_ropa(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_compliance_url}/ropa",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


# ── DPIA ──────────────────────────────────────────────────────────────────────

@router.post("/admin/compliance/dpia", status_code=201)
async def create_dpia(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_compliance_url}/dpia",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/compliance/dpia/{dpia_id}/complete")
async def complete_dpia(
    dpia_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_compliance_url}/dpia/{dpia_id}/complete",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json() if resp.content else {"status": "completed"}


# ── DSAR ──────────────────────────────────────────────────────────────────────

# foundation-compliance ``DsarRequestType`` (GDPR Art. 15-22).  The upstream
# enum is upper-case only; the BFF accepts any case and "-" / " " separators
# (``access``, ``automated-decision``) and forwards the canonical value.
DSAR_REQUEST_TYPES: tuple[str, ...] = (
    "ACCESS",
    "RECTIFICATION",
    "ERASURE",
    "RESTRICTION",
    "PORTABILITY",
    "OBJECTION",
    "AUTOMATED_DECISION",
)


def _invalid_dsar(user_message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"user_message": user_message, "field": "request_type"},
    )


def _normalise_dsar_body(body: object) -> dict:
    """Validate ``request_type`` and replace it with the canonical upper-case value."""
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"user_message": "The request body must be a JSON object."},
        )
    raw = body.get("request_type")
    accepted = ", ".join(DSAR_REQUEST_TYPES)
    if not isinstance(raw, str) or not raw.strip():
        raise _invalid_dsar(f"request_type is required. Use one of: {accepted}.")
    request_type = "_".join(raw.strip().upper().replace("-", " ").split())
    if request_type not in DSAR_REQUEST_TYPES:
        raise _invalid_dsar(f'Unknown request_type "{raw}". Use one of: {accepted}.')
    return {**body, "request_type": request_type}


@router.post("/admin/compliance/dsar", status_code=201)
async def create_dsar(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        raw_body: object = await _body(request)
    except ValueError:  # malformed JSON
        raw_body = None
    body = _normalise_dsar_body(raw_body)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_compliance_url}/dsar",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.get("/admin/compliance/dsar/{dsar_id}")
async def get_dsar(
    dsar_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_compliance_url}/dsar/{dsar_id}",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


# ── Reports ───────────────────────────────────────────────────────────────────

@router.get("/admin/compliance/reports")
async def list_reports(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_compliance_url}/reports",
                params=_forward_params(request),
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/compliance/reports", status_code=201)
async def create_report(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_compliance_url}/reports",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()
