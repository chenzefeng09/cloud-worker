"""Document + folder + search schemas."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class DocumentStatus(StrEnum):
    PENDING = "PENDING"
    INGESTING = "INGESTING"
    READY = "READY"
    FAILED = "FAILED"


class Sensitivity(StrEnum):
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"
    L5 = "L5"


class SearchScope(StrEnum):
    KNOWLEDGE = "knowledge"
    MEMORY = "memory"
    ALL = "all"


class DocumentOut(BaseModel):
    id: str
    tenant_id: str
    title: str
    mime: str
    source_uri: str | None = None
    folder_id: str | None = None
    sensitivity: str = "L1"
    uploaded_by_user_id: str | None = None
    origin_session_id: str | None = None
    origin_user_id: str | None = None
    status: DocumentStatus
    uploaded_at: datetime
    chunk_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class DocumentPatch(BaseModel):
    """PATCH /documents/{id} body — move and/or re-classify a document.

    ``folder_id`` explicitly set to ``null`` moves the document to the root.
    Moving into a folder triggers the only-elevate rule; an explicit
    ``sensitivity`` is the sole way to *lower* a document's level.
    """

    folder_id: str | None = None
    sensitivity: Sensitivity | None = None


class FolderOut(BaseModel):
    id: str
    tenant_id: str
    parent_id: str | None = None
    name: str
    default_sensitivity: str
    created_by_user_id: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class FolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    parent_id: str | None = None
    default_sensitivity: Sensitivity = Sensitivity.L1


class FolderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=500)
    parent_id: str | None = None
    default_sensitivity: Sensitivity | None = None


class IngestResult(BaseModel):
    document_id: str
    status: DocumentStatus
    chunks_created: int


class SearchHit(BaseModel):
    document_id: str
    document_name: str
    folder_path: str
    chunk_id: str
    ord: int
    text: str
    score: float
    scope: str


class SearchResponse(BaseModel):
    hits: list[SearchHit]


class FolderList(BaseModel):
    items: list[FolderOut]
    total: int
