"""SQLAlchemy ORM for app_registry schema.

An **App** is a templatized agent task: a stable identity (``apps``) plus one
or more immutable, reviewable harness snapshots (``app_versions``).  The two
tables mirror skill-store's Skill/SkillVersion pattern, with two deliberate
deviations called out in the plan:

* ``app_versions`` carries its own ``tenant_id`` (skill_versions did not) so
  RLS + the cross-app review queue can filter versions directly.
* ``app_versions`` is a review state machine (``draft → pending_review →
  published / rejected``) rather than a plain upload row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Real Postgres stores these as JSONB; SQLite (unit tests) falls back to JSON.
# ``with_variant`` keeps the ORM definition single-sourced — the 0001_init
# migration declares JSONB explicitly for the production schema.
_JSONB = JSON().with_variant(JSONB(), "postgresql")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:26].upper()}"


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class App(Base):
    """Stable App identity — name, ownership, visibility, published pointer."""

    __tablename__ = "apps"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_app_tenant_name"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("app"))
    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    icon: Mapped[str | None] = mapped_column(String(120), nullable=True)
    author_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    visibility: Mapped[str] = mapped_column(String(20), nullable=False, default="private")
    # Author-assigned labels for the App Center shelf (filterable via ?tag=).
    # Stored as JSONB (list[str]); empty array by default.
    tags: Mapped[list[str]] = mapped_column(_JSONB, nullable=False, default=list)
    # Pointer to the currently-published version (set on approve/publish).
    current_version_id: Mapped[str | None] = mapped_column(String(40), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=utcnow, onupdate=utcnow, server_default=func.now(),
    )

    versions: Mapped[list[AppVersion]] = relationship(
        "AppVersion", back_populates="app", cascade="all, delete-orphan",
    )


class AppVersion(Base):
    """Immutable harness snapshot + review status for one App version."""

    __tablename__ = "app_versions"
    __table_args__ = (UniqueConstraint("app_id", "version", name="uq_app_version"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("appv"))
    app_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("apps.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    # Denormalised tenant_id so RLS and the cross-app review queue can filter
    # versions without joining through ``apps``.
    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")

    # ---- harness ---------------------------------------------------------
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    user_prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    reasoning_effort: Mapped[str | None] = mapped_column(String(20), nullable=True)
    sampling_params: Mapped[dict[str, Any]] = mapped_column(_JSONB, nullable=False, default=dict)
    forced_skills: Mapped[list[Any]] = mapped_column(_JSONB, nullable=False, default=list)
    forced_mcp_server_ids: Mapped[list[Any]] = mapped_column(_JSONB, nullable=False, default=list)
    input_schema: Mapped[list[Any]] = mapped_column(_JSONB, nullable=False, default=list)

    # ---- review trail ----------------------------------------------------
    submitted_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now(),
    )

    app: Mapped[App] = relationship("App", back_populates="versions")
