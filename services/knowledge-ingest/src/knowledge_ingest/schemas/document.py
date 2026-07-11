"""Document + search schemas."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class DocumentStatus(StrEnum):
    PENDING = "PENDING"
    INGESTING = "INGESTING"
    READY = "READY"
    FAILED = "FAILED"


class DocumentOut(BaseModel):
    id: str
    tenant_id: str
    title: str
    mime: str
    source_uri: str | None = None
    origin_session_id: str | None = None
    status: DocumentStatus
    uploaded_at: datetime
    chunk_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class IngestResult(BaseModel):
    document_id: str
    status: DocumentStatus
    chunks_created: int


class SearchHit(BaseModel):
    document_id: str
    chunk_id: str
    ord: int
    text: str
    score: float


class SearchResponse(BaseModel):
    hits: list[SearchHit]
