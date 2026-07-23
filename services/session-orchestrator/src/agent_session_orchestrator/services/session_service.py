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

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.config import Settings
from agent_session_orchestrator.domain.db import get_session, privileged_scalars
from agent_session_orchestrator.domain.models import (
    Attachment,
    Message,
    Run,
    Session,
    Workspace,
)
from agent_session_orchestrator.errors import (
    OrchestratorError,
    RunNotFoundError,
    SessionNotFoundError,
)

logger = logging.getLogger(__name__)


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


async def purge_expired_sessions(settings: Settings, retention_days: int) -> int:
    """Soft-delete every session older than ``retention_days`` (all tenants).

    Designed for the background retention sweep — there is no request and
    therefore no single tenant context.  RLS is FORCEd on ``app_user``
    (see ``0001_init``), so the cross-tenant *scan* runs on the migrator
    (BYPASSRLS) connection via :func:`privileged_scalars`.  The actual
    soft-deletes then go through the normal tenant-bound ``app_user`` path
    (:class:`SessionService.delete_session`) so every mutation is still RLS
    guarded and reuses the cascade-friendly delete logic.

    Conservative by design: each session is deleted in its own transaction;
    a failure on one session is logged and the loop continues.

    Returns the number of sessions successfully soft-deleted.
    """
    cutoff = _now() - dt.timedelta(days=retention_days)

    # Cross-tenant read of expired, not-yet-deleted sessions.  We pull
    # (tenant_id, id) so we can re-bind RLS correctly for each delete.  The
    # scalar helper returns one column, so encode the pair as "tenant_id\x1fid"
    # — tenant_id and session id are opaque slugs without control chars.
    rows = await privileged_scalars(
        settings,
        "SELECT tenant_id || '\x1f' || id"
        " FROM session_orchestrator.session"
        " WHERE deleted_at IS NULL AND created_at < :cutoff"
        " ORDER BY tenant_id",
        {"cutoff": cutoff},
    )

    scanned = len(rows)
    purged = 0
    for row in rows:
        tenant_id, _, session_id = str(row).partition("\x1f")
        if not tenant_id or not session_id:
            logger.warning("retention sweep: malformed scan row %r — skipping", row)
            continue
        try:
            async for db in get_session(
                tenant_id=tenant_id, user_id="SYSTEM", actor="SYSTEM"
            ):
                service = SessionService(db, tenant_id=tenant_id)
                await service.delete_session(session_id)
            purged += 1
        except Exception:  # noqa: BLE001 — one bad session must not abort the sweep
            logger.exception(
                "retention sweep: failed to purge session",
                extra={"tenant_id": tenant_id, "session_id": session_id},
            )

    logger.info(
        "retention sweep: scanned %d expired session(s), purged %d",
        scanned,
        purged,
    )
    return purged


