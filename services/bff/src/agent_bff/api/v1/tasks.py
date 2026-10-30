"""Task board endpoints — agentic work created and tracked from the UI.

A task is a session whose metadata says ``kind="task"`` plus the lifecycle of
its sandbox runs (see the orchestrator's ``GET /tasks`` read model).  Creating
one persists the instructions as the first user turn and starts a run in the
background, exactly like the chat endpoint (DLP, attachment and skill/MCP
presence semantics are shared through :func:`start_turn`), but the caller does
not have to stay on a live chat view: the board polls ``GET /tasks``.

Endpoints:

* ``GET  /tasks``             — the caller's tasks (filters relayed as-is)
* ``GET  /tasks/{id}``        — one task
* ``POST /tasks``             — create a task and start its first run
* ``POST /tasks/{id}/retry``  — re-run the latest attempt of a finished task

Cancelling uses the existing ``POST /sessions/{id}/cancel``; deleting uses
``DELETE /sessions/{id}``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import TClearance, require_auth
from agent_bff.api.v1.conversations import TurnError, schedule_auto_title, start_turn

router = APIRouter(tags=["Tasks"])
logger = logging.getLogger(__name__)

_LIST_PARAMS = ("status", "kind", "workspace_id", "q", "cursor", "limit")
_ACTIVE_RUN_STATUSES = frozenset({"PENDING", "RUNNING", "PAUSED"})
_PLACEHOLDER_TITLE = "New task"


class TaskCreate(BaseModel):
    instructions: str = Field(..., min_length=1, max_length=20000)
    title: str | None = Field(None, max_length=200)
    workspace_id: str | None = None
    # A draft ``kind="task"`` session the UI created to upload attachments to
    # before the task was submitted.  Must be the caller's and not started yet.
    session_id: str | None = None
    attachment_ids: list[str] = Field(default_factory=list, max_length=50)
    # Presence-carried like the chat endpoint: omitted → defaults; [] → none.
    skills: list[str] | None = None
    mcp_server_ids: list[str] | None = None
    model: str | None = Field(None, max_length=200)
    reasoning_effort: str | None = Field(None, max_length=50)
    # False runs the task unattended: the worker never pauses for approval.
    hitl: bool = True


class TaskRetry(BaseModel):
    """Optional overrides; anything omitted replays the stored task options."""

    skills: list[str] | None = None
    mcp_server_ids: list[str] | None = None
    model: str | None = Field(None, max_length=200)
    reasoning_effort: str | None = Field(None, max_length=50)


def _orch_url(request: Request) -> str:
    return request.app.state.settings.session_orchestrator_url


def _relay(resp: httpx.Response) -> JSONResponse:
    try:
        content = resp.json()
    except ValueError:
        content = {"detail": "Upstream returned a non-JSON response"}
    return JSONResponse(status_code=resp.status_code if resp.status_code < 600 else 502,
                        content=content)


def _raise_for(resp: httpx.Response, detail: str) -> None:
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Task not found")
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=resp.status_code if resp.status_code < 600 else 502, detail=detail,
        )


def _run_options(spec: dict[str, Any], fields_set: set[str]) -> dict[str, Any]:
    """Presence-carried run knobs from a create/retry body."""
    options: dict[str, Any] = {}
    for key in ("skills", "mcp_server_ids"):
        if key in fields_set:
            options[key] = spec.get(key) or []
    if spec.get("hitl") is False:
        options["hitl"] = False
    return options


@router.get("/tasks")
async def list_tasks(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    tenant_id, user_id, access_token = _auth
    params = {k: v for k, v in request.query_params.items() if k in _LIST_PARAMS}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{_orch_url(request)}/tasks",
            params=params,
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    return _relay(resp)


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    tenant_id, user_id, access_token = _auth
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{_orch_url(request)}/tasks/{task_id}",
            headers=foundation_headers(tenant_id, user_id, access_token),
        )
    return _relay(resp)


@router.post("/tasks", status_code=201)
async def create_task(
    body: TaskCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    _auth: tuple = Depends(require_auth()),
    clearance: str = TClearance,
) -> JSONResponse:
    """Create a task and start its first run in the background.

    Returns ``201 {"task": {...}, "run_id": "..."}``.  When the session was
    created but the run could not start (e.g. the scheduler is down), returns
    the upstream error with ``task_id`` so the UI can offer a retry.
    """
    tenant_id, user_id, access_token = _auth
    orch_url = _orch_url(request)
    headers = foundation_headers(tenant_id, user_id, access_token, clearance=clearance)
    instructions = body.instructions.strip()
    if not instructions:
        raise HTTPException(status_code=422, detail="instructions must not be blank")
    title = (body.title or "").strip() or None
    options: dict[str, Any] = {
        k: v for k, v in body.model_dump(
            include={"skills", "mcp_server_ids", "model", "reasoning_effort", "hitl"},
        ).items()
        if k in body.model_fields_set or k == "hitl"
    }

    async with httpx.AsyncClient(timeout=30) as client:
        if body.session_id:
            # Adopt the draft session the UI uploaded attachments to.
            resp = await client.get(f"{orch_url}/tasks/{body.session_id}", headers=headers)
            _raise_for(resp, "Failed to load the draft task")
            draft = resp.json()
            if draft.get("kind") != "task":
                raise HTTPException(status_code=409, detail="Session is not a task draft")
            if draft.get("run_count"):
                raise HTTPException(status_code=409, detail="This task has already started")
            session_id = body.session_id
        else:
            resp = await client.post(
                f"{orch_url}/sessions",
                json={
                    "title": title or _PLACEHOLDER_TITLE,
                    "workspace_id": body.workspace_id,
                    "agent_config": {"kind": "task"},
                },
                headers=headers,
            )
            _raise_for(resp, "Failed to create the task")
            session_id = resp.json()["id"]

        patch: dict[str, Any] = {"task": {
            "instructions": instructions,
            "options": options,
            "attachment_ids": body.attachment_ids,
        }}
        if title:
            patch["title"] = title
        resp = await client.patch(f"{orch_url}/sessions/{session_id}", json=patch,
                                  headers=headers)
        _raise_for(resp, "Failed to save the task definition")

        try:
            message, run = await start_turn(
                client,
                orch_url=orch_url,
                headers=headers,
                session_id=session_id,
                message_text=instructions,
                attachment_ids=body.attachment_ids,
                model=body.model,
                reasoning_effort=body.reasoning_effort,
                run_options=_run_options(body.model_dump(), body.model_fields_set),
            )
        except TurnError as exc:
            logger.warning("task %s created but its run did not start: %s", session_id,
                           exc.detail)
            return JSONResponse(
                status_code=exc.status_code,
                # ``reason`` lets the UI tell "saved but not started" (offer
                # Start on the board) apart from "nothing was created".
                content={"detail": exc.detail, "status": exc.status_code,
                         "reason": "task_not_started", "task_id": session_id},
            )

        if not title:
            schedule_auto_title(
                background_tasks, request, message=message, message_text=instructions,
                session_id=session_id, auth=_auth, clearance=clearance, model_hint=body.model,
            )
        resp = await client.get(f"{orch_url}/tasks/{session_id}", headers=headers)
        _raise_for(resp, "Task started but could not be loaded")
        task = resp.json()

    return JSONResponse(status_code=201, content={"task": task, "run_id": run.get("id")})


@router.post("/tasks/{task_id}/retry", status_code=201)
async def retry_task(
    task_id: str,
    request: Request,
    body: TaskRetry | None = None,
    _auth: tuple = Depends(require_auth()),
    clearance: str = TClearance,
) -> JSONResponse:
    """Re-run the task's latest attempt with a fresh sandbox.

    The input is the latest run's ``task_description`` (the instructions, or
    the most recent follow-up), with the attachments of the user message that
    started it.  The failed attempt's output is rewound first so the transcript
    and the agent's restored history both read as a clean retry; run rows are
    kept, so ``run_count`` still counts every attempt.
    """
    tenant_id, user_id, access_token = _auth
    orch_url = _orch_url(request)
    headers = foundation_headers(tenant_id, user_id, access_token, clearance=clearance)
    overrides = body or TaskRetry()

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{orch_url}/sessions/{task_id}", headers=headers)
        _raise_for(resp, "Failed to load the task")
        meta = resp.json().get("metadata") or {}
        spec = meta.get("task") if isinstance(meta.get("task"), dict) else {}
        stored = spec.get("options") if isinstance(spec.get("options"), dict) else {}

        resp = await client.get(f"{orch_url}/sessions/{task_id}/runs", headers=headers)
        _raise_for(resp, "Failed to load the task's runs")
        runs = resp.json().get("items") or []
        if not runs:
            # Never started (its first run failed to launch): start it now.
            task_text = spec.get("instructions")
            if not isinstance(task_text, str) or not task_text.strip():
                raise HTTPException(status_code=409, detail="This task has nothing to retry")
        else:
            latest = runs[0]
            if str(latest.get("status") or "").upper() in _ACTIVE_RUN_STATUSES:
                raise HTTPException(status_code=409, detail="The task is still running")
            task_text = latest.get("task_description") or ""

        turn = await _locate_turn(
            client, orch_url=orch_url, headers=headers, session_id=task_id, task_text=task_text,
        )
        if turn is not None:
            attachment_ids, rewind_from = turn
        else:
            # The turn is not in the transcript: the first run never started
            # (its message is written with the run), or it was rewound by hand.
            # The instructions keep the files they were created with.
            rewind_from = None
            attachment_ids = (
                _stored_attachment_ids(spec)
                if task_text.strip() == str(spec.get("instructions") or "").strip()
                else []
            )
        if rewind_from is not None:
            resp = await client.delete(
                f"{orch_url}/sessions/{task_id}/messages",
                params={"from_sequence": rewind_from},
                headers=headers,
            )
            _raise_for(resp, "Failed to clear the previous attempt")

        merged = {**stored, **overrides.model_dump(exclude_unset=True)}
        options = _run_options(merged, set(merged))
        try:
            _message, run = await start_turn(
                client,
                orch_url=orch_url,
                headers=headers,
                session_id=task_id,
                message_text=task_text,
                attachment_ids=attachment_ids,
                model=merged.get("model"),
                reasoning_effort=merged.get("reasoning_effort"),
                run_options=options,
                # Re-run the user turn already in the transcript; only when it
                # is gone (rewound by hand, or a first run that never started)
                # is it written again, with the run.
                persist_message=turn is None,
            )
        except TurnError as exc:
            return exc.response()

        resp = await client.get(f"{orch_url}/tasks/{task_id}", headers=headers)
        _raise_for(resp, "Retry started but the task could not be loaded")
        task = resp.json()

    # ``rewound_from`` tells an open transcript which rows were cleared, so it
    # can drop them locally instead of re-fetching the whole conversation.
    return JSONResponse(status_code=201, content={
        "task": task, "run_id": run.get("id"), "rewound_from": rewind_from,
    })


async def _list_messages(
    client: httpx.AsyncClient, orch_url: str, headers: dict[str, str], session_id: str,
) -> list[dict[str, Any]]:
    """Every message of the session in sequence order (follows the cursor)."""
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(50):  # hard stop: 50 pages x 200 messages
        params: dict[str, Any] = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = await client.get(f"{orch_url}/sessions/{session_id}/messages",
                                params=params, headers=headers)
        _raise_for(resp, "Failed to load the task transcript")
        page = resp.json()
        items.extend(page.get("items") or [])
        cursor = page.get("next_cursor")
        if not cursor:
            break
    return sorted(items, key=lambda m: int(m.get("sequence") or 0))


def _stored_attachment_ids(spec: dict[str, Any]) -> list[str]:
    raw = spec.get("attachment_ids")
    return [a for a in raw if isinstance(a, str)] if isinstance(raw, list) else []


async def _locate_turn(
    client: httpx.AsyncClient, *, orch_url: str, headers: dict[str, str],
    session_id: str, task_text: str,
) -> tuple[list[str], int | None] | None:
    """Find the user message that started the latest attempt.

    Returns its attachment ids and the sequence to rewind from (the first
    message after it, ``None`` when nothing follows), or ``None`` when no user
    message carries that text.
    """
    messages = await _list_messages(client, orch_url, headers, session_id)
    wanted = task_text.strip()
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if str(msg.get("role") or "").upper() != "USER":
            continue
        if str(msg.get("content") or "").strip() != wanted:
            continue
        attachment_ids = [
            a["id"] if isinstance(a, dict) else str(a)
            for a in (msg.get("attachments") or [])
            if (isinstance(a, dict) and a.get("id")) or isinstance(a, str)
        ]
        following = messages[idx + 1:]
        rewind_from = int(following[0]["sequence"]) if following else None
        return attachment_ids, rewind_from
    return None
