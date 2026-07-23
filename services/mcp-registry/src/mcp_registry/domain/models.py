"""SQLAlchemy ORM for mcp_registry schema.

Postgres types
--------------
We use ``JSON`` (not ``JSONB``) and ``JSON``-encoded text arrays so the same
ORM works against the SQLite-backed unit-test database.  The Alembic
migration upgrades the storage to ``JSONB`` / ``TEXT[]`` for real Postgres
deployments - both column shapes are read/written transparently by SQLAlchemy
because we never lean on JSONB-only operators in service code.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ``args`` is stored as ``TEXT[]`` on Postgres (see Alembic 0002) but as a
# JSON-encoded list on SQLite (unit tests).
# ``with_variant`` keeps the ORM dialect-agnostic *and* binds correctly on each
# backend - a plain ``JSON`` mapping cannot bind a Python list to a Postgres
# ``text[]`` column.
_TEXT_ARRAY_TYPE = JSON().with_variant(PG_ARRAY(Text), "postgresql")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:26].upper()}"


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class McpServer(Base):
    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_mcp_tenant_name"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("mcp"))
    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    transport: Mapped[str] = mapped_column(String(20), nullable=False)
    endpoint: Mapped[str | None] = mapped_column(String(500), nullable=True)
    command: Mapped[str | None] = mapped_column(String(500), nullable=True)
    env: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Extended config (migration 0002).  ``headers`` / ``auth`` / ``tool_filter``
    # are JSONB on Postgres, JSON on SQLite; ``args`` is TEXT[] on Postgres,
    # JSON on SQLite.  ``auth`` is NULL when no auth is configured.
    headers: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    auth: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True, default=None)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    tool_filter: Mapped[dict[str, list[str]]] = mapped_column(JSON, nullable=False, default=dict)
    args: Mapped[list[str]] = mapped_column(_TEXT_ARRAY_TYPE, nullable=False, default=list)

    # Discovered tool list (migration 0004).  Persisted by the ``/test``
    # endpoint on a successful handshake so the admin console can render the
    # upstream's tool catalog from the DB without re-probing on every page
    # load.  Each item is ``{"name": str, "description": str | None}`` - the
    # shape of ``McpTestTool``.  JSONB on Postgres, JSON on SQLite.
    discovered_tools: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )
