"""Canvas v2 API schemas (design §5.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Block(BaseModel):
    """One canvas block: ``{id, type, text}`` (design §4)."""

    id: str
    type: Literal["heading", "paragraph"]
    text: str


class CanvasCreate(BaseModel):
    """Create a canvas — ``source_message_id`` XOR ``content``.

    ``source_message_id`` → server reads ``message.content`` and materializes;
    ``content`` → server splits the supplied markdown directly.
    """

    source_message_id: str | None = None
    content: str | None = None
    kind: str = "article"
    title: str | None = None


class ManualVersionCreate(BaseModel):
    """Persist a manually-edited full-blocks snapshot as a new version."""

    blocks: list[Block]
    base_version: int


class Diff(BaseModel):
    """A single scoped edit in a suggestion (design §4)."""

    block_id: str
    op: Literal["replace", "delete", "insert_after"]
    orig: str | None = None
    sugg: str | None = None
    # Character-selection metadata.  orig/sugg stay full-block snapshots so
    # accept remains a normal atomic block replacement; these fields are only
    # used to render the pending delta at its exact in-block location.
    selection_start: int | None = None
    selection_end: int | None = None
    selected_orig: str | None = None
    selected_sugg: str | None = None


class SuggestionCreate(BaseModel):
    """Persist a model/user suggestion (BFF calls this internally)."""

    base_version: int
    instruction: str
    diffs: list[Diff]
    change_summary: str | None = None


class RevertRequest(BaseModel):
    to_version: int


class CanvasSummaryOut(BaseModel):
    """Canvas list item — no blocks (design §5.1 ``GET /sessions/{sid}/canvases``)."""

    id: str
    session_id: str
    kind: str
    title: str
    current_version: int
    source_message_id: str | None = None
    source_run_id: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CanvasOut(BaseModel):
    """Full canvas wire object — current version blocks inline (design §5.1)."""

    id: str
    session_id: str
    kind: str
    title: str
    current_version: int
    source_message_id: str | None = None
    source_run_id: str | None = None
    blocks: list[Block] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class VersionOut(BaseModel):
    """One canvas version (with blocks + provenance)."""

    id: str
    canvas_id: str
    version: int
    blocks: list[Block]
    change_summary: str | None = None
    created_by: str
    base_version: int | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class VersionSummaryOut(BaseModel):
    """Version list item — metadata only, no blocks (design §5.1)."""

    id: str
    canvas_id: str
    version: int
    change_summary: str | None = None
    created_by: str
    base_version: int | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class SuggestionOut(BaseModel):
    id: str
    canvas_id: str
    base_version: int
    status: str
    instruction: str
    diffs: list[Diff]
    change_summary: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None

    model_config = {"from_attributes": True}
