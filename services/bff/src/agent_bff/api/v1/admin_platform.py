"""BFF platform-governance admin proxy — modules 7-10 of admin-console v2.

Prefix: /admin

Covers cross-tenant governance over the agent-platform's *own* services
(not the saas-foundation base):

  * Skills           → skill-store        (settings.skill_store_url)
  * MCP servers      → mcp-registry       (settings.mcp_registry_url)
  * Knowledge docs   → knowledge-ingest   (settings.knowledge_ingest_url)
  * Runs / sandboxes → agent-scheduler    (settings.agent_scheduler_url)
  * Sandbox pool     → sandbox-pool       (settings.sandbox_pool_url)
  * Token usage      → session-orchestr.  (settings.session_orchestrator_url)

Guard split
───────────
Two altitudes of guard apply here:

  * Skills / MCP / Knowledge (modules 7-9) are *tenant-scoped* resources that
    are delegated to tenant administrators.  They use
    ``require_admin_module_bff(action)`` — a fail-closed guard that authorizes
    a per-module ``action`` (``skill.create`` / ``mcp_server.create`` /
    ``document.create``) against the caller's own tenant, and reserves
    cross-tenant ``?tenant_id=`` operation for the platform super_admin.  These
    endpoints forward **without** ``X-Actor: ADMIN`` so each upstream service's
    own ``require_permission`` guard re-authorizes the *real* caller per action
    (a tenant_admin holds the ``skill.*`` / ``mcp_server.*`` / ``document.*``
    wildcards; a member, holding only ``*.read``, is denied on writes).
  * Ops runs / sandboxes / usage (module 10) are delegated to tenant admins
    the same way, gated on ``sandbox.read`` — held by ``tenant_admin`` (via the
    ``sandbox.*`` wildcard) but NOT by ``member`` (whose run grants are
    ``run.*`` only, with no ``sandbox.*``) — and forward **without**
    ``X-Actor: ADMIN`` so agent-scheduler's own ``require_permission`` guard
    re-authorizes the real caller (tenant_admin holds ``run.*`` / ``sandbox.*``;
    super_admin holds ``*``).
  * The sandbox-pool snapshot (``/admin/ops/pool``) is platform-level
    infrastructure (process-global, not tenant-scoped) and remains
    platform-super-admin only (``require_super_admin_bff``), still asserting
    ``X-Actor: ADMIN`` downstream.

Why ``skill.create`` (and not ``skill.read``) is the BFF gate action:
the ``member`` system role holds ``skill.read`` / ``document.read`` /
``document.search`` / ``mcp_server.read``, so gating on ``read`` would let a
plain member into these admin modules.  The ``create`` token is held by
``tenant_admin`` (via the ``skill.*`` etc. wildcards) but NOT by ``member`` —
so it is the discriminator that admits tenant admins while excluding members.

Cross-tenant convention
───────────────────────
All downstream services are tenant-scoped (RLS by ``X-Tenant-Id``).  Because
the super_admin lives in their own tenant but must manage any tenant, every
endpoint accepts an optional ``tenant_id`` query parameter.  When supplied it
overrides the ``X-Tenant-Id`` header forwarded downstream; otherwise the
caller's own tenant is used.  ``_scope_headers`` centralises this.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from agent_bff.api._audit import emit_admin_audit
from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import TClearance, require_auth

# Reuse the admin guard + upstream-error helper verbatim so the platform
# proxies share exactly one super_admin enforcement + error-surfacing path.
from agent_bff.api.v1.admin import _raise_for_upstream, require_super_admin_bff

router = APIRouter(tags=["Platform Admin"])
logger = logging.getLogger(__name__)

_AUTHZ_TIMEOUT = 2.0


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _scope_headers(
    auth: tuple[str, str, str],
    *,
    tenant_id: str | None,
    admin_actor: bool = True,
) -> dict[str, str]:
    """Build forward headers, letting the admin override the target tenant.

    ``auth`` is ``(caller_tenant_id, user_id, access_token)``.  When
    ``tenant_id`` is supplied (the super_admin is acting cross-tenant) it
    replaces the caller's tenant in the forwarded ``X-Tenant-Id`` header.

    ``admin_actor`` controls whether ``X-Actor: ADMIN`` is asserted downstream:

      * ``True`` (default) — used by the platform-super-admin-only ops/pool/usage
        endpoints; the upstream trusts the BFF's ADMIN assertion and bypasses its
        own per-user RBAC.
      * ``False`` — used by the tenant-delegated skills/mcp/knowledge endpoints;
        no ``X-Actor`` is sent so each upstream service's ``require_permission``
        guard re-authorizes the *real* caller (by ``X-User-Id`` + ``X-Tenant-Id``)
        per action.  This is what makes per-tenant delegation safe end-to-end.
    """
    caller_tenant, user_id, access_token = auth
    effective = tenant_id or caller_tenant
    hdrs = foundation_headers(effective, user_id, access_token)
    if admin_actor:
        hdrs["X-Actor"] = "ADMIN"
    return hdrs


# ─────────────────────────────────────────────────────────────────────────────
# Tenant-delegated module guard (skills / mcp / knowledge)
# ─────────────────────────────────────────────────────────────────────────────
#
# Mirrors ``admin_authz.require_tenant_rbac_bff``: a fail-closed BFF dependency
# with two lanes.
#
#   • Cross-tenant lane — ``?tenant_id=`` pointing at a foreign tenant is a
#     cross-tenant operation reserved for the platform super_admin
#     (``platform.admin``).  A tenant_admin passing someone else's tenant is
#     403'd here, before any upstream call.
#   • Same-tenant lane — authorize the module's ``action`` (e.g. ``skill.create``)
#     against the caller's own tenant.  ``create`` is chosen deliberately: it is
#     held by ``tenant_admin`` (via the ``skill.*`` wildcard) but not by the
#     ``member`` role (whose grants for these services are read-only), so the
#     gate admits tenant admins while excluding members.
#
# The matching upstream service then re-checks the real per-endpoint action
# (read/create/update/delete/search) on arrival — dual enforcement — because
# these endpoints forward without ``X-Actor: ADMIN``.


async def _check_allows(
    *, authz_url: str, tenant_id: str, user_id: str, action: str, resource_type: str,
) -> bool:
    """Run one authz ``/check``; True only on an explicit ``allowed: true``.

    Fail-closed: any transport error, non-200, unparseable body, or a decision
    other than ``allowed=True`` returns False (the caller raises 403).
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
            "bff.require_admin_module.authz_unreachable",
            extra={"action": action, "exc": str(exc)},
        )
        return False

    if resp.status_code != 200:
        return False
    try:
        body = resp.json()
    except Exception:
        return False
    return body.get("allowed") is True


