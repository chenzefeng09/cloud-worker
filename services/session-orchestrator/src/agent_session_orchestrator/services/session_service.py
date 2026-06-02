"""Session service — business logic for session lifecycle.

Transaction discipline
----------------------
The service NEVER calls ``commit()`` on its own.  All writes happen inside
the transaction opened by ``domain.db.get_session`` (which also installed
the ``SET LOCAL app.tenant_id`` that makes RLS work).  Committing here
would close that transaction, release the tenant context, and silently
demote subsequent operations to unauthenticated RLS rejections — exactly
the situation we are guarding against.

When the service needs database-generated columns (id, created_at) before
the transaction ends, it uses ``flush()`` instead — that pushes pending
state to the DB without committing.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.domain.models import Message, Run, Session
from agent_session_orchestrator.errors import RunNotFoundError, SessionNotFoundError


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


class SessionService:
    def __init__(self, db: AsyncSession, *, tenant_id: str) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def list_sessions(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[Session], str | None]:
        stmt = (
            select(Session)
            .where(
                Session.tenant_id == self._tenant_id,
                Session.deleted_at.is_(None),
            )
            .order_by(Session.updated_at.desc())
            .limit(limit + 1)
        )
        if cursor:
            stmt = stmt.where(Session.updated_at < cursor)

        result = await self._db.execute(stmt)
        rows = list(result.scalars().all())

        next_cursor: str | None = None
        if len(rows) > limit:
            next_cursor = rows[-1].updated_at.isoformat()
            rows = rows[:limit]
        return rows, next_cursor

    async def create_session(
        self, *, user_id: str, title: str | None = None, metadata: dict | None = None
    ) -> Session:
        session = Session(
            tenant_id=self._tenant_id,
            user_id=user_id,
            title=title,
            agent_metadata=metadata or {},  # SQL column is still ``metadata``
        )
        self._db.add(session)
        await self._db.flush()  # populate id via PK default
        # refresh pulls server_default columns (created_at / updated_at) so
        # the caller can serialise them immediately without a follow-up read.
        await self._db.refresh(session)
        return session

    async def get_session(self, session_id: str) -> Session:
        stmt = select(Session).where(
            Session.id == session_id,
            Session.tenant_id == self._tenant_id,
            Session.deleted_at.is_(None),
        )
        result = await self._db.execute(stmt)
        session = result.scalar_one_or_none()
        if session is None:
            raise SessionNotFoundError(session_id)
        return session

    async def update_session(self, session_id: str, **kwargs: object) -> Session:
        session = await self.get_session(session_id)
        for key, value in kwargs.items():
            if value is not None and hasattr(session, key):
                setattr(session, key, value)
        await self._db.flush()
        return session

    async def delete_session(self, session_id: str) -> None:
        session = await self.get_session(session_id)
        session.deleted_at = _now()
        await self._db.flush()

    async def create_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        tool_calls: dict | None = None,
        run_id: str | None = None,
    ) -> Message:
        await self.get_session(session_id)

        result = await self._db.execute(
            select(Message.sequence)
            .where(Message.session_id == session_id)
            .order_by(Message.sequence.desc())
            .limit(1)
        )
        last_seq = result.scalar_one_or_none()
        sequence = (last_seq or 0) + 1

        msg = Message(
            tenant_id=self._tenant_id,
            session_id=session_id,
            role=role,
            content=content,
            tool_calls=tool_calls,
            run_id=run_id,
            sequence=sequence,
        )
        self._db.add(msg)
        await self._db.flush()
        await self._db.refresh(msg)
        return msg

    async def list_messages(
        self, session_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[Message], str | None]:
        await self.get_session(session_id)

        stmt = (
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.sequence.asc())
            .limit(limit + 1)
        )
        # Cursor is a stringified sequence number; emit a clear error if a
        # caller passes something else so we notice contract drift quickly.
        if cursor:
            try:
                stmt = stmt.where(Message.sequence > int(cursor))
            except ValueError as exc:
                raise ValueError(
                    f"list_messages cursor must be an int sequence, got {cursor!r}"
                ) from exc

        result = await self._db.execute(stmt)
        rows = list(result.scalars().all())

        next_cursor: str | None = None
        if len(rows) > limit:
            next_cursor = str(rows[-1].sequence)
            rows = rows[:limit]
        return rows, next_cursor

    async def create_run(self, session_id: str, task: str) -> Run:
        await self.get_session(session_id)
        run = Run(
            tenant_id=self._tenant_id,
            session_id=session_id,
            task_description=task,
            status="PENDING",
        )
        self._db.add(run)
        await self._db.flush()
        await self._db.refresh(run)
        return run

    async def list_runs(self, session_id: str) -> list[Run]:
        stmt = (
            select(Run)
            .where(
                Run.session_id == session_id,
                Run.tenant_id == self._tenant_id,
            )
            .order_by(Run.created_at.desc())
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def get_run(self, session_id: str, run_id: str) -> Run:
        stmt = select(Run).where(
            Run.id == run_id,
            Run.session_id == session_id,
            Run.tenant_id == self._tenant_id,
        )
        result = await self._db.execute(stmt)
        run = result.scalar_one_or_none()
        if run is None:
            raise RunNotFoundError(run_id)
        return run
