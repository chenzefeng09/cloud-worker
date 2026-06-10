"""Run API endpoints.

State consistency
-----------------
``create_run`` creates the run row and updates ``session.current_run_id``
inside a *single* transaction.  The earlier implementation committed
twice; if the second commit failed (e.g. session row updated_at conflict)
the run row was already persisted but the session pointed nowhere, leaving
an orphaned run hidden from the UI but still consuming sandbox resources.

If the scheduler call itself fails, the whole transaction rolls back via
the implicit ``async with session.begin()`` opened by ``db_session``, so
no half-baked run record survives.  The container is the one external
side-effect we cannot atomically roll back; if spawn succeeded but the
DB commit fails we instead emit a best-effort cancel — at worst the
sandbox times out via ``Settings.sandbox_timeout`` and stops itself.

Memory pre-fetch
----------------
Before relaying the run to the scheduler, ``create_run`` calls knowledge-ingest
``GET /search`` using the task text as the query.  The top-K hits (memories
stored by previous runs) are formatted into a ``memory`` string and included
in the task body sent to the scheduler, which in turn writes it to task.json.
The in-sandbox worker reads ``task["memory"]`` and prepends it to the system
prompt.  This is intentionally failure-open: if knowledge-ingest is unreachable
the run proceeds without memory context.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.api.authz_guard import require_permission
from agent_session_orchestrator.api.dependencies import TDbSession, TTenantId, TUserId
# Reuse the chat path's run-completion reconciler verbatim (poll scheduler,
# map container status, persist result_summary/status/finished_at/tokens, write
# the assistant message, failure-open).  It is module-private in messages.py but
# both modules live in the same ``api.v1`` package, so importing the
# ``_``-prefixed helper here is the least-invasive way to share it without
# duplicating any of its logic.
from agent_session_orchestrator.api.v1.messages import _reconcile_run_completion
from agent_session_orchestrator.domain.models import Run
from agent_session_orchestrator.services.session_service import SessionService

router = APIRouter(tags=["Runs"])
logger = logging.getLogger(__name__)

_MEMORY_SEARCH_K = 5  # top-K memory hits to inject


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def _serialize_run(r: Run) -> dict[str, Any]:
    """Run → dict for list responses (session-level and tenant-level share this)."""
    return {
        "id": r.id,
        "session_id": r.session_id,
        "sandbox_id": r.sandbox_id,
        "status": r.status,
        "task_description": r.task_description,
        "result_summary": r.result_summary,
        "error_message": r.error_message,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "created_at": r.created_at.isoformat(),
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "total_tokens": r.total_tokens,
        "app_snapshot": r.app_snapshot,
    }


async def _fetch_memory(*, knowledge_ingest_url: str, tenant_id: str, query: str) -> str:
    """Query knowledge-ingest for relevant memories and return a formatted string.

    Uses ``GET /search?q=<query>&k=<K>`` with ``X-Tenant-Id`` header.
    Returns an empty string on any error (failure-open).
    """
    if not query.strip() or not knowledge_ingest_url:
        return ""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{knowledge_ingest_url}/search",
                params={"q": query[:500], "k": _MEMORY_SEARCH_K},
                headers={"X-Tenant-Id": tenant_id, "X-User-Id": "SYSTEM"},
            )
        if resp.status_code != 200:
            logger.debug(
                "memory search returned %s; proceeding without memory",
                resp.status_code,
            )
            return ""
        hits = resp.json().get("hits") or []
        if not hits:
            return ""
        snippets = []
        for hit in hits:
            text = str(hit.get("text") or "").strip()
            if text:
                snippets.append(f"- {text}")
        return "\n".join(snippets)
    except Exception as exc:  # noqa: BLE001
        logger.debug("memory pre-fetch failed (failure-open): %s", exc)
        return ""


async def _assemble_history(
    svc: SessionService, session_id: str, *, current_task: str
) -> list[dict[str, Any]]:
    """Build structured prior-turn history for the worker to restore.

    Returns a list of items shaped like::

        {"role": "user", "type": "text", "content": "..."}
        {"role": "assistant", "type": "text", "content": "..."}
        {"role": "tool", "type": "tool", "tool": {id,name,input,result,state}}

    The current user message — already persisted by BFF just before this run
    was triggered — is excluded (it's this turn's input, not history).  SYSTEM
    rows are dropped; empty ASSISTANT/TOOL rows are skipped.  Failure-open:
    returns an empty list on any error so a history glitch never blocks a run.
    """
    try:
        messages = await svc.list_all_messages(session_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("history assembly failed (failure-open): %s", exc)
        return []

    # Drop the most recent USER row whose content matches this run's task —
    # that's the message BFF just wrote as this turn's input.
    # NB: the message table stores role inconsistently ("USER" upper-cased,
    # but "assistant"/"tool"/"system" lower-cased), so compare case-insensitively
    # — otherwise assistant/tool history is silently dropped and the model only
    # sees a stream of unanswered user questions.
    task_norm = current_task.strip()
    for i in range(len(messages) - 1, -1, -1):
        if (messages[i].role or "").upper() == "USER":
            if (messages[i].content or "").strip() == task_norm:
                messages.pop(i)
            break

    history: list[dict[str, Any]] = []
    for msg in messages:
        role = (msg.role or "").upper()
        if role == "USER":
            history.append(
                {"role": "user", "type": "text", "content": msg.content or ""}
            )
        elif role == "ASSISTANT":
            if (msg.content or "").strip():
                history.append(
                    {"role": "assistant", "type": "text", "content": msg.content}
                )
        elif role == "TOOL":
            calls = msg.tool_calls or []
            tool = calls[0] if isinstance(calls, list) and calls else None
            if isinstance(tool, dict) and tool:
                history.append({"role": "tool", "type": "tool", "tool": tool})
        # SYSTEM rows are intentionally skipped.
    return history


async def _get_scheduler_url(request: Request) -> str:
    url: str = request.app.state.settings.agent_scheduler_url
    return url


@router.post("/sessions/{session_id}/runs", status_code=201,
             dependencies=[Depends(require_permission("run.create", "run"))])
async def create_run(
    session_id: str,
    body: dict[str, Any],
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> dict[str, Any]:
    svc = SessionService(db, tenant_id=tenant_id)
    session = await svc.get_session(session_id)

    task = body.get("task", "")

    # App-harness fields (optional; only App runs send these — chat runs omit
    # them and keep working unchanged).  ``sys_prompt`` is appended to the
    # system prompt by the worker; ``sampling_params`` is applied per-run by
    # the gateway model builder; ``app_snapshot`` is persisted on the run row.
    sys_prompt = body.get("sys_prompt")
    sampling_params = body.get("sampling_params") or {}
    app_snapshot = body.get("app_snapshot")

    # Persist the snapshot atomically with the run row (same transaction).
    run = await svc.create_run(session_id, task, app_snapshot=app_snapshot)

    # Relay to agent-scheduler.
    scheduler_url = await _get_scheduler_url(request)
    headers = {"X-Tenant-Id": tenant_id, "X-User-Id": user_id}

    # Optional attachment files — [{"key": s3key, "filename": name, "mime": mime}, ...]
    files: list[dict[str, Any]] = body.get("files") or []

    # Pre-fetch relevant memory from knowledge-ingest; failure-open.
    knowledge_ingest_url = request.app.state.settings.knowledge_ingest_url
    memory_snippets = await _fetch_memory(
        knowledge_ingest_url=knowledge_ingest_url,
        tenant_id=tenant_id,
        query=task,
    )

    # Assemble prior-turn conversation history so the worker can restore it
    # into the agent's context (multi-turn memory).  BFF writes the current
    # user message *before* triggering this run, so it's already in the table
    # as the latest USER row — we drop it (it's this run's input, not history).
    history = await _assemble_history(svc, session_id, current_task=task)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{scheduler_url}/runs",
                json={
                    "session_id": session_id,
                    "run_id": run.id,
                    "task": task,
                    "agent_config": {
                        **(body.get("agent_config") or {}),
                        "model": body.get("model"),
                        "reasoning_effort": body.get("reasoning_effort"),
                    },
                    "files": files,
                    "memory": memory_snippets,
                    # Per-message skill selection relayed from BFF; scheduler
                    # writes these into task.json["skills"] for the worker.
                    "skills": body.get("skills") or [],
                    # Prior-turn conversation history for multi-turn memory.
                    "history": history,
                    # App-harness fields (empty/absent for chat runs).  The
                    # scheduler writes these into task.json for the worker:
                    # ``sys_prompt`` is appended to the system prompt;
                    # ``sampling_params`` is applied per-run by the gateway model.
                    "sys_prompt": sys_prompt,
                    "sampling_params": sampling_params,
                },
                headers=headers,
            )
    except httpx.HTTPError as exc:
        # No container was spawned — let the transaction roll back.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="agent-scheduler unreachable",
        ) from exc

    if resp.status_code >= 400:
        # Don't echo upstream body verbatim — it may include stack traces or
        # other sensitive details.  Capture in audit instead.
        logger.warning(
            "scheduler rejected run",
            extra={"status": resp.status_code, "run_id": run.id},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"scheduler rejected run (HTTP {resp.status_code})",
        )

    scheduler_run = resp.json()

    # Update both rows; the surrounding ``async with session.begin()`` will
    # commit once when the request completes, or roll back atomically if
    # anything else in this handler raises.
    # Docker container ids are 64 chars; the ``sandbox_id`` column is
    # String(50).  Persist Docker's standard 12-char short form, which
    # the scheduler also accepts on subsequent lookups.
    sandbox_id_full = str(scheduler_run.get("sandbox_id") or "")
    run.sandbox_id = sandbox_id_full[:12] if sandbox_id_full else None
    run.status = "RUNNING"
    run.started_at = _now()
    session.current_run_id = run.id
    await db.flush()

    return {
        "id": run.id,
        "session_id": session_id,
        "sandbox_id": run.sandbox_id,
        "status": run.status,
        "task_description": run.task_description,
        "created_at": run.created_at.isoformat(),
    }


@router.get("/sessions/{session_id}/runs",
            dependencies=[Depends(require_permission("run.read", "run"))])
async def list_runs(
    session_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> dict[str, Any]:
    svc = SessionService(db, tenant_id=tenant_id)
    runs = await svc.list_runs(session_id)
    return {"items": [_serialize_run(r) for r in runs]}


@router.get("/runs",
            dependencies=[Depends(require_permission("run.read", "run"))])
async def list_tenant_runs(
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """List runs for the current tenant across all sessions.

    Tenant is scoped from the X-Tenant-Id middleware (same as every other
    endpoint here).  ``status`` is normalised to upper-case before filtering;
    ``limit`` is capped at 200.  Response shape matches the session-level list.
    """
    svc = SessionService(db, tenant_id=tenant_id)
    status_norm = status.upper() if status else None
    runs = await svc.list_tenant_runs(status=status_norm, limit=limit)
    return {"items": [_serialize_run(r) for r in runs]}


@router.get("/sessions/{session_id}/runs/{run_id}",
            dependencies=[Depends(require_permission("run.read", "run"))])
async def get_run(
    session_id: str,
    run_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> dict[str, Any]:
    svc = SessionService(db, tenant_id=tenant_id)
    run = await svc.get_run(session_id, run_id)

    # Opportunistically reconcile a still-running run before returning.
    #
    # App runs are triggered via POST /sessions/{id}/runs directly (not the
    # chat path), and the App result page polls THIS endpoint for terminal
    # status + result_summary.  It never calls list_messages or opens the chat
    # SSE stream — the only two places that used to invoke reconciliation.  So
    # without this, the sandbox container exits but the run row stays RUNNING
    # forever and the page polls indefinitely.  We reuse the exact same helper
    # (and the same failure-open + flush discipline) as list_messages so the
    # chat path behaviour is unchanged.
    if run.status in ("RUNNING", "PENDING"):
        try:
            scheduler_url = request.app.state.settings.agent_scheduler_url
            knowledge_ingest_url = request.app.state.settings.knowledge_ingest_url
            redis_url = request.app.state.settings.redis_url
            # The scheduler's authz guard rejects the "SYSTEM" sentinel, so use
            # the session's originating user for the poll header (as list_messages does).
            session_obj = await svc.get_session(session_id)
            await _reconcile_run_completion(
                scheduler_url=scheduler_url,
                tenant_id=tenant_id,
                user_id=session_obj.user_id,
                run=run,
                svc=svc,
                knowledge_ingest_url=knowledge_ingest_url,
                redis_url=redis_url,
            )
            # Flush (not commit) so the reconciled status/result_summary/tokens
            # land in this transaction; the db_session dependency commits on
            # return.  ``run`` is the same ORM instance the helper mutated, so
            # the response below already reflects the new values.
            await db.flush()
        except Exception as exc:  # best-effort — never block the run read
            logger.warning(
                "reconcile in get_run failed", extra={"run_id": run_id, "err": str(exc)}
            )

    return {
        "id": run.id,
        "session_id": run.session_id,
        "sandbox_id": run.sandbox_id,
        "status": run.status,
        "task_description": run.task_description,
        "result_summary": run.result_summary,
        "error_message": run.error_message,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_at": run.created_at.isoformat(),
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "total_tokens": run.total_tokens,
        "app_snapshot": run.app_snapshot,
    }


@router.get("/sessions/{session_id}/runs/{run_id}/state",
            dependencies=[Depends(require_permission("run.read", "run"))])
async def get_run_state(
    session_id: str,
    run_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> dict:
    """Return the real-time hot-state for a run from Redis.

    The runtime_worker inside the sandbox writes a JSON blob to
    ``agent:run:{run_id}:state`` after every ThinkEvent / step boundary.
    This endpoint reads that blob so the frontend can show live plan/progress.

    Failure-open: if Redis is unavailable or the key has expired, returns
    200 with ``plan=""``, ``step=0``, ``cursor=""`` and status from the DB row.
    """
    import json

    # Validate ownership: raises 404 if run does not belong to this session/tenant.
    svc = SessionService(db, tenant_id=tenant_id)
    run = await svc.get_run(session_id, run_id)

    redis_state: dict | None = None
    redis_url: str | None = request.app.state.settings.redis_url
    if redis_url:
        try:
            import redis.asyncio as aioredis  # type: ignore[import-not-found]

            client = aioredis.Redis.from_url(
                redis_url,
                socket_connect_timeout=2,
                socket_timeout=2,
                decode_responses=True,
            )
            try:
                raw = await client.get(f"agent:run:{run_id}:state")
                if raw:
                    redis_state = json.loads(raw)
            finally:
                await client.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis read failed for run_state %s: %s", run_id, exc)

    if redis_state:
        return {
            "run_id": run_id,
            "plan": redis_state.get("plan") or "",
            "step": redis_state.get("step") or 0,
            "status": redis_state.get("status") or run.status,
            "cursor": redis_state.get("cursor") or "",
        }

    # Redis key absent / Redis unavailable — return DB status, empty plan.
    return {
        "run_id": run_id,
        "plan": "",
        "step": 0,
        "status": run.status,
        "cursor": "",
    }


async def _do_cancel_run(
    *,
    session_id: str,
    run_id: str,
    request: Request,
    db: AsyncSession,
    tenant_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Core cancel logic — shared by the run-level and session-level endpoints."""
    svc = SessionService(db, tenant_id=tenant_id)
    run = await svc.get_run(session_id, run_id)

    # Use run.sandbox_id (short Docker container id) as the scheduler path
    # segment — the same convention used by _reconcile_run_completion in
    # messages.py.  Passing run_id here would fail because the scheduler
    # looks up the container by its Docker id, not our internal run id.
    scheduler_run_key = run.sandbox_id or run_id

    scheduler_url = await _get_scheduler_url(request)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{scheduler_url}/runs/{scheduler_run_key}/cancel",
                headers={"X-Tenant-Id": tenant_id, "X-User-Id": user_id},
            )
    except httpx.HTTPError:
        # The container may already have stopped; we still mark it cancelled
        # in our DB so the UI stops showing it as RUNNING.  The next sandbox
        # timeout will reap any leftover container.
        logger.warning("cancel: scheduler unreachable", extra={"run_id": run_id})

    run.status = "CANCELLED"
    run.finished_at = _now()
    await db.flush()

    # Write a system message so the transcript records the cancellation.
    await svc.create_message(
        session_id,
        role="system",
        content="任务已取消",
        run_id=run_id,
    )

    return {"id": run.id, "status": "CANCELLED"}


