"""HTTP-layer tests for mcp-registry, end-to-end against in-memory SQLite."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


def _http_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "weather",
        "transport": "http",
        "endpoint": "https://weather.example.com/mcp",
        "env": {"API_KEY": "xxx"},
    }
    base.update(overrides)
    return base


def test_healthz(app_client: TestClient) -> None:
    resp = app_client.get("/api/mcp/v1/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["service"] == "mcp-registry"


def test_list_requires_tenant_header(app_client: TestClient) -> None:
    # authz guard fires before the current_tenant dependency: missing tenant
    # is caught at the guard level (403) before the 401 dependency runs.
    resp = app_client.get("/api/mcp/v1/mcp-servers")
    assert resp.status_code in (401, 403)


def test_create_and_list(app_client: TestClient, tenant_headers: dict[str, str]) -> None:
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers", headers=tenant_headers, json=_http_payload(),
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["name"] == "weather"
    assert created["transport"] == "http"
    assert created["enabled"] is True
    # The dead top-level ``scopes`` field is gone from the write+read contract
    # (``auth.scopes`` — the OAuth form parameter — is a different thing).
    assert "scopes" not in created

    listed = app_client.get("/api/mcp/v1/mcp-servers", headers=tenant_headers).json()
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == created["id"]


def test_create_http_without_endpoint_rejected(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    bad = {"name": "bad", "transport": "http"}
    resp = app_client.post("/api/mcp/v1/mcp-servers", headers=tenant_headers, json=bad)
    assert resp.status_code == 422


def test_create_stdio_requires_command(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json={"name": "fs", "transport": "stdio"},
    )
    assert resp.status_code == 422


def test_create_stdio_ok(app_client: TestClient, tenant_headers: dict[str, str]) -> None:
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json={"name": "fs", "transport": "stdio", "command": "/usr/local/bin/mcp-fs"},
    )
    assert resp.status_code == 201
    assert resp.json()["command"] == "/usr/local/bin/mcp-fs"


def test_create_sse_ok(app_client: TestClient, tenant_headers: dict[str, str]) -> None:
    """sse transport is accepted (legacy SSE, consumable only by the SDK worker
    direct path). It carries an endpoint like http."""
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json={"name": "legacy", "transport": "sse", "endpoint": "https://legacy.local/sse"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["transport"] == "sse"
    assert body["endpoint"] == "https://legacy.local/sse"


def test_create_sse_without_endpoint_rejected(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    """sse requires an endpoint (same shape as http)."""
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json={"name": "bad", "transport": "sse"},
    )
    assert resp.status_code == 422


def test_duplicate_name_conflict(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    app_client.post("/api/mcp/v1/mcp-servers", headers=tenant_headers, json=_http_payload())
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers", headers=tenant_headers, json=_http_payload(),
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "MCP.SERVER.NAME_CONFLICT"


def test_get_unknown_404(app_client: TestClient, tenant_headers: dict[str, str]) -> None:
    resp = app_client.get(
        "/api/mcp/v1/mcp-servers/mcp_doesnotexist", headers=tenant_headers,
    )
    assert resp.status_code == 404


def test_update_enable_flag(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    created = app_client.post(
        "/api/mcp/v1/mcp-servers", headers=tenant_headers, json=_http_payload(),
    ).json()
    resp = app_client.patch(
        f"/api/mcp/v1/mcp-servers/{created['id']}",
        headers=tenant_headers,
        json={"enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


def test_delete_then_404(app_client: TestClient, tenant_headers: dict[str, str]) -> None:
    created = app_client.post(
        "/api/mcp/v1/mcp-servers", headers=tenant_headers, json=_http_payload(),
    ).json()
    resp = app_client.delete(
        f"/api/mcp/v1/mcp-servers/{created['id']}", headers=tenant_headers,
    )
    assert resp.status_code == 204
    follow = app_client.get(
        f"/api/mcp/v1/mcp-servers/{created['id']}", headers=tenant_headers,
    )
    assert follow.status_code == 404


def test_tenant_isolation(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    other_tenant_headers: dict[str, str],
) -> None:
    """Tenant A's server must not appear in Tenant B's listing."""
    app_client.post("/api/mcp/v1/mcp-servers", headers=tenant_headers, json=_http_payload())
    other = app_client.get("/api/mcp/v1/mcp-servers", headers=other_tenant_headers).json()
    assert other["total"] == 0


def test_internal_endpoint_requires_scope(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    app_client.post("/api/mcp/v1/mcp-servers", headers=tenant_headers, json=_http_payload())
    # No X-Service-Scopes header → 403.
    resp = app_client.get(
        "/api/mcp/v1/internal/mcp-servers",
        params={"tenant_id": "tnt_01J5TEST00000000000000TNT1"},
    )
    assert resp.status_code == 403


def test_internal_endpoint_wrong_scope_rejected(app_client: TestClient) -> None:
    """A present but insufficient X-Service-Scopes value still produces 403."""
    resp = app_client.get(
        "/api/mcp/v1/internal/mcp-servers",
        params={"tenant_id": "tnt_01J5TEST00000000000000TNT1"},
        headers={"X-Service-Scopes": "audit.write"},  # wrong scope
    )
    assert resp.status_code == 403


def test_internal_endpoint_multi_scope_header_accepted(
    app_client: TestClient,
    tenant_headers: dict[str, str],
) -> None:
    """X-Service-Scopes can carry multiple comma-separated scopes (as the
    scheduler injects FOUNDATION_SERVICE_SCOPES = 'mcp.read_internal,skill.read_internal').
    The required scope anywhere in the list must be accepted.
    """
    app_client.post("/api/mcp/v1/mcp-servers", headers=tenant_headers, json=_http_payload())
    resp = app_client.get(
        "/api/mcp/v1/internal/mcp-servers",
        params={"tenant_id": "tnt_01J5TEST00000000000000TNT1"},
        headers={
            "X-Tenant-Id": "tnt_01J5TEST00000000000000TNT1",
            "X-Service-Scopes": "mcp.read_internal,skill.read_internal",
        },
    )
    assert resp.status_code == 200, resp.text


def test_internal_endpoint_rejects_query_tenant_mismatch(
    app_client: TestClient,
) -> None:
    resp = app_client.get(
        "/api/mcp/v1/internal/mcp-servers",
        params={"tenant_id": "tnt_01J5TEST00000000000000TNT2"},
        headers={
            "X-Tenant-Id": "tnt_01J5TEST00000000000000TNT1",
            "X-Service-Scopes": "mcp.read_internal",
        },
    )
    assert resp.status_code == 403


def test_internal_endpoint_returns_enabled_servers(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    created = app_client.post(
        "/api/mcp/v1/mcp-servers", headers=tenant_headers, json=_http_payload(),
    ).json()
    # Add a disabled one — must be filtered.
    app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(name="disabled-one", enabled=False),
    )
    resp = app_client.get(
        "/api/mcp/v1/internal/mcp-servers",
        params={"tenant_id": "tnt_01J5TEST00000000000000TNT1"},
        headers=internal_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == created["id"]


# ---------------------------------------------------------------------------
# Extended config (migration 0002): schema validation + CRUD passthrough
# ---------------------------------------------------------------------------


def test_create_with_extended_config_roundtrip(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    payload = _http_payload(
        headers={"X-Custom": "abc"},
        auth={"type": "bearer", "token": "tok-123"},
        timeout_seconds=15,
        tool_filter={"allow": ["echo"], "block": ["dangerous"]},
        args=["--flag", "value"],
    )
    resp = app_client.post("/api/mcp/v1/mcp-servers", headers=tenant_headers, json=payload)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["headers"] == {"X-Custom": "abc"}
    # Tenant-facing endpoints now return masked auth.
    assert created["auth"]["type"] == "bearer"
    assert created["auth"]["token"] == "••••••"
    assert created["auth"]["has_token"] is True
    assert created["timeout_seconds"] == 15
    assert created["tool_filter"] == {"allow": ["echo"], "block": ["dangerous"]}
    assert created["args"] == ["--flag", "value"]

    fetched = app_client.get(
        f"/api/mcp/v1/mcp-servers/{created['id']}", headers=tenant_headers,
    ).json()
    assert fetched["auth"]["type"] == "bearer"
    assert fetched["auth"]["token"] == "••••••"
    assert fetched["auth"]["has_token"] is True
    assert fetched["headers"] == {"X-Custom": "abc"}
    assert fetched["timeout_seconds"] == 15
    assert fetched["tool_filter"] == {"allow": ["echo"], "block": ["dangerous"]}
    assert fetched["args"] == ["--flag", "value"]


def test_create_defaults_extended_fields(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers", headers=tenant_headers, json=_http_payload(),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["headers"] == {}
    assert body["auth"] is None
    assert body["timeout_seconds"] is None
    # tool_filter normalised to the canonical {allow,block} shape even though
    # the DB default is '{}'::jsonb.
    assert body["tool_filter"] == {"allow": [], "block": []}
    assert body["args"] == []


def test_internal_endpoint_surfaces_extended_fields(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(
            auth={"type": "api_key", "api_key": "k", "header_name": "X-Api-Key"},
            tool_filter={"allow": ["a"]},
            timeout_seconds=7,
        ),
    )
    resp = app_client.get(
        "/api/mcp/v1/internal/mcp-servers",
        params={"tenant_id": "tnt_01J5TEST00000000000000TNT1"},
        headers=internal_headers,
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["auth"] == {"type": "api_key", "api_key": "k", "header_name": "X-Api-Key"}
    assert item["tool_filter"] == {"allow": ["a"], "block": []}
    assert item["timeout_seconds"] == 7


# --- auth validation ------------------------------------------------------


def test_auth_invalid_type_rejected(app_client: TestClient, tenant_headers: dict[str, str]) -> None:
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(auth={"type": "weird", "token": "x"}),
    )
    assert resp.status_code == 422


def test_auth_bearer_missing_token_rejected(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(auth={"type": "bearer"}),
    )
    assert resp.status_code == 422


def test_auth_api_key_missing_key_rejected(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(auth={"type": "api_key"}),
    )
    assert resp.status_code == 422


def test_auth_api_key_defaults_header_name(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(auth={"type": "api_key", "api_key": "k"}),
    )
    assert resp.status_code == 201, resp.text
    auth = resp.json()["auth"]
    assert auth["header_name"] == "X-API-Key"
    # api_key is masked on the create response.
    assert auth["api_key"] == "••••••"
    assert auth["has_api_key"] is True


def test_auth_none_explicit_ok(app_client: TestClient, tenant_headers: dict[str, str]) -> None:
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(auth={"type": "none"}),
    )
    assert resp.status_code == 201
    assert resp.json()["auth"] == {"type": "none"}


def test_timeout_seconds_must_be_positive(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(timeout_seconds=0),
    )
    assert resp.status_code == 422


# --- update passthrough ---------------------------------------------------


def test_update_extended_fields(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    created = app_client.post(
        "/api/mcp/v1/mcp-servers", headers=tenant_headers, json=_http_payload(),
    ).json()
    resp = app_client.patch(
        f"/api/mcp/v1/mcp-servers/{created['id']}",
        headers=tenant_headers,
        json={
            "headers": {"X-Trace": "1"},
            "auth": {"type": "bearer", "token": "t"},
            "timeout_seconds": 20,
            "tool_filter": {"allow": ["x"]},
            "args": ["a"],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["headers"] == {"X-Trace": "1"}
    # PATCH response is masked.
    assert body["auth"]["type"] == "bearer"
    assert body["auth"]["token"] == "••••••"
    assert body["auth"]["has_token"] is True
    assert body["timeout_seconds"] == 20
    assert body["tool_filter"] == {"allow": ["x"], "block": []}
    assert body["args"] == ["a"]


# --- test connection endpoint --------------------------------------------


def test_test_endpoint_stdio_not_supported(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json={"name": "fs", "transport": "stdio", "command": "/usr/local/bin/mcp-fs"},
    ).json()
    resp = app_client.post(
        f"/api/mcp/v1/mcp-servers/{created['id']}/test", headers=tenant_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["server_info"] is None
    assert body["tools"] == []
    assert "stdio" in body["error"]


def test_test_endpoint_sse_not_supported(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    """sse servers return a deterministic not-supported result: the registry
    tester does not implement the legacy dual-endpoint SSE handshake, but the
    SDK worker path supports it natively."""
    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json={"name": "legacy", "transport": "sse", "endpoint": "https://legacy.local/sse"},
    ).json()
    resp = app_client.post(
        f"/api/mcp/v1/mcp-servers/{created['id']}/test", headers=tenant_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["server_info"] is None
    assert body["tools"] == []
    assert "sse" in body["error"].lower()
    assert "not supported" in body["error"].lower()
    assert "worker" in body["error"].lower()


def test_test_endpoint_http_success(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock the httpx client so the handshake pipeline is exercised end-to-end."""
    import mcp_registry.services.server_tester as tester_mod

    class _FakeResponse:
        def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
            self._payload = payload
            self.status_code = status_code
            self.headers: dict[str, str] = {"content-type": "application/json"}

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise tester_mod.httpx.HTTPError(f"status {self.status_code}")

        def json(self) -> dict[str, Any]:
            return self._payload

        @property
        def text(self) -> str:
            return ""

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kw: Any) -> None:
            self.calls: list[dict[str, Any]] = []

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(
            self, url: str, json: dict[str, Any], headers: dict[str, str],
        ) -> _FakeResponse:
            # Non-JSON-RPC posts are authz /check calls - allow them so the
            # create-server request (which runs the RBAC guard) succeeds.
            if "method" not in json:
                return _FakeResponse({"allowed": True})
            self.calls.append({"url": url, "json": json, "headers": headers})
            method = json.get("method")
            if method == "initialize":
                return _FakeResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": json.get("id"),
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": {"name": "echo", "version": "1.0.0"},
                        },
                    },
                )
            if method == "notifications/initialized":
                return _FakeResponse({}, status_code=202)
            if method == "tools/list":
                return _FakeResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": json.get("id"),
                        "result": {
                            "tools": [
                                {"name": "echo", "description": "回显文本"},
                                {"name": "ping"},
                            ]
                        },
                    },
                )
            return _FakeResponse({}, status_code=404)

    fake = _FakeAsyncClient()
    monkeypatch.setattr(tester_mod.httpx, "AsyncClient", lambda *a, **kw: fake)

    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(
            auth={"type": "bearer", "token": "tok"},
            headers={"X-Custom": "v"},
            timeout_seconds=12,
        ),
    ).json()
    resp = app_client.post(
        f"/api/mcp/v1/mcp-servers/{created['id']}/test", headers=tenant_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["server_info"] == {"name": "echo", "version": "1.0.0"}
    assert [t["name"] for t in body["tools"]] == ["echo", "ping"]
    assert body["tools"][0]["description"] == "回显文本"
    assert body["error"] is None

    # Auth + custom headers were injected on every request.
    for call in fake.calls:
        assert call["headers"]["Authorization"] == "Bearer tok"
        assert call["headers"]["X-Custom"] == "v"
        assert call["headers"]["MCP-Protocol-Version"] == "2024-11-05"


def test_test_endpoint_http_transport_error(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_registry.services.server_tester as tester_mod

    class _BoomClient:
        async def __aenter__(self) -> _BoomClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, json: dict[str, Any], **kw: Any) -> Any:
            # Allow authz /check calls so create-server succeeds; only boom
            # on the actual MCP JSON-RPC posts.
            if "method" not in json:
                from types import SimpleNamespace
                return SimpleNamespace(status_code=200, json=lambda: {"allowed": True})
            raise tester_mod.httpx.ConnectError("boom")

    monkeypatch.setattr(tester_mod.httpx, "AsyncClient", lambda *a, **kw: _BoomClient())

    created = app_client.post(
        "/api/mcp/v1/mcp-servers", headers=tenant_headers, json=_http_payload(),
    ).json()
    resp = app_client.post(
        f"/api/mcp/v1/mcp-servers/{created['id']}/test", headers=tenant_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["tools"] == []
    assert "transport error" in body["error"]


def test_test_endpoint_unknown_server_404(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers/mcp_nope/test", headers=tenant_headers,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /mcp-servers/probe — id-less live probe of an unsaved config
# ---------------------------------------------------------------------------


def _install_handshake_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch httpx with a fake MCP server that answers initialize + tools/list.

    Returns the fake client so tests can assert on the captured JSON-RPC calls.
    Non-JSON-RPC posts (the authz ``/check`` calls made by the RBAC guard) are
    answered with ``{"allowed": True}`` so the guarded route runs.
    """
    import mcp_registry.services.server_tester as tester_mod

    class _FakeResponse:
        def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
            self._payload = payload
            self.status_code = status_code
            self.headers: dict[str, str] = {"content-type": "application/json"}

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise tester_mod.httpx.HTTPError(f"status {self.status_code}")

        def json(self) -> dict[str, Any]:
            return self._payload

        @property
        def text(self) -> str:
            return ""

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kw: Any) -> None:
            self.calls: list[dict[str, Any]] = []

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(
            self, url: str, json: dict[str, Any], headers: dict[str, str],
        ) -> _FakeResponse:
            if "method" not in json:
                return _FakeResponse({"allowed": True})
            self.calls.append({"url": url, "json": json, "headers": headers})
            method = json.get("method")
            if method == "initialize":
                return _FakeResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": json.get("id"),
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": {"name": "probe-target", "version": "2.0.0"},
                        },
                    },
                )
            if method == "notifications/initialized":
                return _FakeResponse({}, status_code=202)
            if method == "tools/list":
                return _FakeResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": json.get("id"),
                        "result": {
                            "tools": [
                                {"name": "search", "description": "搜索"},
                                {"name": "fetch"},
                            ]
                        },
                    },
                )
            return _FakeResponse({}, status_code=404)

    fake = _FakeAsyncClient()
    monkeypatch.setattr(tester_mod.httpx, "AsyncClient", lambda *a, **kw: fake)
    return fake


def test_probe_returns_tool_list_without_persisting(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The admin create-form path: probe a config that has no row yet.

    Plaintext ``auth`` from the body is injected on every handshake request
    (no decrypt runs — nothing was ever encrypted), and no server is created
    as a side effect.
    """
    fake = _install_handshake_client(monkeypatch)

    resp = app_client.post(
        "/api/mcp/v1/mcp-servers/probe",
        headers=tenant_headers,
        json={
            "transport": "http",
            "endpoint": "https://probe.example.com/mcp",
            "headers": {"X-Custom": "v"},
            "auth": {"type": "bearer", "token": "plain-tok"},
            "timeout_seconds": 7,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["server_info"] == {"name": "probe-target", "version": "2.0.0"}
    assert [t["name"] for t in body["tools"]] == ["search", "fetch"]
    assert body["tools"][0]["description"] == "搜索"
    assert body["error"] is None

    # Plaintext auth + custom headers reached the upstream on every call.
    assert fake.calls, "probe did not perform the handshake"
    for call in fake.calls:
        assert call["url"] == "https://probe.example.com/mcp"
        assert call["headers"]["Authorization"] == "Bearer plain-tok"
        assert call["headers"]["X-Custom"] == "v"

    # Probing is not a write: the registry is still empty.
    listed = app_client.get("/api/mcp/v1/mcp-servers", headers=tenant_headers).json()
    assert listed["total"] == 0


def test_probe_route_not_shadowed_by_server_id(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``probe`` must not be captured as a ``{server_id}`` path parameter.

    If route ordering regressed, this would 404 (no server named ``probe``)
    instead of running the probe.
    """
    _install_handshake_client(monkeypatch)
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers/probe",
        headers=tenant_headers,
        json={"transport": "http", "endpoint": "https://probe.example.com/mcp"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


def test_probe_upstream_unreachable_returns_ok_false(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead upstream is a result, not an exception."""
    import mcp_registry.services.server_tester as tester_mod

    class _BoomClient:
        async def __aenter__(self) -> _BoomClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, json: dict[str, Any], **kw: Any) -> Any:
            if "method" not in json:
                from types import SimpleNamespace
                return SimpleNamespace(status_code=200, json=lambda: {"allowed": True})
            raise tester_mod.httpx.ConnectError("boom")

    monkeypatch.setattr(tester_mod.httpx, "AsyncClient", lambda *a, **kw: _BoomClient())

    resp = app_client.post(
        "/api/mcp/v1/mcp-servers/probe",
        headers=tenant_headers,
        json={"transport": "http", "endpoint": "https://down.example.com/mcp"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["tools"] == []
    assert "transport error" in body["error"]


def test_probe_upstream_5xx_returns_ok_false(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 5xx on ``initialize`` surfaces as ok=false, never a 500 from us."""
    import mcp_registry.services.server_tester as tester_mod

    class _ErrResponse:
        status_code = 503
        headers: dict[str, str] = {"content-type": "application/json"}

        def raise_for_status(self) -> None:
            raise tester_mod.httpx.HTTPStatusError(
                "503 Service Unavailable", request=None, response=None,
            )

        def json(self) -> dict[str, Any]:
            return {}

    class _FiveHundredClient:
        async def __aenter__(self) -> _FiveHundredClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, json: dict[str, Any], **kw: Any) -> Any:
            if "method" not in json:
                from types import SimpleNamespace
                return SimpleNamespace(status_code=200, json=lambda: {"allowed": True})
            return _ErrResponse()

    monkeypatch.setattr(
        tester_mod.httpx, "AsyncClient", lambda *a, **kw: _FiveHundredClient(),
    )

    resp = app_client.post(
        "/api/mcp/v1/mcp-servers/probe",
        headers=tenant_headers,
        json={"transport": "http", "endpoint": "https://broken.example.com/mcp"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["server_info"] is None
    assert body["tools"] == []
    assert body["error"]


def test_probe_stdio_transport_not_supported(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    """stdio needs a local process the registry cannot spawn."""
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers/probe",
        headers=tenant_headers,
        json={"transport": "stdio"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["tools"] == []
    assert "not supported" in body["error"].lower()


def test_probe_sse_transport_not_supported(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers/probe",
        headers=tenant_headers,
        json={"transport": "sse", "endpoint": "https://legacy.local/sse"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert "not supported" in body["error"].lower()


def test_probe_rejects_unknown_transport(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    """Schema validation still applies to the probe body."""
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers/probe",
        headers=tenant_headers,
        json={"transport": "carrier-pigeon"},
    )
    assert resp.status_code == 422


def test_test_endpoint_accepts_config_override(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The edit-form path: re-probe a saved server with unsaved edits.

    Fields present in the body replace the stored ones for this probe only;
    an overridden plaintext ``auth`` bypasses the stored-secret decrypt.
    """
    fake = _install_handshake_client(monkeypatch)

    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(auth={"type": "bearer", "token": "stored-tok"}),
    ).json()

    resp = app_client.post(
        f"/api/mcp/v1/mcp-servers/{created['id']}/test",
        headers=tenant_headers,
        json={
            "endpoint": "https://edited.example.com/mcp",
            "auth": {"type": "bearer", "token": "edited-tok"},
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert fake.calls
    for call in fake.calls:
        assert call["url"] == "https://edited.example.com/mcp"
        assert call["headers"]["Authorization"] == "Bearer edited-tok"


def test_test_endpoint_empty_body_probes_stored_config(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``{}`` — what the BFF forwards when the client sends no body — must
    behave exactly like no body at all."""
    fake = _install_handshake_client(monkeypatch)

    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(auth={"type": "bearer", "token": "stored-tok"}),
    ).json()

    resp = app_client.post(
        f"/api/mcp/v1/mcp-servers/{created['id']}/test",
        headers=tenant_headers,
        json={},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert fake.calls
    for call in fake.calls:
        assert call["url"] == "https://weather.example.com/mcp"
        assert call["headers"]["Authorization"] == "Bearer stored-tok"


def test_test_endpoint_no_body_probes_stored_config(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No body at all — the plain "test this saved server" button — keeps using
    the stored endpoint and the stored (decrypted) auth."""
    fake = _install_handshake_client(monkeypatch)

    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(auth={"type": "bearer", "token": "stored-tok"}),
    ).json()

    resp = app_client.post(
        f"/api/mcp/v1/mcp-servers/{created['id']}/test", headers=tenant_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert fake.calls
    for call in fake.calls:
        assert call["url"] == "https://weather.example.com/mcp"
        assert call["headers"]["Authorization"] == "Bearer stored-tok"


# --- test endpoint: stored credentials never follow a redirected endpoint ----
#
# ``/test`` is gated on ``mcp_server.read``, which the ordinary member role
# holds.  Without the guard below, ``{"endpoint": "https://attacker.example"}``
# with no auth/headers would make the registry decrypt the tenant's stored
# secret and POST it to a caller-chosen URL.


def _install_decrypt_spy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any] | None]:
    """Record every ``decrypt_auth`` call the API layer makes.

    Install *after* the server row is created so the create response's own
    decrypt does not pollute the recording — an empty list then means "no
    stored secret was ever decrypted while serving the request under test".
    """
    import mcp_registry.api.v1.mcp_servers as api_module
    from tests.conftest import _reversible_decrypt_auth

    calls: list[dict[str, Any] | None] = []

    async def _spy(auth, **kw):  # type: ignore[no-untyped-def]
        calls.append(auth)
        return await _reversible_decrypt_auth(auth, **kw)

    monkeypatch.setattr(api_module, "decrypt_auth", _spy)
    return calls


def test_test_endpoint_redirect_refuses_to_reuse_stored_auth(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Endpoint redirected + ``auth`` omitted on a server holding an api_key:
    refused before the handshake, and — the actual security property — without
    the stored secret ever being decrypted."""
    fake = _install_handshake_client(monkeypatch)

    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(
            auth={"type": "api_key", "api_key": "sk-stored", "header_name": "X-API-Key"},
        ),
    ).json()
    decrypt_calls = _install_decrypt_spy(monkeypatch)

    resp = app_client.post(
        f"/api/mcp/v1/mcp-servers/{created['id']}/test",
        headers=tenant_headers,
        json={"endpoint": "https://attacker.example/collect"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["tools"] == []
    assert "endpoint" in body["error"]
    assert "credentials" in body["error"]
    # No outbound handshake, and no decrypt: the secret never left the DB.
    assert fake.calls == []
    assert decrypt_calls == []


def test_test_endpoint_redirect_refuses_to_reuse_stored_bearer(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same guard for the bearer auth type (``token`` is the secret field)."""
    fake = _install_handshake_client(monkeypatch)

    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(auth={"type": "bearer", "token": "stored-tok"}),
    ).json()
    decrypt_calls = _install_decrypt_spy(monkeypatch)

    resp = app_client.post(
        f"/api/mcp/v1/mcp-servers/{created['id']}/test",
        headers=tenant_headers,
        json={"endpoint": "https://attacker.example/collect"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is False
    assert fake.calls == []
    assert decrypt_calls == []


def test_test_endpoint_redirect_refuses_to_reuse_stored_headers(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stored ``headers`` leak the same way (they routinely carry an
    ``Authorization`` / ``X-API-Key`` value), so omitting ``headers`` on a
    redirect is refused even when the server has no ``auth`` block at all."""
    fake = _install_handshake_client(monkeypatch)

    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(headers={"Authorization": "Bearer header-smuggled"}),
    ).json()

    resp = app_client.post(
        f"/api/mcp/v1/mcp-servers/{created['id']}/test",
        headers=tenant_headers,
        json={"endpoint": "https://attacker.example/collect"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert "credentials" in body["error"]
    assert fake.calls == []


def test_test_endpoint_redirect_with_explicit_auth_and_headers_proceeds(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legitimate edit-form case: the caller supplies both halves, so
    nothing stored is reused and the handshake runs against the new endpoint."""
    fake = _install_handshake_client(monkeypatch)

    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(
            auth={"type": "bearer", "token": "stored-tok"},
            headers={"X-Stored": "1"},
        ),
    ).json()
    decrypt_calls = _install_decrypt_spy(monkeypatch)

    resp = app_client.post(
        f"/api/mcp/v1/mcp-servers/{created['id']}/test",
        headers=tenant_headers,
        json={
            "endpoint": "https://edited.example.com/mcp",
            "auth": {"type": "bearer", "token": "edited-tok"},
            "headers": {"X-Edited": "1"},
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert fake.calls
    for call in fake.calls:
        assert call["url"] == "https://edited.example.com/mcp"
        assert call["headers"]["Authorization"] == "Bearer edited-tok"
        assert call["headers"]["X-Edited"] == "1"
        assert "X-Stored" not in call["headers"]
    # Explicit plaintext auth still bypasses the stored-secret decrypt.
    assert decrypt_calls == []


def test_test_endpoint_endpoint_override_equal_to_stored_keeps_stored_auth(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not a redirect: the admin edit form echoes the unchanged endpoint back
    when only ``timeout_seconds`` / ``tool_filter`` changed, and that must keep
    using the stored auth."""
    fake = _install_handshake_client(monkeypatch)

    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(auth={"type": "bearer", "token": "stored-tok"}),
    ).json()

    resp = app_client.post(
        f"/api/mcp/v1/mcp-servers/{created['id']}/test",
        headers=tenant_headers,
        json={"endpoint": "https://weather.example.com/mcp", "timeout_seconds": 30},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert fake.calls
    for call in fake.calls:
        assert call["url"] == "https://weather.example.com/mcp"
        assert call["headers"]["Authorization"] == "Bearer stored-tok"


def test_test_endpoint_redirect_allowed_when_nothing_to_leak(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No auth secret and no stored headers — a redirect leaks nothing, so it
    goes through unchanged."""
    fake = _install_handshake_client(monkeypatch)

    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(auth={"type": "none"}),
    ).json()

    resp = app_client.post(
        f"/api/mcp/v1/mcp-servers/{created['id']}/test",
        headers=tenant_headers,
        json={"endpoint": "https://other.example.com/mcp"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert fake.calls
    for call in fake.calls:
        assert call["url"] == "https://other.example.com/mcp"


# ---------------------------------------------------------------------------
# Phase 2: oauth auth type
# ---------------------------------------------------------------------------


def test_auth_oauth_missing_client_id_rejected(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(
            auth={
                "type": "oauth",
                "client_secret": "s",
                "token_url": "https://oauth.example.com/token",
            },
        ),
    )
    assert resp.status_code == 422


def test_auth_oauth_missing_client_secret_rejected(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(
            auth={
                "type": "oauth",
                "client_id": "cid",
                "token_url": "https://oauth.example.com/token",
            },
        ),
    )
    assert resp.status_code == 422


def test_auth_oauth_missing_token_url_rejected(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(
            auth={"type": "oauth", "client_id": "cid", "client_secret": "s"},
        ),
    )
    assert resp.status_code == 422


def test_auth_oauth_valid_created(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(
            auth={
                "type": "oauth",
                "client_id": "cid",
                "client_secret": "csec",
                "token_url": "https://oauth.example.com/token",
                "scopes": ["read", "write"],
            },
        ),
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    # Admin surface now returns masked auth; client_secret is redacted.
    assert created["auth"]["type"] == "oauth"
    assert created["auth"]["client_id"] == "cid"  # non-secret: passed through
    assert created["auth"]["client_secret"] == "••••••"
    assert created["auth"]["has_client_secret"] is True
    assert created["auth"]["token_url"] == "https://oauth.example.com/token"  # non-secret
    assert created["auth"]["scopes"] == ["read", "write"]  # non-secret


# ---------------------------------------------------------------------------
# Phase 2: secret encryption round-trip (DB ciphertext, API/internal plaintext)
# ---------------------------------------------------------------------------


def test_create_encrypts_and_read_decrypts_auth(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    """Tenant-facing endpoints return masked secrets; the internal endpoint
    returns plaintext.  DB column holds an envelope (verified at the service
    layer in test_registry_service.py).
    """
    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(auth={"type": "bearer", "token": "tok-roundtrip"}),
    ).json()
    # Tenant-facing create response: masked.
    assert created["auth"]["token"] == "••••••"
    assert created["auth"]["has_token"] is True

    fetched = app_client.get(
        f"/api/mcp/v1/mcp-servers/{created['id']}", headers=tenant_headers,
    ).json()
    # Tenant-facing GET: masked.
    assert fetched["auth"]["token"] == "••••••"
    assert fetched["auth"]["has_token"] is True

    internal = app_client.get(
        "/api/mcp/v1/internal/mcp-servers",
        params={"tenant_id": "tnt_01J5TEST00000000000000TNT1"},
        headers=internal_headers,
    ).json()
    # Internal endpoint: plaintext (not masked).
    assert internal["items"][0]["auth"]["token"] == "tok-roundtrip"


def test_update_auth_re_encrypts_secret(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(auth={"type": "bearer", "token": "tok-1"}),
    ).json()
    resp = app_client.patch(
        f"/api/mcp/v1/mcp-servers/{created['id']}",
        headers=tenant_headers,
        json={"auth": {"type": "bearer", "token": "tok-2"}},
    )
    assert resp.status_code == 200, resp.text
    # PATCH response is masked (the new secret is stored but not echoed).
    assert resp.json()["auth"]["token"] == "••••••"
    assert resp.json()["auth"]["has_token"] is True


def test_api_calls_encrypt_and_decrypt(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the API actually wires encrypt-on-write and decrypt-on-read."""
    import mcp_registry.api.v1.mcp_servers as api_module
    from tests.conftest import _reversible_decrypt_auth, _reversible_encrypt_auth

    encrypt_calls: list[dict[str, Any] | None] = []
    decrypt_calls: list[dict[str, Any] | None] = []

    async def _spy_encrypt(auth, **kw):  # type: ignore[no-untyped-def]
        encrypt_calls.append(auth)
        return await _reversible_encrypt_auth(auth, **kw)

    async def _spy_decrypt(auth, **kw):  # type: ignore[no-untyped-def]
        decrypt_calls.append(auth)
        return await _reversible_decrypt_auth(auth, **kw)

    monkeypatch.setattr(api_module, "encrypt_auth", _spy_encrypt)
    monkeypatch.setattr(api_module, "decrypt_auth", _spy_decrypt)

    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(auth={"type": "bearer", "token": "tok-spy"}),
    ).json()
    # The create response is now masked (secret is stored but not echoed).
    assert created["auth"]["token"] == "••••••"
    assert created["auth"]["has_token"] is True
    # encrypt ran on write with the plaintext auth; decrypt ran on the read
    # path that produced the create response (before masking).
    assert any(c and c.get("token") == "tok-spy" for c in encrypt_calls)
    assert decrypt_calls, "decrypt_auth must be called on the read path"


def _record_writes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record the kwargs the API hands to ``RegistryService.create`` / ``update``.

    That is exactly what reaches the DB, so it shows the at-rest form of
    ``env`` / ``headers`` without reading the in-memory engine from a sync test.
    """
    from mcp_registry.services.registry_service import RegistryService

    writes: list[dict[str, Any]] = []
    original_create, original_update = RegistryService.create, RegistryService.update

    async def _create(self, **kw):  # type: ignore[no-untyped-def]
        writes.append(kw)
        return await original_create(self, **kw)

    async def _update(self, server_id, **kw):  # type: ignore[no-untyped-def]
        writes.append(kw)
        return await original_update(self, server_id, **kw)

    monkeypatch.setattr(RegistryService, "create", _create)
    monkeypatch.setattr(RegistryService, "update", _update)
    return writes


def test_env_and_header_values_are_encrypted_at_rest(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    internal_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.conftest import _is_envelope

    writes = _record_writes(monkeypatch)
    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(env={"API_KEY": "sk-env"}, headers={"X-Api-Key": "hdr-secret"}),
    )
    assert created.status_code == 201, created.text
    stored = writes[-1]
    assert set(stored["env"]) == {"API_KEY"} and _is_envelope(stored["env"]["API_KEY"])
    assert set(stored["headers"]) == {"X-Api-Key"}
    assert _is_envelope(stored["headers"]["X-Api-Key"])
    assert "sk-env" not in str(stored) and "hdr-secret" not in str(stored)

    # The admin surface and the internal consumer endpoint see plaintext.
    body = created.json()
    assert body["env"] == {"API_KEY": "sk-env"}
    assert body["headers"] == {"X-Api-Key": "hdr-secret"}
    internal = app_client.get(
        "/api/mcp/v1/internal/mcp-servers",
        params={"tenant_id": "tnt_01J5TEST00000000000000TNT1"},
        headers=internal_headers,
    ).json()
    assert internal["items"][0]["env"] == {"API_KEY": "sk-env"}
    assert internal["items"][0]["headers"] == {"X-Api-Key": "hdr-secret"}

    patched = app_client.patch(
        f"/api/mcp/v1/mcp-servers/{body['id']}",
        headers=tenant_headers,
        json={"env": {"API_KEY": "sk-rotated"}},
    )
    assert patched.status_code == 200, patched.text
    assert _is_envelope(writes[-1]["env"]["API_KEY"])
    assert patched.json()["env"] == {"API_KEY": "sk-rotated"}


def test_secret_write_fails_closed_when_crypto_is_unavailable(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_registry.api.v1.mcp_servers as api_module
    from mcp_registry.errors import SecretEncryptionUnavailableError

    async def _down(*_a, **_kw):  # type: ignore[no-untyped-def]
        raise SecretEncryptionUnavailableError

    monkeypatch.setattr(api_module, "encrypt_values", _down)
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(env={"API_KEY": "sk-env"}),
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "MCP.CRYPTO.UNAVAILABLE"
    listed = app_client.get("/api/mcp/v1/mcp-servers", headers=tenant_headers).json()
    assert listed["total"] == 0  # nothing was written in clear


@pytest.mark.parametrize("sent_headers", [{}, None])
def test_test_endpoint_redirect_with_explicit_empty_headers_sends_none(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    sent_headers: dict[str, str] | None,
) -> None:
    """Explicitly empty headers pass the redirect guard ("no headers"), so the
    probe must not fall back to the stored map and send it to the new URL."""
    fake = _install_handshake_client(monkeypatch)

    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(headers={"Authorization": "Bearer header-smuggled"}),
    ).json()

    resp = app_client.post(
        f"/api/mcp/v1/mcp-servers/{created['id']}/test",
        headers=tenant_headers,
        json={"endpoint": "https://attacker.example/collect", "headers": sent_headers},
    )
    assert resp.status_code == 200, resp.text
    assert fake.calls
    for call in fake.calls:
        assert call["url"] == "https://attacker.example/collect"
        # Not even the envelope: the stored header must not be sent at all.
        assert "Authorization" not in call["headers"]


def test_test_endpoint_probes_with_decrypted_stored_headers(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_handshake_client(monkeypatch)
    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(headers={"X-Api-Key": "hdr-secret"}),
    ).json()
    resp = app_client.post(
        f"/api/mcp/v1/mcp-servers/{created['id']}/test", headers=tenant_headers,
    )
    assert resp.status_code == 200, resp.text
    assert fake.calls
    for call in fake.calls:
        assert call["headers"]["X-Api-Key"] == "hdr-secret"


# ---------------------------------------------------------------------------
# Phase 2: test endpoint with oauth (client-credentials flow)
# ---------------------------------------------------------------------------


def test_test_endpoint_oauth_fetches_token_and_injects_bearer(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """oauth auth: test endpoint runs the client-credentials flow against
    ``token_url`` and injects the resulting bearer token on every handshake
    request.  Crypto (encrypt/decrypt of client_secret) is handled by the
    conftest reversible mock; this test mocks the network (authz /check +
    token_url + handshake) via httpx.
    """
    import mcp_registry.services.server_tester as tester_mod

    token_posts: list[dict[str, Any]] = []
    handshake_posts: list[dict[str, Any]] = []

    token_url = "https://oauth.example.com/token"
    endpoint_url = "https://mcp.example.com/mcp"

    class _FakeResp:
        def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
            self._payload = payload
            self.status_code = status_code
            self.headers: dict[str, str] = {"content-type": "application/json"}

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise tester_mod.httpx.HTTPError(f"status {self.status_code}")

        def json(self) -> dict[str, Any]:
            return self._payload

        @property
        def text(self) -> str:
            return ""

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str] | None = None,
            json: dict[str, Any] | None = None,
            data: dict[str, str] | None = None,
        ) -> _FakeResp:
            # authz /check -> allow (guard runs on create + test endpoints).
            if url.endswith("/check"):
                return _FakeResp({"allowed": True})
            # oauth client-credentials token fetch (form-encoded body).
            if url == token_url:
                token_posts.append({"data": dict(data or {})})
                return _FakeResp({"access_token": "oauth-tok-123", "token_type": "Bearer"})
            # MCP JSON-RPC handshake.
            method = (json or {}).get("method")
            handshake_posts.append({"url": url, "json": json, "headers": headers})
            if method == "initialize":
                return _FakeResp(
                    {
                        "jsonrpc": "2.0",
                        "id": (json or {}).get("id"),
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": {"name": "oauth-mcp", "version": "1.0"},
                        },
                    },
                )
            if method == "notifications/initialized":
                return _FakeResp({}, status_code=202)
            if method == "tools/list":
                return _FakeResp(
                    {
                        "jsonrpc": "2.0",
                        "id": (json or {}).get("id"),
                        "result": {"tools": [{"name": "ping"}]},
                    },
                )
            return _FakeResp({}, status_code=404)

    monkeypatch.setattr(tester_mod.httpx, "AsyncClient", lambda *a, **kw: _FakeAsyncClient())

    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(
            endpoint=endpoint_url,
            auth={
                "type": "oauth",
                "client_id": "cid",
                "client_secret": "csec",
                "token_url": token_url,
                "scopes": ["read", "write"],
            },
        ),
    ).json()
    resp = app_client.post(
        f"/api/mcp/v1/mcp-servers/{created['id']}/test", headers=tenant_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True, body
    assert body["server_info"] == {"name": "oauth-mcp", "version": "1.0"}
    assert [t["name"] for t in body["tools"]] == ["ping"]

    # Token fetch used the client-credentials grant with the decrypted
    # client_secret and space-joined scopes.
    assert len(token_posts) == 1
    form = token_posts[0]["data"]
    assert form["grant_type"] == "client_credentials"
    assert form["client_id"] == "cid"
    assert form["client_secret"] == "csec"  # decrypted before the fetch
    assert form["scope"] == "read write"

    # The fetched bearer token was injected on every handshake request.
    assert handshake_posts, "handshake must have run after the token fetch"
    for call in handshake_posts:
        assert call["headers"]["Authorization"] == "Bearer oauth-tok-123"
        assert call["headers"]["MCP-Protocol-Version"] == "2024-11-05"


def test_test_endpoint_oauth_token_fetch_failure(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the token endpoint is unreachable the test fails with ok=false."""
    import mcp_registry.services.server_tester as tester_mod

    token_url = "https://oauth.example.com/token"
    endpoint_url = "https://mcp.example.com/mcp"

    class _FakeResp:
        def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
            self._payload = payload
            self.status_code = status_code
            self.headers: dict[str, str] = {"content-type": "application/json"}

        def json(self) -> dict[str, Any]:
            return self._payload

        @property
        def text(self) -> str:
            return ""

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise tester_mod.httpx.HTTPError(f"status {self.status_code}")

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str] | None = None,
            json: dict[str, Any] | None = None,
            data: dict[str, str] | None = None,
        ) -> _FakeResp:
            if url.endswith("/check"):
                return _FakeResp({"allowed": True})
            if url == token_url:
                # Token endpoint returns 500 -> fetch fails.
                return _FakeResp({}, status_code=500)
            return _FakeResp({}, status_code=404)

    monkeypatch.setattr(tester_mod.httpx, "AsyncClient", lambda *a, **kw: _FakeAsyncClient())

    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(
            endpoint=endpoint_url,
            auth={
                "type": "oauth",
                "client_id": "cid",
                "client_secret": "csec",
                "token_url": token_url,
            },
        ),
    ).json()
    resp = app_client.post(
        f"/api/mcp/v1/mcp-servers/{created['id']}/test", headers=tenant_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert "oauth token fetch failed" in body["error"]


# ---------------------------------------------------------------------------
# Auth masking — tenant-facing vs internal surface
# ---------------------------------------------------------------------------


def test_list_returns_masked_bearer_auth(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    """GET /mcp-servers (tenant-facing) masks bearer token."""
    app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(auth={"type": "bearer", "token": "real-secret"}),
    )
    listed = app_client.get("/api/mcp/v1/mcp-servers", headers=tenant_headers).json()
    assert listed["total"] == 1
    auth = listed["items"][0]["auth"]
    assert auth["token"] == "••••••"
    assert auth["has_token"] is True
    assert auth["type"] == "bearer"


def test_list_returns_masked_api_key_auth(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    """GET /mcp-servers (tenant-facing) masks api_key."""
    app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(
            auth={"type": "api_key", "api_key": "sk-real-key", "header_name": "X-API-Key"},
        ),
    )
    listed = app_client.get("/api/mcp/v1/mcp-servers", headers=tenant_headers).json()
    auth = listed["items"][0]["auth"]
    assert auth["api_key"] == "••••••"
    assert auth["has_api_key"] is True
    assert auth["header_name"] == "X-API-Key"  # non-secret: passed through


def test_list_returns_masked_oauth_auth(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    """GET /mcp-servers (tenant-facing) masks oauth client_secret."""
    app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(
            auth={
                "type": "oauth",
                "client_id": "cid",
                "client_secret": "real-secret",
                "token_url": "https://oauth.example.com/token",
            },
        ),
    )
    listed = app_client.get("/api/mcp/v1/mcp-servers", headers=tenant_headers).json()
    auth = listed["items"][0]["auth"]
    assert auth["client_secret"] == "••••••"
    assert auth["has_client_secret"] is True
    assert auth["client_id"] == "cid"  # non-secret
    assert auth["token_url"] == "https://oauth.example.com/token"  # non-secret


def test_get_returns_masked_auth(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    """GET /mcp-servers/{id} (tenant-facing) masks the secret."""
    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(
            auth={"type": "api_key", "api_key": "sk-real-key", "header_name": "X-API-Key"},
        ),
    ).json()
    fetched = app_client.get(
        f"/api/mcp/v1/mcp-servers/{created['id']}", headers=tenant_headers,
    ).json()
    assert fetched["auth"]["api_key"] == "••••••"
    assert fetched["auth"]["has_api_key"] is True


def test_internal_endpoint_returns_plaintext_auth(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    """The /internal/mcp-servers endpoint returns plaintext (not masked)."""
    app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(
            auth={"type": "api_key", "api_key": "sk-real-key", "header_name": "X-API-Key"},
        ),
    )
    resp = app_client.get(
        "/api/mcp/v1/internal/mcp-servers",
        params={"tenant_id": "tnt_01J5TEST00000000000000TNT1"},
        headers=internal_headers,
    )
    assert resp.status_code == 200, resp.text
    auth = resp.json()["items"][0]["auth"]
    # Plaintext, NOT the masked sentinel.
    assert auth["api_key"] == "sk-real-key"
    assert auth["api_key"] != "••••••"
    # has_* fields are NOT present on the internal surface.
    assert "has_api_key" not in auth


# ---------------------------------------------------------------------------
# PATCH with masked sentinel — preserves stored secret
# ---------------------------------------------------------------------------


def test_patch_masked_sentinel_preserves_stored_bearer(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    """PATCH with token="••••••" preserves the original stored secret.

    The admin edits non-secret fields without retyping the token; the
    frontend sends the masked sentinel back. The PATCH handler detects
    the sentinel and keeps the stored (encrypted) value unchanged.
    """
    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(auth={"type": "bearer", "token": "original-secret"}),
    ).json()

    # PATCH with masked sentinel + a changed endpoint.
    resp = app_client.patch(
        f"/api/mcp/v1/mcp-servers/{created['id']}",
        headers=tenant_headers,
        json={
            "endpoint": "https://new-endpoint.example.com/mcp",
            "auth": {"type": "bearer", "token": "••••••"},
        },
    )
    assert resp.status_code == 200, resp.text
    patched = resp.json()
    assert patched["auth"]["token"] == "••••••"  # PATCH response is masked
    assert patched["auth"]["has_token"] is True
    assert patched["endpoint"] == "https://new-endpoint.example.com/mcp"

    # Verify via the internal endpoint that the real secret is preserved.
    internal = app_client.get(
        "/api/mcp/v1/internal/mcp-servers",
        params={"tenant_id": "tnt_01J5TEST00000000000000TNT1"},
        headers=internal_headers,
    ).json()
    assert internal["items"][0]["auth"]["token"] == "original-secret"


def test_patch_new_plaintext_secret_replaces_stored(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    """PATCH with a real (non-sentinel) token replaces the stored one."""
    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(auth={"type": "bearer", "token": "old-tok"}),
    ).json()

    resp = app_client.patch(
        f"/api/mcp/v1/mcp-servers/{created['id']}",
        headers=tenant_headers,
        json={"auth": {"type": "bearer", "token": "new-tok"}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["auth"]["token"] == "••••••"  # still masked in response

    # Internal endpoint shows the new plaintext.
    internal = app_client.get(
        "/api/mcp/v1/internal/mcp-servers",
        params={"tenant_id": "tnt_01J5TEST00000000000000TNT1"},
        headers=internal_headers,
    ).json()
    assert internal["items"][0]["auth"]["token"] == "new-tok"


def test_patch_masked_oauth_preserves_client_secret(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    """PATCH with client_secret="••••••" preserves the stored oauth secret."""
    created = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(
            auth={
                "type": "oauth",
                "client_id": "cid",
                "client_secret": "real-csec",
                "token_url": "https://oauth.example.com/token",
            },
        ),
    ).json()

    # Edit the token_url but keep client_secret masked.
    resp = app_client.patch(
        f"/api/mcp/v1/mcp-servers/{created['id']}",
        headers=tenant_headers,
        json={
            "auth": {
                "type": "oauth",
                "client_id": "cid",
                "client_secret": "••••••",
                "token_url": "https://new-oauth.example.com/token",
            },
        },
    )
    assert resp.status_code == 200, resp.text
    patched_auth = resp.json()["auth"]
    assert patched_auth["client_secret"] == "••••••"
    assert patched_auth["has_client_secret"] is True
    assert patched_auth["token_url"] == "https://new-oauth.example.com/token"

    # Internal endpoint confirms the original secret is intact.
    internal = app_client.get(
        "/api/mcp/v1/internal/mcp-servers",
        params={"tenant_id": "tnt_01J5TEST00000000000000TNT1"},
        headers=internal_headers,
    ).json()
    assert internal["items"][0]["auth"]["client_secret"] == "real-csec"
    assert internal["items"][0]["auth"]["token_url"] == "https://new-oauth.example.com/token"


def test_auth_none_not_masked(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    """auth type 'none' is returned unchanged (no has_* fields)."""
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers",
        headers=tenant_headers,
        json=_http_payload(auth={"type": "none"}),
    )
    assert resp.status_code == 201
    assert resp.json()["auth"] == {"type": "none"}


# ---------------------------------------------------------------------------
# Phase 3: discovered_tools persistence (migration 0004)
# ---------------------------------------------------------------------------


def test_list_includes_discovered_tools_field(
    app_client: TestClient, tenant_headers: dict[str, str],
) -> None:
    """GET /mcp-servers response includes the ``discovered_tools`` field,
    defaulting to an empty list on a freshly created server (no test run yet)."""
    created = app_client.post(
        "/api/mcp/v1/mcp-servers", headers=tenant_headers, json=_http_payload(),
    ).json()
    assert created["discovered_tools"] == []

    listed = app_client.get("/api/mcp/v1/mcp-servers", headers=tenant_headers).json()
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == created["id"]
    assert listed["items"][0]["discovered_tools"] == []

    # Single-server GET also surfaces the field.
    fetched = app_client.get(
        f"/api/mcp/v1/mcp-servers/{created['id']}", headers=tenant_headers,
    ).json()
    assert fetched["discovered_tools"] == []


def test_test_endpoint_success_persists_discovered_tools(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful /test handshake writes the upstream's tool list to the
    ``discovered_tools`` column.  The list endpoint then surfaces the tools
    from the DB without any further probe."""
    _install_handshake_client(monkeypatch)

    created = app_client.post(
        "/api/mcp/v1/mcp-servers", headers=tenant_headers, json=_http_payload(),
    ).json()
    # Before the test, the column is empty.
    before = app_client.get(
        f"/api/mcp/v1/mcp-servers/{created['id']}", headers=tenant_headers,
    ).json()
    assert before["discovered_tools"] == []

    resp = app_client.post(
        f"/api/mcp/v1/mcp-servers/{created['id']}/test", headers=tenant_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert [t["name"] for t in body["tools"]] == ["search", "fetch"]

    # After the test, the list endpoint reads the persisted tools from the DB.
    listed = app_client.get("/api/mcp/v1/mcp-servers", headers=tenant_headers).json()
    assert listed["items"][0]["discovered_tools"] == [
        {"name": "search", "description": "搜索"},
        {"name": "fetch", "description": None},
    ]

    # Single-server GET also surfaces the persisted tools.
    fetched = app_client.get(
        f"/api/mcp/v1/mcp-servers/{created['id']}", headers=tenant_headers,
    ).json()
    assert fetched["discovered_tools"] == [
        {"name": "search", "description": "搜索"},
        {"name": "fetch", "description": None},
    ]


def test_test_endpoint_failure_preserves_existing_discovered_tools(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed /test handshake must NOT wipe the previously stored tool list.
    A transient upstream error should leave the last known-good catalog intact
    so the admin console doesn't lose its tool picker data on a blip."""
    # 1. Successful test -> populates discovered_tools.
    _install_handshake_client(monkeypatch)
    created = app_client.post(
        "/api/mcp/v1/mcp-servers", headers=tenant_headers, json=_http_payload(),
    ).json()
    resp = app_client.post(
        f"/api/mcp/v1/mcp-servers/{created['id']}/test", headers=tenant_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    # Confirm tools were persisted.
    stored = app_client.get(
        f"/api/mcp/v1/mcp-servers/{created['id']}", headers=tenant_headers,
    ).json()
    assert len(stored["discovered_tools"]) == 2

    # 2. Swap to a boom client -> the next test fails with a transport error.
    import mcp_registry.services.server_tester as tester_mod

    class _BoomClient:
        async def __aenter__(self) -> _BoomClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, json: dict[str, Any], **kw: Any) -> Any:
            if "method" not in json:
                from types import SimpleNamespace
                return SimpleNamespace(status_code=200, json=lambda: {"allowed": True})
            raise tester_mod.httpx.ConnectError("boom")

    monkeypatch.setattr(tester_mod.httpx, "AsyncClient", lambda *a, **kw: _BoomClient())

    resp = app_client.post(
        f"/api/mcp/v1/mcp-servers/{created['id']}/test", headers=tenant_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["tools"] == []

    # 3. The stored discovered_tools are unchanged - the failure did not clear.
    after = app_client.get(
        f"/api/mcp/v1/mcp-servers/{created['id']}", headers=tenant_headers,
    ).json()
    assert len(after["discovered_tools"]) == 2
    assert [t["name"] for t in after["discovered_tools"]] == ["search", "fetch"]


def test_probe_does_not_persist_discovered_tools(
    app_client: TestClient,
    tenant_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The id-less /probe endpoint is a pure read: it returns the tool list
    but never writes to the DB (no server row exists to write to anyway).

    This complements ``test_probe_returns_tool_list_without_persisting`` by
    also asserting that no server row was created as a side effect."""
    _install_handshake_client(monkeypatch)
    resp = app_client.post(
        "/api/mcp/v1/mcp-servers/probe",
        headers=tenant_headers,
        json={"transport": "http", "endpoint": "https://probe.example.com/mcp"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert len(resp.json()["tools"]) == 2

    listed = app_client.get("/api/mcp/v1/mcp-servers", headers=tenant_headers).json()
    assert listed["total"] == 0

