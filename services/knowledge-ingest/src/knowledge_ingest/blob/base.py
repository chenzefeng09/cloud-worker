"""BlobStore protocol for knowledge-ingest raw documents."""

from __future__ import annotations

from typing import Protocol


class BlobStore(Protocol):
    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        ...

    def get(self, key: str) -> bytes:
        ...

    def delete(self, key: str) -> None:
        ...

    def presign_get(self, key: str, *, ttl_seconds: int = 600) -> str:
        """Return a pre-signed URL the client can GET directly."""
        ...

    def list_prefix(self, prefix: str) -> list[str]:
        """Return all object keys that start with ``prefix``."""
        ...


def make_key(tenant_id: str, document_id: str, filename: str) -> str:
    return f"tenant/{tenant_id}/documents/{document_id}/{filename}"
