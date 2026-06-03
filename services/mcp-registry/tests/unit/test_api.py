"""HTTP-layer tests for mcp-registry, end-to-end against in-memory SQLite."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _http_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "weather",
        "transport": "http",
        "endpoint": "https://weather.example.com/mcp",
        "scopes": ["read:weather"],
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
    assert created["scopes"] == ["read:weather"]

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
        headers={"X-Service-Scopes": "mcp.read_internal,skill.read_internal"},
    )
    assert resp.status_code == 200, resp.text


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
