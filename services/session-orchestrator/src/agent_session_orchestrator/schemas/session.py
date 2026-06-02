"""Session schemas."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SessionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"
    ARCHIVED = "ARCHIVED"


class SessionCreate(BaseModel):
    title: str | None = Field(None, max_length=200)
    agent_config: dict[str, Any] | None = None


class SessionUpdate(BaseModel):
    title: str | None = Field(None, max_length=200)
    status: SessionStatus | None = None


class SessionOut(BaseModel):
    id: str
    tenant_id: str
    user_id: str
    title: str | None = None
    status: SessionStatus
    current_run_id: str | None = None
    # Wire name stays ``metadata`` for backwards compatibility with the
    # OpenAPI contract; the underlying ORM attribute is ``agent_metadata``
    # because ``metadata`` is reserved by SQLAlchemy's DeclarativeBase.
    metadata: dict[str, Any] | None = Field(
        default=None, validation_alias="agent_metadata",
    )
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
