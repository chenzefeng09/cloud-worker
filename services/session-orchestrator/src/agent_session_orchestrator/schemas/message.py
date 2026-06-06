"""Message schemas."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, field_validator


class MessageRole(str, Enum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"
    SYSTEM = "SYSTEM"
    TOOL = "TOOL"


class AttachmentMeta(BaseModel):
    """Minimal attachment metadata embedded in a message row."""

    id: str
    filename: str
    mime: str | None = None
    size: int | None = None


class MessageCreate(BaseModel):
    role: MessageRole
    content: str
    # tool_calls may be a dict (legacy) or a list of tool-call entries (TOOL
    # messages persisted from a run); accept either.
    tool_calls: Any | None = None
    run_id: str | None = None
    attachment_ids: list[str] = []


class MessageOut(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    # TOOL messages store a list of tool-call entries; assistant/user store
    # None.  Accept any JSON shape rather than forcing a dict.
    tool_calls: Any | None = None
    run_id: str | None = None
    sequence: int
    created_at: datetime
    attachments: list[AttachmentMeta] = []

    model_config = {"from_attributes": True}

    @field_validator("attachments", mode="before")
    @classmethod
    def _coerce_attachments(cls, v: Any) -> Any:
        # The DB column is nullable; a NULL value arrives as None, which would
        # fail list validation.  Coerce None -> [].
        return v or []
