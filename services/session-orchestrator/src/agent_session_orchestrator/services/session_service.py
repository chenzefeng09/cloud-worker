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
import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.domain.models import Attachment, Message, Run, Session
from agent_session_orchestrator.errors import RunNotFoundError, SessionNotFoundError

logger = logging.getLogger(__name__)


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
        self, *, user_id: str, title: str | None = None,
        metadata: dict[str, object] | None = None,
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
        tool_calls: dict[str, object] | None = None,
        run_id: str | None = None,
        attachment_ids: list[str] | None = None,
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

        # Resolve attachment metadata for the JSON column (failure-open).
        attachments_meta: list[dict] | None = None
        if attachment_ids:
            try:
                stmt = select(Attachment).where(
                    Attachment.id.in_(attachment_ids),
                    Attachment.session_id == session_id,
                    Attachment.tenant_id == self._tenant_id,
                    Attachment.kind == "input",
                )
                att_result = await self._db.execute(stmt)
                att_rows = list(att_result.scalars().all())
                attachments_meta = [
                    {
                        "id": a.id,
                        "filename": a.filename,
                        "mime": a.mime,
                        "size": a.size_bytes,
                    }
                    for a in att_rows
                ]
            except Exception:  # noqa: BLE001 — failure-open; don't block message creation
                # Log instead of silently swallowing — a swallowed error here
                # previously hid attachments being dropped from messages.
                logger.exception(
                    "attachment metadata resolution failed",
                    extra={"attachment_ids": attachment_ids, "session_id": session_id},
                )
                attachments_meta = None

        msg = Message(
            tenant_id=self._tenant_id,
            session_id=session_id,
            role=role,
            content=content,
            tool_calls=tool_calls,
            attachments=attachments_meta,
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

    async def list_all_messages(self, session_id: str) -> list[Message]:
        """Return every message in the session, oldest first (no paging).

        Used to assemble multi-turn conversation history for a new run.
        """
        await self.get_session(session_id)
        stmt = (
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.sequence.asc())
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

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

    # ------------------------------------------------------------------ attachments

    async def create_attachment(
        self,
        session_id: str,
        *,
        user_id: str,
        filename: str,
        mime: str,
        size_bytes: int,
        blob_key: str,
        kind: str = "input",
        run_id: str | None = None,
    ) -> Attachment:
        await self.get_session(session_id)
        att = Attachment(
            tenant_id=self._tenant_id,
            session_id=session_id,
            user_id=user_id,
            filename=filename,
            mime=mime,
            size_bytes=size_bytes,
            blob_key=blob_key,
            kind=kind,
            run_id=run_id,
        )
        self._db.add(att)
        await self._db.flush()
        await self._db.refresh(att)
        return att

    async def list_attachments(
        self, session_id: str, *, kind: str | None = None,
    ) -> list[Attachment]:
        await self.get_session(session_id)
        stmt = (
            select(Attachment)
            .where(
                Attachment.session_id == session_id,
                Attachment.tenant_id == self._tenant_id,
            )
            .order_by(Attachment.created_at.asc())
        )
        if kind is not None:
            stmt = stmt.where(Attachment.kind == kind)
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def get_attachment(self, session_id: str, attachment_id: str) -> Attachment:
        await self.get_session(session_id)
        stmt = select(Attachment).where(
            Attachment.id == attachment_id,
            Attachment.session_id == session_id,
            Attachment.tenant_id == self._tenant_id,
        )
        result = await self._db.execute(stmt)
        att = result.scalar_one_or_none()
        if att is None:
            from agent_session_orchestrator.errors import SessionNotFoundError
            raise SessionNotFoundError(attachment_id)
        return att

    # ------------------------------------------------------------------ runs

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

    async def delete_messages_from_sequence(self, session_id: str, from_sequence: int) -> int:
        """Delete all messages with sequence >= from_sequence for this session.

        Also deletes output Attachment rows whose run_id matches any deleted
        message's run_id (orphaned output attachments from truncated runs).

        Returns the number of message rows deleted.
        """
        # Validate the session exists first (reuse get_session pattern).
        await self.get_session(session_id)

        # Collect run_ids from messages that will be deleted, for attachment cleanup.
        select_stmt = select(Message.run_id).where(
            Message.session_id == session_id,
            Message.sequence >= from_sequence,
            Message.run_id.is_not(None),
        )
        run_ids_result = await self._db.execute(select_stmt)
        run_ids = {row[0] for row in run_ids_result.fetchall() if row[0]}

        # Delete the messages.
        del_stmt = delete(Message).where(
            Message.session_id == session_id,
            Message.sequence >= from_sequence,
        )
        result = await self._db.execute(del_stmt)
        deleted_count = result.rowcount

        # Clean up orphaned output attachments for the deleted runs.
        if run_ids:
            try:
                att_del_stmt = delete(Attachment).where(
                    Attachment.session_id == session_id,
                    Attachment.tenant_id == self._tenant_id,
                    Attachment.kind == "output",
                    Attachment.run_id.in_(run_ids),
                )
                att_result = await self._db.execute(att_del_stmt)
                logger.debug(
                    "truncate: deleted %d output attachment(s) for %d run(s)",
                    att_result.rowcount,
                    len(run_ids),
                )
            except Exception as exc:  # noqa: BLE001 — failure-open; don't block truncation
                logger.warning(
                    "truncate: attachment cleanup failed (failure-open): %s", exc
                )

        await self._db.flush()
        return deleted_count

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
