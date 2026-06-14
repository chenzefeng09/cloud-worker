"""BFF admin proxy — billing catalogue / subscription / usage (read + actions).

Proxy target: foundation-billing (``settings.foundation_billing_url``).

Guard tiers
-----------
foundation-billing performs no per-user authorization, so the BFF guard is the
authoritative enforcement point.  Read-only surfaces are delegated to tenant
administrators via ``require_tenant_rbac_bff("quota.read")``; subscription
mutations and plan-catalogue creation remain platform super_admin-only
(contract-bound — tenants do not self-serve plan changes):

  • GET  /plans                      → quota.read   (read-only catalogue)
  • POST /plans                      → super_admin  (catalogue authoring)
  • GET  /subscription               → quota.read
  • GET  /usage                      → quota.read
  • POST /subscriptions              → super_admin  (create subscription)
  • POST /subscription/change-plan   → super_admin
  • POST /subscription/cancel        → super_admin

The ``quota.read`` guard's cross-tenant lane preserves super_admin reach: a
request carrying ``?tenant_id=`` for a foreign tenant requires platform.admin,
so a tenant_admin is confined to their own tenant.

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

import asyncio

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from agent_bff.api._http import foundation_headers
from agent_bff.api.v1.admin import _raise_for_upstream, require_super_admin_bff
from agent_bff.api.v1.admin_authz import require_tenant_rbac_bff

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
    _auth: tuple = Depends(require_tenant_rbac_bff("quota.read")),
) -> dict:
    """Full plan catalogue ({items: [...]}).

    Read-only plan catalogue.  Gated by ``quota.read`` (not super_admin): a
    tenant_admin viewing their own subscription needs the catalogue to resolve
    plan names / tiers / limits.  The catalogue is global and read-only, so a
    read grant is the right altitude.  Creating a plan (``POST``) stays
    super_admin-only.  The cross-tenant lane (``?tenant_id=`` ≠ own) still
    requires platform.admin, so a tenant_admin cannot peek into a foreign tenant.
    """
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
    _auth: tuple = Depends(require_tenant_rbac_bff("quota.read")),
) -> dict:
    """The target tenant's active subscription (or NO_SUBSCRIPTION sentinel).

    Read-only.  Gated by ``quota.read`` so a tenant_admin can inspect their own
    subscription.  The cross-tenant lane still requires platform.admin.
    Subscription mutations (change-plan / cancel / create) remain super_admin.
    """
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
    _auth: tuple = Depends(require_tenant_rbac_bff("quota.read")),
) -> dict:
    """Current usage map for the target tenant ({usage: {dimension: value}}).

    Maps to foundation-billing ``GET /quota/usage`` — tenant scope is taken
    from ``X-Tenant-Id`` upstream (no query params forwarded).

    Read-only.  Gated by ``quota.read`` so a tenant_admin can inspect their own
    usage; the cross-tenant lane still requires platform.admin.
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


# ─────────────────────────────────────────────────────────────────────────────
# Composite quota view — plan + limits + usage for one tenant, in one call.
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/admin/billing/quota")
async def get_tenant_billing_quota(
    request: Request,
    tenant_id: str,
    _auth: tuple = Depends(require_tenant_rbac_bff("quota.read")),
) -> dict:
    """Return the current plan + usage for a tenant (composite QuotaInfo).

    Read-only.  Gated by ``quota.read`` so a tenant_admin can inspect *their own*
    tenant's quota; the cross-tenant lane requires platform.admin.  The
    ``tenant_id`` is a **required** query parameter and is also what the guard
    reads to decide the lane: a tenant_admin must pass their own ``tenant_id``
    (== caller's tenant → same-tenant lane → authorized by quota.read); a foreign
    ``tenant_id`` triggers the cross-tenant lane and is rejected unless the caller
    holds platform.admin (super_admin).

    Composes three foundation-billing calls (parallel):
      1. GET /subscriptions/current  → resolve the active subscription's
         plan_id / status (no plan/usage embedded).
      2. GET /plans                  → find the matching plan and map its
         `quotas` field → `limits`.
      3. GET /quota/usage            → the tenant's current usage map.

    Response (QuotaInfo shape expected by the frontend):
        {
          "tenant_id": "<tid>",
          "plan": { "id": str, "name": str, "tier": str, "limits": {str: int} } | null,
          "usage": { str: int }
        }

    When the tenant has no active subscription the response is
    ``{tenant_id, plan: None, usage}`` so the frontend can render an empty
    state instead of crashing.
    """
    _admin_tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings

    target_headers = foundation_headers(tenant_id, user_id, access_token)

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        results = await asyncio.gather(
            client.get(
                f"{settings.foundation_billing_url}/subscriptions/current",
                headers=target_headers,
            ),
            client.get(
                f"{settings.foundation_billing_url}/plans",
                headers=target_headers,
            ),
            client.get(
                f"{settings.foundation_billing_url}/quota/usage",
                headers=target_headers,
            ),
            return_exceptions=True,
        )

    # A connection-level failure on any leg must surface as a readable 502
    # rather than a bare 500 from the unhandled exception.
    for r in results:
        if isinstance(r, httpx.HTTPError):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"billing upstream unreachable: {r}",
            )
        if isinstance(r, BaseException):
            raise r

    sub_resp, plans_resp, usage_resp = results

    _raise_for_upstream(sub_resp)
    _raise_for_upstream(plans_resp)
    _raise_for_upstream(usage_resp)

    sub = sub_resp.json()
    usage: dict = usage_resp.json().get("usage") or {}

    # No active subscription → empty plan, but still return usage + valid shape.
    plan_id = sub.get("plan_id")
    if sub.get("status") != "ACTIVE" or not plan_id:
        return {"tenant_id": tenant_id, "plan": None, "usage": usage}

    # Resolve plan details from the catalogue; billing exposes quotas as
    # `quotas`, the frontend reads `limits`.
    plans_list = plans_resp.json().get("items") or []
    matched = next((p for p in plans_list if p.get("id") == plan_id), None)
    if matched is None:
        return {"tenant_id": tenant_id, "plan": None, "usage": usage}

    plan_obj = {
        "id": matched.get("id"),
        "name": matched.get("name"),
        "tier": matched.get("tier"),
        "limits": matched.get("quotas") or {},
    }

    return {"tenant_id": tenant_id, "plan": plan_obj, "usage": usage}
