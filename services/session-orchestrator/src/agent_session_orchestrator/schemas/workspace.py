"""Workspace schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Sensitivity tiers L1..L5 (L1 < L2 < L3 < L4 < L5).  Duplicated from
# ``services.clearance.CLEARANCE_RANK`` deliberately: schemas are a lower layer
# than services and must not import from them, and the set is small + stable.
_VALID_SENSITIVITY_LEVELS = {"L1", "L2", "L3", "L4", "L5"}


def _validate_sensitivity_level(v: str) -> str:
    if v not in _VALID_SENSITIVITY_LEVELS:
        raise ValueError("sensitivity_level must be one of L1, L2, L3, L4, L5")
    return v


class WorkspaceCreate(BaseModel):
    name: str = Field(..., max_length=200, min_length=1)
    description: str | None = Field(None, max_length=500)
    instructions: str | None = None
    skills: list[str] | None = None
    meta: dict[str, Any] | None = None
    sensitivity_level: str = Field("L2", max_length=8)

    @field_validator("sensitivity_level")
    @classmethod
    def _check_sensitivity_level(cls, v: str) -> str:
        return _validate_sensitivity_level(v)


class WorkspaceUpdate(BaseModel):
    name: str | None = Field(None, max_length=200)
    description: str | None = Field(None, max_length=500)
    instructions: str | None = None
    skills: list[str] | None = None
    meta: dict[str, Any] | None = None
    sensitivity_level: str | None = Field(None, max_length=8)

    @field_validator("sensitivity_level")
    @classmethod
    def _check_sensitivity_level(cls, v: str | None) -> str | None:
        # ``None`` means "leave unchanged" (exclude_unset semantics); only
        # validate when the caller actually supplied a value.
        if v is None:
            return v
        return _validate_sensitivity_level(v)


class WorkspaceOut(BaseModel):
    id: str
    tenant_id: str
    user_id: str
    name: str
    description: str | None = None
    instructions: str | None = None
    skills: list[str]
    is_default: bool
    meta: dict[str, Any]
    sensitivity_level: str
    # ``file_count`` is not an ORM attribute - the endpoint layer computes it and
    # populates the model explicitly (defaults to 0 so the model can also be
    # constructed straight from the ORM row when a count isn't available).
    file_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class WorkspaceFileOut(BaseModel):
    id: str
    workspace_id: str
    filename: str
    mime: str
    size_bytes: int
    description: str | None = None
    sensitivity_level: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
