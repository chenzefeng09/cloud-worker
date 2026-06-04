"""Library proxy endpoints — MCP servers, skills, and knowledge documents.

Proxies to:
* mcp-registry     GET/POST /mcp-servers
* skill-store      GET/POST /skills, POST /skills/{id}/versions
* knowledge-ingest GET/POST /documents, POST /documents/{id}/ingest
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth

router = APIRouter(tags=["Library"])


def _raise_for_upstream(resp: httpx.Response) -> None:
    """Surface upstream 4xx/5xx as HTTPException so the SPA gets a readable error."""
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail)


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


@router.post("/library/mcp-servers", status_code=201)
async def create_mcp_server(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """Register a tenant MCP server in mcp-registry."""
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.mcp_registry_url
    body = await request.json()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{url}/mcp-servers",
            json=body,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        _raise_for_upstream(resp)
        return resp.json()


# --------------------------------------------------------------------------- #
# Skills
# --------------------------------------------------------------------------- #

@router.get("/library/skills")
async def list_skills(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """List tenant skills from skill-store."""
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.skill_store_url
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{url}/skills",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        resp.raise_for_status()
        return resp.json()


@router.post("/library/skills", status_code=201)
async def create_skill(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """Create a tenant skill in skill-store."""
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.skill_store_url
    body = await request.json()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{url}/skills",
            json=body,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        _raise_for_upstream(resp)
        return resp.json()


@router.post("/library/skills/{skill_id}/versions", status_code=201)
async def create_skill_version(
    skill_id: str,
    request: Request,
    file: UploadFile = File(...),
    version: str = Form(...),
    manifest: str | None = Form(None),
    expected_sha256: str | None = Form(None),
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """Upload a skill version tarball to skill-store (multipart passthrough)."""
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.skill_store_url
    data: dict[str, str] = {"version": version}
    if manifest is not None:
        data["manifest"] = manifest
    if expected_sha256 is not None:
        data["expected_sha256"] = expected_sha256
    files = {
        "file": (
            file.filename,
            await file.read(),
            file.content_type or "application/octet-stream",
        )
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{url}/skills/{skill_id}/versions",
            data=data,
            files=files,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        _raise_for_upstream(resp)
        return resp.json()


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


@router.post("/library/documents", status_code=201)
async def create_document(
    request: Request,
    title: str = Form(...),
    mime: str = Form(...),
    file: UploadFile = File(...),
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """Upload a knowledge document to knowledge-ingest (multipart passthrough)."""
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.knowledge_ingest_url
    files = {
        "file": (
            file.filename,
            await file.read(),
            file.content_type or "application/octet-stream",
        )
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{url}/documents",
            data={"title": title, "mime": mime},
            files=files,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        _raise_for_upstream(resp)
        return resp.json()


@router.post("/library/documents/{document_id}/ingest")
async def ingest_document(
    document_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """Trigger chunking/ingestion for an uploaded document."""
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.knowledge_ingest_url
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{url}/documents/{document_id}/ingest",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        _raise_for_upstream(resp)
        return resp.json()
