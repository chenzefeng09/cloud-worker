"""App / AppVersion schemas — harness definition, input form, review.

The input-form schema is the trickiest part: an ordered array of ``InputField``
descriptors that the BFF run resolver replays to validate + render a run's
inputs.  Cross-field rules (unique keys, options/file presence) are enforced
here with a ``@model_validator`` so a malformed form is rejected at create /
edit time — mirroring skill-store's validator style.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

# Tag constraints - shared by AppCreate / AppUpdate.
_TAG_MAX_LEN = 32
_TAG_MAX_COUNT = 20


class Visibility(StrEnum):
    PRIVATE = "private"
    TENANT = "tenant"


class AppVersionStatus(StrEnum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    PUBLISHED = "published"
    REJECTED = "rejected"


class InputFieldType(StrEnum):
    TEXT = "text"
    TEXTAREA = "textarea"
    SELECT = "select"
    MULTISELECT = "multiselect"
    FILE = "file"


class SelectOption(BaseModel):
    value: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)


class FileConstraint(BaseModel):
    accept: list[str] = Field(default_factory=list)
    max_size_bytes: int | None = Field(default=None, ge=0)
    multiple: bool = False


class InputField(BaseModel):
    """One field in an App's input form (ordered within ``input_schema``)."""

    key: str
    type: InputFieldType
    label: str = Field(..., min_length=1, max_length=200)
    help: str | None = Field(default=None, max_length=2000)
    required: bool = False
    default: Any | None = None
    options: list[SelectOption] = Field(default_factory=list)
    file: FileConstraint | None = None

    @model_validator(mode="after")
    def _validate_field(self) -> InputField:
        if not _KEY_RE.match(self.key):
            raise ValueError(
                f"input field key {self.key!r} must match ^[a-z][a-z0-9_]{{0,39}}$"
            )
        if self.type in (InputFieldType.SELECT, InputFieldType.MULTISELECT) and not self.options:
            raise ValueError(
                f"input field {self.key!r} of type {self.type} must define non-empty options"
            )
        if self.type == InputFieldType.FILE and self.file is None:
            raise ValueError(
                f"input field {self.key!r} of type file must define a 'file' constraint block"
            )
        return self


class SamplingParams(BaseModel):
    """Per-run sampling overrides applied at the gateway model layer."""

    temperature: float | None = Field(default=None, ge=0.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    max_tokens: int | None = Field(default=None, ge=1)

    model_config = ConfigDict(extra="forbid")


def _validate_input_schema(fields: list[InputField]) -> list[InputField]:
    seen: set[str] = set()
    for f in fields:
        if f.key in seen:
            raise ValueError(f"duplicate input field key {f.key!r} in form")
        seen.add(f.key)
    return fields


def _validate_tags(tags: list[str]) -> list[str]:
    """Normalize + validate an App's tag list.

    Each tag is stripped, must be non-empty and <= ``_TAG_MAX_LEN`` chars; the
    total (de-duplicated) count must be <= ``_TAG_MAX_COUNT``.  Duplicates are
    removed case-sensitively while preserving first-seen order.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        if not isinstance(raw, str):
            raise ValueError("tags must be strings")
        tag = raw.strip()
        if not tag:
            raise ValueError("tags must be non-empty")
        if len(tag) > _TAG_MAX_LEN:
            raise ValueError(f"tag too long (max {_TAG_MAX_LEN} chars): {tag!r}")
        if tag not in seen:
            seen.add(tag)
            cleaned.append(tag)
    if len(cleaned) > _TAG_MAX_COUNT:
        raise ValueError(
            f"too many tags (max {_TAG_MAX_COUNT}): got {len(cleaned)}"
        )
    return cleaned


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------
class AppVersionCreate(BaseModel):
    """Create a new draft version (or the initial draft when creating an App)."""

    # Deprecated request field kept for older clients. The service assigns the
    # persisted version number monotonically per App.
    version: str | None = Field(default=None, min_length=1, max_length=40)
    # Both prompts may be empty strings: ``system_prompt`` is optional by
    # product decision (an App may rely on the default worker persona), and
    # ``user_prompt_template`` starts out empty on the initial draft created
    # by the "new App" modal (the editor enforces it before publish).
    system_prompt: str = Field(...)
    user_prompt_template: str = Field(...)
    model: str = Field(..., min_length=1, max_length=120)
    reasoning_effort: str | None = Field(default=None, max_length=20)
    sampling_params: SamplingParams = Field(default_factory=SamplingParams)
    forced_skills: list[str] = Field(default_factory=list)
    forced_mcp_server_ids: list[str] = Field(default_factory=list)
    input_schema: list[InputField] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> AppVersionCreate:
        _validate_input_schema(self.input_schema)
        return self


class AppVersionOut(BaseModel):
    id: str
    app_id: str
    tenant_id: str
    version: str
    status: AppVersionStatus
    system_prompt: str
    user_prompt_template: str
    model: str
    reasoning_effort: str | None = None
    sampling_params: dict[str, Any] = Field(default_factory=dict)
    forced_skills: list[Any] = Field(default_factory=list)
    forced_mcp_server_ids: list[Any] = Field(default_factory=list)
    input_schema: list[Any] = Field(default_factory=list)
    submitted_by: str | None = None
    submitted_at: datetime | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None
    created_by: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Apps
# ---------------------------------------------------------------------------
class AppCreate(BaseModel):
    """Create an App together with its initial draft version (atomic)."""

    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(None, max_length=2000)
    icon: str | None = Field(None, max_length=120)
    visibility: Visibility = Visibility.PRIVATE
    tags: list[str] = Field(default_factory=list)
    version: AppVersionCreate

    @field_validator("tags")
    @classmethod
    def _normalize_tags(cls, v: list[str]) -> list[str]:
        return _validate_tags(v)


class AppUpdate(BaseModel):
    """App-level field edits (does not touch versions)."""

    name: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = Field(None, max_length=2000)
    icon: str | None = Field(None, max_length=120)
    visibility: Visibility | None = None
    tags: list[str] | None = None

    @field_validator("tags")
    @classmethod
    def _normalize_tags(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _validate_tags(v)


class AppOut(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: str | None = None
    icon: str | None = None
    author_id: str
    visibility: Visibility
    tags: list[str] = Field(default_factory=list)
    current_version_id: str | None = None
    created_at: datetime
    updated_at: datetime
    versions: list[AppVersionOut] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class ReviewDecision(BaseModel):
    """Body for approve / reject — note required only on reject (enforced in API)."""

    note: str | None = Field(default=None, max_length=2000)


class AppReviewItem(BaseModel):
    """One row in the admin review queue: a pending version + its App context."""

    version: AppVersionOut
    app_id: str
    app_name: str
    author_id: str
