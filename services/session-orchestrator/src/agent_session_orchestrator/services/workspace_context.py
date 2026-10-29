"""Workspace context loader — resolves a session's workspace into run-injectable
context (instructions + base files + skills).

Called from ``create_run``, which folds the context into the scheduler payload
(files[] / skills union / sys_prompt append).  A session with ``workspace_id IS NULL`` — or whose workspace has been
soft-deleted since — yields ``None`` and the run proceeds exactly as before
the feature existed.

The loader runs inside the caller's request transaction (it receives the
request-scoped ``db``), so no manual ``async for db in get_session()`` loop —
and therefore no GeneratorExit/rollback footgun — is involved here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.domain.models import (
    Session,
    Workspace,
    WorkspaceFile,
)
from agent_session_orchestrator.services.clearance import clearance_rank, level_allows

logger = logging.getLogger(__name__)


@dataclass
class WorkspaceContext:
    """Run-injectable workspace context.

    ``files`` holds the ORM rows; callers that cross the request-transaction
    boundary (background tasks) must copy the plain values out first.
    """

    instructions: str | None = None
    files: list[WorkspaceFile] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)


async def load_workspace_context(
    db: AsyncSession, session: Session, *, user_clearance: str
) -> WorkspaceContext | None:
    """Resolve ``session.workspace_id`` into a :class:`WorkspaceContext`.

    Returns ``None`` when the session has no workspace, the workspace has been
    soft-deleted, OR the caller's clearance is below the workspace's sensitivity
    level (fail-closed: the whole context is skipped rather than partially
    leaked).  Files above the caller's clearance are filtered out individually;
    a workspace whose level the caller may see but whose every file is above
    their clearance yields an empty-files context (``[]``), not ``None``.
    """
    if not session.workspace_id:
        return None

    ws_result = await db.execute(
        select(Workspace).where(
            Workspace.id == session.workspace_id,
            Workspace.tenant_id == session.tenant_id,
            Workspace.deleted_at.is_(None),
        )
    )
    workspace = ws_result.scalar_one_or_none()
    if workspace is None:
        return None

    # Fail-closed: a workspace above the caller's clearance contributes nothing.
    if clearance_rank(user_clearance) < clearance_rank(workspace.sensitivity_level):
        return None

    files_result = await db.execute(
        select(WorkspaceFile)
        .where(
            WorkspaceFile.workspace_id == workspace.id,
            WorkspaceFile.tenant_id == session.tenant_id,
        )
        .order_by(WorkspaceFile.created_at.asc())
    )
    files = [
        f
        for f in files_result.scalars().all()
        if level_allows(user_clearance, f.sensitivity_level)
    ]

    instructions = (workspace.instructions or "").strip() or None
    skills = [str(s) for s in (workspace.skills or []) if str(s).strip()]
    return WorkspaceContext(instructions=instructions, files=files, skills=skills)


async def resolve_workspace_sensitivity(
    db: AsyncSession, session: Session
) -> str | None:
    """Return the session's live workspace sensitivity level (``L1``..``L5``).

    ``None`` when the session is not workspace-bound OR the workspace has been
    soft-deleted.  Used by ``create_run`` to compute the run's *effective
    clearance* (``min(user_clearance, workspace_sensitivity)``) BEFORE the
    knowledge/memory prefetch — a lightweight PK lookup that is NOT gated on the
    ``workspace_context_injection`` feature flag (clearance is a security control,
    not opportunistic context).  Runs inside the caller's request transaction, so
    no ``async for get_session`` loop / GeneratorExit footgun is involved.
    """
    if not session.workspace_id:
        return None
    result = await db.execute(
        select(Workspace.sensitivity_level).where(
            Workspace.id == session.workspace_id,
            Workspace.tenant_id == session.tenant_id,
            Workspace.deleted_at.is_(None),
        )
    )
    level = result.scalar_one_or_none()
    return str(level) if level else None


async def workspace_context_injection_enabled(tenant_id: str) -> bool:
    """Whether the ``workspace_context_injection`` flag is on for ``tenant_id``.

    Reads the foundation feature-flag source of truth via the shared client and
    fail-OPENs to enabled.  Workspace context is opportunistic — a model that
    ignores the injected blocks costs nothing — so a flag-service outage should
    not silently strip workspace context from runs.  A tenant that has
    explicitly turned the flag OFF is still respected (the client returns
    ``False`` for a present-but-off flag).
    """
    try:
        from feature_flag_client import get_ff_client  # noqa: PLC0415

        return await get_ff_client().is_enabled(
            "workspace_context_injection", tenant_id, default=True
        )
    except RuntimeError:
        # Client not initialised (e.g. a unit context) — default ON.
        return True
    except Exception:  # noqa: BLE001
        logger.debug(
            "workspace context flag check failed; defaulting injection ON",
            extra={"tenant_id": tenant_id},
        )
        return True
