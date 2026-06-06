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
from fastapi.responses import JSONResponse

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth

router = APIRouter(tags=["Sessions"])


async def _orchestrator_url(request: Request) -> str:
    return request.app.state.settings.session_orchestrator_url


def _proxy_json(resp: httpx.Response) -> JSONResponse:
    """Return a JSONResponse that faithfully forwards the upstream status code.

    Handles 204 / empty body safely: returns an empty JSON object rather than
    blowing up on ``resp.json()`` when there is no body.
    """
    if not resp.content:
        return JSONResponse(content=None, status_code=resp.status_code)
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


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
    limit: int = 50,
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
        return _proxy_json(resp)


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


# --------------------------------------------------------------------------- #
# Runs proxy
# --------------------------------------------------------------------------- #


@router.get("/sessions/{session_id}/runs")
async def list_runs(
    session_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Proxy run listing from session-orchestrator."""
    tenant_id, user_id, access_token = _auth
    orch_url = await _orchestrator_url(request)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{orch_url}/sessions/{session_id}/runs",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    return _proxy_json(resp)


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
