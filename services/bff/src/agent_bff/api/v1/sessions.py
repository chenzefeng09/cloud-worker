"""Session proxy endpoints — proxy to session-orchestrator.

All downstream calls carry:
* ``Authorization: Bearer <access_token>`` — the OIDC token from login
* ``X-Tenant-Id`` / ``X-User-Id`` — defence-in-depth alongside the JWT

session-orchestrator's gateway will JWKS-verify the bearer token and
populate its own tenant context; the headers are belt + braces so the
service can still operate behind a gateway that doesn't unwrap the JWT.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse

from agent_bff.api._audit import emit_admin_audit
from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import TClearance, require_auth

router = APIRouter(tags=["Sessions"])
logger = logging.getLogger(__name__)

# Tight timeout for the authz /check call backing the tenant-admin guard —
# fail-closed (403) on timeout, same posture as require_super_admin_bff.
_AUTHZ_TIMEOUT = 2.0

# Audit event types emitted when an admin views session replay data.
_AUDIT_EVENT_SESSION_LIST = "admin.session.list.viewed"
_AUDIT_EVENT_SESSION_RUNS = "admin.session.runs.viewed"
_AUDIT_EVENT_SESSION_MESSAGES = "admin.session.messages.viewed"


async def _orchestrator_url(request: Request) -> str:
    return request.app.state.settings.session_orchestrator_url


async def _emit_session_view_audit(
    settings,
    tenant_id: str,
    user_id: str,
    access_token: str,
    event_type: str,
    resource_id: str | None,
    *,
    payload: dict[str, Any] | None = None,
) -> None:
    """Write a best-effort audit event for an admin session-view action.

    A side-channel (see ``agent_bff.api._audit``): failures are logged and
    never reach the caller, so the session-view endpoints return their data
    whether or not the audit record landed.  ``payload`` adds counts and ids
    to the ``via`` marker — never message content.
    """
    await emit_admin_audit(
        settings,
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
        event_type=event_type,
        resource_type="session",
        resource_id=resource_id,
        payload={"via": "admin_session_view", **(payload or {})},
    )


def _proxy_json(resp: httpx.Response) -> JSONResponse:
    """Return a JSONResponse that faithfully forwards the upstream status code.

    Handles 204 / empty body safely: returns an empty JSON object rather than
    blowing up on ``resp.json()`` when there is no body.
    """
    if not resp.content:
        return JSONResponse(content=None, status_code=resp.status_code)
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


async def require_tenant_admin_bff(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> tuple[str, str, str]:
    """BFF-side fail-closed guard for the admin session-replay surface.

    Modelled on ``admin.require_super_admin_bff`` — same authz ``/check`` call
    structure, fail-closed on every error path (unreachable / non-200 /
    unparseable / ``allowed != True`` → 403).

    Distinguishing tenant_admin from a normal member is the subtle part.  Both
    roles hold ``session.read`` / ``session.message.read`` (see
    saas-foundation bootstrap_tenant_roles), so checking those would let *any*
    member through.  We instead check ``session.delete``: the ``member`` role's
    permission list is an *explicit* set that does NOT include it, whereas
    ``tenant_admin`` holds the ``session.*`` wildcard which matches it.  The
    action is registered in the authz permission catalogue (migration 0002), so
    ``/check`` returns a real allow/deny rather than PERMISSION_UNKNOWN.  This
    gates "list the whole tenant's sessions" on a tenant-admin-only capability
    without inventing a new permission in the sibling repo.
    """
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    authz_url: str = settings.authz_url

    try:
        async with httpx.AsyncClient(timeout=_AUTHZ_TIMEOUT) as client:
            resp = await client.post(
                f"{authz_url}/check",
                json={
                    "user_id": user_id,
                    "action": "session.delete",
                    "resource_type": "session",
                },
                headers={"X-Tenant-Id": tenant_id},
            )
    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as exc:
        logger.warning(
            "bff.require_tenant_admin.authz_unreachable", extra={"exc": str(exc)}
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ADMIN: authz service unreachable",
        ) from exc

    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"ADMIN: authz returned HTTP {resp.status_code}",
        )

    try:
        body = resp.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ADMIN: authz response unparseable",
        ) from None

    if body.get("allowed") is not True:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ADMIN: permission denied (tenant_admin required)",
        )

    return tenant_id, user_id, access_token


def _admin_actor_headers(tenant_id: str, user_id: str, access_token: str) -> dict[str, str]:
    """``foundation_headers`` + ``X-Actor: ADMIN`` (only after the tenant-admin guard).

    The orchestrator's per-session owner check returns 404 to anyone but the
    owner unless this marker is present, so admin replay must assert it.
    """
    headers = foundation_headers(tenant_id, user_id, access_token)
    headers["X-Actor"] = "ADMIN"
    return headers


def _page_items(resp: httpx.Response) -> list[dict[str, Any]] | None:
    """The ``items`` of a 2xx JSON page, or None when nothing was disclosed."""
    if not 200 <= resp.status_code < 300 or not resp.content:
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return None
    return [item for item in items if isinstance(item, dict)]


async def _session_owner(
    client: httpx.AsyncClient, orch_url: str, session_id: str, headers: dict[str, str],
) -> str | None:
    """The session owner's user id for the audit record (best-effort)."""
    try:
        resp = await client.get(f"{orch_url}/sessions/{session_id}", headers=headers)
        if resp.status_code == 200:
            owner = resp.json().get("user_id")
            return owner if isinstance(owner, str) else None
    except Exception as exc:  # noqa: BLE001 — the audit still lands without it
        logger.debug("session owner lookup for audit failed (%s): %s", session_id, exc)
    return None