@router.get("/runs/token-usage/daily")
async def daily_token_usage(
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    days: int = Query(default=7, ge=1, le=90),
) -> dict[str, Any]:
    """Return per-day token totals for the last N days (default 7).

    SQL: SELECT date(created_at) AS day, SUM(COALESCE(total_tokens, 0)) AS tokens
         FROM run
         WHERE tenant_id = :tenant_id AND created_at >= now() - interval 'N days'
         GROUP BY date(created_at)

    Fills in missing days with 0 so the response always contains exactly
    ``days`` entries in ascending order.
    """
    cutoff = dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=days)

    stmt = (
        select(
            func.date(Run.created_at).label("day"),
            func.sum(func.coalesce(Run.total_tokens, 0)).label("tokens"),
        )
        .where(Run.tenant_id == tenant_id, Run.created_at >= cutoff)
        .group_by(func.date(Run.created_at))
        .order_by(func.date(Run.created_at))
    )
    result = await db.execute(stmt)
    rows = result.all()

    # Build a dict keyed by date string for fast lookup.
    by_day: dict[str, int] = {}
    for row in rows:
        # row.day may be a date object or a string depending on the DB driver.
        day_key = str(row.day) if not isinstance(row.day, str) else row.day
        by_day[day_key] = int(row.tokens or 0)

    # Generate the full range of the last N days (today is index days-1).
    today = dt.date.today()
    out: list[dict[str, Any]] = []
    for i in range(days):
        d = today - dt.timedelta(days=days - 1 - i)
        key = d.isoformat()
        out.append({"day": key, "tokens": by_day.get(key, 0)})

    return {"days": out}


