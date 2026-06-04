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


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("doc"))
    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    source_uri: Mapped[str | None] = mapped_column(String(1000), nullable=True)
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
