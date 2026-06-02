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
"""

from __future__ import annotations

import datetime as dt
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.api.dependencies import TDbSession, TTenantId, TUserId
from agent_session_orchestrator.services.session_service import SessionService

router = APIRouter(tags=["Runs"])
logger = logging.getLogger(__name__)


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


async def _get_scheduler_url(request: Request) -> str:
    return request.app.state.settings.agent_scheduler_url


@router.post("/sessions/{session_id}/runs", status_code=201)
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

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{scheduler_url}/runs",
                json={
                    "session_id": session_id,
                    "run_id": run.id,
                    "task": task,
                    "agent_config": body.get("agent_config", {}),
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
    run.sandbox_id = scheduler_run.get("sandbox_id")
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


@router.get("/sessions/{session_id}/runs")
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
            }
            for r in runs
        ]
    }


@router.get("/sessions/{session_id}/runs/{run_id}")
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
    }


@router.post("/sessions/{session_id}/runs/{run_id}/cancel")
async def cancel_run(
    session_id: str,
    run_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> dict:
    svc = SessionService(db, tenant_id=tenant_id)
    run = await svc.get_run(session_id, run_id)

    scheduler_url = await _get_scheduler_url(request)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{scheduler_url}/runs/{run_id}/cancel",
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

    return {"id": run.id, "status": "CANCELLED"}