def require_admin_module_bff(
    action: str,
) -> Callable[..., Awaitable[tuple[str, str, str]]]:
    """Build a fail-closed BFF guard delegating a tenant-scoped admin module.

    ``action`` is the per-module discriminator authorized in the same-tenant
    lane (``skill.create`` / ``mcp_server.create`` / ``document.create``).

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

        if not await _check_allows(
            authz_url=authz_url,
            tenant_id=tenant_id,
            user_id=user_id,
            action=action,
            resource_type=action.split(".", 1)[0],
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"permission denied ({action} required)",
            )
        return tenant_id, user_id, access_token

    return _dep


def _forward_params(request: Request) -> dict[str, str]:
    """All query params except the BFF-private ``tenant_id`` override."""
    return {k: v for k, v in request.query_params.items() if k != "tenant_id"}


async def _audit_admin_read(
    request: Request,
    auth: tuple[str, str, str],
    tenant_id: str | None,
    *,
    event_type: str,
    resource_type: str,
    resource_id: str | None,
    payload: dict[str, Any],
) -> None:
    """Audit an admin read of tenant members' content (best-effort).

    The event is recorded in the tenant whose data was read — the
    ``?tenant_id=`` target for a cross-tenant super_admin, with the actor's
    own tenant noted in the payload.
    """
    caller_tenant, user_id, access_token = auth
    effective = tenant_id or caller_tenant
    if effective != caller_tenant:
        payload = {**payload, "actor_tenant_id": caller_tenant}
    await emit_admin_audit(
        request.app.state.settings,
        tenant_id=effective,
        user_id=user_id,
        access_token=access_token,
        event_type=event_type,
        resource_type=resource_type,
        resource_id=resource_id,
        payload=payload,
    )


def _item_count(body: Any) -> int | None:
    items = body.get("items") if isinstance(body, dict) else None
    return len(items) if isinstance(items, list) else None


async def _proxy_get(
    request: Request,
    url: str,
    auth: tuple[str, str, str],
    tenant_id: str | None,
    *,
    forward_params: bool = False,
    timeout: float = 15,
    admin_actor: bool = True,
    extra_params: dict[str, str] | None = None,
) -> dict:
    headers = _scope_headers(auth, tenant_id=tenant_id, admin_actor=admin_actor)
    params = _forward_params(request) if forward_params else None
    # ``extra_params`` are BFF-injected query args (e.g. ``include_memory`` for
    # the knowledge audit view). They take precedence over any forwarded param.
    if extra_params:
        params = {**(params or {}), **extra_params}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, params=params, headers=headers)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"upstream unreachable: {url} ({exc})",
        ) from exc
    _raise_for_upstream(resp)
    return resp.json()


# ─────────────────────────────────────────────────────────────────────────────
# Module 7 — Skills (→ skill-store)
# ─────────────────────────────────────────────────────────────────────────────
#
# Downstream divergence (skill-store api/v1/skills.py):
#   * Skills are addressed by ``skill_id``, not ``name``.  The BFF path
#     segment ``{name}`` is forwarded verbatim as the skill identifier; the
#     frontend should pass the skill id.
#   * skill-store now supports both ``GET /skills/{id}/versions`` (list all
#     versions of a skill) and skill-level ``DELETE /skills/{id}`` (delete the
#     skill plus all versions); the BFF endpoints below proxy to those paths.

@router.get("/admin/skills")
async def admin_list_skills(
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("skill.create")),
) -> dict:
    """List skills published to a tenant's shelf (cross-tenant via tenant_id)."""
    url = f"{request.app.state.settings.skill_store_url}/skills"
    return await _proxy_get(
        request, url, _auth, tenant_id, forward_params=True, admin_actor=False,
    )


