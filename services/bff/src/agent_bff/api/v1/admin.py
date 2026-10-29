"""BFF admin proxy — platform-level management endpoints.

Prefix: /admin

All endpoints require the caller to be an authenticated super_admin.
The BFF enforces this check client-side (fast 403 before hitting foundation),
and foundation-identity enforces it again server-side (double check).

Security design:
────────────────
  foundation-identity (/platform/*) sets actor=ADMIN server-side after its own
  super_admin re-check, so those proxies forward only the normal foundation
  headers (X-Tenant-Id, X-User-Id, Authorization).

  The llm-gateway, by contrast, trusts an explicit ``X-Actor`` header from the
  BFF for privileged LLM-management mutations: it requires
  ``X-Actor ∈ {ADMIN, SERVICE}`` (e.g. PUT /tenant-defaults) and rejects others
  with 403.  Since every LLM-management endpoint here has already passed
  ``require_super_admin_bff``, the BFF asserts ``X-Actor: ADMIN`` to the gateway
  via ``admin_headers(...)``.

  The BFF's job is:
    1. Verify the caller's session cookie is valid (require_auth).
    2. Verify the caller has action=platform.admin (require_super_admin_bff).
    3. Forward to the upstream with the appropriate header set.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ValidationError, field_validator

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth

router = APIRouter(tags=["Platform Admin"])
logger = logging.getLogger(__name__)

_AUTHZ_TIMEOUT = 2.0

# Allowed LLM purposes — kept in sync with the gateway's enum.
_ALLOWED_PURPOSES = frozenset({"llm", "embedding", "rerank", "tts", "stt"})


def admin_headers(
    tenant_id: str, user_id: str, access_token: str,
) -> dict[str, str]:
    """foundation_headers + X-Actor: ADMIN.

    Every endpoint that uses this has already passed ``require_super_admin_bff``,
    so it is safe for the BFF to assert an ADMIN actor to the gateway.  The
    gateway requires ``X-Actor ∈ {ADMIN, SERVICE}`` for privileged LLM-management
    mutations (e.g. PUT /tenant-defaults).
    """
    headers = foundation_headers(tenant_id, user_id, access_token)
    headers["X-Actor"] = "ADMIN"
    return headers


class TenantDefaultsRequest(BaseModel):
    """Minimal validation for PUT /admin/llm/tenant-defaults.

    The gateway owns the full field set; the BFF only guards the three
    required identifiers so a malformed write fails fast at the edge.
    """

    model_config = {"extra": "allow"}

    purpose: str
    model_id: str
    provider_id: str

    @field_validator("purpose")
    @classmethod
    def _validate_purpose(cls, v: str) -> str:
        if v not in _ALLOWED_PURPOSES:
            raise ValueError(
                f"purpose must be one of {sorted(_ALLOWED_PURPOSES)}"
            )
        return v

    @field_validator("model_id", "provider_id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("must be a non-empty string")
        return v


async def _parse_model(request: Request, model: type[BaseModel]) -> BaseModel:
    """Parse + validate the request body against ``model``, → 422 on failure."""
    raw = await request.json() if await request.body() else {}
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_url=False, include_context=False),
        ) from exc


async def _require_nonempty_body(request: Request) -> dict:
    """Parse the JSON body, rejecting an empty/blank payload with 400.

    Field-level validation is owned by the gateway for pass-through endpoints
    (providers CRUD, probe); the BFF only refuses a completely empty body.
    """
    raw = await request.json() if await request.body() else {}
    if not raw:
        raise HTTPException(status_code=400, detail="request body must not be empty")
    return raw


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
        ) from exc

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
        ) from None

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
        provisioning = resp.json()
        new_tid = provisioning.get("tenant_id")

        # Provisioning returns {tenant_id, admin_user_id, root_ou_id,
        # subscription_id, steps} — not the full tenant shape the frontend
        # renders ({id, name, tier, status, created_at}). Fetch the full tenant
        # object so the admin UI doesn't crash on missing fields.
        if new_tid:
            tenant_resp = await client.get(
                f"{settings.foundation_identity_url}/platform/tenants/{new_tid}",
                headers=foundation_headers(tenant_id, user_id, access_token),
            )
            if tenant_resp.is_success:
                return tenant_resp.json()

    # Fallback: full lookup failed (or no tenant_id) — return the provisioning
    # body but guarantee an ``id`` field so the frontend has something to key on.
    if new_tid:
        provisioning.setdefault("id", new_tid)
    return provisioning


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
    callers should always supply it).  In that case the BFF also writes the
    effective tenant back into the forwarded body's ``tenant_id`` field so it
    stays consistent with the ``X-Tenant-Id`` header: foundation-audit
    validates ``tenant_id`` in the request body, so without this the fallback
    would never take effect (the downstream would 422).

    Request body (AuditQueryRequest subset forwarded verbatim):
        tenant_id   str       required for meaningful cross-tenant admin view
        actor_id    str       optional
        event_type  str       optional  (e.g. "tenant.create")
        event_types list[str] optional  filter by multiple event types
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

    # foundation-audit validates ``tenant_id`` in the request body (not the
    # X-Tenant-Id header), so when the caller omits it we must inject the
    # effective tenant into the forwarded body to keep the two in sync —
    # otherwise the documented fallback never takes effect and the downstream
    # rejects the query with a 422.
    if not body.get("tenant_id"):
        body["tenant_id"] = target_tenant_id

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.foundation_audit_url}/events:query",
            json=body,
            headers={
                **foundation_headers(target_tenant_id, user_id, access_token),
            },
        )
    _raise_for_upstream(resp)
    # The frontend audit page omits tenant_id when no tenant filter is selected,
    # so the fallback to the admin's own tenant is retained.  Surface the tenant
    # actually queried so the caller is never silently misled about scope.
    result = resp.json()
    if isinstance(result, dict):
        result["effective_tenant_id"] = target_tenant_id
    return result


# ─────────────────────────────────────────────────────────────────────────────
# LLM Model Management — proxy to llm-gateway
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/admin/llm/probe")
async def probe_llm_endpoint(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Probe an LLM endpoint for capability compatibility."""
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    body = await _require_nonempty_body(request)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.foundation_llm_gateway_url}/probe",
            json=body,
            headers=admin_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return resp.json()


