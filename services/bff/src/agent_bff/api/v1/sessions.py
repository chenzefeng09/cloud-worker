"""Session proxy endpoints — proxy to session-orchestrator.

All downstream calls carry:
* ``Authorization: Bearer <access_token>`` — the OIDC token from login
* ``X-Tenant-Id`` / ``X-User-Id`` — defence-in-depth alongside the JWT

session-orchestrator's gateway will JWKS-verify the bearer token and
populate its own tenant context; the headers are belt + braces so the
service can still operate behind a gateway that doesn't unwrap the JWT.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Request, Response, UploadFile

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth

router = APIRouter(tags=["Sessions"])


async def _orchestrator_url(request: Request) -> str:
    return request.app.state.settings.session_orchestrator_url


@router.get("/sessions")
async def list_sessions(
    request: Request,
    cursor: str | None = None,
    limit: int = 50,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    params = {"limit": limit}
    if cursor:
        params["cursor"] = cursor

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{orch_url}/sessions",
            params=params,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        return resp.json()


@router.post("/sessions", status_code=201)
async def create_session(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    tenant_id, user_id, access_token = _auth
    body = await request.json() if await request.body() else {}

    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{orch_url}/sessions",
            json=body,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        return resp.json()


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{orch_url}/sessions/{session_id}",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        return resp.json()


@router.patch("/sessions/{session_id}")
async def update_session(
    session_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    tenant_id, user_id, access_token = _auth
    body = await request.json() if await request.body() else {}
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.patch(
            f"{orch_url}/sessions/{session_id}",
            json=body,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        return resp.json()


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> Response:
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        await client.delete(
            f"{orch_url}/sessions/{session_id}",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    return Response(status_code=204)


@router.post("/sessions/{session_id}/cancel")
async def cancel_session(
    session_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """Cancel the current active run for a session.

    Proxies to orchestrator POST /sessions/{session_id}/cancel.
    """
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{orch_url}/sessions/{session_id}/cancel",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        return resp.json()


@router.get("/sessions/{session_id}/messages")
async def list_messages(
    session_id: str,
    request: Request,
    cursor: str | None = None,
    limit: int = 50,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """Proxy the transcript fetch so the SPA can hydrate after refresh."""
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    params: dict = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{orch_url}/sessions/{session_id}/messages",
            params=params,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        return resp.json()


# --------------------------------------------------------------------------- #
# Attachment proxy
# --------------------------------------------------------------------------- #


@router.post("/sessions/{session_id}/attachments", status_code=201)
async def upload_attachment(
    session_id: str,
    file: UploadFile,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """Proxy multipart upload to session-orchestrator.

    httpx supports multipart forwarding natively via the ``files=`` arg.
    The file content is read fully in-process (BFF ↔ user connection is
    already in memory); for very large uploads the caller should use a
    streaming pre-signed URL instead (future work).
    """
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    data = await file.read()
    filename = file.filename or "upload.bin"
    content_type = file.content_type or "application/octet-stream"

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{orch_url}/sessions/{session_id}/attachments",
            files={"file": (filename, data, content_type)},
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    if resp.status_code >= 400:
        return resp.json()
    return resp.json()


@router.get("/sessions/{session_id}/attachments")
async def list_attachments(
    session_id: str,
    request: Request,
    kind: str | None = None,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """Proxy attachment listing from session-orchestrator.

    Optional ``kind`` query param: ``input`` or ``output``.
    """
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    params: dict = {}
    if kind:
        params["kind"] = kind
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{orch_url}/sessions/{session_id}/attachments",
            params=params,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        return resp.json()


@router.get("/sessions/{session_id}/files/{file_id}/content")
async def get_file_content(
    session_id: str,
    file_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> Response:
    """Proxy file content download from session-orchestrator."""
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{orch_url}/sessions/{session_id}/files/{file_id}/content",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    content_type = resp.headers.get("content-type", "application/octet-stream")
    content_disp = resp.headers.get("content-disposition", "")
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=content_type,
        headers={"Content-Disposition": content_disp} if content_disp else {},
    )