@router.get("/admin/skills/{name}")
async def admin_get_skill(
    name: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("skill.create")),
) -> dict:
    """Get a single skill by id (skill-store keys on skill_id, not name)."""
    url = f"{request.app.state.settings.skill_store_url}/skills/{name}"
    return await _proxy_get(request, url, _auth, tenant_id, admin_actor=False)


@router.get("/admin/skills/{name}/versions")
async def admin_list_skill_versions(
    name: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("skill.create")),
) -> dict:
    """List a skill's versions.

    Proxies to skill-store ``GET /skills/{id}/versions``, which returns a
    ``{items, total}`` envelope of the skill's versions (newest upload first).
    """
    url = f"{request.app.state.settings.skill_store_url}/skills/{name}/versions"
    return await _proxy_get(request, url, _auth, tenant_id, admin_actor=False)


@router.delete("/admin/skills/{name}")
async def admin_delete_skill(
    name: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("skill.create")),
) -> dict:
    """Delete a skill.

    Proxies to skill-store ``DELETE /skills/{id}``, which removes the skill
    along with all of its versions (blobs + rows) and per-user install records.
    """
    headers = _scope_headers(_auth, tenant_id=tenant_id, admin_actor=False)
    url = f"{request.app.state.settings.skill_store_url}/skills/{name}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(url, headers=headers)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"skill-store unreachable ({exc})",
        ) from exc
    _raise_for_upstream(resp)
    return {"status": "deleted"}


