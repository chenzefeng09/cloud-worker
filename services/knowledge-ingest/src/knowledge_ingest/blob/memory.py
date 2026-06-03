"""In-memory BlobStore — tests only."""

from __future__ import annotations


class InMemoryBlobStore:
    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        self._data[key] = data
        return f"memory://{key}"

    def get(self, key: str) -> bytes:
        return self._data[key]

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def presign_get(self, key: str, *, ttl_seconds: int = 600) -> str:
        if key not in self._data:
            raise KeyError(key)
        return f"memory://{key}?ttl={ttl_seconds}"

    def list_prefix(self, prefix: str) -> list[str]:
        return [k for k in self._data if k.startswith(prefix)]

    @property
    def size(self) -> int:
        return len(self._data)
