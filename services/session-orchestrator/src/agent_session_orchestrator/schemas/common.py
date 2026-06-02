"""Shared schemas — pagination, health."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CursorPage(BaseModel):
    items: list[Any] = Field(default_factory=list)
    next_cursor: str | None = None


class HealthStatus(BaseModel):
    status: str = "OK"
    service: str = "session-orchestrator"
    version: str = "0.1.0a1"
