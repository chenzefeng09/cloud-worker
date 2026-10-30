"""Task board API — the caller's sessions with their latest-run lifecycle.

Read-only: tasks are created, retried and cancelled through the ordinary
session/run endpoints (the BFF composes them).  See
``services/task_service.py`` for how a session maps onto a task.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.api.authz_guard import require_permission
from agent_session_orchestrator.api.dependencies import TDbSession, TTenantId, TUserId
from agent_session_orchestrator.services.task_service import (
    TASK_KINDS,
    TASK_STATUSES,
    TaskService,
    parse_csv_filter,
)

router = APIRouter(tags=["Tasks"])


@router.get("/tasks", dependencies=[Depends(require_permission("session.read", "session"))])
async def list_tasks(
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
    status_filter: str | None = Query(default=None, alias="status"),
    kind: str | None = None,
    workspace_id: str | None = None,
    q: str | None = Query(default=None, max_length=200),
    cursor: str | None = None,
    limit: int = Query(default=30, ge=1, le=100),
) -> dict[str, Any]:
    """List the caller's tasks, newest activity first.

    * ``status`` — comma-separated board statuses (``queued``, ``running``,
      ``needs_input``, ``completed``, ``failed``, ``cancelled``, ``idle``).
    * ``kind``   — comma-separated ``task`` / ``chat`` / ``app_run``.
    * ``q``      — case-insensitive title substring.
    * ``cursor`` — the ``next_cursor`` of the previous page.

    ``counts`` holds per-status totals under the same kind/workspace/search
    filters, ignoring ``status`` so filter tabs can show every bucket.
    """
    try:
        statuses = parse_csv_filter(status_filter, TASK_STATUSES, name="status")
        kinds = parse_csv_filter(kind, TASK_KINDS, name="kind")
        cursor_dt = datetime.fromisoformat(cursor) if cursor else None
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc),
        ) from exc

    svc = TaskService(db, tenant_id=tenant_id, user_id=user_id)
    items, next_cursor = await svc.list_tasks(
        statuses=statuses, kinds=kinds, workspace_id=workspace_id, q=q,
        cursor=cursor_dt, limit=limit,
    )
    counts = await svc.count_by_status(kinds=kinds, workspace_id=workspace_id, q=q)
    return {"items": items, "next_cursor": next_cursor, "counts": counts}


@router.get("/tasks/{session_id}",
            dependencies=[Depends(require_permission("session.read", "session"))])
async def get_task(
    session_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> dict[str, Any]:
    svc = TaskService(db, tenant_id=tenant_id, user_id=user_id)
    return await svc.get_task(session_id)
