"""Unit tests for the BlobStore implementations."""

from __future__ import annotations

import pytest

from skill_store.blob.base import make_key
from skill_store.blob.memory import InMemoryBlobStore


def test_make_key_layout() -> None:
    assert make_key("tnt_1", "skl_2", "1.0.0") == "tenant/tnt_1/skills/skl_2/1.0.0.tar.gz"


def test_memory_put_get_roundtrip() -> None:
    store = InMemoryBlobStore()
    url = store.put("k1", b"hello")
    assert url == "memory://k1"
    assert store.get("k1") == b"hello"
    assert store.size == 1


def test_memory_presign_includes_ttl() -> None:
    store = InMemoryBlobStore()
    store.put("k1", b"x")
    assert store.presign_get("k1", ttl_seconds=42) == "memory://k1?ttl=42"


def test_memory_presign_missing_raises() -> None:
    store = InMemoryBlobStore()
    with pytest.raises(KeyError):
        store.presign_get("missing")


def test_memory_delete_is_idempotent() -> None:
    store = InMemoryBlobStore()
    store.put("k1", b"x")
    store.delete("k1")
    store.delete("k1")
    assert store.size == 0


def test_s3_blob_store_uses_provided_client() -> None:
    """Constructor accepts an injected client — no real boto3 needed."""
    from skill_store.blob.s3 import S3BlobStore

    class _FakeClient:
        def __init__(self) -> None:
            self.put_calls: list[tuple] = []

        def put_object(self, **kw: object) -> None:
            self.put_calls.append((kw["Bucket"], kw["Key"], kw["Body"]))

        def generate_presigned_url(self, **kw: object) -> str:
            return f"https://signed/{kw['Params']['Key']}"

        def delete_object(self, **kw: object) -> None:  # pragma: no cover
            pass

        def get_object(self, **kw: object) -> dict:  # pragma: no cover
            return {"Body": _Body(b"")}

    class _Body:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def read(self) -> bytes:
            return self._data

    fake = _FakeClient()
    store = S3BlobStore(bucket="b", region="r", client=fake)
    url = store.put("k", b"data")
    assert url == "s3://b/k"
    assert fake.put_calls == [("b", "k", b"data")]
    assert store.presign_get("k") == "https://signed/k"
