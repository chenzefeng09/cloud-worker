"""Skill / SkillVersion schemas."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Visibility(str, Enum):
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
