"""BFF admin proxy — SSO identity source management.

Proxy target: foundation-identity (``settings.foundation_identity_url``).

Upstream paths (foundation-identity /sso):
    GET    /sso/providers                      → list[SsoProviderRead]
    POST   /sso/providers                      → 201 SsoProviderRead
    POST   /sso/providers/{provider_id}/enable  → SsoProviderRead
    POST   /sso/providers/{provider_id}/disable → SsoProviderRead
    DELETE /sso/providers/{provider_id}         → 204

Security:
    Every endpoint requires ``platform.admin`` (via ``require_super_admin_bff``).
    The create body may contain sensitive fields (LDAP bind passwords, SAML
    certificates, OAuth client secrets) — the BFF is a thin proxy and MUST NOT
    log request bodies.  Encryption at rest is handled by foundation-identity.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from agent_bff.api._http import foundation_headers
from agent_bff.api.v1.admin import _raise_for_upstream, require_super_admin_bff

router = APIRouter(tags=["Platform Admin"])

_SERVICE = "identity/sso"
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


@router.get("/admin/sso/providers")
async def list_sso_providers(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> list:
    """List enterprise SSO providers configured for the target tenant."""
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_identity_url}/sso/providers",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/sso/providers", status_code=201)
async def create_sso_provider(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Create a new enterprise SSO provider (OIDC / SAML / AD / LDAP / etc.)."""
    settings = request.app.state.settings
    body = await request.json() if await request.body() else {}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_identity_url}/sso/providers",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/sso/providers/{provider_id}/enable")
async def enable_sso_provider(
    provider_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Enable an SSO provider."""
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_identity_url}/sso/providers/{provider_id}/enable",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/sso/providers/{provider_id}/disable")
async def disable_sso_provider(
    provider_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Disable an SSO provider (does not delete configuration)."""
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_identity_url}/sso/providers/{provider_id}/disable",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.delete("/admin/sso/providers/{provider_id}")
async def delete_sso_provider(
    provider_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Permanently delete an SSO provider and its Keycloak IdP mapping."""
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.delete(
                f"{settings.foundation_identity_url}/sso/providers/{provider_id}",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return {"status": "deleted"}


# ─── Platform-scoped providers + tenant routing rules ────────────────────────
#
# Upstream paths (foundation-identity /platform/sso):
#     GET    /platform/sso/providers                                  → {"items": [...]}
#     POST   /platform/sso/providers                                  → 201 SsoProviderRead
#     POST   /platform/sso/providers/{id}/enable | /disable           → SsoProviderRead
#     DELETE /platform/sso/providers/{id}                             → 204
#     GET    /platform/sso/providers/{id}/routing-rules              → {"items": [...]}
#     POST   /platform/sso/providers/{id}/routing-rules             → 201 SsoRoutingRule
#     PATCH  /platform/sso/providers/{id}/routing-rules/{rule_id}   → SsoRoutingRule
#     DELETE /platform/sso/providers/{id}/routing-rules/{rule_id}   → 204
#
# super_admin enforced upstream; the platform scope ignores tenant context so
# we simply forward the caller's own tenant headers via ``_headers``.


@router.get("/admin/sso/platform/providers")
async def list_platform_sso_providers(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """List platform-scoped SSO providers."""
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_identity_url}/platform/sso/providers",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/sso/platform/providers", status_code=201)
async def create_platform_sso_provider(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Create a platform-scoped (or tenant-scoped) SSO provider."""
    settings = request.app.state.settings
    body = await request.json() if await request.body() else {}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_identity_url}/platform/sso/providers",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/sso/platform/providers/{provider_id}/enable")
async def enable_platform_sso_provider(
    provider_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Enable a platform-scoped SSO provider."""
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_identity_url}/platform/sso/providers/{provider_id}/enable",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/sso/platform/providers/{provider_id}/disable")
async def disable_platform_sso_provider(
    provider_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Disable a platform-scoped SSO provider (does not delete configuration)."""
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_identity_url}/platform/sso/providers/{provider_id}/disable",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.delete("/admin/sso/platform/providers/{provider_id}")
async def delete_platform_sso_provider(
    provider_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Permanently delete a platform-scoped SSO provider."""
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.delete(
                f"{settings.foundation_identity_url}/platform/sso/providers/{provider_id}",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return {"status": "deleted"}


@router.get("/admin/sso/platform/providers/{provider_id}/routing-rules")
async def list_platform_sso_routing_rules(
    provider_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """List tenant-routing rules for a platform-scoped SSO provider."""
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_identity_url}/platform/sso/providers/{provider_id}/routing-rules",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post(
    "/admin/sso/platform/providers/{provider_id}/routing-rules", status_code=201
)
async def create_platform_sso_routing_rule(
    provider_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Create a tenant-routing rule for a platform-scoped SSO provider."""
    settings = request.app.state.settings
    body = await request.json() if await request.body() else {}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_identity_url}/platform/sso/providers/{provider_id}/routing-rules",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.patch(
    "/admin/sso/platform/providers/{provider_id}/routing-rules/{rule_id}"
)
async def update_platform_sso_routing_rule(
    provider_id: str,
    rule_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Partially update a tenant-routing rule."""
    settings = request.app.state.settings
    body = await request.json() if await request.body() else {}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.patch(
                f"{settings.foundation_identity_url}/platform/sso/providers/{provider_id}/routing-rules/{rule_id}",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.delete(
    "/admin/sso/platform/providers/{provider_id}/routing-rules/{rule_id}"
)
async def delete_platform_sso_routing_rule(
    provider_id: str,
    rule_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Delete a tenant-routing rule."""
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.delete(
                f"{settings.foundation_identity_url}/platform/sso/providers/{provider_id}/routing-rules/{rule_id}",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return {"status": "deleted"}
