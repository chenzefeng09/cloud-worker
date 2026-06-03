"""MCP server schemas."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Transport(str, Enum):
    HTTP = "http"
    STDIO = "stdio"


class McpServerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    transport: Transport
    endpoint: str | None = Field(None, max_length=500)
    command: str | None = Field(None, max_length=500)
    env: dict[str, str] = Field(default_factory=dict)
    scopes: list[str] = Field(default_factory=list)
    enabled: bool = True

    @model_validator(mode="after")
    def _check_endpoint_or_command(self) -> McpServerCreate:
        if self.transport == Transport.HTTP and not self.endpoint:
            raise ValueError("endpoint is required for http transport")
        if self.transport == Transport.STDIO and not self.command:
            raise ValueError("command is required for stdio transport")
        return self


class McpServerUpdate(BaseModel):
    transport: Transport | None = None
    endpoint: str | None = Field(None, max_length=500)
    command: str | None = Field(None, max_length=500)
    env: dict[str, str] | None = None
    scopes: list[str] | None = None
    enabled: bool | None = None


class McpServerOut(BaseModel):
    id: str
    tenant_id: str
    name: str
    transport: Transport
    endpoint: str | None = None
    command: str | None = None
    env: dict[str, Any] = Field(default_factory=dict)
    scopes: list[str] = Field(default_factory=list)
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