# ─────────────────────────────────────────────────────────────────────────────
# Module 8 — MCP servers (→ mcp-registry)
# ─────────────────────────────────────────────────────────────────────────────
#
# Downstream (mcp-registry api/v1/mcp_servers.py) is a full match:
#   GET/POST /mcp-servers, GET/PATCH/DELETE /mcp-servers/{server_id},
#   POST /mcp-servers/probe             (live probe of an *unsaved* config),
#   POST /mcp-servers/{server_id}/test  (live initialize + tools/list probe of
#                                        the stored config, with an optional
#                                        body overriding fields for one probe).
#
# The BFF is a pure passthrough on every MCP endpoint: POST/PATCH forward the
# client JSON body verbatim and GET returns ``resp.json()`` unchanged, so the
# registry's newer connection-config fields (headers / auth / timeout_seconds /
# tool_filter / args) flow through end-to-end without any BFF schema to extend.

@router.get("/admin/mcp/servers")
async def admin_list_mcp_servers(
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("mcp_server.create")),
) -> dict:
    url = f"{request.app.state.settings.mcp_registry_url}/mcp-servers"
    return await _proxy_get(
        request, url, _auth, tenant_id, forward_params=True, admin_actor=False,
    )


@router.post("/admin/mcp/servers", status_code=201)
async def admin_create_mcp_server(
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("mcp_server.create")),
) -> dict:
    headers = _scope_headers(_auth, tenant_id=tenant_id, admin_actor=False)
    body = await request.json() if await request.body() else {}
    url = f"{request.app.state.settings.mcp_registry_url}/mcp-servers"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=body, headers=headers)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"mcp-registry unreachable ({exc})",
        ) from exc
    _raise_for_upstream(resp)
    return resp.json()


# NOTE: declared *before* ``/admin/mcp/servers/{server_id}`` — Starlette matches
# in declaration order, so a later position would make ``probe`` bind as a
# ``server_id``.
@router.post("/admin/mcp/servers/probe")
async def admin_probe_mcp_server(
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("mcp_server.create")),
) -> dict:
    """Probe an unsaved MCP server config (live initialize + tools/list).

    Proxies to mcp-registry ``POST /mcp-servers/probe`` and returns its
    ``{ok, server_info, tools, error}`` response verbatim.  Unlike
    ``/{id}/test`` this needs no stored row, so the admin console can populate
    its tool allow/block picker straight from the create form.

    Conventions are identical to ``admin_test_mcp_server``: the
    ``require_admin_module_bff("mcp_server.create")`` gate, no ``X-Actor:
    ADMIN`` so the registry re-authorizes the real caller, an optional
    ``?tenant_id=`` cross-tenant override for the super_admin, the client body
    forwarded verbatim (it carries the connection config, including plaintext
    ``auth``), and a 30s timeout because the registry performs a real outbound
    handshake.
    """
    headers = _scope_headers(_auth, tenant_id=tenant_id, admin_actor=False)
    body = await request.json() if await request.body() else {}
    url = f"{request.app.state.settings.mcp_registry_url}/mcp-servers/probe"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=body, headers=headers)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"mcp-registry unreachable ({exc})",
        ) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.get("/admin/mcp/servers/{server_id}")
async def admin_get_mcp_server(
    server_id: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("mcp_server.create")),
) -> dict:
    url = f"{request.app.state.settings.mcp_registry_url}/mcp-servers/{server_id}"
    return await _proxy_get(request, url, _auth, tenant_id, admin_actor=False)


@router.patch("/admin/mcp/servers/{server_id}")
async def admin_update_mcp_server(
    server_id: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("mcp_server.create")),
) -> dict:
    headers = _scope_headers(_auth, tenant_id=tenant_id, admin_actor=False)
    body = await request.json() if await request.body() else {}
    url = f"{request.app.state.settings.mcp_registry_url}/mcp-servers/{server_id}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(url, json=body, headers=headers)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"mcp-registry unreachable ({exc})",
        ) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.delete("/admin/mcp/servers/{server_id}")
async def admin_delete_mcp_server(
    server_id: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("mcp_server.create")),
) -> dict:
    headers = _scope_headers(_auth, tenant_id=tenant_id, admin_actor=False)
    url = f"{request.app.state.settings.mcp_registry_url}/mcp-servers/{server_id}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(url, headers=headers)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"mcp-registry unreachable ({exc})",
        ) from exc
    _raise_for_upstream(resp)
    return {"status": "deleted"}


