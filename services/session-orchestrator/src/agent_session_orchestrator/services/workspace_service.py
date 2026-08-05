"""Workspace service — business logic for workspace + workspace-file lifecycle.

Transaction discipline mirrors :class:`SessionService`: the service NEVER
calls ``commit()``.  All writes happen inside the transaction opened by
``domain.db.get_session`` (which installed the ``SET LOCAL app.tenant_id``
that makes RLS work); ``flush()`` is used when database-generated columns are
needed before the transaction ends.

Owner scoping
-------------
Every read/write takes an explicit ``user_id`` filter.  ``user_id=None``
widens to the whole tenant — reserved for the admin path (``X-Actor: ADMIN``),
mirroring the sessions endpoints' owner-filter convention.  Tenant RLS already
scopes rows to the tenant; the ``user_id`` predicate is a strictly tighter
filter on top of that.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.domain.models import Workspace, WorkspaceFile
from agent_session_orchestrator.errors import (
    OrchestratorError,
    WorkspaceDefaultDeleteError,
    WorkspaceFileNotFoundError,
    WorkspaceNotFoundError,
)
from agent_session_orchestrator.services.clearance import clearance_rank

logger = logging.getLogger(__name__)

# Fields a PATCH may touch.  ``is_default`` is deliberately absent — the
# default workspace is system-managed (lazy-created) and not user-flippable.
_UPDATABLE_FIELDS = {"name", "description", "instructions", "skills", "meta", "sensitivity_level"}

# Lazy-created default workspace presentation (glyph/accent consumed by the UI).
_DEFAULT_WORKSPACE_NAME = "工作区"
_DEFAULT_WORKSPACE_META = {"glyph": "◇", "accent": "#d23a1f"}


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


class WorkspaceService:
    def __init__(self, db: AsyncSession, *, tenant_id: str) -> None:
        self._db = db
        self._tenant_id = tenant_id

    # ------------------------------------------------------------- workspaces

    async def list_workspaces(self, *, user_id: str | None) -> list[Workspace]:
        stmt = (
            select(Workspace)
            .where(
                Workspace.tenant_id == self._tenant_id,
                Workspace.deleted_at.is_(None),
            )
            .order_by(Workspace.created_at.asc())
        )
        if user_id is not None:
            stmt = stmt.where(Workspace.user_id == user_id)
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def get_workspace(
        self, workspace_id: str, *, user_id: str | None
    ) -> Workspace:
        stmt = select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.tenant_id == self._tenant_id,
            Workspace.deleted_at.is_(None),
        )
        if user_id is not None:
            stmt = stmt.where(Workspace.user_id == user_id)
        result = await self._db.execute(stmt)
        workspace = result.scalar_one_or_none()
        if workspace is None:
            raise WorkspaceNotFoundError(workspace_id)
        return workspace

    async def create_workspace(
        self,
        *,
        user_id: str,
        name: str,
        description: str | None = None,
        instructions: str | None = None,
        skills: list[str] | None = None,
        meta: dict | None = None,
        is_default: bool = False,
        sensitivity_level: str = "L2",
    ) -> Workspace:
        workspace = Workspace(
            tenant_id=self._tenant_id,
            user_id=user_id,
            name=name,
            description=description,
            instructions=instructions,
            skills=list(skills or []),
            meta=dict(meta or {}),
            is_default=is_default,
            sensitivity_level=sensitivity_level,
        )
        self._db.add(workspace)
        await self._db.flush()
        await self._db.refresh(workspace)
        return workspace

    async def update_workspace(
        self, workspace_id: str, *, user_id: str | None, updates: dict
    ) -> Workspace:
        workspace = await self.get_workspace(workspace_id, user_id=user_id)
        if "sensitivity_level" in updates:
            new_level = updates["sensitivity_level"]
            # Defence-in-depth: the schema already rejects levels outside
            # {L1..L5}, but a caller hitting the service directly (or a future
            # internal path) must not be able to persist a bogus tier.
            try:
                new_rank = clearance_rank(new_level)
            except ValueError as exc:
                raise OrchestratorError(
                    f"invalid sensitivity_level {new_level!r}",
                    code="AGENT.WORKSPACE.INVALID_LEVEL",
                    status=422,
                    user_message="密级级别无效，必须是 L1 到 L5 之一。",
                ) from exc
            # LOWERING the workspace level below an existing file's level would
            # violate file_level <= workspace_level.  Raise 409 (conflict) so the
            # caller can either delete the offending files or pick a higher tier.
            if new_rank < clearance_rank(workspace.sensitivity_level):
                files_result = await self._db.execute(
                    select(WorkspaceFile).where(
                        WorkspaceFile.workspace_id == workspace_id,
                        WorkspaceFile.tenant_id == self._tenant_id,
                    )
                )
                for f in files_result.scalars().all():
                    if clearance_rank(f.sensitivity_level) > new_rank:
                        raise OrchestratorError(
                            "workspace sensitivity_level lowered below existing file level",
                            code="AGENT.WORKSPACE.LEVEL_LOWER_THAN_FILES",
                            status=409,
                            user_message="工作区密级不能低于已有文件密级。",
                        )
        for key, value in updates.items():
            if key in _UPDATABLE_FIELDS:
                setattr(workspace, key, value)
        await self._db.flush()
        return workspace

    async def soft_delete_workspace(
        self, workspace_id: str, *, user_id: str | None
    ) -> None:
        workspace = await self.get_workspace(workspace_id, user_id=user_id)
        if workspace.is_default:
            raise WorkspaceDefaultDeleteError(workspace_id)
        workspace.deleted_at = _now()
        await self._db.flush()

    async def ensure_default_workspace(self, *, user_id: str) -> Workspace:
        """Create the lazy default workspace for ``user_id``.

        Callers are responsible for the concurrency story: the partial unique
        index ``uq_workspace_default_per_user`` (0011) rejects a second live
        default per (tenant, user); the endpoint catches that IntegrityError in
        a separate short transaction and re-queries.
        """
        return await self.create_workspace(
            user_id=user_id,
            name=_DEFAULT_WORKSPACE_NAME,
            meta=dict(_DEFAULT_WORKSPACE_META),
            is_default=True,
        )

    # ------------------------------------------------------------------ files

    async def count_files(self, workspace_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(WorkspaceFile)
            .where(
                WorkspaceFile.workspace_id == workspace_id,
                WorkspaceFile.tenant_id == self._tenant_id,
            )
        )
        result = await self._db.execute(stmt)
        return int(result.scalar_one() or 0)

    async def create_file(
        self,
        *,
        workspace_id: str,
        owner: str | None,
        user_id: str,
        filename: str,
        mime: str,
        size_bytes: int,
        blob_key: str,
        description: str | None = None,
        sensitivity_level: str = "L2",
    ) -> WorkspaceFile:
        # Ownership check first — a file may only be attached to a workspace
        # the caller owns.  ``owner`` is the endpoint's owner filter (None for
        # the admin bypass); ``user_id`` is the real uploader recorded on the row.
        workspace = await self.get_workspace(workspace_id, user_id=owner)
        # Invariant: file_level <= workspace_level.  A file above its workspace's
        # tier would let a low-clearance workspace smuggle high-clearance bytes.
        if clearance_rank(sensitivity_level) > clearance_rank(workspace.sensitivity_level):
            raise OrchestratorError(
                "file sensitivity_level exceeds workspace sensitivity_level",
                code="AGENT.WORKSPACE.FILE_LEVEL_EXCEEDS_WORKSPACE",
                status=422,
                user_message="文件密级不能高于工作区密级。",
            )
        wsf = WorkspaceFile(
            tenant_id=self._tenant_id,
            workspace_id=workspace_id,
            user_id=user_id,
            filename=filename,
            mime=mime,
            size_bytes=size_bytes,
            blob_key=blob_key,
            description=description,
            sensitivity_level=sensitivity_level,
        )
        self._db.add(wsf)
        await self._db.flush()
        await self._db.refresh(wsf)
        return wsf

    async def list_files(
        self, workspace_id: str, *, user_id: str | None
    ) -> list[WorkspaceFile]:
        await self.get_workspace(workspace_id, user_id=user_id)
        stmt = (
            select(WorkspaceFile)
            .where(
                WorkspaceFile.workspace_id == workspace_id,
                WorkspaceFile.tenant_id == self._tenant_id,
            )
            .order_by(WorkspaceFile.created_at.asc())
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def get_file(
        self, workspace_id: str, file_id: str, *, user_id: str | None
    ) -> WorkspaceFile:
        await self.get_workspace(workspace_id, user_id=user_id)
        stmt = select(WorkspaceFile).where(
            WorkspaceFile.id == file_id,
            WorkspaceFile.workspace_id == workspace_id,
            WorkspaceFile.tenant_id == self._tenant_id,
        )
        result = await self._db.execute(stmt)
        wsf = result.scalar_one_or_none()
        if wsf is None:
            raise WorkspaceFileNotFoundError(file_id)
        return wsf

    async def delete_file(
        self, workspace_id: str, file_id: str, *, user_id: str | None
    ) -> WorkspaceFile:
        """Hard-delete the row; return it so the caller can best-effort delete
        the blob (``blob_key`` stays readable on the detached instance)."""
        wsf = await self.get_file(workspace_id, file_id, user_id=user_id)
        await self._db.delete(wsf)
        await self._db.flush()
        return wsf
