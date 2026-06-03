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
