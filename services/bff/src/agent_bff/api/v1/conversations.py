"""Chat/SSE endpoint — send message + relay agent SSE events."""

from __future__ import annotations

import json

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth

router = APIRouter(tags=["Conversations"])


@router.post("/sessions/{session_id}/chat")
async def chat(
    session_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> StreamingResponse:
    """Send a user message, trigger run, relay SSE stream from orchestrator."""
    tenant_id, user_id, access_token = _auth
    orch_url = request.app.state.settings.session_orchestrator_url
    body = await request.json()
    message_text = body.get("message", "")

    async def event_generator():
        async with httpx.AsyncClient(timeout=600) as client:
            headers = foundation_headers(tenant_id, user_id, access_token)

            # 1. Create user message in orchestrator
            resp = await client.post(
                f"{orch_url}/sessions/{session_id}/messages",
                json={"role": "USER", "content": message_text},
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

            # 2. Trigger run
            resp = await client.post(
                f"{orch_url}/sessions/{session_id}/runs",
                json={"task": message_text},
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

            # 3. Relay orchestrator SSE messages verbatim.
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
