"""Library proxy endpoints — MCP servers, skills, and knowledge documents.

Proxies to:
* mcp-registry     GET/POST /mcp-servers
* skill-store      GET/POST /skills, POST /skills/{id}/versions
* knowledge-ingest GET/POST /documents, POST /documents/{id}/ingest,
                   PATCH/DELETE /documents/{id}, folders CRUD

Knowledge-source guard split (knowledge-library-v1)
───────────────────────────────────────────────────
The knowledge-source surface is a *tenant-level* resource: tenant admins
maintain it, and every tenant user reads it under a clearance constraint.

  * Reads (list documents / list folders) are open to any authenticated user
    (``require_auth``).
  * Writes (upload, ingest, document PATCH/DELETE, folder CRUD) are delegated
    to tenant administrators via ``require_admin_module_bff("document.create")``
    — the same fail-closed BFF guard the admin console uses, so a plain member
    (holding only ``document.read``) is 403'd before any upstream call, while a
    tenant_admin (holding the ``document.*`` wildcard) passes.  The guard
    forwards **without** ``X-Actor: ADMIN`` so knowledge-ingest re-authorizes
    the real caller per action.

Every knowledge-source proxy — read *and* write — forwards the caller's
``X-User-Clearance`` (and the ``X-User-Id`` / ``X-Tenant-Id`` identity headers)
so knowledge-ingest's two-gate model (reachability + clearance ≥ sensitivity)
stays enforced end-to-end.  This mirrors the hard-won chat-path lesson where a
dropped ``X-User-Clearance`` silently defeated the sensitivity gate.
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
from agent_bff.api.dependencies import TClearance, require_auth
from agent_bff.api.v1.admin_platform import require_admin_module_bff

router = APIRouter(tags=["Library"])

# Guard shared by every knowledge-source write (documents + folders): delegates
# to tenant administrators exactly as the admin console does.  ``document.create``
# is the discriminator — held by tenant_admin (via ``document.*``) but not by the
# read-only ``member`` role.
_require_knowledge_admin = require_admin_module_bff("document.create")

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

def _scrub_mcp_server_secrets(server: dict) -> dict:
    """Strip connection credentials from an MCP server record for the
    normal-user library surface.

    The library list/create responses are served to any authenticated tenant
    user (``require_auth``), so they must not carry the secret material that
    mcp-registry stores for connecting to the upstream MCP server:

      * ``auth`` is reduced to ``{"type": ...}`` - the scheme
        (none/bearer/api_key/oauth) is useful metadata, but the credentials
        are dropped: ``token`` / ``api_key`` / ``header_name`` for the
        bearer/api_key schemes, and ``client_id`` / ``client_secret`` /
        ``token_url`` / ``scopes`` for the oauth scheme.
      * ``headers`` is removed entirely - HTTP headers routinely carry
        ``Authorization`` / ``X-API-Key`` / cookies, and distinguishing secret
        from non-secret entries at the BFF edge is unreliable, so the whole
        map is withheld from the library surface.

    Everything else (name/transport/endpoint/command/env/enabled/
    timeout_seconds/tool_filter/args/timestamps) is passed through unchanged -
    these are the metadata a tenant user needs to browse and select servers.
    The admin console (admin_platform.py) now sees masked auth: the registry
    replaces secret fields (``token`` / ``api_key`` / ``client_secret``) with a
    redacted sentinel on its tenant-facing read paths, so plaintext credentials
    never reach the browser.  Only the internal consumer endpoint
    (``/internal/mcp-servers``) still returns plaintext secrets.

    Note: ``env`` and ``args`` can also carry secrets (API keys as env vars,
    tokens in CLI args).  They are left intact here to preserve the existing
    library contract (the SPA types and renders ``env``); masking their values
    is a follow-up if the registry starts returning secret material in them.
    """
    if not isinstance(server, dict):
        return server
    scrubbed = dict(server)
    auth = scrubbed.get("auth")
    if isinstance(auth, dict):
        # Keep only the auth scheme; drop all credentials - token / api_key /
        # header_name (bearer/api_key) and client_id / client_secret /
        # token_url / scopes (oauth).
        scrubbed["auth"] = {"type": auth.get("type")}
    scrubbed.pop("headers", None)
    return scrubbed


@router.get("/library/mcp-servers")
async def list_mcp_servers(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """List tenant MCP servers from mcp-registry (secret-scrubbed).

    Returns the mcp-registry ``{items, total}`` envelope with each server's
    connection credentials stripped - ``auth`` reduced to its ``type`` and
    ``headers`` dropped (see ``_scrub_mcp_server_secrets``).  Tenant users see
    the metadata they need to pick a server, never the stored secrets.
    """
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.mcp_registry_url
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{url}/mcp-servers",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        resp.raise_for_status()
        body = resp.json()
    if isinstance(body, dict) and isinstance(body.get("items"), list):
        body = {
            **body,
            "items": [_scrub_mcp_server_secrets(s) for s in body["items"]],
        }
    elif isinstance(body, list):
        # Defensive: if the registry ever returns a bare array, scrub in place.
        body = [_scrub_mcp_server_secrets(s) for s in body]
    return body


@router.post("/library/mcp-servers", status_code=201)
async def create_mcp_server(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """Register a tenant MCP server in mcp-registry (response secret-scrubbed).

    The created record is returned with connection credentials stripped (see
    ``_scrub_mcp_server_secrets``) so the library surface never echoes
    ``auth.token`` / ``auth.api_key`` / ``headers`` back to a normal user; the
    caller already supplied them and does not need them returned.
    """
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
        created = resp.json()
    return _scrub_mcp_server_secrets(created) if isinstance(created, dict) else created


# --------------------------------------------------------------------------- #
# Skills
# --------------------------------------------------------------------------- #

@router.get("/library/skills")
async def list_skills(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """List tenant skills published to the shelf (browse list).

    Requests ``include_platform=true`` so the user-facing library also surfaces
    the platform-global default built-ins (read-only, every org has them). The
    admin skill-management proxy deliberately omits this flag.
    """
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.skill_store_url
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{url}/skills",
            params={"include_platform": "true"},
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
    clearance: str = TClearance,
) -> dict:
    """List tenant knowledge documents from knowledge-ingest.

    Read surface — open to any authenticated tenant user.  ``include_memory``
    is never forwarded, so session-memory docs (titled "[memory] …") stay
    excluded from the resource library; full-set auditing lives on the admin
    proxy (admin_platform.py), which forces ``include_memory=true``.

    An optional ``folder_id`` query param is forwarded to filter by folder
    (knowledge-ingest treats a missing value as "all folders").  The caller's
    ``X-User-Clearance`` is always forwarded so knowledge-ingest hides docs
    whose sensitivity exceeds the caller's clearance.
    """
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.knowledge_ingest_url
    params: dict[str, str] = {}
    folder_id = request.query_params.get("folder_id")
    if folder_id is not None:
        params["folder_id"] = folder_id
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{url}/documents",
            params=params or None,
            headers=foundation_headers(
                tenant_id, user_id, access_token, clearance=clearance,
            ),
        )
        resp.raise_for_status()
        return resp.json()


@router.post("/library/documents", status_code=201)
async def create_document(
    request: Request,
    title: str = Form(...),
    mime: str = Form(...),
    file: UploadFile = File(...),
    folder_id: str | None = Form(None),
    sensitivity: str | None = Form(None),
    _auth: tuple = Depends(_require_knowledge_admin),
    clearance: str = TClearance,
) -> dict:
    """Upload a knowledge document to knowledge-ingest (multipart passthrough).

    Tenant-admin only.  Optional ``folder_id`` places the document in a folder
    (default: root); optional ``sensitivity`` (L1..L5) overrides the folder's
    default classification — knowledge-ingest applies the folder default and the
    only-raise rule when either is omitted.
    """
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.knowledge_ingest_url
    files = {
        "file": (
            file.filename,
            await file.read(),
            file.content_type or "application/octet-stream",
        )
    }
    data: dict[str, str] = {"title": title, "mime": mime}
    if folder_id is not None:
        data["folder_id"] = folder_id
    if sensitivity is not None:
        data["sensitivity"] = sensitivity
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{url}/documents",
            data=data,
            files=files,
            headers=foundation_headers(
                tenant_id, user_id, access_token, clearance=clearance,
            ),
        )
        _raise_for_upstream(resp)
        return resp.json()


@router.patch("/library/documents/{document_id}")
async def update_document(
    document_id: str,
    request: Request,
    _auth: tuple = Depends(_require_knowledge_admin),
    clearance: str = TClearance,
) -> dict:
    """Move a document / change its sensitivity (``{folder_id?, sensitivity?}``).

    Tenant-admin only.  Moving into a folder with a higher default triggers
    knowledge-ingest's only-raise rule; explicit ``sensitivity`` may lower it.
    """
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.knowledge_ingest_url
    body = await request.json() if await request.body() else {}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.patch(
            f"{url}/documents/{document_id}",
            json=body,
            headers=foundation_headers(
                tenant_id, user_id, access_token, clearance=clearance,
            ),
        )
        _raise_for_upstream(resp)
        return resp.json()


@router.delete("/library/documents/{document_id}")
async def delete_document(
    document_id: str,
    request: Request,
    _auth: tuple = Depends(_require_knowledge_admin),
    clearance: str = TClearance,
) -> dict:
    """Delete a knowledge document (row + chunks + blob). Tenant-admin only."""
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.knowledge_ingest_url
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(
            f"{url}/documents/{document_id}",
            headers=foundation_headers(
                tenant_id, user_id, access_token, clearance=clearance,
            ),
        )
        _raise_for_upstream(resp)
        return {"status": "deleted"}


@router.get("/library/documents/{document_id}/content")
async def get_document_content(
    document_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
    clearance: str = TClearance,
) -> Response:
    """Proxy a knowledge document's source bytes from knowledge-ingest.

    Read surface — open to any authenticated tenant user (mirrors
    ``list_documents``).  The caller's ``X-User-Clearance`` is forwarded so
    knowledge-ingest's fail-closed visibility gate (sensitivity ≤ clearance)
    holds end-to-end; without it the service defaults the caller to L1 and
    every L2+ document would 403.  The upstream ``Content-Disposition``
    (RFC 6266, with non-ASCII ``filename*``) is passed through unchanged.
    """
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.knowledge_ingest_url
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            f"{url}/documents/{document_id}/content",
            headers=foundation_headers(
                tenant_id, user_id, access_token, clearance=clearance,
            ),
        )
    content_type = resp.headers.get("content-type", "application/octet-stream")
    content_disp = resp.headers.get("content-disposition", "")
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=content_type,
        headers={"Content-Disposition": content_disp} if content_disp else {},
    )



@router.post("/library/documents/{document_id}/ingest")
async def ingest_document(
    document_id: str,
    request: Request,
    _auth: tuple = Depends(_require_knowledge_admin),
    clearance: str = TClearance,
) -> dict:
    """Trigger chunking/ingestion for an uploaded document. Tenant-admin only."""
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.knowledge_ingest_url
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{url}/documents/{document_id}/ingest",
            headers=foundation_headers(
                tenant_id, user_id, access_token, clearance=clearance,
            ),
        )
        _raise_for_upstream(resp)
        return resp.json()


# --------------------------------------------------------------------------- #
# Knowledge folders (knowledge-library-v1)
# --------------------------------------------------------------------------- #
#
# Downstream (knowledge-ingest /folders): the tree is a self-referential
# parent_id hierarchy; the BFF proxies the flat list and CRUD verbatim and the
# SPA assembles the tree client-side.

@router.get("/library/folders")
async def list_folders(
    request: Request,
    _auth: tuple = Depends(require_auth()),
    clearance: str = TClearance,
) -> dict:
    """Flat list of tenant knowledge folders (SPA assembles the tree).

    Read surface — open to any authenticated tenant user; clearance forwarded.
    """
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.knowledge_ingest_url
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{url}/folders",
            headers=foundation_headers(
                tenant_id, user_id, access_token, clearance=clearance,
            ),
        )
        resp.raise_for_status()
        return resp.json()


@router.post("/library/folders", status_code=201)
async def create_folder(
    request: Request,
    _auth: tuple = Depends(_require_knowledge_admin),
    clearance: str = TClearance,
) -> dict:
    """Create a folder (``{name, parent_id?, default_sensitivity?}``). Admin only."""
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.knowledge_ingest_url
    body = await request.json() if await request.body() else {}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{url}/folders",
            json=body,
            headers=foundation_headers(
                tenant_id, user_id, access_token, clearance=clearance,
            ),
        )
        _raise_for_upstream(resp)
        return resp.json()


@router.patch("/library/folders/{folder_id}")
async def update_folder(
    folder_id: str,
    request: Request,
    _auth: tuple = Depends(_require_knowledge_admin),
    clearance: str = TClearance,
) -> dict:
    """Rename / move / re-default a folder (``{name?, parent_id?,
    default_sensitivity?}``). Tenant-admin only."""
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.knowledge_ingest_url
    body = await request.json() if await request.body() else {}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.patch(
            f"{url}/folders/{folder_id}",
            json=body,
            headers=foundation_headers(
                tenant_id, user_id, access_token, clearance=clearance,
            ),
        )
        _raise_for_upstream(resp)
        return resp.json()


@router.delete("/library/folders/{folder_id}")
async def delete_folder(
    folder_id: str,
    request: Request,
    _auth: tuple = Depends(_require_knowledge_admin),
    clearance: str = TClearance,
) -> dict:
    """Delete an empty folder (knowledge-ingest 409s a non-empty one).

    Tenant-admin only.
    """
    tenant_id, user_id, access_token = _auth
    url = request.app.state.settings.knowledge_ingest_url
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(
            f"{url}/folders/{folder_id}",
            headers=foundation_headers(
                tenant_id, user_id, access_token, clearance=clearance,
            ),
        )
        _raise_for_upstream(resp)
        return {"status": "deleted"}
