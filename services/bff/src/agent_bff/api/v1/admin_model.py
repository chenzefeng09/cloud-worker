"""BFF admin proxy — LLM model management, *tenant-scoped* surfaces.

Proxy target: foundation llm-gateway (``settings.foundation_llm_gateway_url``).

Scope split
-----------
The model-management surface is split by *scope*.  This module hosts only the
**tenant-scoped** lanes — delegated to tenant administrators — while the
platform-wide provider directory mutations (POST/PATCH/DELETE /providers) and
the probe diagnostic stay super_admin-only in ``admin.py``:

  • GET  /admin/llm/providers           → model.read   (read provider catalogue)
  • GET  /admin/llm/tenant-defaults      → model.read
  • GET  /admin/llm/tenant-defaults/resolve → model.read
  • PUT  /admin/llm/tenant-defaults      → model.admin

The provider *read* lane is delegated to ``model.read`` (not super_admin) on
purpose: a tenant_admin must enumerate the model catalogue to choose their
tenant's default models — without it the default-model picker would be empty.
Provider *writes* (the shared platform directory) remain super_admin-only and
live in ``admin.py``.

Why this module exists (circular-import avoidance)
--------------------------------------------------
``require_tenant_rbac_bff`` is defined in ``admin_authz``, which imports
``_raise_for_upstream`` from ``admin``.  If these tenant-scoped endpoints stayed
in ``admin`` and imported the guard, ``admin`` ↔ ``admin_authz`` would form an
import cycle.  Mirroring ``admin_billing.py``, the tenant-scoped endpoints live
here, importing the guard from ``admin_authz`` and the helpers/validators from
``admin`` — a one-way edge (admin_model → {admin, admin_authz}) with no cycle.

X-Actor: ADMIN is RETAINED here (and is safe)
---------------------------------------------
llm-gateway's ``PUT /tenant-defaults`` requires ``ctx.actor in {ADMIN, SERVICE}``
to write, and scopes the write to ``ctx.tenant_id`` — the *caller's own* tenant
(the X-Tenant-Id header).  The ADMIN actor therefore only unlocks the "may write
tenant defaults" capability; it is NOT an RLS cross-tenant bypass.  So sending
``X-Actor: ADMIN`` (via ``admin_headers``) for a tenant_admin writes only their
own tenant's defaults.  The authoritative cross-tenant gate is the BFF guard:
``require_tenant_rbac_bff`` 403s any request carrying a foreign ``?tenant_id=``
without ``platform.admin``, so a tenant_admin can never reach another tenant.
This is deliberately UNLIKE the skills/ops rounds (which dropped X-Actor: ADMIN)
— there the actor would have granted a real cross-tenant escalation; here the
gateway makes it tenant-scoped, so it stays.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Request

from agent_bff.api.v1.admin import (
    TenantDefaultsRequest,
    _parse_model,
    _raise_for_upstream,
    admin_headers,
)
from agent_bff.api.v1.admin_authz import require_tenant_rbac_bff

router = APIRouter(tags=["Platform Admin"])

_TIMEOUT = 15


# ─────────────────────────────────────────────────────────────────────────────
# Provider catalogue — read lane (model.read); writes stay super_admin in admin.py
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/admin/llm/providers")
async def list_llm_providers(
    request: Request,
    _auth: tuple = Depends(require_tenant_rbac_bff("model.read")),
) -> dict:
    """List all registered LLM providers (read-only).

    Delegated to tenant administrators (``model.read``) so they can enumerate
    the model catalogue when choosing their tenant defaults.  Provider mutations
    remain super_admin-only (see ``admin.py``).  Retains ``X-Actor: ADMIN``:
    the provider directory is platform-shared and read-only here, and the
    upstream list is not tenant-scoped — the cross-tenant gate in the guard
    already confines non-super callers to their own tenant.
    """
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"{settings.foundation_llm_gateway_url}/providers",
            headers=admin_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return resp.json()


# ─────────────────────────────────────────────────────────────────────────────
# LLM Tenant Defaults — proxy to llm-gateway /tenant-defaults
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/admin/llm/tenant-defaults")
async def get_tenant_defaults(
    request: Request,
    _auth: tuple = Depends(require_tenant_rbac_bff("model.read")),
) -> dict:
    """Return the tenant-level LLM defaults (read → model.read)."""
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"{settings.foundation_llm_gateway_url}/tenant-defaults",
            headers=admin_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return resp.json()


@router.put("/admin/llm/tenant-defaults")
async def update_tenant_defaults(
    request: Request,
    _auth: tuple = Depends(require_tenant_rbac_bff("model.admin")),
) -> dict:
    """Update the tenant-level LLM defaults (write → model.admin).

    Retains ``X-Actor: ADMIN`` (via ``admin_headers``): llm-gateway requires
    ``ctx.actor in {ADMIN, SERVICE}`` to write tenant-defaults and scopes the
    write to ``ctx.tenant_id`` (the caller's own tenant).  The ADMIN actor is
    therefore tenant-scoped, NOT an RLS bypass.  Cross-tenant confinement is
    enforced by the ``model.admin`` guard's cross-tenant lane (a foreign
    ``?tenant_id=`` demands ``platform.admin``), so a tenant_admin writes only
    their own tenant.
    """
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    model = await _parse_model(request, TenantDefaultsRequest)
    body = model.model_dump()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.put(
            f"{settings.foundation_llm_gateway_url}/tenant-defaults",
            json=body,
            headers=admin_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return resp.json()


@router.get("/admin/llm/tenant-defaults/resolve")
async def resolve_tenant_defaults(
    request: Request,
    _auth: tuple = Depends(require_tenant_rbac_bff("model.read")),
) -> dict:
    """Resolve the effective tenant-level LLM defaults (read → model.read)."""
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    params = dict(request.query_params)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"{settings.foundation_llm_gateway_url}/tenant-defaults/resolve",
            params=params,
            headers=admin_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return resp.json()
