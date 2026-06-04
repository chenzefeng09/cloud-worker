"""Fleet proxy endpoints — sandbox-pool snapshot and scheduler runs.

Proxies to:
* sandbox-pool     GET /sandboxes/_pool
* agent-scheduler  GET /sandboxes  (running containers for tenant)
* mcp-registry     GET /mcp-servers (for topology worker→mcp edges)
"""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, Depends, Request

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth

router = APIRouter(tags=["Fleet"])


@router.get("/fleet/pool")
async def pool_snapshot(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """Return sandbox pool occupancy from sandbox-pool service."""
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.sandbox_pool_url
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{url}/sandboxes/_pool",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        resp.raise_for_status()
        return resp.json()


@router.get("/fleet/sandboxes")
async def list_sandboxes(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """List running sandbox containers for the tenant from agent-scheduler."""
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.agent_scheduler_url
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{url}/sandboxes",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        resp.raise_for_status()
        return resp.json()


@router.get("/fleet/topology")
async def fleet_topology(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """Build a topology graph (nodes + edges) for the current tenant.

    Fan-out to agent-scheduler (sandboxes) and mcp-registry (servers),
    then assemble nodes and edges.  x/y coordinates are intentionally
    omitted — the frontend layout algorithm computes them.

    Node types:
      - worker: a running sandbox container
      - task:   an active session associated with a sandbox
      - mcp:    a registered MCP server (enabled or disabled)

    Edge kinds:
      - worker→task  derived from sandbox.session_id / sandbox.run_id
      - worker→mcp   APPROXIMATE — every active worker is connected to
                     each enabled MCP server for the tenant.  This is a
                     best-effort approximation because the scheduler does
                     not track per-sandbox MCP attachment at this layer.
    """
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    hdrs = foundation_headers(tenant_id, user_id, access_token)

    async with httpx.AsyncClient(timeout=15) as client:
        sandboxes_resp, mcps_resp = await asyncio.gather(
            client.get(f"{settings.agent_scheduler_url}/sandboxes", headers=hdrs),
            client.get(f"{settings.mcp_registry_url}/mcp-servers", headers=hdrs),
            return_exceptions=True,
        )

    # ── Parse sandboxes ──────────────────────────────────────────────────────
    sandbox_items: list[dict] = []
    if isinstance(sandboxes_resp, httpx.Response) and sandboxes_resp.is_success:
        try:
            body = sandboxes_resp.json()
            sandbox_items = body.get("items", body) if isinstance(body, dict) else body
        except Exception:
            sandbox_items = []

    # ── Parse MCP servers ────────────────────────────────────────────────────
    mcp_items: list[dict] = []
    if isinstance(mcps_resp, httpx.Response) and mcps_resp.is_success:
        try:
            body = mcps_resp.json()
            mcp_items = body.get("items", body) if isinstance(body, dict) else body
        except Exception:
            mcp_items = []

    # ── Build node lists ─────────────────────────────────────────────────────
    nodes: list[dict] = []
    edges: list[dict] = []

    # Worker nodes (one per sandbox)
    worker_ids: list[str] = []
    for sb in sandbox_items:
        node_id = sb.get("container_id") or sb.get("id") or ""
        if not node_id:
            continue
        status = (sb.get("status") or "unknown").lower()
        label = sb.get("name") or node_id[:12]
        nodes.append({
            "id": node_id,
            "type": "worker",
            "label": label,
            "status": status,
            # Extra fields used for edge building — not part of the public shape
            "_session_id": sb.get("session_id"),
            "_run_id": sb.get("run_id"),
        })
        worker_ids.append(node_id)

    # Task nodes — derived from sandboxes that carry a session_id
    task_node_ids: set[str] = set()
    for sb in sandbox_items:
        session_id = sb.get("session_id")
        if not session_id or session_id in task_node_ids:
            continue
        task_node_ids.add(session_id)
        nodes.append({
            "id": f"task:{session_id}",
            "type": "task",
            "label": session_id[:12],
            "status": "active",
        })

    # MCP nodes
    enabled_mcp_ids: list[str] = []
    for mcp in mcp_items:
        mcp_id = mcp.get("id") or mcp.get("name") or ""
        if not mcp_id:
            continue
        enabled = mcp.get("enabled", True)
        mcp_status = "enabled" if enabled else "disabled"
        nodes.append({
            "id": f"mcp:{mcp_id}",
            "type": "mcp",
            "label": mcp.get("name") or mcp_id,
            "status": mcp_status,
        })
        if enabled:
            enabled_mcp_ids.append(mcp_id)

    # ── Build edges ───────────────────────────────────────────────────────────
    for node in nodes:
        if node["type"] != "worker":
            continue
        worker_id = node["id"]
        session_id = node.get("_session_id")

        # worker → task edge (data-driven)
        if session_id and session_id in task_node_ids:
            edges.append({
                "from": worker_id,
                "to": f"task:{session_id}",
                "kind": "worker→task",
            })

        # worker → mcp edges (approximation: active workers connect to
        # all enabled MCPs; real per-worker attachment data not available here)
        for mcp_id in enabled_mcp_ids:
            edges.append({
                "from": worker_id,
                "to": f"mcp:{mcp_id}",
                "kind": "worker→mcp",
            })

    # Strip internal helper keys before returning
    for node in nodes:
        node.pop("_session_id", None)
        node.pop("_run_id", None)

    return {"nodes": nodes, "edges": edges}
