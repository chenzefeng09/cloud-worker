"""Chat endpoint (fire-and-forget trigger) + standalone SSE stream relay."""

from __future__ import annotations

import json
import logging
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth

router = APIRouter(tags=["Conversations"])
logger = logging.getLogger(__name__)


class _ExecutionModeEnvelope(BaseModel):
    """Validates only the optional ``execution_mode`` field of a chat request.

    The chat endpoint reads the rest of its body as a loose dict (message,
    attachment_ids, skills, model, reasoning_effort), but ``execution_mode``
    must be strictly one of ``inline`` / ``sandbox`` — an unknown value is a
    422, not a silent fallthrough.  Defaults to ``sandbox`` when omitted so
    existing clients keep the current agent-mode behaviour with zero change.

    * ``inline``  — chat mode: the orchestrator answers in-process via the LLM
                    gateway, no sandbox is dispatched.
    * ``sandbox`` — agent mode (default): the existing sandboxed-run behaviour.
    """

    execution_mode: Literal["inline", "sandbox"] = "sandbox"


def _validate_execution_mode(body: dict) -> str:
    """Return the validated execution_mode, raising 422 on an illegal value.

    Only the ``execution_mode`` key is inspected; anything else in ``body`` is
    ignored here (the endpoint reads those fields separately).
    """
    try:
        return _ExecutionModeEnvelope(
            execution_mode=body.get("execution_mode", "sandbox")
        ).execution_mode
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


async def _resolve_attachment_files(
    *,
    client: httpx.AsyncClient,
    orch_url: str,
    session_id: str,
    attachment_ids: list[str],
    headers: dict[str, str],
) -> list[dict]:
    """Fetch attachment metadata from orchestrator and return files list.

    Failure-open: if the attachment list fetch fails, we log a warning and
    return an empty list so the run proceeds without files rather than failing.
    """
    if not attachment_ids:
        return []
    try:
        resp = await client.get(
            f"{orch_url}/sessions/{session_id}/attachments",
            headers=headers,
        )
        if resp.status_code != 200:
            logger.warning("attachment list fetch failed: HTTP %s", resp.status_code)
            return []
        data = resp.json()
        items = data.get("items", [])
        att_map = {a["id"]: a for a in items}
        files = []
        for aid in attachment_ids:
            att = att_map.get(aid)
            if att:
                files.append({
                    "key": att["blob_key"],
                    "filename": att["filename"],
                    "mime": att.get("mime", "application/octet-stream"),
                })
            else:
                logger.warning("attachment id %s not found in session", aid)
        return files
    except Exception as exc:  # noqa: BLE001
        logger.warning("resolve_attachment_files error: %s", exc)
        return []


