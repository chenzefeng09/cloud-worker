"""Skill / SkillVersion schemas."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Visibility(StrEnum):
    PRIVATE = "private"
    TENANT = "tenant"
    PUBLIC = "public"


class SkillCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(None, max_length=2000)
    owner: str | None = Field(None, max_length=120)
    visibility: Visibility = Visibility.PRIVATE
    # Platform-global default flag.  Only honoured when the skill is created
    # under ``PLATFORM_TENANT_ID`` (the seed) — an ordinary tenant creating a
    # skill leaves this false and it has no effect on resolution.
    default_enabled: bool = False


class SkillOut(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: str | None = None
    owner: str | None = None
    visibility: Visibility
    created_at: datetime
    # True for platform-global default skills (owned by PLATFORM_TENANT_ID). The
    # library surfaces these as read-only built-ins — every org has them, no
    # per-tenant install/uninstall applies. Not a DB column: set by the endpoint.
    is_platform_default: bool = False

    model_config = ConfigDict(from_attributes=True)


class SkillInstallRequest(BaseModel):
    enabled_by_default: bool = True
    # Optional explicit version to pin (must match an existing SkillVersion.version
    # of this skill).  ``None`` = follow latest at resolve time.
    version: str | None = Field(default=None, max_length=40)


class ResolvedSkillOut(BaseModel):
    """One entry in the scheduler ``/internal/skills/resolve`` response.

    ``blob_url`` is a short-lived presigned HTTP URL; the scheduler downloads
    by ``sha256`` (content-addressed cache key) and just uses the URL to fetch
    the bytes once per (skill, version).  ``description`` is copied from the
    resolved version's manifest (empty string when the manifest has none).
    """

    name: str
    version: str
    sha256: str
    blob_url: str
    description: str = ""


class SkillFileOut(BaseModel):
    """Response body for ``GET /internal/skills/{name}/file``.

    A single member of the skill package returned as UTF-8 text so the
    session-orchestrator's inline mode can read a skill's SKILL.md body or a
    ``references/`` file in-process without downloading and unpacking the whole
    tarball.  ``path`` is the normalised path relative to the skill root.
    """

    name: str
    version: str
    sha256: str
    path: str
    content: str


class DefaultInstalledSkillOut(BaseModel):
    """One entry in the ``/internal/skills/default-installed`` response.

    The orchestrator only needs the skill *name* to auto-inject the user's
    default skill set into a run that didn't specify skills explicitly, so
    this is intentionally minimal.
    """

    name: str


class InstalledSkillOut(BaseModel):
    """A skill in the user's toolbox — skill fields + install metadata."""

    id: str
    tenant_id: str
    name: str
    description: str | None = None
    owner: str | None = None
    visibility: Visibility
    created_at: datetime
    enabled_by_default: bool
    installed_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SkillVersionOut(BaseModel):
    id: str
    skill_id: str
    version: str
    manifest: dict[str, Any] = Field(default_factory=dict)
    blob_url: str
    sha256: str
    uploaded_by: str | None = None
    uploaded_at: datetime

    model_config = ConfigDict(from_attributes=True)
