"""Chat/SSE endpoint — send message + relay agent SSE events."""

from __future__ import annotations

import json
import logging

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth

router = APIRouter(tags=["Conversations"])
logger = logging.getLogger(__name__)


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
) -> StreamingResponse:
    """Send a user message, trigger run, relay SSE stream from orchestrator.

    Body fields:
    * ``message``        — required, user text
    * ``attachment_ids`` — optional list of attachment ids already uploaded to
                           this session; they will be downloaded into the sandbox
    """
    tenant_id, user_id, access_token = _auth
    orch_url = request.app.state.settings.session_orchestrator_url
    body = await request.json()
    message_text = body.get("message", "")
    attachment_ids: list[str] = body.get("attachment_ids") or []
    # Optional per-message skill selection (names); forwarded to the orchestrator
    # which relays into task.json so the worker loads exactly these skills.
    skills: list[str] = body.get("skills") or []
    # Optional per-message model + reasoning effort selection (forwarded to the
    # orchestrator → scheduler → worker → gateway request).
    model: str | None = body.get("model") or None
    reasoning_effort: str | None = body.get("reasoning_effort") or None

    async def event_generator():
        async with httpx.AsyncClient(timeout=600) as client:
            headers = foundation_headers(tenant_id, user_id, access_token)

            # 1. Create user message in orchestrator (pass attachment_ids so the
            #    orchestrator can embed attachment metadata on the message row).
            resp = await client.post(
                f"{orch_url}/sessions/{session_id}/messages",
                json={"role": "USER", "content": message_text, "attachment_ids": attachment_ids},
                headers=headers,
            )
            if resp.status_code >= 400:
                payload = json.dumps(
                    {"detail": "Failed to save message", "status": resp.status_code}
                )
                yield f"event: error\ndata: {payload}\n\n"
                return

            msg = resp.json()
            yield f"event: message_created\ndata: {json.dumps(msg)}\n\n"

            # 2. Resolve attachment files (failure-open)
            files = await _resolve_attachment_files(
                client=client,
                orch_url=orch_url,
                session_id=session_id,
                attachment_ids=attachment_ids,
                headers=headers,
            )

            # 3. Trigger run (pass files so worker downloads them)
            resp = await client.post(
                f"{orch_url}/sessions/{session_id}/runs",
                json={
                    "task": message_text,
                    "files": files,
                    "skills": skills,
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                },
                headers=headers,
            )
            if resp.status_code >= 400:
                payload = json.dumps(
                    {"detail": "Failed to start run", "status": resp.status_code}
                )
                yield f"event: error\ndata: {payload}\n\n"
                return

            run = resp.json()
            yield f"event: run_started\ndata: {json.dumps(run)}\n\n"

            # 4. Relay orchestrator SSE messages verbatim.
            async with client.stream(
                "GET",
                f"{orch_url}/sessions/{session_id}/messages/stream",
                headers=headers,
            ) as stream:
                async for line in stream.aiter_lines():
                    if line.startswith("event:") or line.startswith("data:"):
                        yield f"{line}\n"
                    elif line == "":
                        yield "\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
