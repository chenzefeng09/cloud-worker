"""Session-memory schemas (GDPR self-service listing / erasure)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class MemoryOut(BaseModel):
    """One session-memory document, as the data subject sees it.

    ``content`` is the stored text of the memory (the run's task + answer
    summary), so a listing doubles as the knowledge-ingest part of a data
    export.
    """

    id: str
    title: str
    content: str
    origin_session_id: str | None = None
    created_at: datetime


class MemoryList(BaseModel):
    items: list[MemoryOut]


class DeletedCount(BaseModel):
    deleted: int
