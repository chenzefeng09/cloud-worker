"""Pydantic v2 request/response schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class FetchRequest(BaseModel):
    url: str
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    # Per-request byte cap; bounded to settings.max_response_bytes server-side.
    max_bytes: int | None = None


class FetchResponse(BaseModel):
    url: str
    status_code: int
    content_type: str
    body: str
    truncated: bool
    error: str | None = None
