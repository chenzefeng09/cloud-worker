"""SQLAlchemy ORM for knowledge_ingest schema."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from knowledge_ingest.domain.types import VectorColumn

EMBEDDING_DIM = 1536


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:26].upper()}"


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Folder(Base):
    """Tenant-scoped folder for organising knowledge documents.

    A folder tree is a plain ``parent_id`` self-reference (NULL == root layer);
    the dataset is small so paths are resolved in the application layer rather
    than with ``ltree``.  Same-parent name collisions are rejected (409) and a
    folder can only be deleted when it holds no sub-folders and no documents.
    """

    __tablename__ = "folders"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("fld"))
    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    parent_id: Mapped[str | None] = mapped_column(
        String(40),
        ForeignKey("folders.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    # New documents dropped in the folder inherit this when no explicit
    # sensitivity is given at upload time (L1 .. L5).
    default_sensitivity: Mapped[str] = mapped_column(
        String(8), nullable=False, default="L1", server_default="L1",
    )
    created_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("doc"))
    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    source_uri: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # Folder this document lives in (NULL == root).  A folder can only be
    # deleted when empty, so this FK never cascades in practice; SET NULL is a
    # defensive fallback.
    folder_id: Mapped[str | None] = mapped_column(
        String(40),
        ForeignKey("folders.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Data sensitivity (L1 .. L5).  The *visibility* gate: search / list only
    # surface a document to a caller whose clearance is >= this.  Ordinary
    # uploads default to L1 (or the folder default); memory docs carry L1 and
    # are gated by owner, not clearance.
    sensitivity: Mapped[str] = mapped_column(
        String(8), nullable=False, default="L1", server_default="L1",
    )
    # The user that uploaded this document.  Reserved: v1 keeps resource-library
    # docs tenant-shared, but recording the uploader lets a future "personal
    # knowledge source" flip visibility on without a schema change.
    uploaded_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Session that produced this document, when it was written as a run-completion
    # memory doc by the session-orchestrator. NULL for ordinary uploads. Search
    # can exclude same-session memory docs via ``exclude_origin_session_id`` so a
    # session's own prior Q&A isn't re-fed as RAG on top of the restored history.
    origin_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # User that owns this memory document — the originating session's user, set
    # when the session-orchestrator writes a run-completion memory doc. NULL for
    # ordinary uploads (resource-library docs stay tenant-shared). Search returns
    # a doc only when it is an ordinary upload (``origin_session_id`` IS NULL) OR
    # ``origin_user_id`` equals the requesting user, so one user can never
    # retrieve another user's session memory.
    origin_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    mime: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now(),
    )

    chunks: Mapped[list[Chunk]] = relationship(
        "Chunk", back_populates="document", cascade="all, delete-orphan",
    )


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("chk"))
    document_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    ord: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(VectorColumn(EMBEDDING_DIM), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now(),
    )

    document: Mapped[Document] = relationship("Document", back_populates="chunks")
