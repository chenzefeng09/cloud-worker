"""Session schemas."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SessionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"
    ARCHIVED = "ARCHIVED"


class SessionCreate(BaseModel):
    title: str | None = Field(None, max_length=200)
    agent_config: dict[str, Any] | None = None
    workspace_id: str | None = None


class TaskSpec(BaseModel):
    """What a task-board task was asked to do, kept for retry and display.

    Stored under ``metadata.task`` of a ``kind="task"`` session.  ``options``
    holds the run knobs the task was started with (skills, MCP servers, model,
    reasoning effort, hitl) so a retry can replay them faithfully.
    """

    instructions: str = Field(..., min_length=1, max_length=20000)
    options: dict[str, Any] = Field(default_factory=dict)


class SessionUpdate(BaseModel):
    title: str | None = Field(None, max_length=200)
    status: SessionStatus | None = None
    # Only accepted on ``kind="task"`` sessions; replaces ``metadata.task``.
    task: TaskSpec | None = None


class SessionOut(BaseModel):
    id: str
    tenant_id: str
    user_id: str
    title: str | None = None
    status: SessionStatus
    current_run_id: str | None = None
    workspace_id: str | None = None
    # Wire name stays ``metadata`` for backwards compatibility with the
    # OpenAPI contract; the underlying ORM attribute is ``agent_metadata``
    # because ``metadata`` is reserved by SQLAlchemy's DeclarativeBase.
    metadata: dict[str, Any] | None = Field(
        default=None, validation_alias="agent_metadata",
    )
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
