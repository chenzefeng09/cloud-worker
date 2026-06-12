"""Tests for SkillCache — resolve → download → unpack → mount."""

from __future__ import annotations

import hashlib
import io
import tarfile
import threading
from pathlib import Path
from typing import Any

import httpx

from agent_scheduler.services.skill_cache import (
    SkillCache,
    _safe_extract,
    _target_ready,
)


# --------------------------------------------------------------------- helpers
def _build_tarball(members: dict[str, bytes]) -> bytes:
    """Build a gzip-compressed tar of ``{path: content}``."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _build_traversal_tarball() -> bytes:
    """Tarball with an absolute-path member that should be rejected."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="../etc/passwd")
        body = b"root:x:0:0::/root:/bin/sh\n"
        info.size = len(body)
        tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, status_code: int, *, content: bytes = b"", json_body: Any = None) -> None:
        self.status_code = status_code
        self.content = content
        self._json = json_body

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeHTTPClient:
    """Stand-in for ``httpx.Client`` used by SkillCache.

    Routes ``GET`` calls by URL: anything matching ``resolve_url`` returns the
    canned resolve payload; anything matching ``blob_url`` returns canned bytes.
    """

    def __init__(
        self,
        *,
        resolve_payload: dict[str, Any] | None = None,
        resolve_status: int = 200,
        blob_payloads: dict[str, bytes] | None = None,
        blob_status: int = 200,
        raise_on_get: bool = False,
    ) -> None:
        self.resolve_payload = resolve_payload
        self.resolve_status = resolve_status
        self.blob_payloads = blob_payloads or {}
        self.blob_status = blob_status
        self.raise_on_get = raise_on_get
        self.resolve_calls: list[dict[str, Any]] = []
        self.blob_calls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        if self.raise_on_get:
            raise httpx.ConnectError("boom")
        if "resolve" in url:
            self.resolve_calls.append(kwargs)
            if self.resolve_status >= 400:
                return _FakeResponse(self.resolve_status)
            return _FakeResponse(self.resolve_status, json_body=self.resolve_payload)
        # Blob URL — match by exact URL.
        self.blob_calls.append(url)
        if self.blob_status >= 400:
            return _FakeResponse(self.blob_status)
        body = self.blob_payloads.get(url, b"")
        return _FakeResponse(self.blob_status, content=body)


def _make_cache(tmp_path: Path, client: _FakeHTTPClient | None = None) -> SkillCache:
    cache = SkillCache(
        cache_base=str(tmp_path),
        skill_store_url="http://skill-store.test/api/skills/v1",
        service_scopes="skill.read_internal",
    )
    if client is not None:
        cache._client = client  # type: ignore[assignment]
    return cache


# --------------------------------------------------------------------- tests
def test_empty_skills_short_circuits(tmp_path: Path) -> None:
    """Empty input must not touch the network or filesystem."""
    fake = _FakeHTTPClient()
    cache = _make_cache(tmp_path, client=fake)
    result = cache.materialize([], tenant_id="t1", user_id="u1")
    assert result == []
    assert fake.resolve_calls == []
    assert fake.blob_calls == []


def test_cache_hit_skips_download(tmp_path: Path) -> None:
    """If the target dir already has SKILL.md, no download is performed."""
    members = {"SKILL.md": b"---\nname: x\ndescription: y\n---\nhello"}
    tarball = _build_tarball(members)
    sha = hashlib.sha256(tarball).hexdigest()

    # Pre-populate the target as if a previous spawn extracted it.
    target = tmp_path / sha
    target.mkdir()
    (target / "SKILL.md").write_bytes(members["SKILL.md"])

    fake = _FakeHTTPClient(
        resolve_payload={
            "items": [
                {"name": "x", "version": "1.0.0", "sha256": sha,
                 "blob_url": "http://blob.test/x.tar.gz"},
            ],
            "total": 1,
        },
    )
    cache = _make_cache(tmp_path, client=fake)
    result = cache.materialize(["x"], tenant_id="t1", user_id="u1")

    assert len(result) == 1
    assert result[0].name == "x"
    assert result[0].sha256 == sha
    assert result[0].host_path == str(target)
    # No blob fetch happened.
    assert fake.blob_calls == []


