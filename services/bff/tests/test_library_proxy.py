"""Tests for BFF library proxy endpoints.

Covers:
* GET  /library/mcp-servers   — 401 unauth, 200 auth + proxied
* POST /library/mcp-servers   — 401 unauth, 201 auth + proxied
* GET  /library/skills        — 401 unauth, 200 auth + proxied
* POST /library/skills        — 401 unauth, 201 auth + proxied
* POST /library/skills/{id}/versions — 401 unauth, 201 auth + proxied
* GET  /library/documents     — 401 unauth, 200 auth + proxied
* POST /library/documents     — 401 unauth, 201 auth + proxied
* POST /library/documents/{id}/ingest — 401 unauth, 200 auth + proxied
* Upstream 4xx surfaced as HTTPException (_raise_for_upstream)
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fake upstream for library services
# ---------------------------------------------------------------------------

class FakeLibraryUpstream:
    """Configurable stub for mcp-registry, skill-store, knowledge-ingest."""

    def __init__(
        self,
        *,
        status: int = 200,
        body: dict | None = None,
    ) -> None:
        self.status = status
        self.body: dict = body if body is not None else {"items": [], "ok": True}
        self.calls: list[tuple[str, str]] = []

    def route(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, url))
        # Attach a dummy request so raise_for_status() works
        resp = httpx.Response(self.status, json=self.body)
        resp.request = httpx.Request(method, url)  # type: ignore[assignment]
        return resp


def _patch_async_client(fake: FakeLibraryUpstream, monkeypatch: pytest.MonkeyPatch) -> None:
    class _AC:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _AC:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def get(self, url: str, **kw: Any) -> httpx.Response:
            return fake.route("GET", url, **kw)

        async def post(self, url: str, **kw: Any) -> httpx.Response:
            return fake.route("POST", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------

def test_list_mcp_servers_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/library/mcp-servers")
    assert resp.status_code == 401


def test_list_mcp_servers_proxied(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLibraryUpstream(body={"items": [{"id": "mcp_01", "name": "GitHub"}]})
    _patch_async_client(fake, monkeypatch)

    resp = client.get("/api/bff/v1/library/mcp-servers", cookies=authed_cookie)
    assert resp.status_code == 200
    assert resp.json()["items"][0]["id"] == "mcp_01"
    assert any("mcp-servers" in c[1] for c in fake.calls)


def test_create_mcp_server_requires_auth(client: TestClient) -> None:
    resp = client.post("/api/bff/v1/library/mcp-servers", json={"name": "GitHub"})
    assert resp.status_code == 401


def test_create_mcp_server_proxied(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLibraryUpstream(status=201, body={"id": "mcp_01", "name": "GitHub"})
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/library/mcp-servers",
        cookies=authed_cookie,
        json={"name": "GitHub", "url": "https://github.com/mcp"},
    )
    assert resp.status_code == 201
    assert resp.json()["id"] == "mcp_01"


def test_create_mcp_server_surfaces_upstream_error(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upstream 422 is surfaced as 422 HTTPException."""
    fake = FakeLibraryUpstream(status=422, body={"detail": "invalid url"})
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/library/mcp-servers",
        cookies=authed_cookie,
        json={"name": "bad"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

def test_list_skills_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/library/skills")
    assert resp.status_code == 401


def test_list_skills_proxied(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLibraryUpstream(body={"items": [{"id": "sk_01", "name": "Summarizer"}]})
    _patch_async_client(fake, monkeypatch)

    resp = client.get("/api/bff/v1/library/skills", cookies=authed_cookie)
    assert resp.status_code == 200
    assert resp.json()["items"][0]["id"] == "sk_01"
    assert any("skills" in c[1] for c in fake.calls)


def test_create_skill_requires_auth(client: TestClient) -> None:
    resp = client.post("/api/bff/v1/library/skills", json={"name": "MySkill"})
    assert resp.status_code == 401


def test_create_skill_proxied(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLibraryUpstream(status=201, body={"id": "sk_01", "name": "MySkill"})
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/library/skills",
        cookies=authed_cookie,
        json={"name": "MySkill", "description": "does stuff"},
    )
    assert resp.status_code == 201
    assert resp.json()["id"] == "sk_01"


def test_create_skill_surfaces_upstream_error(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLibraryUpstream(status=409, body={"detail": "name conflict"})
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/library/skills",
        cookies=authed_cookie,
        json={"name": "duplicate"},
    )
    assert resp.status_code == 409


def test_create_skill_version_requires_auth(client: TestClient) -> None:
    resp = client.post(
        "/api/bff/v1/library/skills/sk_01/versions",
        data={"version": "1.0.0"},
        files={"file": ("skill.tar.gz", b"data", "application/gzip")},
    )
    assert resp.status_code == 401


def test_create_skill_version_proxied(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLibraryUpstream(status=201, body={"id": "sv_01", "version": "1.0.0"})
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/library/skills/sk_01/versions",
        cookies=authed_cookie,
        data={"version": "1.0.0"},
        files={"file": ("skill.tar.gz", b"fake-tar-content", "application/gzip")},
    )
    assert resp.status_code == 201
    assert resp.json()["version"] == "1.0.0"
    assert any("versions" in c[1] for c in fake.calls)


def test_create_skill_version_with_manifest(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skill version upload with optional manifest and expected_sha256 fields."""
    fake = FakeLibraryUpstream(status=201, body={"id": "sv_02", "version": "2.0.0"})
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/library/skills/sk_01/versions",
        cookies=authed_cookie,
        data={
            "version": "2.0.0",
            "manifest": '{"entry": "main.py"}',
            "expected_sha256": "abc123",
        },
        files={"file": ("skill.tar.gz", b"content", "application/gzip")},
    )
    assert resp.status_code == 201


def test_create_skill_version_rejects_oversized_upload(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Payloads above MAX_UPLOAD_BYTES are rejected at the edge with 413 and a
    user_message, and must NOT be forwarded to skill-store."""
    from agent_bff.api.v1 import library

    fake = FakeLibraryUpstream(status=201, body={"id": "sv_99"})
    _patch_async_client(fake, monkeypatch)

    oversized = b"x" * (library.MAX_UPLOAD_BYTES + 1)
    resp = client.post(
        "/api/bff/v1/library/skills/sk_01/versions",
        cookies=authed_cookie,
        data={"version": "9.9.9"},
        files={"file": ("huge.tar.gz", oversized, "application/gzip")},
    )
    assert resp.status_code == 413
    assert resp.json()["detail"]["user_message"] == "技能包超过大小上限 20 MB"
    # Upstream must never be reached for an oversized payload.
    assert fake.calls == []


def test_create_skill_version_accepts_upload_at_limit(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A payload exactly at the limit is allowed through (boundary is inclusive)."""
    from agent_bff.api.v1 import library

    fake = FakeLibraryUpstream(status=201, body={"id": "sv_03", "version": "3.0.0"})
    _patch_async_client(fake, monkeypatch)

    at_limit = b"x" * library.MAX_UPLOAD_BYTES
    resp = client.post(
        "/api/bff/v1/library/skills/sk_01/versions",
        cookies=authed_cookie,
        data={"version": "3.0.0"},
        files={"file": ("ok.tar.gz", at_limit, "application/gzip")},
    )
    assert resp.status_code == 201
    assert any("versions" in c[1] for c in fake.calls)


# ---------------------------------------------------------------------------
# Knowledge documents
# ---------------------------------------------------------------------------

def test_list_documents_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/library/documents")
    assert resp.status_code == 401


def test_list_documents_proxied(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLibraryUpstream(body={"items": [{"id": "doc_01", "title": "Report"}]})
    _patch_async_client(fake, monkeypatch)

    resp = client.get("/api/bff/v1/library/documents", cookies=authed_cookie)
    assert resp.status_code == 200
    assert resp.json()["items"][0]["id"] == "doc_01"
    assert any("documents" in c[1] for c in fake.calls)


def test_create_document_requires_auth(client: TestClient) -> None:
    resp = client.post(
        "/api/bff/v1/library/documents",
        data={"title": "My Doc", "mime": "application/pdf"},
        files={"file": ("doc.pdf", b"data", "application/pdf")},
    )
    assert resp.status_code == 401


def test_create_document_proxied(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLibraryUpstream(status=201, body={"id": "doc_01", "title": "Report"})
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/library/documents",
        cookies=authed_cookie,
        data={"title": "Report", "mime": "application/pdf"},
        files={"file": ("report.pdf", b"pdf-content", "application/pdf")},
    )
    assert resp.status_code == 201
    assert resp.json()["id"] == "doc_01"
    assert any("documents" in c[1] for c in fake.calls)


def test_create_document_surfaces_upstream_error(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLibraryUpstream(status=413, body={"detail": "file too large"})
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/library/documents",
        cookies=authed_cookie,
        data={"title": "Big File", "mime": "application/pdf"},
        files={"file": ("huge.pdf", b"x" * 10, "application/pdf")},
    )
    assert resp.status_code == 413


def test_ingest_document_requires_auth(client: TestClient) -> None:
    resp = client.post("/api/bff/v1/library/documents/doc_01/ingest")
    assert resp.status_code == 401


def test_ingest_document_proxied(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLibraryUpstream(body={"status": "QUEUED", "doc_id": "doc_01"})
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/library/documents/doc_01/ingest",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "QUEUED"
    assert any("ingest" in c[1] for c in fake.calls)


def test_ingest_document_surfaces_upstream_error(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLibraryUpstream(status=404, body={"detail": "document not found"})
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/library/documents/doc_missing/ingest",
        cookies=authed_cookie,
    )
    assert resp.status_code == 404