@router.post("/sessions/{session_id}/runs/{run_id}/cancel",
             dependencies=[Depends(require_permission("run.cancel", "run"))])
async def cancel_run(
    session_id: str,
    run_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> dict[str, Any]:
    return await _do_cancel_run(
        session_id=session_id,
        run_id=run_id,
        request=request,
        db=db,
        tenant_id=tenant_id,
        user_id=user_id,
    )


@router.post("/sessions/{session_id}/cancel",
             dependencies=[Depends(require_permission("run.cancel", "run"))])
async def cancel_session_run(
    session_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> dict[str, Any]:
    """Convenience endpoint: cancel the current active run for a session.

    The frontend only knows the session_id at cancel time; this endpoint
    resolves the current run internally so the client doesn't need to track
    run_id.
    """
    from sqlalchemy import select as _select

    from agent_session_orchestrator.domain.models import Run as _Run

    svc = SessionService(db, tenant_id=tenant_id)
    await svc.get_session(session_id)

    # Find the most recent RUNNING or PENDING run for this session.
    stmt = (
        _select(_Run)
        .where(
            _Run.session_id == session_id,
            _Run.tenant_id == tenant_id,
            _Run.status.in_(("RUNNING", "PENDING")),
        )
        .order_by(_Run.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    run = result.scalar_one_or_none()

    if run is None:
        # No active run — idempotently return success.
        return {"id": None, "status": "CANCELLED", "detail": "no active run"}

    return await _do_cancel_run(
        session_id=session_id,
        run_id=run.id,
        request=request,
        db=db,
        tenant_id=tenant_id,
        user_id=user_id,
    )
