"""BFF billing endpoints — self-service (current user's own tenant).

Prefix: /billing

All endpoints require normal authentication (session cookie).  The caller's
tenant_id is taken from the signed session cookie — it is NOT accepted as a
query parameter, preventing horizontal privilege escalation.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import APIRouter, Depends, Request

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth

router = APIRouter(tags=["Billing"])
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# GET /billing/me — return the caller's own plan + quota usage
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/billing/me")
async def get_my_billing(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """Return the current plan + usage for the authenticated user's tenant.

    This is the non-admin mirror of ``GET /admin/billing/quota``.  The key
    difference is that ``tenant_id`` is derived from the caller's signed
    session cookie (not a query parameter), so a user cannot inspect another
    tenant's billing data.

    Response shape (QuotaInfo, same as admin endpoint):
        {
          "tenant_id": "<tid>",
          "plan": { "id": str, "name": str, "tier": str, "limits": {str: int} } | null,
          "usage": { str: int }
        }

    When the tenant has no active subscription, ``plan`` is ``null`` and
    ``usage`` is still returned so the frontend can render an empty state.
    """
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings

    my_headers = foundation_headers(tenant_id, user_id, access_token)

    async with httpx.AsyncClient(timeout=15) as client:
        sub_resp, plans_resp, usage_resp = await asyncio.gather(
            client.get(
                f"{settings.foundation_billing_url}/subscriptions/current",
                headers=my_headers,
            ),
            client.get(
                f"{settings.foundation_billing_url}/plans",
                headers=my_headers,
            ),
            client.get(
                f"{settings.foundation_billing_url}/quota/usage",
                headers=my_headers,
            ),
        )

    # Surface upstream errors; let foundation's 404/403 propagate
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


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _raise_for_upstream(resp: httpx.Response) -> None:
    """Surface upstream 4xx/5xx as HTTPException to the SPA."""
    from fastapi import HTTPException

    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text or "upstream error"
        raise HTTPException(status_code=resp.status_code, detail=detail)
