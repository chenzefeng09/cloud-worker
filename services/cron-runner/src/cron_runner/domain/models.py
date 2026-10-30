"""SQLAlchemy ORM for cron_runner schema."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Index, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id(prefix: str) -> str:
    """Generate a prefixed ID — e.g. ``cron_`` + 26-char upper-hex UUID fragment."""
    return f"{prefix}_{uuid.uuid4().hex[:26].upper()}"


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class CronJob(Base):
    __tablename__ = "cron_jobs"
    __table_args__ = (
        # A user's triggers, for their data export / erasure.
        Index("ix_cron_jobs_tenant_created_by", "tenant_id", "created_by_user_id"),
    )

    id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_id("cron")
    )
    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    app_id: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    # Trigger kind: "schedule" (cron-fired, the original behavior) or "webhook"
    # (fired by an external HTTP call in a later phase — never scheduled here).
    trigger_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="schedule", server_default="schedule"
    )
    # Nullable now: webhook triggers have no cron expression.
    cron_expression: Mapped[str | None] = mapped_column(String(120), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")

    # Fixed non-file App inputs: {"field_key": value, ...}
    input_values: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )

    # Webhook only: maps incoming payload field keys → App input_schema field
    # keys. Stored/returned as opaque JSON in this phase; no interpretation.
    field_mapping: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # SHA-256 hex digest of the per-webhook secret. The plaintext is returned
    # to the caller exactly once at create/rotate time and never persisted.
    webhook_secret_hash: Mapped[str | None] = mapped_column(String, nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="USER")
    # The user who created the trigger (``X-User-Id`` at create time; NULL for
    # system-created rows).  Runs fire as the cron service account, so this is
    # what ties a trigger, and the runs it starts, to a person.  PATCH never
    # changes it.
    created_by_user_id: Mapped[str | None] = mapped_column(String(40), nullable=True)

    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # ok | error | skipped
    last_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now(),
        onupdate=utcnow,
    )

    @property
    def has_webhook_secret(self) -> bool:
        """True if a webhook secret hash is stored (never exposes the value)."""
        return bool(self.webhook_secret_hash)