@router.post("/admin/mcp/servers/{server_id}/test")
async def admin_test_mcp_server(
    server_id: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("mcp_server.create")),
) -> dict:
    """Probe a registered MCP server (live initialize + tools/list).

    Proxies to mcp-registry ``POST /mcp-servers/{id}/test`` and returns its
    ``{ok, server_info, tools, error}`` response verbatim.  The registry
    performs the actual outbound probe against the stored server config, so
    the BFF grants a generous 30s timeout (the registry's own
    ``timeout_seconds`` bounds the upstream wait).

    Forwarding conventions match the sibling admin MCP endpoints exactly:
    ``require_admin_module_bff("mcp_server.create")`` gate (admits tenant_admin
    via the ``mcp_server.*`` wildcard, excludes member), no ``X-Actor: ADMIN``
    so the registry's own ``require_permission`` re-authorizes the real caller,
    optional ``?tenant_id=`` cross-tenant override for the super_admin, and the
    client body forwarded as-is so the registry may accept test-time config
    overrides (an empty body probes the stored config unchanged).
    """
    headers = _scope_headers(_auth, tenant_id=tenant_id, admin_actor=False)
    body = await request.json() if await request.body() else {}
    url = (
        f"{request.app.state.settings.mcp_registry_url}"
        f"/mcp-servers/{server_id}/test"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=body, headers=headers)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"mcp-registry unreachable ({exc})",
        ) from exc
    _raise_for_upstream(resp)
    return resp.json()


# ─────────────────────────────────────────────────────────────────────────────
# Module 9 — Knowledge documents (→ knowledge-ingest)
# ─────────────────────────────────────────────────────────────────────────────
#
# Downstream (knowledge-ingest api/v1/documents.py):
#   GET /documents, GET /documents/{document_id},
#   POST /documents/{document_id}/ingest   (= "reingest" — re-runs chunk+embed)
#   DELETE /documents/{document_id}        (removes the doc row + chunks + blob)
#
# knowledge-ingest's GET /documents hides session-memory docs (those stamped
# with an ``origin_session_id``, titled "[memory] …") by default. The admin
# audit listing below forces ``include_memory=true`` so governance sees the
# full tenant set — every user's memory rows included. The ordinary tenant
# library (BFF library.py) omits the flag and stays memory-free.

@router.get("/admin/knowledge/documents")
async def admin_list_documents(
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("document.create")),
) -> dict:
    url = f"{request.app.state.settings.knowledge_ingest_url}/documents"
    body = await _proxy_get(
        request, url, _auth, tenant_id, forward_params=True, admin_actor=False,
        extra_params={"include_memory": "true"},
    )
    # The listing includes members' session memories, whose titles quote
    # their prompts — audited like the session list.
    await _audit_admin_read(
        request, _auth, tenant_id,
        event_type="admin.knowledge.document.list.viewed",
        resource_type="document",
        resource_id=None,
        payload={
            "via": "admin_knowledge", "includes_memory": True,
            "document_count": _item_count(body),
        },
    )
    return body


@router.get("/admin/knowledge/documents/{document_id}")
async def admin_get_document(
    document_id: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("document.create")),
) -> dict:
    url = (
        f"{request.app.state.settings.knowledge_ingest_url}"
        f"/documents/{document_id}"
    )
    body = await _proxy_get(request, url, _auth, tenant_id, admin_actor=False)
    payload: dict[str, Any] = {"via": "admin_knowledge"}
    subject = body.get("origin_user_id") or body.get("uploaded_by_user_id")
    if isinstance(subject, str) and subject:
        payload["subject_user_id"] = subject
    await _audit_admin_read(
        request, _auth, tenant_id,
        event_type="admin.knowledge.document.viewed",
        resource_type="document",
        resource_id=document_id,
        payload=payload,
    )
    return body