def test_cache_miss_downloads_and_unpacks(tmp_path: Path) -> None:
    members = {"SKILL.md": b"---\nname: y\ndescription: z\n---\nhi"}
    tarball = _build_tarball(members)
    sha = hashlib.sha256(tarball).hexdigest()
    blob_url = "http://blob.test/y.tar.gz?sig=abc"

    fake = _FakeHTTPClient(
        resolve_payload={
            "items": [
                {"name": "y", "version": "1.0.0", "sha256": sha,
                 "blob_url": blob_url},
            ],
            "total": 1,
        },
        blob_payloads={blob_url: tarball},
    )
    cache = _make_cache(tmp_path, client=fake)
    result = cache.materialize(["y"], tenant_id="t1", user_id="u1")

    assert len(result) == 1
    ms = result[0]
    assert ms.name == "y"
    assert ms.sha256 == sha
    target = Path(ms.host_path)
    assert target == tmp_path / sha
    assert (target / "SKILL.md").is_file()


def test_sha_mismatch_is_skipped_other_succeeds(tmp_path: Path) -> None:
    """When one skill's sha mismatches, drop it but still return the good one."""
    good_members = {"SKILL.md": b"---\nname: good\ndescription: ok\n---\nhi"}
    good_tar = _build_tarball(good_members)
    good_sha = hashlib.sha256(good_tar).hexdigest()
    good_url = "http://blob.test/good.tar.gz"

    bad_members = {"SKILL.md": b"---\nname: bad\ndescription: bd\n---\nhi"}
    bad_tar = _build_tarball(bad_members)
    bad_sha_real = hashlib.sha256(bad_tar).hexdigest()
    # Advertise a wrong sha to trigger mismatch.
    bad_sha_lie = "0" * 64 if bad_sha_real != "0" * 64 else "1" * 64
    bad_url = "http://blob.test/bad.tar.gz"

    fake = _FakeHTTPClient(
        resolve_payload={
            "items": [
                {"name": "good", "version": "1", "sha256": good_sha,
                 "blob_url": good_url},
                {"name": "bad", "version": "1", "sha256": bad_sha_lie,
                 "blob_url": bad_url},
            ],
            "total": 2,
        },
        blob_payloads={good_url: good_tar, bad_url: bad_tar},
    )
    cache = _make_cache(tmp_path, client=fake)
    result = cache.materialize(["good", "bad"], tenant_id="t1", user_id="u1")

    assert [ms.name for ms in result] == ["good"]


def test_resolve_5xx_returns_empty(tmp_path: Path) -> None:
    fake = _FakeHTTPClient(resolve_status=503)
    cache = _make_cache(tmp_path, client=fake)
    result = cache.materialize(["x"], tenant_id="t1", user_id="u1")
    assert result == []


def test_resolve_network_error_returns_empty(tmp_path: Path) -> None:
    fake = _FakeHTTPClient(raise_on_get=True)
    cache = _make_cache(tmp_path, client=fake)
    result = cache.materialize(["x"], tenant_id="t1", user_id="u1")
    assert result == []


def test_path_traversal_rejected(tmp_path: Path) -> None:
    """A tarball with ``../etc/passwd`` must not extract; skill is dropped."""
    bad = _build_traversal_tarball()
    sha = hashlib.sha256(bad).hexdigest()
    url = "http://blob.test/evil.tar.gz"
    fake = _FakeHTTPClient(
        resolve_payload={
            "items": [
                {"name": "evil", "version": "1", "sha256": sha, "blob_url": url},
            ],
            "total": 1,
        },
        blob_payloads={url: bad},
    )
    cache = _make_cache(tmp_path, client=fake)
    result = cache.materialize(["evil"], tenant_id="t1", user_id="u1")

    assert result == []
    # The would-be target dir must not exist either.
    assert not (tmp_path / sha).exists()


