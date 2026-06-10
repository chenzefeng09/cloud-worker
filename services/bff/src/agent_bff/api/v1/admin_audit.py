"""BFF admin proxy — audit chain integrity verification.

Proxy target: foundation-audit (``settings.foundation_audit_url``).

NOTE on spec vs. base reality (see delivery report):
  The spec assumed ``POST /chain/verify`` plus ``POST /export`` +
  ``GET /export/{id}``.  The *running* foundation-audit service
  (packages/audit api/v1/events.py) implements integrity verification as
  ``POST /events:verify-integrity`` and has NO export endpoints at all.
  Therefore:
    • ``POST /admin/audit/chain:verify`` is proxied to the real
      ``/events:verify-integrity`` route.
    • The export endpoints are NOT shipped (listed as skipped in the report).

  The upstream verifier requires ``body.tenant_id == X-Tenant-Id`` (cross-tenant
  is blocked), so the BFF derives the forwarded ``X-Tenant-Id`` from the body's
  ``tenant_id`` (falling back to the admin's own tenant), mirroring the existing
  ``/admin/audit/events:query`` proxy in admin.py.

(admin.py already owns ``POST /admin/audit/events:query`` — not redefined here.)
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

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
