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


class SkillOut(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: str | None = None
    owner: str | None = None
    visibility: Visibility
    created_at: datetime

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
    the bytes once per (skill, version).
    """

    name: str
    version: str
    sha256: str
    blob_url: str


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