def test_unsafe_skill_name_dropped(tmp_path: Path) -> None:
    """Skill name with shell metacharacters is filtered out."""
    fake = _FakeHTTPClient(
        resolve_payload={
            "items": [
                {"name": "weird;rm -rf /", "version": "1",
                 "sha256": "a" * 64, "blob_url": "http://blob.test/x"},
            ],
            "total": 1,
        },
    )
    cache = _make_cache(tmp_path, client=fake)
    result = cache.materialize(["weird;rm -rf /"], tenant_id="t1", user_id="u1")
    assert result == []
    assert fake.blob_calls == []  # never even attempted to download


def test_target_ready_accepts_subdir_layout(tmp_path: Path) -> None:
    target = tmp_path / "abc"
    target.mkdir()
    sub = target / "mytool"
    sub.mkdir()
    (sub / "SKILL.md").write_text("---\nname: x\ndescription: y\n---\n")
    assert _target_ready(target)


def test_target_ready_rejects_missing_skill_md(tmp_path: Path) -> None:
    target = tmp_path / "abc"
    target.mkdir()
    (target / "OTHER.md").write_text("not a skill")
    assert not _target_ready(target)


# --------------------------------------------------------------------- concurrency
def _materialize_in_thread(
    results: list, *, cache: SkillCache, skill: str, sha: str, host_path: str
) -> None:
    out = cache.materialize([skill], tenant_id="t1", user_id="u1")
    results.append(out)


def test_concurrent_materialize_same_sha_serialises(tmp_path: Path) -> None:
    """Two threads racing the same sha must end up with one extraction."""
    members = {"SKILL.md": b"---\nname: race\ndescription: r\n---\nhi"}
    tarball = _build_tarball(members)
    sha = hashlib.sha256(tarball).hexdigest()
    blob_url = "http://blob.test/race.tar.gz"

    # Share one cache instance across threads; fake client tracks call count.
    call_counter = {"blob_gets": 0}
    lock = threading.Lock()

    class _CountingClient(_FakeHTTPClient):
        def get(self, url: str, **kwargs: Any) -> _FakeResponse:
            if "resolve" not in url:
                with lock:
                    call_counter["blob_gets"] += 1
            return super().get(url, **kwargs)

    fake = _CountingClient(
        resolve_payload={
            "items": [
                {"name": "race", "version": "1", "sha256": sha, "blob_url": blob_url},
            ],
            "total": 1,
        },
        blob_payloads={blob_url: tarball},
    )
    cache = _make_cache(tmp_path, client=fake)

    results: list = []
    t1 = threading.Thread(
        target=_materialize_in_thread,
        kwargs={"results": results, "cache": cache, "skill": "race",
                "sha": sha, "host_path": str(tmp_path / sha)},
    )
    t2 = threading.Thread(
        target=_materialize_in_thread,
        kwargs={"results": results, "cache": cache, "skill": "race",
                "sha": sha, "host_path": str(tmp_path / sha)},
    )
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Both calls got a mounted skill back.
    assert len(results) == 2
    for r in results:
        assert len(r) == 1
        assert r[0].name == "race"
        assert r[0].sha256 == sha
    # Blob was downloaded at most once thanks to the double-check inside the
    # lock — the loser sees the target already populated and skips.
    assert call_counter["blob_gets"] == 1


def test_safe_extract_rejects_oversized_total(tmp_path: Path) -> None:
    """Cumulative size cap is enforced."""
    big = b"\0" * (4 * 1024 * 1024)  # 4 MiB per member, under 5 MiB per-member cap
    members = {f"f{i}": big for i in range(20)}  # 80 MiB total > 50 MiB cap
    tarball = _build_tarball(members)
    ok = _safe_extract(tarball, tmp_path / "out")
    assert ok is False
