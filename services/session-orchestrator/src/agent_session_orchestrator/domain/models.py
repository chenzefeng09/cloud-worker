"""Domain models — SQLAlchemy ORM for session_orchestrator schema."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, func
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
