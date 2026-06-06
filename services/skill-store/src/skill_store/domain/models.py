"""SQLAlchemy ORM for skill_store schema."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:26].upper()}"


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Skill(Base):
    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_skill_tenant_name"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("skl"))
    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    visibility: Mapped[str] = mapped_column(String(20), nullable=False, default="private")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now(),
    )

    versions: Mapped[list["SkillVersion"]] = relationship(
        "SkillVersion", back_populates="skill", cascade="all, delete-orphan",
    )


class UserInstalledSkill(Base):
    """A skill a user has installed into their personal toolbox.

    Install is the middle layer between org-publish (Skill.visibility) and
    per-session enablement: a user installs a skill they can see, and
    ``enabled_by_default`` controls whether new sessions auto-enable it. The
    flag lives on the install relation (not the skill) so two users can install
    the same skill with different defaults.
    """

    __tablename__ = "user_installed_skills"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", "skill_id", name="uq_user_installed_skill",
        ),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("uis"))
    tenant_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    skill_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("skills.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    enabled_by_default: Mapped[bool] = mapped_column(nullable=False, default=True)
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now(),
    )


class SkillVersion(Base):
    __tablename__ = "skill_versions"
    __table_args__ = (UniqueConstraint("skill_id", "version", name="uq_skill_version"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("sv"))
    skill_id: Mapped[str] = mapped_column(String(40), ForeignKey("skills.id"), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    blob_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now(),
    )

    skill: Mapped[Skill] = relationship("Skill", back_populates="versions")