@router.get("/admin/sessions")
async def admin_list_sessions(
    request: Request,
    cursor: str | None = None,
    limit: int = 50,
    _auth: tuple = Depends(require_tenant_admin_bff),
) -> JSONResponse:
    """List EVERY session in the caller's tenant (admin replay).

    Separate from ``GET /sessions`` — which is now owner-scoped — so a normal
    member can never reach the whole-tenant listing.  Gated by
    ``require_tenant_admin_bff``.  Forwards ``X-Actor: ADMIN`` so the
    orchestrator skips its per-user owner filter and returns all sessions in
    the tenant (still tenant-bound; never cross-tenant).  Emits the same
    ``admin.session.list.viewed`` audit event as the legacy view path.
    """
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    params: dict[str, int | str] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor

    headers = _admin_actor_headers(tenant_id, user_id, access_token)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{orch_url}/sessions",
            params=params,
            headers=headers,
        )
    await _emit_session_view_audit(
        request.app.state.settings,
        tenant_id,
        user_id,
        access_token,
        _AUDIT_EVENT_SESSION_LIST,
        None,
    )
    return _proxy_json(resp)


@router.get("/admin/sessions/{session_id}/runs")
async def admin_list_session_runs(
    session_id: str,
    request: Request,
    _auth: tuple = Depends(require_tenant_admin_bff),
) -> JSONResponse:
    """A session's runs for admin replay — any member's session in the tenant.

    Runs carry the task text and result summary, so a successful read emits
    ``admin.session.runs.viewed`` (resource = the session; payload = the
    owner and the run count).
    """
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    headers = _admin_actor_headers(tenant_id, user_id, access_token)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{orch_url}/sessions/{session_id}/runs", headers=headers)
        runs = _page_items(resp)
        owner = (
            await _session_owner(client, orch_url, session_id, headers)
            if runs is not None else None
        )
    if runs is not None:
        payload: dict[str, Any] = {"run_count": len(runs)}
        if owner:
            payload["subject_user_id"] = owner
        await _emit_session_view_audit(
            request.app.state.settings,
            tenant_id,
            user_id,
            access_token,
            _AUDIT_EVENT_SESSION_RUNS,
            session_id,
            payload=payload,
        )
    return _proxy_json(resp)


@router.get("/admin/sessions/{session_id}/messages")
async def admin_list_session_messages(
    session_id: str,
    request: Request,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    _auth: tuple = Depends(require_tenant_admin_bff),
) -> JSONResponse:
    """A page of any member's transcript for admin replay.

    Same parameters as ``GET /sessions/{id}/messages``.  A successful read
    emits ``admin.session.messages.viewed`` (resource = the session; payload
    = the owner, the message count and the sequence range read — never the
    content).
    """
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    params: dict[str, int | str] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    headers = _admin_actor_headers(tenant_id, user_id, access_token)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{orch_url}/sessions/{session_id}/messages", params=params, headers=headers,
        )
        messages = _page_items(resp)
        owner = (
            await _session_owner(client, orch_url, session_id, headers)
            if messages is not None else None
        )
    if messages is not None:
        payload: dict[str, Any] = {"message_count": len(messages)}
        if owner:
            payload["subject_user_id"] = owner
        sequences = [m["sequence"] for m in messages if isinstance(m.get("sequence"), int)]
        if sequences:
            payload["first_sequence"] = min(sequences)
            payload["last_sequence"] = max(sequences)
        await _emit_session_view_audit(
            request.app.state.settings,
            tenant_id,
            user_id,
            access_token,
            _AUDIT_EVENT_SESSION_MESSAGES,
            session_id,
            payload=payload,
        )
    return _proxy_json(resp)


