"""BFF admin proxy — platform-level management endpoints.

Prefix: /admin

All endpoints require the caller to be an authenticated super_admin.
The BFF enforces this check client-side (fast 403 before hitting foundation),
and foundation-identity enforces it again server-side (double check).

Security design (aligned with Task 1 middleware hardening):
──────────────────────────────────────────────────────────
  The foundation identity service now REJECTS any inbound X-Actor=ADMIN header
  from external callers (it is silently downgraded to USER in middleware).
  actor=ADMIN is set server-side by the platform-admin endpoint ONLY after the
  super_admin authz check passes — it never travels over HTTP.

  Therefore, this BFF proxy does NOT add X-Actor=ADMIN to forwarded requests.
  The BFF's job is:
    1. Verify the caller's session cookie is valid (require_auth).
    2. Verify the caller has action=platform.admin (require_super_admin_bff).
    3. Forward to identity /platform/* with normal foundation headers
       (X-Tenant-Id, X-User-Id, Authorization).  foundation-identity will
       re-run the authz check internally and set actor=ADMIN in DB scope.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth

router = APIRouter(tags=["Platform Admin"])
logger = logging.getLogger(__name__)

_AUTHZ_TIMEOUT = 2.0


# ─────────────────────────────────────────────────────────────────────────────
# BFF-side super_admin guard (fast 403 before forwarding to foundation)
# ─────────────────────────────────────────────────────────────────────────────

async def require_super_admin_bff(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> tuple[str, str, str]:
    """BFF-side fail-closed check for platform.admin permission.

    Calls authz /check.  If authz is unreachable or returns denied → 403.
    Foundation will re-check on arrival (dual enforcement).
    """
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    authz_url: str = settings.authz_url

    try:
        async with httpx.AsyncClient(timeout=_AUTHZ_TIMEOUT) as client:
            resp = await client.post(
                f"{authz_url}/check",
                json={
                    "user_id": user_id,
                    "action": "platform.admin",
                    "resource_type": "platform",
                },
                headers={"X-Tenant-Id": tenant_id},
            )
    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as exc:
        logger.warning("bff.require_super_admin.authz_unreachable", extra={"exc": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ADMIN: authz service unreachable",
        )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"ADMIN: authz returned HTTP {resp.status_code}",
        )

    try:
        body = resp.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ADMIN: authz response unparseable",
        )

    if body.get("allowed") is not True:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ADMIN: permission denied (platform.admin required)",
        )

    return tenant_id, user_id, access_token


# ─────────────────────────────────────────────────────────────────────────────
# Tenant management — proxy to identity /platform/tenants
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/admin/tenants")
async def list_tenants(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    params = dict(request.query_params)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{settings.foundation_identity_url}/platform/tenants",
            params=params,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/tenants", status_code=201)
async def create_tenant(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    body = await request.json() if await request.body() else {}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.foundation_identity_url}/platform/tenants",
            json=body,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return resp.json()


@router.get("/admin/tenants/{tid}")
async def get_tenant(
    tid: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{settings.foundation_identity_url}/platform/tenants/{tid}",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return resp.json()


@router.patch("/admin/tenants/{tid}")
async def patch_tenant(
    tid: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    body = await request.json() if await request.body() else {}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.patch(
            f"{settings.foundation_identity_url}/platform/tenants/{tid}",
            json=body,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return resp.json()


# ─────────────────────────────────────────────────────────────────────────────
# Cross-tenant user listing — proxy to identity /platform/users
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/admin/users")
async def list_users_cross_tenant(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    params = dict(request.query_params)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{settings.foundation_identity_url}/platform/users",
            params=params,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return resp.json()


# ─────────────────────────────────────────────────────────────────────────────
# Audit — proxy to foundation-audit /events:query
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/admin/audit/events:query")
async def query_audit_events(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Query audit events for a given tenant.

    The super_admin specifies the target tenant via ``tenant_id`` in the
    request body.  The BFF uses that value as ``X-Tenant-Id`` when calling
    the foundation audit service so that row-level security scopes the result
    to only that tenant's events.

    If ``tenant_id`` is omitted from the body the BFF falls back to the
    admin's own tenant (which typically yields no cross-tenant data —
    callers should always supply it).

    Request body (AuditQueryRequest subset forwarded verbatim):
        tenant_id   str       required for meaningful cross-tenant admin view
        actor_id    str       optional
        action      str       optional  (e.g. "tenant.create")
        from        str       optional  ISO-8601 datetime
        to          str       optional  ISO-8601 datetime
        limit       int       optional  default 50
        cursor      str       optional  pagination cursor

    Response: { items: AuditEvent[], next_cursor?: str }
    """
    admin_tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    body = await request.json() if await request.body() else {}

    # Determine the target tenant for RLS — prefer body.tenant_id.
    target_tenant_id: str = body.get("tenant_id") or admin_tenant_id

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.foundation_audit_url}/events:query",
            json=body,
            headers={
                **foundation_headers(target_tenant_id, user_id, access_token),
            },
        )
    _raise_for_upstream(resp)
    return resp.json()


