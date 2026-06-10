"""BFF admin proxy — billing catalogue / subscription / usage (read + actions).

Proxy target: foundation-billing (``settings.foundation_billing_url``).

NOTE on spec vs. base reality (see delivery report):
  The admin-console spec assumed ``POST /subscriptions/{id}/change-plan`` and
  ``POST /subscriptions/{id}/cancel`` (per billing.yaml).  The *running*
  foundation-billing service (packages/billing api/v1/billing.py) does NOT
  implement those routes — it exposes only ``/plans``, ``/subscriptions``
  (POST), ``/subscriptions/current`` (GET) and ``/quota/usage``.  Therefore the
  change-plan / cancel proxies are NOT shipped here; they are listed as skipped
  in the report.  The "usage" endpoint maps to the real ``GET /quota/usage``
  (no query params upstream — tenant scope comes from X-Tenant-Id).

Tenant scope: pass ``tenant_id`` to inspect another tenant (overrides
``X-Tenant-Id``).

Upstream paths:
    GET /plans                 → {items: BillingPlan[]}
    GET /subscriptions/current → TenantSubscription | {status: NO_SUBSCRIPTION}
    GET /quota/usage           → {usage: {dimension: value}}
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from agent_bff.api._http import foundation_headers
from agent_bff.api.v1.admin import _raise_for_upstream, require_super_admin_bff

router = APIRouter(tags=["Platform Admin"])

_SERVICE = "billing"
_TIMEOUT = 15


def _headers(request: Request, auth: tuple) -> dict[str, str]:
    tenant_id, user_id, access_token = auth
    target_tenant = request.query_params.get("tenant_id") or tenant_id
    return foundation_headers(target_tenant, user_id, access_token)


def _connect_502(exc: httpx.HTTPError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"{_SERVICE} upstream unreachable: {exc}",
    )


@router.get("/admin/billing/plans")
async def list_plans(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Full plan catalogue ({items: [...]})."""
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_billing_url}/plans",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.get("/admin/billing/subscription")
async def get_current_subscription(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """The target tenant's active subscription (or NO_SUBSCRIPTION sentinel)."""
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_billing_url}/subscriptions/current",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.get("/admin/billing/usage")
async def get_usage(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Current usage map for the target tenant ({usage: {dimension: value}}).

    Maps to foundation-billing ``GET /quota/usage`` — tenant scope is taken
    from ``X-Tenant-Id`` upstream (no query params forwarded).
    """
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_billing_url}/quota/usage",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()