class SessionService:
    def __init__(self, db: AsyncSession, *, tenant_id: str) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def list_sessions(
        self, *, user_id: str | None = None, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[Session], str | None]:
        """List sessions for the current tenant.

        ``user_id`` narrows the result to a single owner.  This is the IDOR
        guard for the normal chat path: a regular member must only ever see
        their own sessions.  Tenant RLS already scopes to ``tenant_id``; the
        ``user_id`` predicate is a strictly tighter filter on top of that
        (never broadens scope, never crosses tenants).

        ``user_id=None`` lists *every* session in the tenant — reserved for the
        admin replay path, which gates on a tenant_admin permission upstream
        (BFF) and never crosses the tenant boundary.
        """
        stmt = (
            select(Session)
            .where(
                Session.tenant_id == self._tenant_id,
                Session.deleted_at.is_(None),
            )
            .order_by(Session.updated_at.desc())
            .limit(limit + 1)
        )
        if user_id is not None:
            stmt = stmt.where(Session.user_id == user_id)
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
        workspace_id: str | None = None,
    ) -> Session:
        # A session may be scoped to a workspace the caller owns.  Validate the
        # reference before wiring it up: the workspace must exist in this tenant,
        # belong to this user, and not be soft-deleted.
        if workspace_id is not None:
            ws_result = await self._db.execute(
                select(Workspace.id).where(
                    Workspace.id == workspace_id,
                    Workspace.tenant_id == self._tenant_id,
                    Workspace.user_id == user_id,
                    Workspace.deleted_at.is_(None),
                )
            )
            if ws_result.scalar_one_or_none() is None:
                raise OrchestratorError(
                    "workspace not found or not owned",
                    code="AGENT.SESSION.WORKSPACE_INVALID",
                    status=422,
                    user_message="所选工作区不存在或无权访问。",
                )

        session = Session(
            tenant_id=self._tenant_id,
            user_id=user_id,
            title=title,
            agent_metadata=metadata or {},  # SQL column is still ``metadata``
            workspace_id=workspace_id,
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
        now = _now()
        session.deleted_at = now
        # Canvas v2 retention cascade (design §3.5): soft-delete the session's
        # canvas documents in the same transaction so no canvas outlives its
        # session (avoids a second "soft-deleted orphan" leak class).  Versions
        # and suggestions are pruned with the document on physical purge; the
        # soft-delete only needs to flip the parent's deleted_at.
        from agent_session_orchestrator.domain.models import CanvasDocument

        await self._db.execute(
            update(CanvasDocument)
            .where(
                CanvasDocument.session_id == session_id,
                CanvasDocument.tenant_id == self._tenant_id,
                CanvasDocument.deleted_at.is_(None),
            )
            .values(deleted_at=now)
        )
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

    async def create_run(
        self,
        session_id: str,
        task: str,
        *,
        app_snapshot: dict | None = None,
        source: str = "user",
        execution_mode: str = "sandbox",
    ) -> Run:
        await self.get_session(session_id)
        # Pull ``app_id`` out of the snapshot onto a first-class indexed column
        # so the run history of an App is queryable without unpacking the JSONB
        # (migration 0014).  NULL for ordinary chat runs (no snapshot).  The
        # value is a string id (``app_<hex>``); guard against a malformed
        # non-string snapshot value so a bad payload never 500s the run.
        snap_app_id = app_snapshot.get("app_id") if app_snapshot else None
        app_id: str | None = snap_app_id if isinstance(snap_app_id, str) else None
        run = Run(
            tenant_id=self._tenant_id,
            session_id=session_id,
            task_description=task,
            status="PENDING",
            app_snapshot=app_snapshot,
            app_id=app_id,
            source=source,
            execution_mode=execution_mode,
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

    async def list_attachments_for_user(
        self, user_id: str, *, kind: str | None = "output"
    ) -> list[Attachment]:
        """Return every attachment owned by ``user_id`` in this tenant.

        Cross-session listing for the Artifacts panel.  ``kind`` narrows to
        ``'input'`` / ``'output'`` when given; the Artifacts panel only ever
        asks for ``'output'`` (agent-produced run outputs) but the parameter is
        kept general so an inputs view can reuse the same path.  Tenant RLS
        scopes to ``tenant_id``; the ``user_id`` predicate is a strictly tighter
        filter on top (never crosses tenants).  Ordered newest-first.
        """
        stmt = (
            select(Attachment)
            .where(
                Attachment.tenant_id == self._tenant_id,
                Attachment.user_id == user_id,
            )
            .order_by(Attachment.created_at.desc())
        )
        if kind is not None:
            stmt = stmt.where(Attachment.kind == kind)
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def delete_attachment(
        self,
        session_id: str,
        attachment_id: str,
        *,
        blob_store=None,
    ) -> Attachment:
        """Delete an attachment row and its S3 blob (hard delete).

        Ownership is enforced by ``get_attachment`` (raises NOT_FOUND for a
        missing / cross-tenant / soft-deleted-session attachment).  The S3 blob
        is removed best-effort before the row delete — a missing blob is not a
        blocker (it may already have been purged by retention), and the row is
        the authoritative metadata record.  ``blob_store`` is the
        ``app.state.blob_store`` passed in by the endpoint; when ``None`` the
        blob step is skipped (the row still deletes).
        """
        att = await self.get_attachment(session_id, attachment_id)
        if blob_store is not None and att.blob_key:
            # Mirror attachments.py:222 — synchronous boto3 call off-loaded to
            # a thread.  Best-effort: a blob delete failure is logged but does
            # not abort the row delete (an orphaned blob only costs storage).
            import asyncio

            try:
                await asyncio.to_thread(blob_store.delete, att.blob_key)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "attachment blob delete failed (best-effort)",
                    extra={"attachment_id": att.id, "key": att.blob_key, "err": str(exc)},
                )
        await self._db.execute(
            delete(Attachment).where(
                Attachment.id == att.id,
                Attachment.tenant_id == self._tenant_id,
            )
        )
        await self._db.flush()
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

    async def list_tenant_runs(
        self,
        status: str | None = None,
        limit: int = 50,
        app_id: str | None = None,
        offset: int = 0,
    ) -> list[Run]:
        """List all runs for the current tenant (across every session).

        Tenant-scoped via ``self._tenant_id`` (sourced from the X-Tenant-Id
        middleware, same as every other method here).  ``status`` filters on an
        exact match when provided; callers normalise casing.  ``app_id`` narrows
        to a single App's execution history when provided - NULL ``app_id``
        rows (ordinary chat runs) never match an app filter, so the App history
        view is naturally free of chat noise.  Both ``app_id`` and ``status``
        default to ``None`` (no filter) so existing callers are unchanged.
        Ordered newest first, paged by ``limit`` + ``offset``.
        """
        stmt = select(Run).where(Run.tenant_id == self._tenant_id)
        if status is not None:
            stmt = stmt.where(Run.status == status)
        if app_id is not None:
            stmt = stmt.where(Run.app_id == app_id)
        stmt = stmt.order_by(Run.created_at.desc()).limit(limit)
        if offset:
            stmt = stmt.offset(offset)
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