@router.get("/sessions")
async def list_sessions(
    request: Request,
    cursor: str | None = None,
    limit: int = 50,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    params: dict[str, int | str] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{orch_url}/sessions",
            params=params,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    # No audit here: this is the owner-scoped path (a user listing *their own*
    # sessions), not a privileged action.  The tenant-wide replay surface
    # ``GET /admin/sessions`` is what emits ``admin.session.list.viewed``.
    return _proxy_json(resp)


@router.post("/sessions", status_code=201)
async def create_session(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    tenant_id, user_id, access_token = _auth
    body = await request.json() if await request.body() else {}

    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{orch_url}/sessions",
            json=body,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        return _proxy_json(resp)


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{orch_url}/sessions/{session_id}",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        return _proxy_json(resp)


@router.patch("/sessions/{session_id}")
async def update_session(
    session_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    tenant_id, user_id, access_token = _auth
    body = await request.json() if await request.body() else {}
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.patch(
            f"{orch_url}/sessions/{session_id}",
            json=body,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        return _proxy_json(resp)


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


@router.get("/sessions/{session_id}/runs")
async def list_runs(
    session_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Proxy the run list for a session from session-orchestrator.

    Forwards to ``GET /sessions/{session_id}/runs`` and returns the JSON
    unchanged (``{"items": [...]}``).  The web context rail uses this to show
    the active worker and elapsed time.
    """
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{orch_url}/sessions/{session_id}/runs",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        return _proxy_json(resp)


@router.get("/sessions/{session_id}/runs/{run_id}/state")
async def get_run_state(
    session_id: str,
    run_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Proxy run hot-state (plan/step/cursor) from session-orchestrator.

    Forwards to ``GET /sessions/{session_id}/runs/{run_id}/state`` on the
    orchestrator and returns the JSON unchanged.  Failure-open on the
    orchestrator side: even if Redis is down, the orchestrator returns 200
    with empty plan/cursor and DB-sourced status.
    """
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{orch_url}/sessions/{session_id}/runs/{run_id}/state",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
        return _proxy_json(resp)


@router.post("/sessions/{session_id}/cancel")
async def cancel_session(
    session_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
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
        return _proxy_json(resp)


@router.delete("/sessions/{session_id}/messages")
async def delete_messages_from_sequence(
    session_id: str,
    from_sequence: int,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Proxy message truncation to session-orchestrator.

    Deletes all messages with sequence >= from_sequence, allowing the frontend
    to rewind a conversation to a specific user turn.
    """
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(
            f"{orch_url}/sessions/{session_id}/messages",
            params={"from_sequence": from_sequence},
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    return _proxy_json(resp)


@router.get("/sessions/{session_id}/messages")
async def list_messages(
    session_id: str,
    request: Request,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
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
    # No audit here: this is the owner-scoped path (a user reading *their own*
    # conversation).  Admin replay of a peer's transcript goes through
    # ``GET /admin/sessions/{id}/messages``, which audits every page it reads.
    return _proxy_json(resp)


# --------------------------------------------------------------------------- #
# Attachment large-check (pre-upload gate)
# --------------------------------------------------------------------------- #


@router.get("/attachments/large-check")
async def attachment_large_check(
    request: Request,
    size_bytes: int = Query(..., ge=0),
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Pre-upload gate: can the current user upload an attachment of this size?

    Large attachments (>= ``large_attachment_threshold_bytes``) trigger the
    large-memory (4g) sandbox tier and require the lifecycle ``large-attachment``
    entitlement.  The frontend calls this at file-selection time, BEFORE the
    upload network call, so it can show a friendly "exceeds your quota" message
    instead of letting the upload through and failing later.

    Response shape::

        {"allowed": bool, "is_large": bool, "reason": str | None}

    * Small files (below threshold) are ALWAYS allowed without consulting
      lifecycle — they never hit the large-memory tier.
    * Large files consult lifecycle ``/large-attachment/resolve``.  ``reason``
      is ``"large_attachment_not_permitted"`` when a large file is denied.

    FAIL-OPEN: if the lifecycle call fails (unreachable / 5xx), we ALLOW and log
    a warning.  Rationale: this is only a UX pre-check — the server-side upload
    path and run creation (scheduler) are the real enforcement of the memory
    tier — so a flaky dependency must not block users from uploading.
    """
    settings = request.app.state.settings
    threshold = settings.large_attachment_threshold_bytes
    is_large = size_bytes >= threshold

    if not is_large:
        return JSONResponse(
            content={"allowed": True, "is_large": False, "reason": None},
            status_code=200,
        )

    tenant_id, user_id, access_token = _auth
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{settings.foundation_lifecycle_url}/large-attachment/resolve",
                params={"user_id": user_id},
                headers=foundation_headers(tenant_id, user_id, access_token),
            )
        resp.raise_for_status()
        allowed = bool(resp.json().get("allowed", False))
    except Exception as exc:  # noqa: BLE001 — fail-open on any lifecycle failure
        logger.warning(
            "large-attachment resolve failed; failing open (allow): %s", exc
        )
        return JSONResponse(
            content={"allowed": True, "is_large": True, "reason": None},
            status_code=200,
        )

    return JSONResponse(
        content={
            "allowed": allowed,
            "is_large": True,
            "reason": None if allowed else "large_attachment_not_permitted",
        },
        status_code=200,
    )


# --------------------------------------------------------------------------- #
# Attachment proxy
# --------------------------------------------------------------------------- #


@router.post("/sessions/{session_id}/attachments", status_code=201)
async def upload_attachment(
    session_id: str,
    file: UploadFile,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
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
    return _proxy_json(resp)


@router.get("/sessions/{session_id}/attachments")
async def list_attachments(
    session_id: str,
    request: Request,
    kind: str | None = None,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
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
        return _proxy_json(resp)


@router.get("/sessions/{session_id}/files/{file_id}/content")
async def get_file_content(
    session_id: str,
    file_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
    clearance: str = TClearance,
) -> Response:
    """Proxy file content download from session-orchestrator.

    Forwards the caller's clearance as ``X-User-Clearance`` so the
    orchestrator's DLP guard (``check: download``) can enforce the level — this
    is the only BFF proxy that reaches the DLP-gated download path.
    """
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{orch_url}/sessions/{session_id}/files/{file_id}/content",
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


@router.delete("/sessions/{session_id}/attachments/{attachment_id}", status_code=204)
async def delete_attachment(
    session_id: str,
    attachment_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> Response:
    """Delete a session attachment (Artifacts 专区删除) — proxy to orchestrator.

    Orchestrator does a hard delete of both the S3 blob (best-effort) and the
    attachment row.  Owner-scoping lives in the orchestrator's
    ``SessionService.get_attachment`` (raises 404 for a missing / cross-tenant /
    soft-deleted-session attachment).  Forwards the DELETE and returns 204 on
    success; upstream non-2xx is relayed via ``_proxy_json``.
    """
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(
            f"{orch_url}/sessions/{session_id}/attachments/{attachment_id}",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    # 204 → empty body; ``_proxy_json`` handles that case safely.
    if resp.status_code == 204 or not resp.content:
        return Response(status_code=resp.status_code)
    return _proxy_json(resp)


# --------------------------------------------------------------------------- #
# Runs proxy
# --------------------------------------------------------------------------- #


@router.get("/sessions/{session_id}/runs/{run_id}")
async def get_run(
    session_id: str,
    run_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Proxy single run fetch from session-orchestrator.

    The AgentPage uses this to get ``run.started_at`` for the elapsed timer.
    """
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{orch_url}/sessions/{session_id}/runs/{run_id}",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    return _proxy_json(resp)


@router.get("/runs/count")
async def count_runs(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Return the total number of runs ever created for the caller's tenant.

    Fetches all sessions first, then aggregates run counts across them.
    Orchestrator does not expose a cross-session count endpoint, so we use
    a lightweight approach: query per-session run lists with limit=1 to count
    sessions, then return the sum.  For the ProfilePage stat tile, we call
    the orchestrator's list_runs with a high limit per session and sum up.

    For scalability this endpoint should eventually be served from an
    orchestrator aggregate.  For now it is sufficient for M2 ProfilePage.

    Response: { "count": int }
    """
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    hdrs = foundation_headers(tenant_id, user_id, access_token)

    # Step 1: get all sessions (up to 200 to bound the request fanout).
    async with httpx.AsyncClient(timeout=20) as client:
        sess_resp = await client.get(
            f"{orch_url}/sessions",
            params={"limit": 200},
            headers=hdrs,
        )
    if sess_resp.status_code >= 400:
        return JSONResponse(content={"count": 0}, status_code=200)

    sessions_data = sess_resp.json()
    session_items: list[dict] = sessions_data.get("items") or []

    if not session_items:
        return JSONResponse(content={"count": 0}, status_code=200)

    # Step 2: for each session, fetch runs and sum lengths.
    import asyncio

    async def _count_session_runs(session_id: str) -> int:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(
                    f"{orch_url}/sessions/{session_id}/runs",
                    headers=hdrs,
                )
            if r.status_code == 200:
                return len(r.json().get("items") or [])
        except Exception:  # noqa: BLE001
            pass
        return 0

    counts = await asyncio.gather(
        *[_count_session_runs(s["id"]) for s in session_items]
    )
    total = sum(counts)
    return JSONResponse(content={"count": total}, status_code=200)


@router.get("/runs/token-usage/daily")
async def daily_token_usage(
    request: Request,
    days: int = 7,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Proxy GET /runs/token-usage/daily?days=N to orchestrator.

    Returns per-day token totals for the last N days (default 7).
    Response: { "days": [{ "day": "YYYY-MM-DD", "tokens": int }, ...] }
    """
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    hdrs = foundation_headers(tenant_id, user_id, access_token)

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{orch_url}/runs/token-usage/daily",
            params={"days": days},
            headers=hdrs,
        )
    return _proxy_json(resp)


@router.get("/runs/token-usage/breakdown")
async def token_usage_breakdown(
    request: Request,
    days: int = 7,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Proxy GET /runs/token-usage/breakdown?days=N to orchestrator.

    Returns a token-usage breakdown for the last N days (default 7): a per-day
    input/output/total series plus a per-source rollup.
    Response: {
        "days_window": int,
        "daily": [{ "day": "YYYY-MM-DD", "input_tokens": int,
                    "output_tokens": int, "total_tokens": int }, ...],
        "by_source": [{ "source": str, "total_tokens": int,
                        "run_count": int }, ...]
    }
    """
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    hdrs = foundation_headers(tenant_id, user_id, access_token)

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{orch_url}/runs/token-usage/breakdown",
            params={"days": days},
            headers=hdrs,
        )
    return _proxy_json(resp)


@router.get("/runs/token-usage")
async def token_usage(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Return the total token usage across all runs for the caller's tenant.

    Aggregates ``input_tokens``, ``output_tokens``, and ``total_tokens`` from
    every run the tenant has ever created.  Uses the same session-fanout pattern
    as ``/runs/count``.

    Response: { "input_tokens": int, "output_tokens": int, "total_tokens": int }
    """
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    hdrs = foundation_headers(tenant_id, user_id, access_token)

    # Step 1: get all sessions (up to 200 to bound the request fanout).
    async with httpx.AsyncClient(timeout=20) as client:
        sess_resp = await client.get(
            f"{orch_url}/sessions",
            params={"limit": 200},
            headers=hdrs,
        )
    if sess_resp.status_code >= 400:
        return JSONResponse(
            content={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            status_code=200,
        )

    sessions_data = sess_resp.json()
    session_items: list[dict] = sessions_data.get("items") or []

    if not session_items:
        return JSONResponse(
            content={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            status_code=200,
        )

    # Step 2: for each session, fetch runs and sum token fields.
    import asyncio

    async def _session_tokens(session_id: str) -> tuple[int, int, int]:
        """Return (input_tokens, output_tokens, total_tokens) for one session."""
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(
                    f"{orch_url}/sessions/{session_id}/runs",
                    headers=hdrs,
                )
            if r.status_code == 200:
                items = r.json().get("items") or []
                inp = sum(run.get("input_tokens") or 0 for run in items)
                out = sum(run.get("output_tokens") or 0 for run in items)
                tot = sum(run.get("total_tokens") or 0 for run in items)
                return inp, out, tot
        except Exception:  # noqa: BLE001
            pass
        return 0, 0, 0

    results = await asyncio.gather(
        *[_session_tokens(s["id"]) for s in session_items]
    )
    input_tokens = sum(r[0] for r in results)
    output_tokens = sum(r[1] for r in results)
    total_tokens = sum(r[2] for r in results)
    return JSONResponse(
        content={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        },
        status_code=200,
    )