@router.post("/sessions/{session_id}/chat")
async def chat(
    session_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Persist user message, trigger run, return run_id + message_id.

    Body fields:
    * ``message``          — required, user text
    * ``attachment_ids``   — optional list of attachment ids already uploaded to
                             this session
    * ``skills``           — optional list of skill names
    * ``model``            — optional model name
    * ``reasoning_effort`` — optional reasoning effort string
    * ``execution_mode``   — optional ``"inline"`` | ``"sandbox"`` (default
                             ``"sandbox"``); ``inline`` = chat mode (no sandbox),
                             ``sandbox`` = agent mode.  An illegal value → 422.

    Returns JSON ``{"run_id": "...", "message_id": "...", "run": {...}}``.
    On upstream failure returns a JSON error with the upstream status code.
    """
    tenant_id, user_id, access_token = _auth
    orch_url = request.app.state.settings.session_orchestrator_url
    body = await request.json()
    message_text = body.get("message", "")
    attachment_ids: list[str] = body.get("attachment_ids") or []
    skills: list[str] = body.get("skills") or []
    model: str | None = body.get("model") or None
    reasoning_effort: str | None = body.get("reasoning_effort") or None
    # Strictly validated (inline/sandbox); defaults to sandbox for existing
    # clients so agent mode remains the zero-change default.
    execution_mode = _validate_execution_mode(body)

    async with httpx.AsyncClient(timeout=30) as client:
        headers = foundation_headers(tenant_id, user_id, access_token)

        # 1. Persist user message
        resp = await client.post(
            f"{orch_url}/sessions/{session_id}/messages",
            json={"role": "USER", "content": message_text, "attachment_ids": attachment_ids},
            headers=headers,
        )
        if resp.status_code >= 400:
            return JSONResponse(
                status_code=resp.status_code if resp.status_code < 600 else 502,
                content={"detail": "Failed to save message", "status": resp.status_code},
            )
        msg = resp.json()
        message_id = msg.get("id")

        # 2. Resolve attachment files (failure-open)
        files = await _resolve_attachment_files(
            client=client,
            orch_url=orch_url,
            session_id=session_id,
            attachment_ids=attachment_ids,
            headers=headers,
        )

        # 3. Trigger run
        resp = await client.post(
            f"{orch_url}/sessions/{session_id}/runs",
            json={
                "task": message_text,
                "files": files,
                "skills": skills,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "execution_mode": execution_mode,
            },
            headers=headers,
        )
        if resp.status_code >= 400:
            return JSONResponse(
                status_code=resp.status_code if resp.status_code < 600 else 502,
                content={"detail": "Failed to start run", "status": resp.status_code},
            )
        run = resp.json()
        # run["id"] is the canonical field; fall back to run["run_id"] if present
        run_id = run.get("id") or run.get("run_id")

    return JSONResponse(
        status_code=200,
        content={"run_id": run_id, "message_id": message_id, "run": run},
    )


@router.post("/sessions/{session_id}/runs/{run_id}/respond")
async def respond(
    session_id: str,
    run_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Forward a human-in-the-loop response to the orchestrator.

    Body fields:
    * ``kind``        — required, "confirm" or "answer"
    * ``tool_call_id``— required, non-empty string
    * ``confirmed``   — bool or null (for kind=confirm)
    * ``answer``      — str or null (for kind=answer)

    Returns the orchestrator response verbatim on success.
    On upstream 409 (run not paused) or 404 (pause expired), the status code
    and body are relayed as-is so the frontend can handle them explicitly.
    On upstream errors ≥ 400 the body is relayed with the same status code.
    """
    tenant_id, user_id, access_token = _auth
    orch_url = request.app.state.settings.session_orchestrator_url
    body = await request.json()

    # Minimal validation: kind must be "confirm" or "answer", tool_call_id non-empty.
    # Deeper semantic validation is delegated to the orchestrator.
    kind = body.get("kind")
    tool_call_id = body.get("tool_call_id")
    if kind not in ("confirm", "answer"):
        return JSONResponse(
            status_code=400,
            content={"detail": "kind must be 'confirm' or 'answer'"},
        )
    if not tool_call_id or not isinstance(tool_call_id, str):
        return JSONResponse(
            status_code=400,
            content={"detail": "tool_call_id must be a non-empty string"},
        )

    async with httpx.AsyncClient(timeout=30) as client:
        headers = foundation_headers(tenant_id, user_id, access_token)
        resp = await client.post(
            f"{orch_url}/sessions/{session_id}/runs/{run_id}/respond",
            json=body,
            headers=headers,
        )

    try:
        resp_body = resp.json()
    except Exception:  # noqa: BLE001
        resp_body = {"detail": "Upstream returned non-JSON response", "status": resp.status_code}

    return JSONResponse(
        status_code=resp.status_code if resp.status_code < 600 else 502,
        content=resp_body,
    )


@router.get("/sessions/{session_id}/pending")
async def pending(
    session_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Relay the session's currently-paused HITL interactions from the orchestrator.

    The frontend calls this on mount / reconnect to rehydrate ConfirmCard /
    QuestionCard for a run that paused while the page was closed.  The
    orchestrator endpoint is best-effort (always 200 with an ``items`` array),
    so this proxy just relays whatever it returns verbatim.
    """
    tenant_id, user_id, access_token = _auth
    orch_url = request.app.state.settings.session_orchestrator_url

    async with httpx.AsyncClient(timeout=30) as client:
        headers = foundation_headers(tenant_id, user_id, access_token)
        resp = await client.get(
            f"{orch_url}/sessions/{session_id}/pending",
            headers=headers,
        )

    try:
        resp_body = resp.json()
    except Exception:  # noqa: BLE001
        resp_body = {"items": []}

    return JSONResponse(
        status_code=resp.status_code if resp.status_code < 600 else 502,
        content=resp_body,
    )


@router.get("/sessions/{session_id}/stream")
async def stream(
    session_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> StreamingResponse:
    """Relay the session-scoped SSE stream from the orchestrator.

    Connects to ``GET {orch}/sessions/{session_id}/stream`` and relays all
    SSE lines verbatim: ``event:``, ``data:``, and blank-line frame separators.

    Emits an ``event: error`` frame if the upstream connection fails.
    """
    tenant_id, user_id, access_token = _auth
    orch_url = request.app.state.settings.session_orchestrator_url

    async def event_generator():
        headers = foundation_headers(tenant_id, user_id, access_token)
        try:
            async with (
                httpx.AsyncClient(timeout=600) as client,
                client.stream(
                    "GET",
                    f"{orch_url}/sessions/{session_id}/stream",
                    headers=headers,
                ) as upstream,
            ):
                async for line in upstream.aiter_lines():
                    if line.startswith("event:") or line.startswith("data:"):
                        yield f"{line}\n"
                    elif line == "":
                        yield "\n"
        except Exception as exc:  # noqa: BLE001
            logger.warning("stream relay error for session %s: %s", session_id, exc)
            payload = json.dumps({"detail": "Stream connection failed", "error": str(exc)})
            yield f"event: error\ndata: {payload}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
