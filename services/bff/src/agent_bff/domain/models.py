"""SQLAlchemy ORM for agent_bff schema.

Tables owned by this module:
- ``notification_preferences`` — per-user, per-event notification config.
- ``api_keys``                 — user-managed API keys (hash-only; never plaintext).
- ``privacy_notice_acknowledgements`` — append-only log of privacy-notice
  versions a user acknowledged.
- ``breach_records``           — the controller's personal-data breach register
  (GDPR Art. 33(5)), one row per breach, tenant-scoped.

The schema follows the same dialect-agnostic pattern as mcp-registry: JSON
types for SQLite test compatibility, Postgres-specific types only in Alembic
migrations.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
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


# ─────────────────────────────────────────────────────────────────────────────
# Privacy-notice acknowledgements
# ─────────────────────────────────────────────────────────────────────────────

class PrivacyNoticeAcknowledgement(Base):
    """One "I have read the privacy notice" event (GDPR Art. 5(2) accountability).

    Append-only: every acknowledgement is a new row and rows are never
    updated, so the log shows which notice version a user saw and when.  The
    latest acknowledgement is the row with the greatest ``acknowledged_at``.
    Rows are removed only by the user's own erasure request.
    """

    __tablename__ = "privacy_notice_acknowledgements"
    __table_args__ = (
        Index("ix_privacy_ack_user_time", "tenant_id", "user_id", "acknowledged_at"),
    )

    id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_id("pna")
    )
    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False)
    user_id: Mapped[str] = mapped_column(String(40), nullable=False)
    # The ``version`` string served by GET /privacy/notice at the time.
    notice_version: Mapped[str] = mapped_column(String(100), nullable=False)
    acknowledged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Personal-data breach register
# ─────────────────────────────────────────────────────────────────────────────

class BreachRecord(Base):
    """One personal-data breach (GDPR Art. 33(5): document every breach).

    Every breach is recorded — its facts, effects and remedial action — whether
    or not it was notified, so the supervisory authority can verify compliance.
    ``aware_at`` (when the controller became aware) starts the Art. 33(1)
    72-hour clock for notifying the authority; ``delay_reason`` records why a
    due notification was late or never made (Art. 33(1), last sentence).
    ``risk_level`` is the assessed risk to individuals: ``low`` (unlikely to
    result in a risk — no notification due), ``medium`` (notify the authority)
    or ``high`` (also tell the individuals, Art. 34).

    Rows hold no personal data of their own beyond the free-text fields an
    admin types; tenant-scoped by RLS on ``tenant_id``.
    """

    __tablename__ = "breach_records"
    __table_args__ = (
        Index("ix_breach_records_tenant_aware", "tenant_id", "aware_at"),
    )

    id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: new_id("brc")
    )
    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # When it happened, if known (often only established later).
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    aware_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # open | contained | closed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    # low | medium | high
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    data_categories: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    approx_subjects: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approx_records: Mapped[int | None] = mapped_column(Integer, nullable=True)
    likely_consequences: Mapped[str] = mapped_column(Text, nullable=False, default="")
    measures_taken: Mapped[str] = mapped_column(Text, nullable=False, default="")
    authority_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    authority_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    subjects_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    delay_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional link to the foundation security-ops incident it came from.
    security_incident_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str] = mapped_column(String(40), nullable=False)
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
