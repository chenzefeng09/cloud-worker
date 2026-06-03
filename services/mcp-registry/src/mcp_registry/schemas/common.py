"""Shared schemas — health + pagination."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HealthStatus(BaseModel):
    status: str = "OK"
    service: str = "mcp-registry"
    version: str = "0.1.0a1"


class ListPage(BaseModel):
    items: list[Any] = Field(default_factory=list)
    total: int = 0
