"""SQLAlchemy ORM for agent_bff schema.

Tables owned by this module:
- ``notification_preferences`` — per-user, per-event notification config.
- ``api_keys``                 — user-managed API keys (hash-only; never plaintext).

The schema follows the same dialect-agnostic pattern as mcp-registry: JSON
types for SQLite test compatibility, Postgres-specific types only in Alembic
migrations.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:26].upper()}"


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class NotificationPreference(Base):
    """Per-user, per-event notification preference row.

    Each event key gets exactly one row per user (UNIQUE on user_id + event_key).
    Missing rows are treated as defaults (email=True, push=True) by the service
    layer — callers never see a 404 for preferences.
    """

    __tablename__ = "notification_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "event_key", name="uq_notif_user_event"),
    )

    id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_id("np")
    )
    # tenant_id stored for future RLS / bulk tenant admin; not currently used for
    # filtering because preferences are already scoped by user_id.
    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # Canonical event key, e.g. "task_completed", "needs_review", "budget_80pct"
    event_key: Mapped[str] = mapped_column(String(80), nullable=False)
    email_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    push_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

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


# ─────────────────────────────────────────────────────────────────────────────
# API Keys
# ─────────────────────────────────────────────────────────────────────────────

class ApiKey(Base):
    """User-managed API key — DB only stores the SHA-256 hash, never plaintext.

    Lifecycle:
    - ``revoked_at IS NULL``  → active
    - ``revoked_at IS NOT NULL`` → soft-deleted / revoked

    The raw key is generated on creation and returned to the user exactly once.
    ``key_prefix`` is the first 16 chars of the raw key and is safe to display.
    """

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_id("key")
    )
    user_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)

    # Human-readable label chosen by the user (e.g. "CI pipeline", "Local dev")
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    # First 16 chars of the raw key — safe to display; not enough to brute-force
    key_prefix: Mapped[str] = mapped_column(String(40), nullable=False)

    # SHA-256 hex digest of the raw key — the ONLY secret stored in the DB
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=func.now(),
    )

    # None until the key is first used via the API key auth middleware
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    # None → active.  Set to revocation time on DELETE /api-keys/{id}.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
