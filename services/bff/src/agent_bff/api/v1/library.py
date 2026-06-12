"""Library proxy endpoints — MCP servers, skills, and knowledge documents.

Proxies to:
* mcp-registry     GET/POST /mcp-servers
* skill-store      GET/POST /skills, POST /skills/{id}/versions
* knowledge-ingest GET/POST /documents, POST /documents/{id}/ingest
"""

from __future__ import annotations

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth

router = APIRouter(tags=["Library"])

# Hard cap on the compressed bytes accepted for a skill-version upload at the
# BFF edge. skill-store also bounds the *decompressed* footprint (block 1,
# MAX_TOTAL_SIZE=50MiB), but we reject oversized payloads here so GB-scale
# files never tunnel through to the backend just to be rejected.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MiB


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


@router.get("/library/skills/{skill_id}/versions")
async def list_skill_versions(
    skill_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """List all published versions of a skill from skill-store.

    Used by the SPA's "install to toolbox" modal to let the user lock a
    specific version (or leave null → follow latest).
    """
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.skill_store_url
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{url}/skills/{skill_id}/versions",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        resp.raise_for_status()
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
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"user_message": "技能包超过大小上限 20 MB"},
        )
    files = {
        "file": (
            file.filename,
            content,
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
