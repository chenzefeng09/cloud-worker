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
    """Configurable stub for mcp-registry, skill-store, knowledge-ingest.

    Also answers the authz ``/check`` calls made by the tenant-admin guard on
    knowledge-source writes.  By default every ``/check`` is allowed (so the
    happy-path write tests pass without ceremony).  Pass ``allowed_actions`` to
    model a specific role — a ``/check`` is allowed iff its ``action`` is in the
    set — or ``authz_allowed=False`` to deny all.  Each checked action is
    recorded in ``checked_actions``.
    """

    def __init__(
        self,
        *,
        status: int = 200,
        body: dict | None = None,
        authz_allowed: bool = True,
        allowed_actions: set[str] | None = None,
    ) -> None:
        self.status = status
        self.body: dict = body if body is not None else {"items": [], "ok": True}
        self.authz_allowed = authz_allowed
        self.allowed_actions = allowed_actions
        self.checked_actions: list[str] = []
        self.calls: list[tuple[str, str]] = []
        # (method, url, forwarded query params) for params-forwarding assertions.
        self.param_calls: list[tuple[str, str, dict]] = []
        # (method, url, forwarded headers) for header-forwarding assertions.
        self.header_calls: list[tuple[str, str, dict]] = []

    def route(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        # authz /check — the BFF tenant-admin guard (knowledge-source writes).
        if "/check" in url:
            action = (kwargs.get("json") or {}).get("action", "")
            self.checked_actions.append(action)
            if self.allowed_actions is not None:
                allowed = action in self.allowed_actions
            else:
                allowed = self.authz_allowed
            return httpx.Response(200, json={"allowed": allowed})

        self.calls.append((method, url))
        self.param_calls.append((method, url, dict(kwargs.get("params") or {})))
        self.header_calls.append((method, url, dict(kwargs.get("headers") or {})))
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

        async def patch(self, url: str, **kw: Any) -> httpx.Response:
            return fake.route("PATCH", url, **kw)

        async def delete(self, url: str, **kw: Any) -> httpx.Response:
            return fake.route("DELETE", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)


# The ``member`` system role's knowledge grants are read-only (document.read /
# document.search); it holds no ``document.create`` — the BFF write gate action.
_MEMBER_ACTIONS = {"document.read", "document.search", "mcp_server.read", "skill.read"}
# tenant_admin holds the ``document.*`` wildcard → document.create, but not
# ``platform.admin`` (cross-tenant is out of scope for the library surface).
_TENANT_ADMIN_ACTIONS = {"document.create"}


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


def test_list_mcp_servers_scrubs_secrets(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Library list never leaks auth credentials, connection headers, env
    values or CLI args.

    ``auth`` is reduced to ``{type}`` (token dropped), ``headers`` is withheld
    entirely, and env values / args are masked (env keys kept); metadata
    (timeout_seconds / tool_filter) is kept.
    """
    fake = FakeLibraryUpstream(body={"items": [{
        "id": "mcp_01",
        "name": "GitHub",
        "transport": "http",
        "auth": {"type": "bearer", "token": "sk-secret"},
        "headers": {"Authorization": "Bearer sk-secret", "X-Trace": "1"},
        "env": {"GITHUB_TOKEN": "ghp-secret", "LOG_LEVEL": "debug"},
        "timeout_seconds": 30,
        "tool_filter": {"allow": ["search"], "block": []},
        "args": ["--token", "ghp-secret"],
    }]})
    _patch_async_client(fake, monkeypatch)

    resp = client.get("/api/bff/v1/library/mcp-servers", cookies=authed_cookie)
    assert resp.status_code == 200
    server = resp.json()["items"][0]
    # auth reduced to its scheme only - token dropped.
    assert server["auth"] == {"type": "bearer"}
    # headers withheld entirely.
    assert "headers" not in server
    # env keys kept, values and args masked.
    assert server["env"] == {"GITHUB_TOKEN": "••••••", "LOG_LEVEL": "••••••"}
    assert server["args"] == ["••••••", "••••••"]
    assert "ghp-secret" not in resp.text
    # metadata still present end-to-end.
    assert server["timeout_seconds"] == 30
    assert server["tool_filter"] == {"allow": ["search"], "block": []}


def test_create_mcp_server_scrubs_secrets(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Library create response never echoes auth credentials or headers."""
    fake = FakeLibraryUpstream(
        status=201,
        body={
            "id": "mcp_01",
            "name": "GitHub",
            "auth": {
                "type": "api_key",
                "api_key": "sk-secret",
                "header_name": "X-API-Key",
            },
            "headers": {"X-API-Key": "sk-secret"},
        },
    )
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/library/mcp-servers",
        cookies=authed_cookie,
        json={"name": "GitHub", "transport": "http", "endpoint": "https://x"},
    )
    assert resp.status_code == 201
    server = resp.json()
    # auth reduced to its scheme only - api_key / header_name dropped.
    assert server["auth"] == {"type": "api_key"}
    # headers withheld entirely.
    assert "headers" not in server
    assert server["id"] == "mcp_01"


def test_list_mcp_servers_scrubs_oauth_secrets(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Library list never leaks oauth credentials.

    The oauth scheme carries ``client_id`` / ``client_secret`` / ``token_url``
    / ``scopes`` inside ``auth``; all of them are dropped and ``auth`` is
    reduced to ``{type: "oauth"}``.  ``headers`` is withheld entirely.
    """
    fake = FakeLibraryUpstream(body={"items": [{
        "id": "mcp_02",
        "name": "OAuthGateway",
        "transport": "http",
        "auth": {
            "type": "oauth",
            "client_id": "cid-secret",
            "client_secret": "cs-secret",
            "token_url": "https://auth.example.com/token",
            "scopes": ["read", "write"],
        },
        "headers": {"Authorization": "Bearer from-oauth"},
    }]})
    _patch_async_client(fake, monkeypatch)

    resp = client.get("/api/bff/v1/library/mcp-servers", cookies=authed_cookie)
    assert resp.status_code == 200
    server = resp.json()["items"][0]
    # auth reduced to its scheme only - all oauth credentials dropped.
    assert server["auth"] == {"type": "oauth"}
    assert "client_id" not in server["auth"]
    assert "client_secret" not in server["auth"]
    assert "token_url" not in server["auth"]
    assert "scopes" not in server["auth"]
    # headers withheld entirely.
    assert "headers" not in server
    assert server["id"] == "mcp_02"


def test_create_mcp_server_scrubs_oauth_secrets(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Library create response never echoes oauth credentials or headers."""
    fake = FakeLibraryUpstream(
        status=201,
        body={
            "id": "mcp_02",
            "name": "OAuthGateway",
            "auth": {
                "type": "oauth",
                "client_id": "cid-secret",
                "client_secret": "cs-secret",
                "token_url": "https://auth.example.com/token",
                "scopes": ["read", "write"],
            },
            "headers": {"Authorization": "Bearer from-oauth"},
        },
    )
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/library/mcp-servers",
        cookies=authed_cookie,
        json={
            "name": "OAuthGateway",
            "transport": "http",
            "endpoint": "https://x",
            "auth": {
                "type": "oauth",
                "client_id": "cid-secret",
                "client_secret": "cs-secret",
                "token_url": "https://auth.example.com/token",
                "scopes": ["read", "write"],
            },
        },
    )
    assert resp.status_code == 201
    server = resp.json()
    # auth reduced to its scheme only - all oauth credentials dropped.
    assert server["auth"] == {"type": "oauth"}
    assert "client_id" not in server["auth"]
    assert "client_secret" not in server["auth"]
    assert "token_url" not in server["auth"]
    assert "scopes" not in server["auth"]
    # headers withheld entirely.
    assert "headers" not in server
    assert server["id"] == "mcp_02"


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


def test_list_skill_versions_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/library/skills/sk_01/versions")
    assert resp.status_code == 401


def test_list_skill_versions_proxied(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /library/skills/{id}/versions surfaces the upstream items[] envelope.

    skill-store returns ``{items: [...], total: N}`` (ListPage). The BFF passes
    that through untouched — the SPA's install-to-toolbox modal renders the
    items in a dropdown so the user can pin a specific version (or leave null
    → follow latest).
    """
    fake = FakeLibraryUpstream(
        body={
            "items": [
                {"id": "sv_01", "skill_id": "sk_01", "version": "0.1.0"},
                {"id": "sv_02", "skill_id": "sk_01", "version": "0.2.0"},
            ],
            "total": 2,
        },
    )
    _patch_async_client(fake, monkeypatch)

    resp = client.get(
        "/api/bff/v1/library/skills/sk_01/versions",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["items"][0]["version"] == "0.1.0"
    assert body["items"][1]["version"] == "0.2.0"
    assert any("sk_01/versions" in c[1] and c[0] == "GET" for c in fake.calls)


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
    assert resp.json()["detail"]["user_message"] == "The skill package exceeds the 20 MB size limit."
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


def test_list_documents_does_not_send_include_memory(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary library listing must NOT forward include_memory, so
    knowledge-ingest keeps session-memory docs hidden by default."""
    fake = FakeLibraryUpstream(body={"items": []})
    _patch_async_client(fake, monkeypatch)

    resp = client.get("/api/bff/v1/library/documents", cookies=authed_cookie)
    assert resp.status_code == 200
    doc_params = [
        p for (m, u, p) in fake.param_calls if m == "GET" and "documents" in u
    ]
    assert doc_params, "library documents list did not hit knowledge-ingest"
    assert "include_memory" not in doc_params[0]


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


# ---------------------------------------------------------------------------
# Knowledge documents — extended fields (folder_id filter, PATCH, DELETE)
# ---------------------------------------------------------------------------

def test_list_documents_forwards_folder_id_filter(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ?folder_id= query is forwarded to knowledge-ingest as a filter."""
    fake = FakeLibraryUpstream(body={"items": []})
    _patch_async_client(fake, monkeypatch)

    resp = client.get(
        "/api/bff/v1/library/documents?folder_id=fld_01",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    doc_params = [
        p for (m, u, p) in fake.param_calls if m == "GET" and "documents" in u
    ]
    assert doc_params and doc_params[0].get("folder_id") == "fld_01"


def test_update_document_requires_auth(client: TestClient) -> None:
    resp = client.patch(
        "/api/bff/v1/library/documents/doc_01", json={"sensitivity": "L3"},
    )
    assert resp.status_code == 401


def test_update_document_proxied(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLibraryUpstream(body={"id": "doc_01", "sensitivity": "L3"})
    _patch_async_client(fake, monkeypatch)

    resp = client.patch(
        "/api/bff/v1/library/documents/doc_01",
        cookies=authed_cookie,
        json={"folder_id": "fld_02", "sensitivity": "L3"},
    )
    assert resp.status_code == 200
    assert resp.json()["sensitivity"] == "L3"
    assert any(
        m == "PATCH" and u.endswith("/documents/doc_01") for (m, u) in fake.calls
    )


def test_delete_document_requires_auth(client: TestClient) -> None:
    resp = client.delete("/api/bff/v1/library/documents/doc_01")
    assert resp.status_code == 401


def test_delete_document_proxied(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLibraryUpstream(status=204, body={})
    _patch_async_client(fake, monkeypatch)

    resp = client.delete(
        "/api/bff/v1/library/documents/doc_01", cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    assert any(
        m == "DELETE" and u.endswith("/documents/doc_01") for (m, u) in fake.calls
    )


# ---------------------------------------------------------------------------
# Knowledge folders — CRUD proxy
# ---------------------------------------------------------------------------

def test_list_folders_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/library/folders")
    assert resp.status_code == 401


def test_list_folders_proxied(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLibraryUpstream(
        body={"items": [{"id": "fld_01", "name": "产品", "parent_id": None}]},
    )
    _patch_async_client(fake, monkeypatch)

    resp = client.get("/api/bff/v1/library/folders", cookies=authed_cookie)
    assert resp.status_code == 200
    assert resp.json()["items"][0]["id"] == "fld_01"
    assert any(m == "GET" and u.endswith("/folders") for (m, u) in fake.calls)


def test_create_folder_requires_auth(client: TestClient) -> None:
    resp = client.post("/api/bff/v1/library/folders", json={"name": "产品"})
    assert resp.status_code == 401


def test_create_folder_proxied(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLibraryUpstream(status=201, body={"id": "fld_new", "name": "产品"})
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/library/folders",
        cookies=authed_cookie,
        json={"name": "产品", "parent_id": None, "default_sensitivity": "L2"},
    )
    assert resp.status_code == 201
    assert resp.json()["id"] == "fld_new"
    assert any(m == "POST" and u.endswith("/folders") for (m, u) in fake.calls)


def test_create_folder_surfaces_upstream_conflict(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate folder name under the same parent is a 409 from ki."""
    fake = FakeLibraryUpstream(status=409, body={"detail": "folder name conflict"})
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/library/folders",
        cookies=authed_cookie,
        json={"name": "产品"},
    )
    assert resp.status_code == 409


def test_update_folder_proxied(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLibraryUpstream(body={"id": "fld_01", "name": "合同模板"})
    _patch_async_client(fake, monkeypatch)

    resp = client.patch(
        "/api/bff/v1/library/folders/fld_01",
        cookies=authed_cookie,
        json={"name": "合同模板"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "合同模板"
    assert any(
        m == "PATCH" and u.endswith("/folders/fld_01") for (m, u) in fake.calls
    )


def test_delete_folder_proxied(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLibraryUpstream(status=204, body={})
    _patch_async_client(fake, monkeypatch)

    resp = client.delete(
        "/api/bff/v1/library/folders/fld_01", cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    assert any(
        m == "DELETE" and u.endswith("/folders/fld_01") for (m, u) in fake.calls
    )


def test_delete_folder_surfaces_non_empty_conflict(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """knowledge-ingest 409s a non-empty folder; the BFF surfaces it verbatim."""
    fake = FakeLibraryUpstream(status=409, body={"detail": "folder not empty"})
    _patch_async_client(fake, monkeypatch)

    resp = client.delete(
        "/api/bff/v1/library/folders/fld_01", cookies=authed_cookie,
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Tenant-admin guard on knowledge-source writes
# ---------------------------------------------------------------------------
#
# Writes (upload / ingest / doc PATCH+DELETE / folder CRUD) are gated on
# ``document.create`` — held by tenant_admin (via ``document.*``) but NOT by a
# read-only member.  Reads (list documents / folders) stay open to all.

# (path, method, json | None) — a "json" of None means multipart (upload).
_WRITE_ENDPOINTS = [
    ("/library/documents", "POST", None),  # multipart upload
    ("/library/documents/doc_01/ingest", "POST", {}),
    ("/library/documents/doc_01", "PATCH", {"sensitivity": "L3"}),
    ("/library/documents/doc_01", "DELETE", {}),
    ("/library/folders", "POST", {"name": "x"}),
    ("/library/folders/fld_01", "PATCH", {"name": "y"}),
    ("/library/folders/fld_01", "DELETE", {}),
]

_READ_ENDPOINTS = [
    ("/library/documents", "GET"),
    ("/library/folders", "GET"),
]


def _write_call(client: TestClient, path: str, method: str, json, cookies):
    url = f"/api/bff/v1{path}"
    if method == "POST" and json is None:
        # multipart upload
        return client.post(
            url,
            cookies=cookies,
            data={"title": "t", "mime": "text/plain"},
            files={"file": ("t.txt", b"data", "text/plain")},
        )
    return client.request(method, url, json=json, cookies=cookies)


@pytest.mark.parametrize("path,method,json", _WRITE_ENDPOINTS)
def test_write_denied_for_member(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    method: str,
    json,
) -> None:
    """A plain member (no document.create) is 403'd at the BFF, before any
    upstream call to knowledge-ingest."""
    fake = FakeLibraryUpstream(status=201, allowed_actions=_MEMBER_ACTIONS)
    _patch_async_client(fake, monkeypatch)

    resp = _write_call(client, path, method, json, authed_cookie)
    assert resp.status_code == 403, f"{method} {path} → {resp.status_code}"
    # The guard checked document.create and denied → no ki call forwarded.
    assert "document.create" in fake.checked_actions
    assert fake.calls == [], f"{method} {path} leaked an upstream call: {fake.calls}"


@pytest.mark.parametrize("path,method,json", _WRITE_ENDPOINTS)
def test_write_allowed_for_tenant_admin(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    method: str,
    json,
) -> None:
    """A tenant_admin (holds document.create) passes the BFF guard and the call
    is forwarded to knowledge-ingest."""
    fake = FakeLibraryUpstream(status=201, allowed_actions=_TENANT_ADMIN_ACTIONS)
    _patch_async_client(fake, monkeypatch)

    resp = _write_call(client, path, method, json, authed_cookie)
    assert resp.status_code in (200, 201), f"{method} {path} → {resp.status_code}"
    assert "document.create" in fake.checked_actions
    assert fake.calls, f"{method} {path} did not forward to knowledge-ingest"


@pytest.mark.parametrize("path,method", _READ_ENDPOINTS)
def test_read_allowed_for_member(
    client: TestClient,
    authed_cookie: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    method: str,
) -> None:
    """Reads are open to any authenticated user — no document.create needed and
    no authz /check performed."""
    fake = FakeLibraryUpstream(body={"items": []}, allowed_actions=_MEMBER_ACTIONS)
    _patch_async_client(fake, monkeypatch)

    resp = client.get(f"/api/bff/v1{path}", cookies=authed_cookie)
    assert resp.status_code == 200
    # A read must not consult authz at all.
    assert fake.checked_actions == []


# ---------------------------------------------------------------------------
# X-User-Clearance forwarding — every knowledge-source proxy must carry it so
# the sensitivity gate stays enforced (the dropped-clearance chat-path lesson).
# ---------------------------------------------------------------------------

def _clearance_headers_for(fake: FakeLibraryUpstream, needle: str) -> list[dict]:
    return [h for (m, u, h) in fake.header_calls if needle in u]


def test_list_documents_forwards_clearance(
    client: TestClient,
    authed_cookie_with_clearance: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLibraryUpstream(body={"items": []})
    _patch_async_client(fake, monkeypatch)

    resp = client.get(
        "/api/bff/v1/library/documents", cookies=authed_cookie_with_clearance,
    )
    assert resp.status_code == 200
    hdrs = _clearance_headers_for(fake, "/documents")
    assert hdrs and hdrs[0].get("X-User-Clearance") == "L3"
    assert hdrs[0].get("X-User-Id")  # identity headers forwarded too


def test_list_folders_forwards_clearance(
    client: TestClient,
    authed_cookie_with_clearance: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeLibraryUpstream(body={"items": []})
    _patch_async_client(fake, monkeypatch)

    resp = client.get(
        "/api/bff/v1/library/folders", cookies=authed_cookie_with_clearance,
    )
    assert resp.status_code == 200
    hdrs = _clearance_headers_for(fake, "/folders")
    assert hdrs and hdrs[0].get("X-User-Clearance") == "L3"


def test_create_folder_forwards_clearance(
    client: TestClient,
    authed_cookie_with_clearance: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Writes forward clearance too (authz allows the tenant-admin action)."""
    fake = FakeLibraryUpstream(status=201, body={"id": "fld_new"})
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/library/folders",
        cookies=authed_cookie_with_clearance,
        json={"name": "产品"},
    )
    assert resp.status_code == 201
    hdrs = _clearance_headers_for(fake, "/folders")
    assert hdrs and hdrs[0].get("X-User-Clearance") == "L3"


def test_upload_document_forwards_clearance_and_fields(
    client: TestClient,
    authed_cookie_with_clearance: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upload forwards clearance and the new folder_id / sensitivity fields."""
    fake = FakeLibraryUpstream(status=201, body={"id": "doc_new"})
    _patch_async_client(fake, monkeypatch)

    resp = client.post(
        "/api/bff/v1/library/documents",
        cookies=authed_cookie_with_clearance,
        data={
            "title": "合同",
            "mime": "application/pdf",
            "folder_id": "fld_01",
            "sensitivity": "L3",
        },
        files={"file": ("c.pdf", b"pdf", "application/pdf")},
    )
    assert resp.status_code == 201
    hdrs = _clearance_headers_for(fake, "/documents")
    assert hdrs and hdrs[0].get("X-User-Clearance") == "L3"