# NOTE: ``GET /admin/llm/providers`` is intentionally NOT here.  The provider
# *read* lane is delegated to tenant administrators (``model.read``) so they can
# enumerate the model catalogue needed to choose their tenant defaults; it lives
# in ``admin_model.py`` alongside the tenant-defaults endpoints.  Provider
# mutations (POST/PATCH/DELETE) below stay super_admin-only (platform.admin).


@router.post("/admin/llm/providers", status_code=201)
async def register_llm_provider(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Register a new LLM provider."""
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    body = await _require_nonempty_body(request)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.foundation_llm_gateway_url}/providers",
            json=body,
            headers=admin_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return resp.json()


@router.patch("/admin/llm/providers/{pid}")
async def update_llm_provider(
    pid: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Update an LLM provider (enable/disable, rename, etc.)."""
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    body = await _require_nonempty_body(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.patch(
            f"{settings.foundation_llm_gateway_url}/providers/{pid}",
            json=body,
            headers=admin_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return resp.json()


@router.delete("/admin/llm/providers/{pid}")
async def delete_llm_provider(
    pid: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Delete an LLM provider."""
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(
            f"{settings.foundation_llm_gateway_url}/providers/{pid}",
            headers=admin_headers(tenant_id, user_id, access_token),
        )
    _raise_for_upstream(resp)
    return {"status": "deleted"}


# NOTE: the LLM *tenant-defaults* endpoints (GET / PUT / resolve) and the
# provider *read* lane (GET /providers) live in ``admin_model.py``.  They are
# tenant-scoped surfaces delegated to tenant administrators (model.read /
# model.admin) and therefore guarded by ``require_tenant_rbac_bff`` rather than
# ``require_super_admin_bff``.  Keeping them out of this module avoids a circular
# import: ``admin_authz`` (which defines ``require_tenant_rbac_bff``) imports
# from ``admin``, so ``admin`` must not import from ``admin_authz``.  See
# ``admin_model.py``.


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _raise_for_upstream(resp: httpx.Response) -> None:
    """Surface upstream 4xx/5xx as HTTPException to the SPA.

    The full upstream body is preserved under ``detail``. When the upstream
    speaks RFC7807 problem+json, its user-facing ``user_message`` is also
    lifted alongside the nested document so the SPA can show a friendly message
    without digging through the problem body. Note that header transport is
    avoided on purpose: ``user_message`` may be non-ASCII (upstream text) and
    HTTP header values are latin-1, which would crash on encode.
    """
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        if isinstance(body, dict) and body.get("user_message") is not None:
            # Keep the original problem document under ``detail`` (backward
            # compatible: callers still read detail["detail"], detail["code"],
            # …) and surface ``user_message`` at the same level for the SPA.
            raise HTTPException(
                status_code=resp.status_code,
                detail={"detail": body, "user_message": body["user_message"]},
            )
        raise HTTPException(status_code=resp.status_code, detail=body)
