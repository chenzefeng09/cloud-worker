"""Message schemas."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel


class MessageRole(str, Enum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"
    SYSTEM = "SYSTEM"
    TOOL = "TOOL"


class MessageCreate(BaseModel):
    role: MessageRole
    content: str
    tool_calls: dict[str, Any] | None = None
    run_id: str | None = None


class MessageOut(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    tool_calls: dict[str, Any] | None = None
    run_id: str | None = None
    sequence: int
    created_at: datetime

    model_config = {"from_attributes": True}
