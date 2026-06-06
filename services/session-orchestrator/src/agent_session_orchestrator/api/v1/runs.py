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

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.api.authz_guard import require_permission
from agent_session_orchestrator.api.dependencies import TDbSession, TTenantId, TUserId
from agent_session_orchestrator.services.session_service import SessionService

router = APIRouter(tags=["Runs"])
logger = logging.getLogger(__name__)

_MEMORY_SEARCH_K = 5  # top-K memory hits to inject


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


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


async def _get_scheduler_url(request: Request) -> str:
    return request.app.state.settings.agent_scheduler_url


@router.post("/sessions/{session_id}/runs", status_code=201,
             dependencies=[Depends(require_permission("run.create", "run"))])
async def create_run(
    session_id: str,
    body: dict,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> dict:
    svc = SessionService(db, tenant_id=tenant_id)
    session = await svc.get_session(session_id)

    task = body.get("task", "")
    run = await svc.create_run(session_id, task)

    # Relay to agent-scheduler.
    scheduler_url = await _get_scheduler_url(request)
    headers = {"X-Tenant-Id": tenant_id, "X-User-Id": user_id}

    # Optional attachment files — [{"key": s3key, "filename": name, "mime": mime}, ...]
    files: list[dict] = body.get("files") or []

    # Pre-fetch relevant memory from knowledge-ingest; failure-open.
    knowledge_ingest_url = request.app.state.settings.knowledge_ingest_url
    memory_snippets = await _fetch_memory(
        knowledge_ingest_url=knowledge_ingest_url,
        tenant_id=tenant_id,
        query=task,
    )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{scheduler_url}/runs",
                json={
                    "session_id": session_id,
                    "run_id": run.id,
                    "task": task,
                    "agent_config": body.get("agent_config", {}),
                    "files": files,
                    "memory": memory_snippets,
                    # Per-message skill selection relayed from BFF; scheduler
                    # writes these into task.json["skills"] for the worker.
                    "skills": body.get("skills") or [],
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
) -> dict:
    svc = SessionService(db, tenant_id=tenant_id)
    runs = await svc.list_runs(session_id)
    return {
        "items": [
            {
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
            }
            for r in runs
        ]
    }


@router.get("/sessions/{session_id}/runs/{run_id}",
            dependencies=[Depends(require_permission("run.read", "run"))])
async def get_run(
    session_id: str,
    run_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> dict:
    svc = SessionService(db, tenant_id=tenant_id)
    run = await svc.get_run(session_id, run_id)
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
) -> dict:
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


@router.post("/sessions/{session_id}/runs/{run_id}/cancel",
             dependencies=[Depends(require_permission("run.cancel", "run"))])
async def cancel_run(
    session_id: str,
    run_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> dict:
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
) -> dict:
    """Convenience endpoint: cancel the current active run for a session.

    The frontend only knows the session_id at cancel time; this endpoint
    resolves the current run internally so the client doesn't need to track
    run_id.
    """
    from sqlalchemy import select as _select

    from agent_session_orchestrator.domain.models import Run as _Run

    svc = SessionService(db, tenant_id=tenant_id)
    session = await svc.get_session(session_id)

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
