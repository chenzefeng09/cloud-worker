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

Every endpoint is guarded by ``require_super_admin_bff`` (imported from
``admin.py``): the caller must hold ``platform.admin`` or the BFF fast-fails
403 before any upstream call.

Cross-tenant convention
───────────────────────
All downstream services are tenant-scoped (RLS by ``X-Tenant-Id``).  Because
the super_admin lives in their own tenant but must manage any tenant, every
endpoint accepts an optional ``tenant_id`` query parameter.  When supplied it
overrides the ``X-Tenant-Id`` header forwarded downstream; otherwise the
caller's own tenant is used.  ``_scope_headers`` centralises this.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from agent_bff.api._http import foundation_headers

# Reuse the admin guard + upstream-error helper verbatim so the platform
# proxies share exactly one super_admin enforcement + error-surfacing path.
from agent_bff.api.v1.admin import _raise_for_upstream, require_super_admin_bff

router = APIRouter(tags=["Platform Admin"])


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _scope_headers(
    auth: tuple[str, str, str],
    *,
    tenant_id: str | None,
) -> dict[str, str]:
    """Build forward headers, letting the admin override the target tenant.

    ``auth`` is ``(caller_tenant_id, user_id, access_token)``.  When
    ``tenant_id`` is supplied (the super_admin is acting cross-tenant) it
    replaces the caller's tenant in the forwarded ``X-Tenant-Id`` header.
    """
    caller_tenant, user_id, access_token = auth
    effective = tenant_id or caller_tenant
    return foundation_headers(effective, user_id, access_token)


def _forward_params(request: Request) -> dict[str, str]:
    """All query params except the BFF-private ``tenant_id`` override."""
    return {k: v for k, v in request.query_params.items() if k != "tenant_id"}


async def _proxy_get(
    request: Request,
    url: str,
    auth: tuple[str, str, str],
    tenant_id: str | None,
    *,
    forward_params: bool = False,
    timeout: float = 15,
) -> dict:
    headers = _scope_headers(auth, tenant_id=tenant_id)
    params = _forward_params(request) if forward_params else None
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
#   * There is NO ``GET /skills/{id}/versions`` endpoint and NO skill-level
#     ``DELETE /skills/{id}`` in skill-store.  These BFF endpoints forward to
#     the natural paths anyway; until skill-store adds them the upstream
#     responds 404/405 which ``_raise_for_upstream`` surfaces transparently.

