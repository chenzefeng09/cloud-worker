"""BFF admin proxy — RBAC (roles / permissions / bindings).

Proxy target: foundation-authz (``settings.authz_url``).

Endpoints are gated by ``require_tenant_rbac_bff(action)`` — a reusable
fail-closed BFF guard that delegates these surfaces to *tenant administrators*:

  • Role read/write → ``authz.role.manage``
  • Binding read/write → ``authz.binding.manage``
  • Permission catalogue (global, read-only) → ``authz.role.manage``

The guard's cross-tenant lane preserves super_admin reach: a request carrying
``?tenant_id=`` for a *foreign* tenant requires ``platform.admin`` (super_admin
only), so a tenant_admin operating on someone else's tenant is 403'd before any
upstream call.  A same-tenant request is authorized against the action above.
``_headers()`` still applies the ``?tenant_id=`` override on ``X-Tenant-Id`` —
safe because the guard has already rejected foreign-tenant access for non-super
callers.  foundation-authz re-checks per-action on arrival (dual enforcement).

Upstream paths (foundation-authz REST mirror):
    GET    /roles                 → list (CursorPage {items, next_cursor})
    POST   /roles                 → create (201, Role)
    GET    /roles/{role_id}       → Role
    PATCH  /roles/{role_id}       → Role          (merge-patch semantics)
    DELETE /roles/{role_id}       → 204
    GET    /permissions           → bare array of Permission (filter.service)
    GET    /bindings              → list (CursorPage; ACTIVE only unless filter.status)
    POST   /bindings              → create (201, RoleBinding)
    DELETE /bindings/{binding_id} → 204           (soft revoke)
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth
from agent_bff.api.v1.admin import _raise_for_upstream

router = APIRouter(tags=["Platform Admin"])
logger = logging.getLogger(__name__)

_SERVICE = "authz"
_TIMEOUT = 15
_AUTHZ_TIMEOUT = 2.0


# ── Reusable tenant-RBAC guard (BFF-side fail-closed) ─────────────────────────
#
# The pattern below is the geatekeeper for *tenant-scoped* admin surfaces being
# delegated to tenant administrators.  It models two lanes:
#
#   • Cross-tenant lane — a request that carries ``?tenant_id=`` pointing at a
#     tenant other than the caller's own is a cross-tenant operation and is
#     reserved for the platform super_admin (``platform.admin``).  A
#     tenant_admin passing someone else's tenant is 403'd here.
#   • Same-tenant lane — a request scoped to the caller's own tenant (no
#     ``?tenant_id=`` or one equal to their own) is authorized against the
#     supplied ``action`` (e.g. ``authz.role.manage``).
#
# Modelled on ``admin.require_super_admin_bff`` / ``sessions.require_tenant_admin_bff``:
# a single authz ``/check`` per lane, fail-closed on every error path
# (unreachable / non-200 / unparseable / ``allowed != True`` → 403).


async def _check_allows(
    *, authz_url: str, tenant_id: str, user_id: str, action: str, resource_type: str,
) -> bool:
    """Run one authz ``/check``; True only on an explicit ``allowed: true``.

    Fail-closed: any transport error, non-200, unparseable body, or a decision
    other than ``allowed=True`` raises a 403.  The ``resource_type`` is a
    synthetic namespace token — these management actions are granted via
    TENANT-scoped bindings, so the value does not affect the RBAC decision (it
    only matters for RESOURCE-scoped bindings), it merely satisfies the authz
    ``/check`` request schema's non-empty ``resource_type`` field.
    """
    try:
        async with httpx.AsyncClient(timeout=_AUTHZ_TIMEOUT) as client:
            resp = await client.post(
                f"{authz_url}/check",
                json={
                    "user_id": user_id,
                    "action": action,
                    "resource_type": resource_type,
                },
                headers={"X-Tenant-Id": tenant_id},
            )
    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as exc:
        logger.warning(
            "bff.require_tenant_rbac.authz_unreachable",
            extra={"action": action, "exc": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="authz service unreachable",
        ) from exc

    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"authz returned HTTP {resp.status_code}",
        )

    try:
        body = resp.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="authz response unparseable",
        ) from None

    return body.get("allowed") is True


def require_tenant_rbac_bff(
    action: str,
) -> Callable[..., Awaitable[tuple[str, str, str]]]:
    """Build a fail-closed BFF dependency gating a tenant-scoped RBAC ``action``.

    Returns a FastAPI dependency that, given the authenticated caller:

      1. **Cross-tenant gate** — if the request carries ``?tenant_id=`` and it
         differs from the caller's own tenant, the operation is cross-tenant and
         requires ``platform.admin`` (super_admin only).  Allowed → return;
         denied → 403.  (A tenant_admin operating on a foreign tenant is stopped
         here.)
      2. **Same-tenant lane** — otherwise authorize ``action`` against the
         caller's *own* tenant.  Allowed → return ``(tenant_id, user_id,
         access_token)`` (same shape as the other admin guards); denied → 403.

    super_admin (holding ``*``) satisfies every ``/check`` and therefore passes
    both lanes — its existing cross-tenant ``?tenant_id=`` reach is preserved.
    """

    async def _dep(
        request: Request,
        _auth: tuple = Depends(require_auth()),
    ) -> tuple[str, str, str]:
        tenant_id, user_id, access_token = _auth
        authz_url: str = request.app.state.settings.authz_url

        requested_tenant = request.query_params.get("tenant_id")
        is_cross_tenant = bool(requested_tenant) and requested_tenant != tenant_id

        if is_cross_tenant:
            # Cross-tenant operation → platform.admin required (super_admin only).
            if not await _check_allows(
                authz_url=authz_url,
                tenant_id=tenant_id,
                user_id=user_id,
                action="platform.admin",
                resource_type="platform",
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        "cross-tenant operation requires platform.admin "
                        "(super_admin)"
                    ),
                )
            return tenant_id, user_id, access_token

        # Same-tenant lane → authorize the supplied action against own tenant.
        if not await _check_allows(
            authz_url=authz_url,
            tenant_id=tenant_id,
            user_id=user_id,
            action=action,
            resource_type="authz",
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"permission denied ({action} required)",
            )
        return tenant_id, user_id, access_token

    return _dep


def _headers(request: Request, auth: tuple) -> dict[str, str]:
    """foundation_headers with an optional ``tenant_id`` query override.

    super_admin in tenant A manages any tenant by passing ``?tenant_id=tnt_…``;
    the override is safe because the caller already passed platform.admin.
    """
    tenant_id, user_id, access_token = auth
    target_tenant = request.query_params.get("tenant_id") or tenant_id
    return foundation_headers(target_tenant, user_id, access_token)


def _params(request: Request) -> dict[str, str]:
    """Forward query params except the BFF-private ``tenant_id`` selector."""
    return {k: v for k, v in request.query_params.items() if k != "tenant_id"}


async def _body(request: Request) -> dict:
    return await request.json() if await request.body() else {}


# ── Role body whitelisting (anti-corruption layer) ────────────────────────────
#
# Upstream foundation-authz ``CreateRoleRequest`` / role PATCH bodies are
# ``extra="forbid"``: any unknown key (notably the front-end's legacy ``scope``)
# yields a 422 on every create.  These pure helpers project the incoming body
# onto the exact keys upstream accepts, so a stray field from an older client can
# never reach upstream.  Keys absent from the body are simply omitted (PATCH is
# merge-patch — only present fields are touched).

# Fields accepted by upstream CreateRoleRequest (extra="forbid").
_ROLE_CREATE_KEYS = ("name", "display_name", "description", "permissions")
# Fields accepted by an upstream role merge-patch (name is immutable upstream).
_ROLE_UPDATE_KEYS = ("display_name", "description", "permissions")


def _pick(body: dict, allowed: tuple[str, ...]) -> dict:
    """Return a copy of ``body`` containing only the ``allowed`` keys.

    Pure, no side effects; tolerant of a non-dict body (returns it unchanged).
    Drops every other key (e.g. the front-end's ``scope``) so an ``extra=forbid``
    upstream model never sees an unexpected field.
    """
    if not isinstance(body, dict):
        return body
    return {k: v for k, v in body.items() if k in allowed}


# ── Binding field translation (anti-corruption layer) ─────────────────────────
#
# The admin web client speaks its own ``RoleBinding`` vocabulary
# (subject_type / subject_id / reason / scope_resource_id) while the upstream
# foundation-authz ``CreateBindingRequest`` / ``RoleBindingRead`` use
# principal_kind / principal_id / grant_reason / scope_id (+ scope_kind).
# Upstream's request model is ``extra="forbid"``, so passing the front-end body
# through verbatim yields a 422 on every create.  These two pure helpers
# translate request (front-end → upstream) and response (upstream → front-end);
# ``_binding_list_params`` does the same for the list endpoint's filters.

# request direction: front-end key → upstream key
_BINDING_IN_MAP = {
    "subject_type": "principal_kind",
    "subject_id": "principal_id",
    "reason": "grant_reason",
    "scope_resource_id": "scope_id",
}
# response direction: upstream key → front-end key
_BINDING_OUT_MAP = {
    "principal_kind": "subject_type",
    "principal_id": "subject_id",
    "grant_reason": "reason",
    "scope_id": "scope_resource_id",
}
# upstream keys passed through to the front-end unchanged.
_BINDING_OUT_PASSTHROUGH = (
    "id",
    "tenant_id",
    "role_id",
    "granted_by",
    "granted_at",
    "expires_at",
    "scope_kind",
    # lifecycle: status is ACTIVE / EXPIRED / REVOKED (revocation wins).
    "status",
    "revoked_at",
    "revoked_by",
    "revoked_reason",
)
# list-filter direction: front-end query key → upstream query key.  Upstream
# silently ignores filters it doesn't know, so an untranslated
# ``filter.subject_id`` would list every binding instead of the subject's.
_BINDING_FILTER_MAP = {
    "filter.subject_type": "filter.principal_kind",
    "filter.subject_id": "filter.principal_id",
}


def _binding_in(body: dict) -> dict:
    """Translate a front-end create-binding body to the upstream request shape.

    Pure, no side effects.  Only keys that are actually present (and non-empty
    for optional string fields) are carried over — never inject explicit
    ``null`` values, which would trip upstream validation.  ``scope_kind`` is
    derived from the presence of a scope: a concrete ``scope_id`` → RESOURCE,
    otherwise the tenant-wide default TENANT.
    """
    out: dict = {}
    for fe_key, up_key in _BINDING_IN_MAP.items():
        if fe_key not in body:
            continue
        value = body[fe_key]
        # Treat empty/blank optional strings (e.g. an untouched reason field) as
        # "not provided" so they don't become empty upstream values.
        if isinstance(value, str) and value.strip() == "":
            continue
        if value is None:
            continue
        out[up_key] = value
    # role_id / expires_at carry over verbatim (same name both sides).
    for key in ("role_id", "expires_at"):
        if key not in body:
            continue
        value = body[key]
        if value is None:
            continue
        if isinstance(value, str) and value.strip() == "":
            continue
        out[key] = value
    # Always send scope_kind explicitly (upstream default is TENANT).
    out["scope_kind"] = "RESOURCE" if out.get("scope_id") else "TENANT"
    return out


def _binding_out(item: dict) -> dict:
    """Translate an upstream ``RoleBindingRead`` to the front-end binding shape.

    Tolerant of missing upstream keys (never raises).  Guarantees the keys the
    front-end ``RoleBinding`` type consumes — subject_type / subject_id /
    reason / scope_resource_id — are mapped when present; scope_kind and the
    lifecycle fields (status / revoked_at / revoked_by / revoked_reason) are
    preserved.
    """
    if not isinstance(item, dict):
        return item
    out: dict = {}
    for up_key, fe_key in _BINDING_OUT_MAP.items():
        if up_key in item:
            out[fe_key] = item[up_key]
    for key in _BINDING_OUT_PASSTHROUGH:
        if key in item:
            out[key] = item[key]
    return out


def _binding_list_params(request: Request) -> dict[str, str]:
    """Translate the front-end list-bindings query to the upstream one.

    ``filter.subject_type`` / ``filter.subject_id`` become upstream's
    ``filter.principal_kind`` / ``filter.principal_id``; ``filter.role_id``,
    ``cursor`` and ``limit`` keep their names.  ``filter.status`` is upstream's
    comma-separated lifecycle filter (absent → ACTIVE only); a repeated
    ``filter.status`` is folded into that form instead of keeping only its last
    value.  Values are not validated here — upstream 422s on anything outside
    ACTIVE / EXPIRED / REVOKED and ``_raise_for_upstream`` surfaces it.
    """
    params = _params(request)
    for fe_key, up_key in _BINDING_FILTER_MAP.items():
        if fe_key in params:
            params[up_key] = params.pop(fe_key)
    statuses = request.query_params.getlist("filter.status")
    if statuses:
        params["filter.status"] = ",".join(statuses)
    return params


def _connect_502(exc: httpx.HTTPError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"{_SERVICE} upstream unreachable: {exc}",
    )


# ── Roles ────────────────────────────────────────────────────────────────────

@router.get("/admin/authz/roles")
async def list_roles(
    request: Request,
    _auth: tuple = Depends(require_tenant_rbac_bff("authz.role.manage")),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.authz_url}/roles",
                params=_params(request), headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/authz/roles", status_code=201)
async def create_role(
    request: Request,
    _auth: tuple = Depends(require_tenant_rbac_bff("authz.role.manage")),
) -> dict:
    settings = request.app.state.settings
    # Whitelist to upstream's CreateRoleRequest keys — drop any stray field (e.g.
    # a legacy ``scope``) that the extra="forbid" upstream model would 422 on.
    body = _pick(await _body(request), _ROLE_CREATE_KEYS)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.authz_url}/roles",
                json=body, headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.get("/admin/authz/roles/{role_id}")
async def get_role(
    role_id: str,
    request: Request,
    _auth: tuple = Depends(require_tenant_rbac_bff("authz.role.manage")),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.authz_url}/roles/{role_id}",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.patch("/admin/authz/roles/{role_id}")
async def update_role(
    role_id: str,
    request: Request,
    _auth: tuple = Depends(require_tenant_rbac_bff("authz.role.manage")),
) -> dict:
    settings = request.app.state.settings
    # Whitelist to the keys an upstream role merge-patch accepts (name is
    # immutable upstream); drop any extra field a stale client might send.
    body = _pick(await _body(request), _ROLE_UPDATE_KEYS)
    # authz role PATCH uses application/merge-patch+json semantics upstream.
    headers = _headers(request, _auth)
    headers["Content-Type"] = "application/merge-patch+json"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.patch(
                f"{settings.authz_url}/roles/{role_id}",
                json=body, headers=headers,
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.delete("/admin/authz/roles/{role_id}")
async def delete_role(
    role_id: str,
    request: Request,
    _auth: tuple = Depends(require_tenant_rbac_bff("authz.role.manage")),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.delete(
                f"{settings.authz_url}/roles/{role_id}",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return {"status": "deleted"}


# ── Permissions (catalogue) ──────────────────────────────────────────────────

@router.get("/admin/authz/permissions")
async def list_permissions(
    request: Request,
    _auth: tuple = Depends(require_tenant_rbac_bff("authz.role.manage")),
) -> list | dict:
    """Permission catalogue.  Upstream returns a *bare array* (not {items}).

    Gated by ``authz.role.manage`` (not super_admin): a tenant_admin authoring a
    role needs to read the global permission catalogue to pick tokens.  The
    catalogue itself is global/read-only, so a tenant-scoped management grant is
    the right altitude here.
    """
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.authz_url}/permissions",
                params=_params(request), headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


# ── Bindings ─────────────────────────────────────────────────────────────────

@router.get("/admin/authz/bindings")
async def list_bindings(
    request: Request,
    _auth: tuple = Depends(require_tenant_rbac_bff("authz.binding.manage")),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.authz_url}/bindings",
                params=_binding_list_params(request), headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    # Upstream returns a CursorPage {items, next_cursor}; translate each item to
    # the front-end vocabulary while preserving the pagination envelope.
    page = resp.json()
    if isinstance(page, dict) and isinstance(page.get("items"), list):
        page["items"] = [_binding_out(item) for item in page["items"]]
    return page


@router.post("/admin/authz/bindings", status_code=201)
async def create_binding(
    request: Request,
    _auth: tuple = Depends(require_tenant_rbac_bff("authz.binding.manage")),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.authz_url}/bindings",
                json=_binding_in(body), headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return _binding_out(resp.json())


@router.delete("/admin/authz/bindings/{binding_id}")
async def delete_binding(
    binding_id: str,
    request: Request,
    _auth: tuple = Depends(require_tenant_rbac_bff("authz.binding.manage")),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.delete(
                f"{settings.authz_url}/bindings/{binding_id}",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return {"status": "deleted"}
