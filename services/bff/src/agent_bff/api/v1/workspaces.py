"""Workspace proxy endpoints — proxy to session-orchestrator.

The workspace surface is a persistent, user-scoped "home directory" of
instructions, skills, and files that a session can be pinned to
(``POST /sessions`` accepts an optional ``workspace_id``).

Every endpoint here is a *transparent* 1:1 proxy to the orchestrator's
``/workspaces`` surface (prefix ``/api/session/v1``).  The BFF adds nothing
but auth — it forwards the same ``foundation_headers`` (``X-Tenant-Id`` /
``X-User-Id`` + bearer) every other orchestrator proxy uses, faithfully
mirrors the upstream status code (including 204 / 409), and passes error
bodies through unchanged so the orchestrator's problem+json / ``user_message``
reaches the frontend intact.

Written to the same shape as ``sessions.py``: ``_proxy_json`` for JSON
responses, native ``files=`` multipart forwarding for uploads, and raw
``Response`` (with ``Content-Disposition`` passthrough) for the byte-stream
download.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import TClearance, require_auth

router = APIRouter(tags=["Workspaces"])
logger = logging.getLogger(__name__)


async def _orchestrator_url(request: Request) -> str:
    return request.app.state.settings.session_orchestrator_url


def _proxy_json(resp: httpx.Response) -> JSONResponse:
    """Forward the upstream status + body faithfully.

    Handles 204 / empty body: returns ``null`` rather than raising on
    ``resp.json()`` when there is no body.  Error bodies (problem+json with
    ``user_message``) pass through unchanged.
    """
    if not resp.content:
        return JSONResponse(content=None, status_code=resp.status_code)
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


# --------------------------------------------------------------------------- #
# Workspace CRUD
# --------------------------------------------------------------------------- #


@router.get("/workspaces")
async def list_workspaces(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """List the caller's workspaces (orchestrator lazily creates a default)."""
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{orch_url}/workspaces",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    return _proxy_json(resp)


@router.post("/workspaces", status_code=201)
async def create_workspace(
    request: Request,
    _auth: tuple = Depends(require_auth()),
    clearance: str = TClearance,
) -> JSONResponse:
    """Create a workspace. Body: name, description?, instructions?, skills?, meta?."""
    tenant_id, user_id, access_token = _auth
    body = await request.json() if await request.body() else {}
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{orch_url}/workspaces",
            json=body,
            headers=foundation_headers(
                tenant_id, user_id, access_token, clearance=clearance
            ),
        )
    return _proxy_json(resp)


@router.get("/workspaces/{workspace_id}")
async def get_workspace(
    workspace_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{orch_url}/workspaces/{workspace_id}",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    return _proxy_json(resp)


@router.patch("/workspaces/{workspace_id}")
async def update_workspace(
    workspace_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
    clearance: str = TClearance,
) -> JSONResponse:
    """Partial update — all POST fields optional."""
    tenant_id, user_id, access_token = _auth
    body = await request.json() if await request.body() else {}
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.patch(
            f"{orch_url}/workspaces/{workspace_id}",
            json=body,
            headers=foundation_headers(
                tenant_id, user_id, access_token, clearance=clearance
            ),
        )
    return _proxy_json(resp)


@router.delete("/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> Response:
    """Delete a workspace. Orchestrator 409s on the default workspace.

    Faithfully forwards the upstream status: 204 on success, or the
    orchestrator's 409 (+ problem+json body) when deleting the default.
    No ``status_code`` decorator here so a 409 isn't rewritten to 204.
    """
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(
            f"{orch_url}/workspaces/{workspace_id}",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    if resp.status_code == 204 or not resp.content:
        return Response(status_code=resp.status_code)
    return _proxy_json(resp)


# --------------------------------------------------------------------------- #
# Workspace files
# --------------------------------------------------------------------------- #


@router.post("/workspaces/{workspace_id}/files", status_code=201)
async def upload_workspace_file(
    workspace_id: str,
    file: UploadFile,
    request: Request,
    description: str | None = Form(default=None),
    sensitivity_level: str | None = Form(default=None),
    _auth: tuple = Depends(require_auth()),
    clearance: str = TClearance,
) -> JSONResponse:
    """Proxy multipart upload to the orchestrator.

    Mirrors ``sessions.upload_attachment``: reads the file fully in-process and
    re-emits it via httpx ``files=``.  The optional ``description`` and
    ``sensitivity_level`` form fields are forwarded as multipart ``data=`` only
    when present, so the wire matches what the orchestrator expects (``file``
    part + optional ``description``/``sensitivity_level``).
    """
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    data = await file.read()
    filename = file.filename or "upload.bin"
    content_type = file.content_type or "application/octet-stream"

    form: dict[str, str] = {}
    if description is not None:
        form["description"] = description
    if sensitivity_level is not None:
        form["sensitivity_level"] = sensitivity_level

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{orch_url}/workspaces/{workspace_id}/files",
            files={"file": (filename, data, content_type)},
            data=form or None,
            headers=foundation_headers(
                tenant_id, user_id, access_token, clearance=clearance
            ),
        )
    return _proxy_json(resp)


@router.get("/workspaces/{workspace_id}/files")
async def list_workspace_files(
    workspace_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{orch_url}/workspaces/{workspace_id}/files",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    return _proxy_json(resp)


@router.delete("/workspaces/{workspace_id}/files/{file_id}")
async def delete_workspace_file(
    workspace_id: str,
    file_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> Response:
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(
            f"{orch_url}/workspaces/{workspace_id}/files/{file_id}",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    if resp.status_code == 204 or not resp.content:
        return Response(status_code=resp.status_code)
    return _proxy_json(resp)


@router.get("/workspaces/{workspace_id}/files/{file_id}/content")
async def get_workspace_file_content(
    workspace_id: str,
    file_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
    clearance: str = TClearance,
) -> Response:
    """Proxy file content download from the orchestrator.

    Forwards the caller's clearance as ``X-User-Clearance`` (same posture as
    the session file-content proxy) and passes the upstream
    ``Content-Disposition`` (RFC 6266) through unchanged so non-ASCII filenames
    survive.
    """
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{orch_url}/workspaces/{workspace_id}/files/{file_id}/content",
            headers=foundation_headers(
                tenant_id, user_id, access_token, clearance=clearance
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


# --------------------------------------------------------------------------- #
# File versions
# --------------------------------------------------------------------------- #


@router.get("/workspaces/{workspace_id}/files/{file_id}/versions")
async def list_file_versions(
    workspace_id: str,
    file_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """List historical versions of a workspace file."""
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{orch_url}/workspaces/{workspace_id}/files/{file_id}/versions",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    return _proxy_json(resp)


@router.post("/workspaces/{workspace_id}/files/{file_id}/versions/{version_id}/restore")
async def restore_file_version(
    workspace_id: str,
    file_id: str,
    version_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Restore a workspace file to a historical version."""
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{orch_url}/workspaces/{workspace_id}/files/{file_id}/versions/{version_id}/restore",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    return _proxy_json(resp)
