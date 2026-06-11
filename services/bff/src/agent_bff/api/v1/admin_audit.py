"""BFF admin proxy — audit chain integrity verification.

Proxy target: foundation-audit (``settings.foundation_audit_url``).

Endpoints:
  • ``POST /admin/audit/chain:verify`` → upstream ``POST /events:verify-integrity``
    (integrity verification — the running foundation-audit service implements it
    under that name).
  • ``POST /admin/audit/export`` → upstream ``POST /events:export`` — streams a
    CSV (text/csv + Content-Disposition) or JSON export of an audit query.  The
    BFF buffers the upstream body and re-emits its content / content-type /
    Content-Disposition verbatim (export volume is bounded).

Both routes are cross-tenant-scoped via ``body.tenant_id`` (the upstream
verifier/exporter requires ``body.tenant_id == X-Tenant-Id``, blocking
cross-tenant access), so the BFF derives the forwarded ``X-Tenant-Id`` from the
body's ``tenant_id`` (falling back to the admin's own tenant) and injects it back
into the forwarded body, mirroring the existing ``/admin/audit/events:query``
proxy in admin.py.

(admin.py already owns ``POST /admin/audit/events:query`` — not redefined here.)
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from agent_bff.api._http import foundation_headers
from agent_bff.api.v1.admin import _raise_for_upstream, require_super_admin_bff

router = APIRouter(tags=["Platform Admin"])

_SERVICE = "audit"
_TIMEOUT = 30


def _connect_502(exc: httpx.HTTPError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"{_SERVICE} upstream unreachable: {exc}",
    )


@router.post("/admin/audit/chain:verify")
async def verify_chain(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Verify the audit hash-chain + Merkle signatures for a tenant.

    Proxies to foundation-audit ``POST /events:verify-integrity``.  The target
    tenant is taken from ``body.tenant_id`` (required by the upstream verifier,
    which blocks cross-tenant checks) and used as ``X-Tenant-Id``.

    Request body (forwarded verbatim):
        tenant_id      str   target tenant (also drives X-Tenant-Id)
        service        str   optional — restrict to one service's events
        from_event_id  str   optional — start of range
        limit          int   optional — max events to check
    """
    admin_tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    body = await request.json() if await request.body() else {}

    target_tenant_id: str = body.get("tenant_id") or admin_tenant_id

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_audit_url}/events:verify-integrity",
                json=body,
                headers=foundation_headers(target_tenant_id, user_id, access_token),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    result = resp.json()
    if isinstance(result, dict):
        result["effective_tenant_id"] = target_tenant_id
    return result


@router.post("/admin/audit/export")
async def export_audit_events(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> Response:
    """Export audit events for a tenant as CSV or JSON.

    Proxies to foundation-audit ``POST /events:export?format=csv|json``.  The
    request body carries the same query filters as ``events:query``
    (``AuditQuery``); like that proxy, the BFF resolves the effective target
    tenant from ``body.tenant_id`` (falling back to the admin's own tenant),
    uses it as ``X-Tenant-Id`` and writes it back into the forwarded body so the
    downstream's body-level ``tenant_id`` check passes.

    The upstream returns a CSV file (text/csv + Content-Disposition) or a JSON
    array.  The BFF buffers the response and re-emits its body, content-type and
    Content-Disposition unchanged (export volume is bounded).

    Query params:
        format  str   csv | json (forwarded to the downstream verbatim)
    """
    admin_tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    body = await request.json() if await request.body() else {}

    target_tenant_id: str = body.get("tenant_id") or admin_tenant_id
    if not body.get("tenant_id"):
        body["tenant_id"] = target_tenant_id

    # Forward all query params (notably ``format``) but never the BFF-private
    # ``tenant_id`` override (tenant scope rides X-Tenant-Id + body).
    params = {k: v for k, v in request.query_params.items() if k != "tenant_id"}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_audit_url}/events:export",
                params=params,
                json=body,
                headers=foundation_headers(target_tenant_id, user_id, access_token),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)

    media_type = resp.headers.get("content-type")
    passthrough: dict[str, str] = {}
    disposition = resp.headers.get("content-disposition")
    if disposition:
        passthrough["Content-Disposition"] = disposition
    return Response(
        content=resp.content,
        media_type=media_type,
        headers=passthrough,
    )
