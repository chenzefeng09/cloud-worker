"""Task board read model — sessions viewed as agentic tasks.

A *task* is a session plus the lifecycle of its runs.  There is no separate
table: the board status is derived from the session's most recent run, so it
can never drift from what the sandbox worker actually reported.

* ``kind`` comes from ``sessions.metadata->>'kind'``: ``task`` (created from the
  task board's structured form), ``app_run`` (an App execution) or absent
  (an interactive chat, reported as ``chat``).
* ``status`` maps the latest run's status onto the board vocabulary below; a
  session that has never run is ``idle``.

Everything here is owner-scoped: the board only ever lists the caller's own
sessions (same IDOR posture as ``GET /sessions``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from agent_session_orchestrator.domain.models import Run, Session
from agent_session_orchestrator.errors import SessionNotFoundError

# Board vocabulary, in display order.
TASK_STATUSES: tuple[str, ...] = (
    "queued", "running", "needs_input", "completed", "failed", "cancelled", "idle",
)
TASK_KINDS: tuple[str, ...] = ("task", "chat", "app_run")

_RUN_TO_TASK: dict[str, str] = {
    "PENDING": "queued",
    "RUNNING": "running",
    "PAUSED": "needs_input",
    "COMPLETED": "completed",
    "FAILED": "failed",
    "CANCELLED": "cancelled",
}
_TASK_TO_RUN: dict[str, str] = {v: k for k, v in _RUN_TO_TASK.items()}

# Text fields are clipped so a board page stays small; the full transcript is
# one click away on the task detail view.
_SNIPPET_CHARS = 400
_INSTRUCTIONS_CHARS = 1000


def parse_csv_filter(raw: str | None, allowed: tuple[str, ...], *, name: str) -> list[str] | None:
    """Parse ``a,b`` into a validated list; ``None``/empty means no filter."""
    if raw is None or not raw.strip():
        return None
    values = [v.strip().lower() for v in raw.split(",") if v.strip()]
    unknown = sorted(set(values) - set(allowed))
    if unknown:
        raise ValueError(f"unknown {name} value(s): {', '.join(unknown)}")
    return values


def task_status_for(run_status: str | None) -> str:
    if run_status is None:
        return "idle"
    return _RUN_TO_TASK.get(run_status.upper(), "idle")


class TaskService:
    def __init__(self, db: AsyncSession, *, tenant_id: str, user_id: str) -> None:
        self._db = db
        self._tenant_id = tenant_id
        self._user_id = user_id

    # ── query construction ────────────────────────────────────────────────

    def _latest_run_id(self) -> Any:
        """Correlated scalar subquery: the id of the session's newest run."""
        return (
            select(Run.id)
            .where(Run.session_id == Session.id, Run.tenant_id == self._tenant_id)
            .order_by(Run.created_at.desc(), Run.id.desc())
            .limit(1)
            .correlate(Session)
            .scalar_subquery()
        )

    def _run_count(self) -> Any:
        return (
            select(func.count(Run.id))
            .where(Run.session_id == Session.id, Run.tenant_id == self._tenant_id)
            .correlate(Session)
            .scalar_subquery()
        )

    def _filters(
        self, *, kinds: list[str] | None, workspace_id: str | None, q: str | None,
    ) -> list[ColumnElement[bool]]:
        clauses: list[ColumnElement[bool]] = [
            Session.tenant_id == self._tenant_id,
            Session.user_id == self._user_id,
            Session.deleted_at.is_(None),
        ]
        if kinds:
            kind_col = Session.agent_metadata["kind"].astext
            alternatives: list[ColumnElement[bool]] = []
            explicit = [k for k in kinds if k != "chat"]
            if explicit:
                alternatives.append(kind_col.in_(explicit))
            if "chat" in kinds:
                # Interactive chats carry no kind (or a legacy explicit "chat").
                alternatives.append(or_(kind_col.is_(None), kind_col == "chat"))
            clauses.append(or_(*alternatives))
        if workspace_id:
            clauses.append(Session.workspace_id == workspace_id)
        if q and q.strip():
            clauses.append(
                Session.title.ilike(f"%{_escape_like(q.strip())}%", escape="\\")
            )
        return clauses

    @staticmethod
    def _status_clause(statuses: list[str]) -> ColumnElement[bool]:
        alternatives: list[ColumnElement[bool]] = []
        run_statuses = [_TASK_TO_RUN[s] for s in statuses if s in _TASK_TO_RUN]
        if run_statuses:
            alternatives.append(Run.status.in_(run_statuses))
        if "idle" in statuses:
            alternatives.append(Run.id.is_(None))
        return or_(*alternatives)

    # ── public API ─────────────────────────────────────────────────────────

    async def list_tasks(
        self,
        *,
        statuses: list[str] | None = None,
        kinds: list[str] | None = None,
        workspace_id: str | None = None,
        q: str | None = None,
        cursor: datetime | None = None,
        limit: int = 30,
    ) -> tuple[list[dict[str, Any]], str | None]:
        stmt = (
            select(Session, Run, self._run_count().label("run_count"))
            .outerjoin(Run, Run.id == self._latest_run_id())
            .where(*self._filters(kinds=kinds, workspace_id=workspace_id, q=q))
        )
        if statuses:
            stmt = stmt.where(self._status_clause(statuses))
        if cursor is not None:
            stmt = stmt.where(Session.updated_at < cursor)
        stmt = stmt.order_by(Session.updated_at.desc(), Session.id.desc()).limit(limit + 1)

        rows = list((await self._db.execute(stmt)).all())
        next_cursor: str | None = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = rows[-1][0].updated_at.isoformat()
        return [serialize_task(s, r, c) for s, r, c in rows], next_cursor

    async def count_by_status(
        self, *, kinds: list[str] | None = None, workspace_id: str | None = None,
        q: str | None = None,
    ) -> dict[str, int]:
        """Per-status totals for the board's filter tabs (status filter ignored)."""
        stmt = (
            select(Run.status, func.count(Session.id))
            .select_from(Session)
            .outerjoin(Run, Run.id == self._latest_run_id())
            .where(*self._filters(kinds=kinds, workspace_id=workspace_id, q=q))
            .group_by(Run.status)
        )
        counts = {s: 0 for s in TASK_STATUSES}
        for run_status, n in (await self._db.execute(stmt)).all():
            counts[task_status_for(run_status)] += int(n)
        return counts

    async def get_task(self, session_id: str) -> dict[str, Any]:
        stmt = (
            select(Session, Run, self._run_count().label("run_count"))
            .outerjoin(Run, Run.id == self._latest_run_id())
            .where(
                Session.id == session_id,
                *self._filters(kinds=None, workspace_id=None, q=None),
            )
        )
        row = (await self._db.execute(stmt)).first()
        if row is None:
            raise SessionNotFoundError(session_id)
        session, run, run_count = row
        return serialize_task(session, run, run_count)


