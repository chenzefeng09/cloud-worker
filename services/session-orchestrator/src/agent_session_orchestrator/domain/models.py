"""Domain models — SQLAlchemy ORM for session_orchestrator schema."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:26].upper()}"


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Session(Base):
    __tablename__ = "session"
    __table_args__ = {"schema": "session_orchestrator"}

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=lambda: new_id("ses"))
    tenant_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    current_run_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # Soft reference (no FK, like ``current_run_id``) to the Workspace this
    # session was created against.  NULL for sessions not scoped to a workspace.
    workspace_id: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    # ``metadata`` is reserved on SQLAlchemy's DeclarativeBase (it points at
    # the MetaData registry).  Map our column to a differently-named Python
    # attribute so the ORM stays happy; the SQL column name in
    # ``migrations/versions/0001_init.py`` is unchanged.
    agent_metadata: Mapped[dict[str, object] | None] = mapped_column(
        "metadata", JSONB, nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utcnow, onupdate=utcnow, server_default=func.now(),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Message(Base):
    __tablename__ = "message"
    __table_args__ = {"schema": "session_orchestrator"}

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=lambda: new_id("msg"))
    tenant_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("session_orchestrator.session.id"), nullable=False, index=True,
    )
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_calls: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    attachments: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # Canvas v2: links a message to a materialized canvas document.  Phase A
    # backfills this on the source assistant message; Phase B model replies
    # carry it directly.  NULL for every ordinary (non-canvas) message.
    canvas_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class Run(Base):
    __tablename__ = "run"
    __table_args__ = {"schema": "session_orchestrator"}

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=lambda: new_id("run"))
    tenant_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("session_orchestrator.session.id"), nullable=False, index=True,
    )
    sandbox_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    task_description: Mapped[str] = mapped_column(Text, nullable=False)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Immutable App-run harness snapshot (model/version/inputs/etc.) captured
    # at run creation.  NULL for ordinary chat runs.
    app_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Denormalised ``app_snapshot->>'app_id'`` on a first-class indexed column
    # so an App's execution history is queryable without unpacking the JSONB.
    # NULL for ordinary chat runs.  Set in ``create_run`` from the snapshot;
    # type matches ``app_registry.apps.id`` (``String(40)``, prefixed ids like
    # ``app_<hex>``) so foreign values stay valid.  See migration 0014.
    app_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    # Origin of the run: "user" for interactive chat/app runs, "schedule" for
    # cron-triggered runs dispatched by the cron-runner service.
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="user", server_default="user")
    # Execution mode: "sandbox" (default) spawns an agent worker container via
    # the scheduler; "inline" streams a plain chat completion directly from the
    # LLM gateway in the orchestrator process (chat mode — no sandbox, no
    # scheduler).  Orthogonal to the scheduler's RuntimeKind (docker vs cube).
    execution_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="sandbox", server_default="sandbox"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class Attachment(Base):
    """File/image attached to a session by the user before or during a chat,
    or an artifact produced by an agent run.

    The raw bytes live in S3 under ``blob_key``; this row is the authoritative
    metadata record.  RLS is enabled on this table (same pattern as ``session``).

    ``kind`` distinguishes user-uploaded inputs ('input', default) from
    agent-produced artifacts ('output').  ``run_id`` links output attachments
    back to the run that produced them.
    """

    __tablename__ = "attachment"
    __table_args__ = {"schema": "session_orchestrator"}

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=lambda: new_id("att"))
    tenant_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("session_orchestrator.session.id"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(String(30), nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    mime: Mapped[str] = mapped_column(
        String(200), nullable=False, default="application/octet-stream",
    )
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    blob_key: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(
        String(10), nullable=False, default="input", server_default="input",
    )
    run_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class CanvasDocument(Base):
    """A first-class canvas document (canvas v2 §3.1).

    The canvas is scoped to a session so it rides the session's RLS / retention
    / soft-delete.  ``current_version`` points at the head ``CanvasVersion``;
    the actual block content lives in the version snapshots, never on this row.
    """

    __tablename__ = "canvas_document"
    __table_args__ = {"schema": "session_orchestrator"}

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=lambda: new_id("cvs"))
    tenant_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("session_orchestrator.session.id"), nullable=False, index=True,
    )
    user_id: Mapped[str] = mapped_column(String(30), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    current_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1",
    )
    source_message_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    source_run_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utcnow, onupdate=utcnow, server_default=func.now(),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CanvasVersion(Base):
    """An immutable full-blocks snapshot of a canvas (canvas v2 §3.2).

    Every change — model-suggestion accept, manual save, or revert — appends a
    new row; history is never mutated.  ``blocks`` is the whole document
    (``[{id, type, text}]``), so a revert is a straight copy of an older
    snapshot into a new version.  ``UNIQUE(canvas_id, version)`` is the
    optimistic-lock's authority.
    """

    __tablename__ = "canvas_version"
    __table_args__ = (
        UniqueConstraint("canvas_id", "version", name="uq_canvas_version_canvas_version"),
        {"schema": "session_orchestrator"},
    )

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=lambda: new_id("cvv"))
    tenant_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    canvas_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("session_orchestrator.canvas_document.id"),
        nullable=False, index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    blocks: Mapped[list] = mapped_column(JSONB, nullable=False)
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(10), nullable=False)
    base_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class CanvasSuggestion(Base):
    """A proposed edit awaiting accept/reject (canvas v2 §3.3).

    ``diffs`` (``[{block_id, op, orig, sugg}]``, op ∈ replace/delete/insert_after)
    is applied only on accept, inside one transaction guarded by
    ``base_version`` (optimistic lock).  A suggestion never mutates the canvas
    on its own.
    """

    __tablename__ = "canvas_suggestion"
    __table_args__ = {"schema": "session_orchestrator"}

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=lambda: new_id("csg"))
    tenant_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    canvas_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("session_orchestrator.canvas_document.id"),
        nullable=False, index=True,
    )
    base_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="proposed", server_default="proposed",
    )
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    diffs: Mapped[list] = mapped_column(JSONB, nullable=False)
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Workspace(Base):
    """A user-owned workspace — a reusable bundle of instructions, skills, and
    files that a session can be created against.

    Scoped to a tenant + user with the same RLS discipline as ``session``.  At
    most one workspace per user may be ``is_default`` (enforced by a partial
    unique index in the migration).  Soft-deletes via ``deleted_at``.
    """

    __tablename__ = "workspace"
    __table_args__ = {"schema": "session_orchestrator"}

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=lambda: new_id("wsp"))
    tenant_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    skills: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Sensitivity tier L1..L5 (L1 < L2 < L3 < L4 < L5), server-authoritative.
    # A file's level may not exceed its workspace's level; a caller whose
    # clearance is below the workspace level gets no workspace context at all.
    sensitivity_level: Mapped[str] = mapped_column(
        String(8), nullable=False, default="L2", server_default="L2",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utcnow, onupdate=utcnow, server_default=func.now(),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkspaceFile(Base):
    """A file attached to a :class:`Workspace`.

    The raw bytes live in S3 under ``blob_key``; this row is the authoritative
    metadata record (same pattern as :class:`Attachment`).  RLS is enabled on
    this table.
    """

    __tablename__ = "workspace_file"
    __table_args__ = {"schema": "session_orchestrator"}

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=lambda: new_id("wsf"))
    tenant_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("session_orchestrator.workspace.id"), nullable=False, index=True,
    )
    user_id: Mapped[str] = mapped_column(String(30), nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    mime: Mapped[str] = mapped_column(
        String(200), nullable=False, default="application/octet-stream",
    )
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    blob_key: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Sensitivity tier L1..L5; must be <= the parent workspace's level (enforced
    # in workspace_service.create_file).  Filtered out of run context when above
    # the caller's clearance.
    sensitivity_level: Mapped[str] = mapped_column(
        String(8), nullable=False, default="L2", server_default="L2",
    )
    current_version: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class WorkspaceFileVersion(Base):
    """Historical snapshot of a workspace file's bytes.

    Created when an agent run writes back a modified file — the previous
    blob_key is preserved here before the workspace_file row is updated.
    """

    __tablename__ = "workspace_file_version"
    __table_args__ = {"schema": "session_orchestrator"}

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=lambda: new_id("wfv"))
    tenant_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    workspace_file_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("session_orchestrator.workspace_file.id"), nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    blob_key: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class PlatformConfig(Base):
    """Tenant-scoped key-value store for admin-configurable platform settings."""

    __tablename__ = "platform_config"
    __table_args__ = {"schema": "session_orchestrator"}

    id: Mapped[str] = mapped_column(String(30), primary_key=True, default=lambda: new_id("pcf"))
    tenant_id: Mapped[str] = mapped_column(String(30), nullable=False)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_by: Mapped[str | None] = mapped_column(String(60), nullable=True)
