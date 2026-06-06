"""Library proxy endpoints — MCP servers, skills, and knowledge documents.

Proxies to:
* mcp-registry  GET /mcp-servers
* skill-store   GET /skills
* knowledge-ingest GET /documents
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Request, Response

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth

router = APIRouter(tags=["Library"])


# --------------------------------------------------------------------------- #
# MCP servers
# --------------------------------------------------------------------------- #

@router.get("/library/mcp-servers")
async def list_mcp_servers(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """List tenant MCP servers from mcp-registry."""
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.mcp_registry_url
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{url}/mcp-servers",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        resp.raise_for_status()
        return resp.json()


# --------------------------------------------------------------------------- #
# Skills
# --------------------------------------------------------------------------- #

@router.get("/library/skills")
async def list_skills(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """List tenant skills published to the shelf (browse list)."""
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.skill_store_url
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{url}/skills",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        resp.raise_for_status()
        return resp.json()


@router.get("/library/skills/installed")
async def list_installed_skills(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """The calling user's installed skills (their personal toolbox)."""
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.skill_store_url
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{url}/skills/installed",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        resp.raise_for_status()
        return resp.json()


@router.post("/library/skills/{skill_id}/install")
async def install_skill(
    skill_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """Install a skill into the calling user's toolbox."""
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.skill_store_url
    try:
        body = await request.json()
    except Exception:
        body = {}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{url}/skills/{skill_id}/install",
            json=body or {},
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        resp.raise_for_status()
        return resp.json()


@router.delete("/library/skills/{skill_id}/install", status_code=204)
async def uninstall_skill(
    skill_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> Response:
    """Remove a skill from the calling user's toolbox."""
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.skill_store_url
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(
            f"{url}/skills/{skill_id}/install",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        resp.raise_for_status()
        return Response(status_code=204)


# --------------------------------------------------------------------------- #
# Knowledge documents
# --------------------------------------------------------------------------- #

@router.get("/library/documents")
async def list_documents(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """List tenant knowledge documents from knowledge-ingest."""
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.knowledge_ingest_url
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{url}/documents",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        resp.raise_for_status()
        return resp.json()