def serialize_task(session: Session, run: Run | None, run_count: int | None) -> dict[str, Any]:
    meta = session.agent_metadata if isinstance(session.agent_metadata, dict) else {}
    kind = meta.get("kind")
    task = meta.get("task")
    spec: dict[str, Any] = task if isinstance(task, dict) else {}
    instructions = spec.get("instructions")
    if not isinstance(instructions, str):
        instructions = run.task_description if run is not None else None
    options = spec.get("options")
    latest_run: dict[str, Any] | None = None
    if run is not None:
        latest_run = {
            "id": run.id,
            "status": run.status,
            "source": run.source,
            "app_id": run.app_id,
            "created_at": _iso(run.created_at),
            "started_at": _iso(run.started_at),
            "finished_at": _iso(run.finished_at),
            "result_summary": _clip(run.result_summary, _SNIPPET_CHARS),
            "error_message": _clip(run.error_message, _SNIPPET_CHARS),
            "total_tokens": run.total_tokens,
        }
    return {
        "id": session.id,
        "title": session.title,
        "kind": kind if kind in TASK_KINDS else "chat",
        "status": task_status_for(run.status if run is not None else None),
        "workspace_id": session.workspace_id,
        "instructions": _clip(instructions, _INSTRUCTIONS_CHARS),
        "options": options if isinstance(options, dict) else None,
        "latest_run": latest_run,
        "run_count": int(run_count or 0),
        "created_at": _iso(session.created_at),
        "updated_at": _iso(session.updated_at),
    }


def _clip(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
