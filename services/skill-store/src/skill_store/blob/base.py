"""BlobStore protocol — minimum surface area used by SkillService.

Implementations
---------------
* ``S3BlobStore``    — production; boto3 against a configurable endpoint
                       (S3 / MinIO).
* ``InMemoryBlobStore`` — unit tests; data lives in a ``dict``.

Tarballs are stored under ``tenant/{tenant_id}/skills/{skill_id}/{version}.tar.gz``.
"""

from __future__ import annotations

from typing import Protocol


class BlobStore(Protocol):
    def put(self, key: str, data: bytes, *, content_type: str = "application/gzip") -> str:
        """Upload ``data`` at ``key``; return the canonical blob URL stored in DB."""
        ...

    def presign_get(self, key: str, *, ttl_seconds: int = 600) -> str:
        """Return a (signed) URL the client can GET directly."""
        ...

    def delete(self, key: str) -> None:
        ...

    def get(self, key: str) -> bytes:
        """Mostly used in tests — production path uses presigned URLs."""
        ...

    def list_prefix(self, prefix: str) -> list[str]:
        """Return all object keys that start with ``prefix``."""
        ...


def make_key(tenant_id: str, skill_id: str, version: str) -> str:
    return f"tenant/{tenant_id}/skills/{skill_id}/{version}.tar.gz"
