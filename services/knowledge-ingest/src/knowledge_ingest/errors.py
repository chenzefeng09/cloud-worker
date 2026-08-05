"""Service-specific error codes — RFC 9457 Problem Details."""

from __future__ import annotations


class IngestError(Exception):
    def __init__(self, detail: str, *, code: str, status: int = 400) -> None:
        self.detail = detail
        self.code = code
        self.status = status


class DocumentNotFoundError(IngestError):
    def __init__(self, document_id: str) -> None:
        super().__init__(
            f"Document {document_id} not found",
            code="KNOWLEDGE.DOCUMENT.NOT_FOUND",
            status=404,
        )


class UnsupportedMimeError(IngestError):
    def __init__(self, mime: str) -> None:
        super().__init__(
            f"Unsupported MIME type {mime!r} (allowed: text/plain, text/markdown, application/pdf)",
            code="KNOWLEDGE.DOCUMENT.UNSUPPORTED_MIME",
            status=415,
        )


class IngestFailedError(IngestError):
    def __init__(self, document_id: str, reason: str) -> None:
        super().__init__(
            f"Ingest for {document_id} failed: {reason}",
            code="KNOWLEDGE.INGEST.FAILED",
            status=500,
        )


class EmbeddingDimensionError(IngestError):
    def __init__(self, expected: int, got: int) -> None:
        super().__init__(
            f"Embedding dimension mismatch: expected {expected}, got {got}",
            code="KNOWLEDGE.EMBEDDING.DIM_MISMATCH",
            status=500,
        )


class FolderNotFoundError(IngestError):
    def __init__(self, folder_id: str) -> None:
        super().__init__(
            f"Folder {folder_id} not found",
            code="KNOWLEDGE.FOLDER.NOT_FOUND",
            status=404,
        )


class FolderNameConflictError(IngestError):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"A folder named {name!r} already exists in this location",
            code="KNOWLEDGE.FOLDER.NAME_CONFLICT",
            status=409,
        )


class FolderNotEmptyError(IngestError):
    def __init__(self, folder_id: str) -> None:
        super().__init__(
            f"Folder {folder_id} is not empty (contains sub-folders or documents)",
            code="KNOWLEDGE.FOLDER.NOT_EMPTY",
            status=409,
        )


class FolderCycleError(IngestError):
    def __init__(self, folder_id: str) -> None:
        super().__init__(
            f"Cannot move folder {folder_id} under itself or one of its descendants",
            code="KNOWLEDGE.FOLDER.CYCLE",
            status=409,
        )


class InvalidSensitivityError(IngestError):
    def __init__(self, value: str) -> None:
        super().__init__(
            f"Invalid sensitivity {value!r} (allowed: L1, L2, L3, L4, L5)",
            code="KNOWLEDGE.SENSITIVITY.INVALID",
            status=400,
        )