@router.get("/admin/skills")
async def admin_list_skills(
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """List skills published to a tenant's shelf (cross-tenant via tenant_id)."""
    url = f"{request.app.state.settings.skill_store_url}/skills"
    return await _proxy_get(request, url, _auth, tenant_id, forward_params=True)


@router.get("/admin/skills/{name}")
async def admin_get_skill(
    name: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Get a single skill by id (skill-store keys on skill_id, not name)."""
    url = f"{request.app.state.settings.skill_store_url}/skills/{name}"
    return await _proxy_get(request, url, _auth, tenant_id)


@router.get("/admin/skills/{name}/versions")
async def admin_list_skill_versions(
    name: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """List a skill's versions.

    NOTE: skill-store has no public ``/skills/{id}/versions`` route today;
    this proxies to that path so the endpoint lights up automatically once
    skill-store adds it.  Until then the upstream 404 is surfaced verbatim.
    """
    url = f"{request.app.state.settings.skill_store_url}/skills/{name}/versions"
    return await _proxy_get(request, url, _auth, tenant_id)


@router.delete("/admin/skills/{name}")
async def admin_delete_skill(
    name: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Delete a skill.

    NOTE: skill-store currently exposes only per-version delete
    (``DELETE /skills/{id}/versions/{version}``), not skill-level delete.
    This proxies to ``DELETE /skills/{id}`` so it works once skill-store
    adds it; until then the upstream 404/405 is surfaced verbatim.
    """
    headers = _scope_headers(_auth, tenant_id=tenant_id)
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
#   GET/POST /mcp-servers, GET/PATCH/DELETE /mcp-servers/{server_id}.

@router.get("/admin/mcp/servers")
async def admin_list_mcp_servers(
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    url = f"{request.app.state.settings.mcp_registry_url}/mcp-servers"
    return await _proxy_get(request, url, _auth, tenant_id, forward_params=True)


@router.post("/admin/mcp/servers", status_code=201)
async def admin_create_mcp_server(
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    headers = _scope_headers(_auth, tenant_id=tenant_id)
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


@router.get("/admin/mcp/servers/{server_id}")
async def admin_get_mcp_server(
    server_id: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    url = f"{request.app.state.settings.mcp_registry_url}/mcp-servers/{server_id}"
    return await _proxy_get(request, url, _auth, tenant_id)


@router.patch("/admin/mcp/servers/{server_id}")
async def admin_update_mcp_server(
    server_id: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    headers = _scope_headers(_auth, tenant_id=tenant_id)
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
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    headers = _scope_headers(_auth, tenant_id=tenant_id)
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


# ─────────────────────────────────────────────────────────────────────────────
# Module 9 — Knowledge documents (→ knowledge-ingest)
# ─────────────────────────────────────────────────────────────────────────────
#
# Downstream (knowledge-ingest api/v1/documents.py):
#   GET /documents, GET /documents/{document_id},
#   POST /documents/{document_id}/ingest   (= "reingest" — re-runs chunk+embed)
#   There is NO DELETE for documents → the spec's optional delete is omitted.

@router.get("/admin/knowledge/documents")
async def admin_list_documents(
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    url = f"{request.app.state.settings.knowledge_ingest_url}/documents"
    return await _proxy_get(request, url, _auth, tenant_id, forward_params=True)


@router.get("/admin/knowledge/documents/{document_id}")
async def admin_get_document(
    document_id: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    url = (
        f"{request.app.state.settings.knowledge_ingest_url}"
        f"/documents/{document_id}"
    )
    return await _proxy_get(request, url, _auth, tenant_id)


@router.post("/admin/knowledge/documents/{document_id}/reingest")
async def admin_reingest_document(
    document_id: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Re-run chunking + embedding for a document.

    Maps to knowledge-ingest ``POST /documents/{id}/ingest`` (idempotent
    re-ingest).  No request body is required.
    """
    headers = _scope_headers(_auth, tenant_id=tenant_id)
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


# ─────────────────────────────────────────────────────────────────────────────
# Module 10 — Runs & sandbox governance (→ scheduler / sandbox-pool / orch.)
# ─────────────────────────────────────────────────────────────────────────────
#
# Downstream divergences:
#   * agent-scheduler has NO list-runs route (only POST /runs,
#     GET/POST /runs/{run_id}[/cancel]).  The authoritative run *list* lives in
#     session-orchestrator, but it is session-scoped
#     (GET /sessions/{id}/runs) — there is no tenant-wide runs list anywhere.
#     ``GET /admin/ops/runs`` therefore proxies to the scheduler /runs path
#     (params透传) so it works the instant a list route is added; today the
#     upstream 404/405 is surfaced verbatim.  See report for the gap.
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
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """List runs (scheduler params透传).

    See module note: the scheduler currently has no list-runs route, so this
    surfaces the upstream's response verbatim (404 until one is added).
    """
    url = f"{request.app.state.settings.agent_scheduler_url}/runs"
    return await _proxy_get(request, url, _auth, tenant_id, forward_params=True)


@router.post("/admin/ops/runs/{run_id}/cancel")
async def admin_cancel_run(
    run_id: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Cancel a run via the scheduler (looked up by container/sandbox id)."""
    headers = _scope_headers(_auth, tenant_id=tenant_id)
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
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """List running sandbox containers for a tenant (scheduler)."""
    url = f"{request.app.state.settings.agent_scheduler_url}/sandboxes"
    return await _proxy_get(request, url, _auth, tenant_id, forward_params=True)


@router.post("/admin/ops/sandboxes/{sandbox_id}/stop")
async def admin_stop_sandbox(
    sandbox_id: str,
    request: Request,
    tenant_id: str | None = None,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Stop a running sandbox container (scheduler; 204 upstream)."""
    headers = _scope_headers(_auth, tenant_id=tenant_id)
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
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Per-day token totals (orchestrator GET /runs/token-usage/daily).

    Query params (e.g. ``days``) are forwarded verbatim.
    """
    url = (
        f"{request.app.state.settings.session_orchestrator_url}"
        f"/runs/token-usage/daily"
    )
    return await _proxy_get(request, url, _auth, tenant_id, forward_params=True)