@router.get("/admin/knowledge/documents/{document_id}/content")
async def admin_get_document_content(
    document_id: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("document.create")),
    clearance: str = TClearance,
) -> Response:
    """Cross-tenant audit preview — stream a knowledge document's source bytes.

    Fetches the raw blob for governance / audit preview of any tenant's
    documents via ``?tenant_id=``.  Deliberately does NOT use
    ``_scope_headers``: that helper calls ``foundation_headers(...)`` without
    forwarding ``X-User-Clearance``, so knowledge-ingest's fail-closed L1
    default would block a platform super_admin from previewing any L2+
    document.  Instead we build the headers inline, forwarding the caller's
    clearance (super_admin typically L5 sees all; tenant audit carries its own
    clearance) so the service's sensitivity gate holds without spuriously
    locking out audit.
    """
    caller_tenant, user_id, access_token = _auth
    effective = tenant_id or caller_tenant
    headers = foundation_headers(effective, user_id, access_token, clearance=clearance)
    url = (
        f"{request.app.state.settings.knowledge_ingest_url}"
        f"/documents/{document_id}/content"
    )
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(url, headers=headers)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"knowledge-ingest unreachable ({exc})",
        ) from exc
    if 200 <= resp.status_code < 300:
        await _audit_admin_read(
            request, _auth, tenant_id,
            event_type="admin.knowledge.document.content.viewed",
            resource_type="document",
            resource_id=document_id,
            payload={"via": "admin_knowledge_preview", "bytes": len(resp.content)},
        )
    content_type = resp.headers.get("content-type", "application/octet-stream")
    content_disp = resp.headers.get("content-disposition", "")
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=content_type,
        headers={"Content-Disposition": content_disp} if content_disp else {},
    )



@router.post("/admin/knowledge/documents/{document_id}/reingest")
async def admin_reingest_document(
    document_id: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("document.create")),
) -> dict:
    """Re-run chunking + embedding for a document.

    Maps to knowledge-ingest ``POST /documents/{id}/ingest`` (idempotent
    re-ingest).  No request body is required.
    """
    headers = _scope_headers(_auth, tenant_id=tenant_id, admin_actor=False)
    url = (
        f"{request.app.state.settings.knowledge_ingest_url}"
        f"/documents/{document_id}/ingest"
    )
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, headers=headers)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"knowledge-ingest unreachable ({exc})",
        ) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.delete("/admin/knowledge/documents/{document_id}")
async def admin_delete_document(
    document_id: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("document.create")),
) -> dict:
    """Delete a knowledge document.

    Proxies to knowledge-ingest ``DELETE /documents/{id}``, which removes the
    document row (chunks cascade) and best-effort deletes the source blob.
    """
    headers = _scope_headers(_auth, tenant_id=tenant_id, admin_actor=False)
    url = (
        f"{request.app.state.settings.knowledge_ingest_url}"
        f"/documents/{document_id}"
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(url, headers=headers)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"knowledge-ingest unreachable ({exc})",
        ) from exc
    _raise_for_upstream(resp)
    return {"status": "deleted"}


# ─────────────────────────────────────────────────────────────────────────────
# Module 10 — Runs & sandbox governance (→ scheduler / sandbox-pool / orch.)
# ─────────────────────────────────────────────────────────────────────────────
#
# Downstream divergences:
#   * agent-scheduler exposes GET /runs (tenant-wide list); ``GET /admin/ops/runs``
#     proxies to it (params透传).  Per-run detail/cancel live at
#     GET/POST /runs/{run_id}[/cancel].
#   * Scheduler cancel is POST /runs/{run_id}/cancel and looks the container up
#     by the *sandbox/container id*, not the orchestrator run id — the caller
#     must pass the scheduler's run/container id as {run_id}.
#   * Sandbox list/stop: GET /sandboxes, POST /sandboxes/{id}/stop (204).
#   * Pool snapshot: sandbox-pool GET /sandboxes/_pool.
#   * Daily usage: orchestrator GET /runs/token-usage/daily (?days= 透传).

