"""Chat endpoint (fire-and-forget trigger) + standalone SSE stream relay."""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import TClearance, require_auth
from agent_bff.session_title import generate_and_update_session_title

router = APIRouter(tags=["Conversations"])
logger = logging.getLogger(__name__)


class _ChatBodyEnvelope(BaseModel):
    """Validates the strictly-typed optional fields of a chat request body.

    The chat endpoint reads the rest of its body as a loose dict (message,
    attachment_ids, skills, model, reasoning_effort), but some fields must
    be strictly validated so an illegal value is a 422 rather than a silent
    fallthrough:

    * ``workspace_file_ids`` — optional list of workspace-file ids (``wsf_…``) the
                               user @-referenced in this message.  A non-list value
                               or a non-string element is a 422; omitted → ``None``.
    """

    workspace_file_ids: list[str] | None = None


def _validate_chat_body(body: dict) -> _ChatBodyEnvelope:
    """Return the validated chat-body envelope, raising 422 on an illegal value.

    Only the strictly-typed keys are inspected; anything else in ``body`` is
    ignored here (the endpoint reads those fields separately).
    """
    try:
        return _ChatBodyEnvelope(
            workspace_file_ids=body.get("workspace_file_ids"),
        )
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


# What the user is told when the orchestrator refuses to start a turn, by its
# status.  The orchestrator's own wording is for operators (it names internal
# ids and upstream services), so it goes to the log instead.  Nothing of the
# turn was saved, so the send can always be retried.
_TURN_REFUSALS: dict[int, str] = {
    403: "You don't have permission to send messages in this conversation.",
    404: "This conversation no longer exists.",
    409: (
        "A run is already in progress in this conversation. "
        "Wait for it to finish or stop it, then send your message again."
    ),
    422: "Your message could not be sent because the request was not valid.",
}
_TURN_UNAVAILABLE = (
    "The agent could not be started, so your message was not sent. "
    "Please try again in a moment."
)
# Refusals whose orchestrator ``user_message`` is written for the user and says
# what to do: no model set up for the tenant, its run quota used up.  Retrying
# does not help with either, so the status-based wording would mislead.
_USER_ACTIONABLE_REFUSALS = frozenset({
    "AGENT.RUN.LLM_NOT_CONFIGURED",
    "AGENT.RUN.QUOTA_EXCEEDED",
})