# ─────────────────────────────────────────────────────────────────────────────
# Billing — proxy quota info for a tenant to the admin panel
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/admin/billing/quota")
async def get_tenant_billing_quota(
    request: Request,
    tenant_id: str,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Return the current plan + usage for a tenant.

    Composes two foundation-billing calls:
      * GET /plans          — full plan catalogue (to resolve plan details)
      * GET /subscriptions/current — the tenant's active subscription + usage

    Query parameter:
        tenant_id   str   required — the target tenant whose quota to inspect

    Response (QuotaInfo shape expected by the frontend):
        {
          "tenant_id": "<tid>",
          "plan": { "id": str, "name": str, "tier": str, "limits": {str: int} },
          "usage": { str: int }
        }

    The BFF merges the two billing responses into this shape.  If the billing
    service already returns the combined shape from /subscriptions/current the
    plan details are inlined directly; otherwise the BFF merges /plans.
    """
    admin_tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings

    target_headers = foundation_headers(tenant_id, user_id, access_token)

    async with httpx.AsyncClient(timeout=15) as client:
        sub_resp, plans_resp = await _gather(
            client.get(
                f"{settings.foundation_billing_url}/subscriptions/current",
                headers=target_headers,
            ),
            client.get(
                f"{settings.foundation_billing_url}/plans",
                headers=target_headers,
            ),
        )

    _raise_for_upstream(sub_resp)
    sub = sub_resp.json()

    # Build QuotaInfo.  The subscription response may already embed the plan
    # object; if not we look it up from the plans list.
    plan_obj: dict = sub.get("plan") or {}
    if not plan_obj.get("limits") and plans_resp.status_code == 200:
        try:
            plans_data = plans_resp.json()
            plan_id = sub.get("plan_id") or plan_obj.get("id")
            plans_list = plans_data.get("items") if isinstance(plans_data, dict) else plans_data if isinstance(plans_data, list) else []
            for p in plans_list:
                if p.get("id") == plan_id:
                    plan_obj = p
                    break
        except Exception:
            pass  # best-effort; return whatever we have

    return {
        "tenant_id": tenant_id,
        "plan": plan_obj,
        "usage": sub.get("usage") or {},
    }


# ─────────────────────────────────────────────────────────────────────────────
# LLM Endpoint Diagnostics — proxy to foundation llm-gateway /probe
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/admin/llm/probe")
async def probe_llm_endpoint(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Probe an LLM endpoint for capability compatibility.

    Forwards the request body verbatim to the foundation llm-gateway /probe
    endpoint and returns the compatibility report.

    Request body (ProbeRequest):
        url           str   required — base URL of the LLM endpoint to probe
        api_key       str   required — API key for the endpoint (NOT logged)
        provider_type str   optional — default "openai_compatible"

    Response (ProbeReport shape):
        {
          "url": str,
          "provider_type": str,
          "results": {
            "list_models": bool | str,
            "model_count": str,
            "function_calling": bool | str,
            "streaming": bool | str,
            "context_window_32k": bool | "unknown",
            "context_window_value": str,
            "parallel_tool_calls": bool | str,
          }
        }

    Security: the api_key in the request body is forwarded to the foundation
    llm-gateway service only and is never written to BFF logs.
    """
    _tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    body = await request.json() if await request.body() else {}

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{settings.foundation_llm_gateway_url}/probe",
            json=body,
            headers=foundation_headers(_tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return resp.json()


async def _gather(*coros):
    """Run coroutines concurrently and return results in order."""
    return await asyncio.gather(*coros)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _raise_for_upstream(resp: httpx.Response) -> None:
    """Surface upstream 4xx/5xx as HTTPException to the SPA."""
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail)