@router.get("/admin/ops/runs")
async def admin_list_runs(
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("sandbox.read")),
) -> dict:
    """List runs via the scheduler's tenant-wide ``GET /runs`` (params透传).

    Each run carries its task text and result summary, so the listing is
    audited (``admin.ops.runs.list.viewed``, counts only).
    """
    url = f"{request.app.state.settings.agent_scheduler_url}/runs"
    body = await _proxy_get(
        request, url, _auth, tenant_id, forward_params=True, admin_actor=False,
    )
    await _audit_admin_read(
        request, _auth, tenant_id,
        event_type="admin.ops.runs.list.viewed",
        resource_type="run",
        resource_id=None,
        payload={"via": "admin_ops", "run_count": _item_count(body)},
    )
    return body


@router.post("/admin/ops/runs/{run_id}/cancel")
async def admin_cancel_run(
    run_id: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("sandbox.read")),
) -> dict:
    """Cancel a run via the scheduler (looked up by container/sandbox id)."""
    headers = _scope_headers(_auth, tenant_id=tenant_id, admin_actor=False)
    body = await request.json() if await request.body() else {}
    url = f"{request.app.state.settings.agent_scheduler_url}/runs/{run_id}/cancel"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=body, headers=headers)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"agent-scheduler unreachable ({exc})",
        ) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.get("/admin/ops/sandboxes")
async def admin_list_sandboxes(
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("sandbox.read")),
) -> dict:
    """List running sandbox containers for a tenant (scheduler)."""
    url = f"{request.app.state.settings.agent_scheduler_url}/sandboxes"
    return await _proxy_get(
        request, url, _auth, tenant_id, forward_params=True, admin_actor=False,
    )


@router.post("/admin/ops/sandboxes/{sandbox_id}/stop")
async def admin_stop_sandbox(
    sandbox_id: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("sandbox.read")),
) -> dict:
    """Stop a running sandbox container (scheduler; 204 upstream)."""
    headers = _scope_headers(_auth, tenant_id=tenant_id, admin_actor=False)
    url = (
        f"{request.app.state.settings.agent_scheduler_url}"
        f"/sandboxes/{sandbox_id}/stop"
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, headers=headers)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"agent-scheduler unreachable ({exc})",
        ) from exc
    _raise_for_upstream(resp)
    return {"status": "stopped"}


@router.get("/admin/ops/pool")
async def admin_pool_snapshot(
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Sandbox-pool occupancy snapshot (GET /sandboxes/_pool).

    The pool snapshot is process-global (not tenant-scoped), but the optional
    tenant_id is still accepted for header uniformity.
    """
    url = f"{request.app.state.settings.sandbox_pool_url}/sandboxes/_pool"
    return await _proxy_get(request, url, _auth, tenant_id)


@router.get("/admin/ops/usage/daily")
async def admin_daily_usage(
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("sandbox.read")),
) -> dict:
    """Per-day token totals (orchestrator GET /runs/token-usage/daily).

    Query params (e.g. ``days``) are forwarded verbatim.
    """
    url = (
        f"{request.app.state.settings.session_orchestrator_url}"
        f"/runs/token-usage/daily"
    )
    return await _proxy_get(
        request, url, _auth, tenant_id, forward_params=True, admin_actor=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Platform Config (→ session-orchestrator)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/admin/platform/config")
async def admin_list_config(
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("platform.admin")),
) -> dict:
    url = (
        f"{request.app.state.settings.session_orchestrator_url}"
        f"/admin/config"
    )
    return await _proxy_get(request, url, _auth, tenant_id, forward_params=True)


@router.put("/admin/platform/config/{key:path}")
async def admin_update_config(
    key: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_admin_module_bff("platform.admin")),
) -> dict:
    url = (
        f"{request.app.state.settings.session_orchestrator_url}"
        f"/admin/config/{key}"
    )
    headers = _scope_headers(_auth, tenant_id=tenant_id, admin_actor=True)
    body = await request.json()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.put(url, json=body, headers=headers)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"upstream unreachable: {url} ({exc})",
        ) from exc
    _raise_for_upstream(resp)
    return resp.json()