class TurnError(Exception):
    """The orchestrator refused to start a turn; nothing of it was saved.

    The turn's user message is written together with its run, so a refused
    turn leaves no trace.  ``detail`` is meant for the user as is;
    ``status_code`` is the orchestrator's (409: another run is live).
    """

    def __init__(self, status_code: int, detail: str, *, code: str | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code if status_code < 600 else 502
        self.detail = detail
        self.code = code

    @classmethod
    def from_response(cls, resp: httpx.Response) -> TurnError:
        problem: Any = None
        with contextlib.suppress(ValueError):
            problem = resp.json()
        if not isinstance(problem, dict):
            problem = {}
        code = problem.get("code")
        logger.warning(
            "run start refused: HTTP %s %s %s",
            resp.status_code, code or "-", problem.get("detail"),
        )
        user_message = problem.get("user_message")
        if code in _USER_ACTIONABLE_REFUSALS and isinstance(user_message, str) and user_message:
            detail = user_message
        else:
            detail = _TURN_REFUSALS.get(resp.status_code, _TURN_UNAVAILABLE)
        return cls(resp.status_code, detail, code=code if isinstance(code, str) else None)

    def response(self) -> JSONResponse:
        content: dict[str, Any] = {"detail": self.detail, "status": self.status_code}
        if self.code:
            content["code"] = self.code
        return JSONResponse(status_code=self.status_code, content=content)


async def start_turn(
    client: httpx.AsyncClient,
    *,
    orch_url: str,
    headers: dict[str, str],
    session_id: str,
    message_text: str,
    attachment_ids: list[str],
    model: str | None,
    reasoning_effort: str | None,
    run_options: dict[str, Any],
    persist_message: bool = True,
) -> tuple[dict | None, dict]:
    """Start a sandbox run for a user turn.

    Shared by the chat endpoint and the task board (create / retry).
    ``run_options`` holds the presence-carried run knobs (``skills``,
    ``mcp_server_ids``, ``workspace_file_ids``, ``include_platform_defaults``,
    ``hitl``): a key is forwarded only when the caller expressed a choice, so
    "omitted → defaults" and "explicit [] → none" stay distinguishable.

    The user message rides in the run request and the orchestrator writes it
    in the run's transaction: if the run is refused (another one is live) or
    cannot start, no unanswered message is left in the transcript.
    ``persist_message=False`` re-runs a user message already in the transcript
    (task retry); the orchestrator's history assembly drops that row as this
    turn's input.

    Returns ``(message, run)`` (``message`` is ``None`` when none was written);
    raises :class:`TurnError` when the orchestrator refuses the run.
    """
    files = await _resolve_attachment_files(
        client=client,
        orch_url=orch_url,
        session_id=session_id,
        attachment_ids=attachment_ids,
        headers=headers,
    )
    run_body: dict[str, Any] = {
        "task": message_text,
        "files": files,
        "model": model,
        "reasoning_effort": reasoning_effort,
        **run_options,
    }
    if persist_message:
        run_body["message"] = {"content": message_text, "attachment_ids": attachment_ids}
    resp = await client.post(
        f"{orch_url}/sessions/{session_id}/runs",
        json=run_body,
        headers=headers,
    )
    if resp.status_code >= 400:
        raise TurnError.from_response(resp)
    run = resp.json()
    message = run.pop("message", None)
    return message, run


def schedule_auto_title(
    background_tasks: BackgroundTasks,
    request: Request,
    *,
    message: dict | None,
    message_text: str,
    session_id: str,
    auth: tuple[str, str, str],
    clearance: str,
    model_hint: str | None,
) -> None:
    """Name the session from its first user message, after the response.

    Runs in the background so it never delays task startup; the session SSE
    stream publishes the eventual PATCH to every open client.
    """
    if not message or message.get("sequence") != 1 or not message_text.strip():
        return
    tenant_id, user_id, access_token = auth
    background_tasks.add_task(
        generate_and_update_session_title,
        settings=request.app.state.settings,
        session_id=session_id,
        message=message_text,
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
        clearance=clearance,
        model_hint=model_hint,
    )


@router.post("/sessions/{session_id}/chat")
async def chat(
    session_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    _auth: tuple = Depends(require_auth()),
    clearance: str = TClearance,
) -> JSONResponse:
    """Start a run for the user's message (saved with it); return run_id + message_id.

    Body fields:
    * ``message``            — required, user text
    * ``attachment_ids``     — optional list of attachment ids already uploaded to
                               this session
    * ``workspace_file_ids`` — optional list of workspace-file ids (``wsf_…``) the
                               user @-referenced; forwarded verbatim so the
                               orchestrator can prioritise those files in the
                               injected workspace context.
    * ``skills``             — optional list of skill names
    * ``mcp_server_ids``     — optional list of MCP server ids to enable for
                               this run.  Presence-aware, exactly like
                               ``skills``: key omitted → the tenant's
                               enabled-by-default servers; explicit ``[]`` →
                               no MCP at all for this run; non-empty list →
                               only those servers.
    * ``model``              — optional model name
    * ``reasoning_effort``   — optional reasoning effort string

    Returns JSON ``{"run_id": "...", "message_id": "...", "run": {...}}``.
    When the run does not start, nothing was saved: the response carries the
    orchestrator's status (409 while another run is live) and a ``detail``
    written for the user.
    """
    tenant_id, user_id, access_token = _auth
    orch_url = request.app.state.settings.session_orchestrator_url
    body = await request.json()
    message_text = body.get("message", "")
    attachment_ids: list[str] = body.get("attachment_ids") or []
    model: str | None = body.get("model") or None
    reasoning_effort: str | None = body.get("reasoning_effort") or None
    # Strictly validated: workspace_file_ids (optional list of strings; illegal
    # types → pydantic 422).  Every run executes in a Claude SDK worker sandbox.
    envelope = _validate_chat_body(body)

    run_options: dict[str, Any] = {}
    # Forward @-referenced workspace file ids verbatim (only when the client
    # sent the field) so the orchestrator can prioritise those files.
    if envelope.workspace_file_ids is not None:
        run_options["workspace_file_ids"] = envelope.workspace_file_ids
    # Presence is meaningful: omitted lets orchestrator inject the user's
    # enabled-by-default installs; explicit [] disables all skills for this
    # session/run and must not be replaced with defaults.
    if "skills" in body:
        run_options["skills"] = body.get("skills") or []
    # Presence is meaningful (mirrors ``skills``): omitted lets the
    # orchestrator fall back to the tenant's enabled-by-default MCP
    # servers (today's auto-inject-everything behaviour); explicit []
    # disables MCP entirely for this run and must NOT be dropped by a
    # truthiness check.
    if "mcp_server_ids" in body:
        run_options["mcp_server_ids"] = body.get("mcp_server_ids") or []
    # Per-session opt-out of the platform-global default skills. Presence-
    # tolerant: forward verbatim only when the client sent it. Omitted keeps
    # the default (platform defaults injected); explicit ``false`` suppresses
    # them for this run. The orchestrator treats only a literal ``false`` as
    # disable, so forwarding the raw value is safe.
    if "include_platform_defaults" in body:
        run_options["include_platform_defaults"] = body.get("include_platform_defaults")

    headers = foundation_headers(tenant_id, user_id, access_token, clearance=clearance)
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            message, run = await start_turn(
                client,
                orch_url=orch_url,
                headers=headers,
                session_id=session_id,
                message_text=message_text,
                attachment_ids=attachment_ids,
                model=model,
                reasoning_effort=reasoning_effort,
                run_options=run_options,
            )
        except TurnError as exc:
            return exc.response()

    schedule_auto_title(
        background_tasks, request, message=message, message_text=message_text,
        session_id=session_id, auth=_auth, clearance=clearance, model_hint=model,
    )
    # run["id"] is the canonical field; fall back to run["run_id"] if present
    run_id = run.get("id") or run.get("run_id")
    return JSONResponse(
        status_code=200,
        content={"run_id": run_id, "message_id": (message or {}).get("id"), "run": run},
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


@router.get("/sessions/{session_id}/stream", response_model=None)
async def stream(
    session_id: str,
    request: Request,
    after: int | None = Query(default=None, ge=0),
    after_id: str | None = Query(default=None, max_length=64),
    _auth: tuple = Depends(require_auth()),
) -> StreamingResponse | JSONResponse:
    """Relay the session-scoped SSE stream from the orchestrator.

    Connects to ``GET {orch}/sessions/{session_id}/stream`` and relays all
    SSE lines verbatim: ``event:``, ``data:``, and blank-line frame separators.
    ``after`` / ``after_id`` (the newest message the client holds) are
    forwarded so a reconnect only receives newer rows.

    The upstream connection is opened before the response starts, so its
    status reaches the client: a 4xx (not the owner, session deleted) is
    passed through and tells the client to stop reconnecting, and an
    unreachable orchestrator is a 502.  A connection lost mid-stream ends with
    a generic ``event: error`` frame — never internal exception text.
    """
    tenant_id, user_id, access_token = _auth
    orch_url = request.app.state.settings.session_orchestrator_url
    headers = foundation_headers(tenant_id, user_id, access_token)

    client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10))
    upstream_cm = client.stream(
        "GET", f"{orch_url}/sessions/{session_id}/stream", headers=headers,
        params={
            key: value for key, value in (("after", after), ("after_id", after_id))
            if value is not None
        } or None,
    )
    try:
        upstream = await upstream_cm.__aenter__()
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        logger.warning("stream relay connect failed for session %s: %s", session_id, exc)
        return JSONResponse(status_code=502,
                            content={"detail": "Stream unavailable", "status": 502})

    status = getattr(upstream, "status_code", 200)
    if status != 200:
        detail: Any = "Stream unavailable"
        with contextlib.suppress(Exception):
            body = json.loads(await upstream.aread())
            detail = body.get("detail", detail) if isinstance(body, dict) else detail
        with contextlib.suppress(Exception):
            await upstream_cm.__aexit__(None, None, None)
        await client.aclose()
        relay_status = status if 400 <= status < 500 else 502
        return JSONResponse(status_code=relay_status,
                            content={"detail": detail, "status": relay_status})

    async def event_generator():
        try:
            async for line in upstream.aiter_lines():
                if line.startswith("event:") or line.startswith("data:"):
                    yield f"{line}\n"
                elif line == "":
                    yield "\n"
        except Exception as exc:  # noqa: BLE001
            logger.warning("stream relay error for session %s: %s", session_id, exc)
            payload = json.dumps({"detail": "Stream connection lost"})
            yield f"event: error\ndata: {payload}\n\n"
        finally:
            with contextlib.suppress(Exception):
                await upstream_cm.__aexit__(None, None, None)
            await client.aclose()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
