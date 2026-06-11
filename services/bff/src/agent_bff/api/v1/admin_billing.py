"""BFF admin proxy — billing catalogue / subscription / usage (read + actions).

Proxy target: foundation-billing (``settings.foundation_billing_url``).

foundation-billing now exposes subscription mutations directly:
  ``POST /subscriptions/change-plan`` (body {new_plan_id}) and
  ``POST /subscriptions/cancel`` (empty body); both return the subscription
  object.  These are proxied as ``/admin/billing/subscription/change-plan`` and
  ``/admin/billing/subscription/cancel``.  The "usage" read maps to the real
  ``GET /quota/usage`` (no query params upstream — tenant scope comes from
  X-Tenant-Id).

Tenant scope: pass ``tenant_id`` to inspect / act on another tenant (overrides
``X-Tenant-Id``).

Upstream paths:
    GET  /plans                      → {items: BillingPlan[]}
    POST /plans                      → BillingPlan (create)
    GET  /subscriptions/current      → TenantSubscription | {status: NO_SUBSCRIPTION}
    POST /subscriptions             → TenantSubscription (create)
    POST /subscriptions/change-plan  → TenantSubscription
    POST /subscriptions/cancel       → TenantSubscription
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


@router.post("/admin/billing/plans", status_code=201)
async def create_plan(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Create a new billing plan."""
    settings = request.app.state.settings
    body = await request.json()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_billing_url}/plans",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/billing/subscriptions", status_code=201)
async def create_subscription(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Create a subscription for a tenant (admin-initiated)."""
    settings = request.app.state.settings
    body = await request.json()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_billing_url}/subscriptions",
                json=body,
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


@router.post("/admin/billing/subscription/change-plan")
async def change_plan(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Change the target tenant's subscription plan.

    Forwards ``{new_plan_id}`` to foundation-billing
    ``POST /subscriptions/change-plan``; returns the updated subscription.
    """
    settings = request.app.state.settings
    body = await request.json() if await request.body() else {}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_billing_url}/subscriptions/change-plan",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/billing/subscription/cancel")
async def cancel_subscription(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Cancel the target tenant's subscription.

    Proxies to foundation-billing ``POST /subscriptions/cancel`` (empty body);
    returns the updated subscription.
    """
    settings = request.app.state.settings
    body = await request.json() if await request.body() else {}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_billing_url}/subscriptions/cancel",
                json=body,
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
