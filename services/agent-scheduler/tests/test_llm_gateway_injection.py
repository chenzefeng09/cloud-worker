"""Unit tests for M1-P3 LLM gateway env injection.

Covers:
* ``_fetch_tenant_virtual_key`` — key found, key absent, gateway unreachable.
* ``create_run`` end-to-end — with the default enforcer enabled, sandbox
  spawn is fail-closed unless the gateway returns a tenant virtual key.
* Direct provider keys are blanked from the sandbox in gateway mode.
* RUNTIME_EXTENSIONS default contains llm_gateway_enforcer.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_scheduler.api.v1.runs import _fetch_tenant_virtual_key
from agent_scheduler.config import Settings


# ---------------------------------------------------------------------------
# _fetch_tenant_virtual_key unit tests
# ---------------------------------------------------------------------------

class _MockHTTPX:
    """Minimal async context-manager httpx.AsyncClient stand-in."""

    def __init__(self, status_code: int, body: Any) -> None:
        self._status_code = status_code
        self._body = body

    async def __aenter__(self) -> "_MockHTTPX":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return httpx.Response(self._status_code, json=self._body)


@pytest.mark.asyncio
async def test_fetch_virtual_key_returns_first_enabled_key(monkeypatch):
    """When the gateway returns a list of key objects, we pick the first enabled one."""
    mock_client = _MockHTTPX(200, [
        {"key": "vk-abc", "enabled": True},
        {"key": "vk-def", "enabled": True},
    ])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: mock_client)

    settings = Settings(
        env="test",
        scheduler_service_token="tok",
        otel_enabled=False,
        llm_gateway_url="http://llm-gateway:8011/api/llm-gateway/v1",
    )
    result = await _fetch_tenant_virtual_key(
        settings=settings,
        tenant_id="tnt_test",
        bearer="tok",
    )
    assert result == "vk-abc"


@pytest.mark.asyncio
async def test_fetch_virtual_key_returns_none_when_list_empty(monkeypatch):
    """Empty list → None (no key configured for tenant)."""
    mock_client = _MockHTTPX(200, [])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: mock_client)

    settings = Settings(
        env="test",
        scheduler_service_token="tok",
        otel_enabled=False,
        llm_gateway_url="http://llm-gateway:8011/api/llm-gateway/v1",
    )
    result = await _fetch_tenant_virtual_key(
        settings=settings,
        tenant_id="tnt_no_keys",
        bearer="tok",
    )
    assert result is None


@pytest.mark.asyncio
async def test_fetch_virtual_key_returns_none_on_gateway_error(monkeypatch):
    """Non-200 HTTP status → None; the caller decides whether to fail-closed."""
    mock_client = _MockHTTPX(404, {"detail": "not found"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: mock_client)

    settings = Settings(
        env="test",
        scheduler_service_token="tok",
        otel_enabled=False,
        llm_gateway_url="http://llm-gateway:8011/api/llm-gateway/v1",
    )
    result = await _fetch_tenant_virtual_key(
        settings=settings,
        tenant_id="tnt_err",
        bearer="tok",
    )
    assert result is None


@pytest.mark.asyncio
async def test_fetch_virtual_key_returns_none_when_gateway_unreachable(monkeypatch):
    """Network error → None; the caller decides whether to fail-closed."""

    class _UnreachableClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kw):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _UnreachableClient())

    settings = Settings(
        env="test",
        scheduler_service_token="tok",
        otel_enabled=False,
        llm_gateway_url="http://llm-gateway:8011/api/llm-gateway/v1",
    )
    result = await _fetch_tenant_virtual_key(
        settings=settings,
        tenant_id="tnt_unreachable",
        bearer="tok",
    )
    assert result is None


@pytest.mark.asyncio
async def test_fetch_virtual_key_returns_none_when_url_empty():
    """When llm_gateway_url is empty the function short-circuits and returns None."""
    settings = Settings(
        env="test",
        scheduler_service_token="tok",
        otel_enabled=False,
        llm_gateway_url="",
    )
    result = await _fetch_tenant_virtual_key(
        settings=settings,
        tenant_id="tnt_noop",
        bearer="tok",
    )
    assert result is None


# ---------------------------------------------------------------------------
# create_run end-to-end: gateway URL always present, virtual key conditional
# ---------------------------------------------------------------------------


def _make_gateway_client(status_code: int, body: Any):
    """Build a fake httpx.AsyncClient that routes GET /tenant-keys to a stub."""

    class _FakeClient:
        def __init__(self, *args, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url: str, **kw):
            return httpx.Response(status_code, json=body)

        async def post(self, url: str, **kw):
            # Identity stub — always returns a minted token
            if "service-account-token" in url:
                return httpx.Response(201, json={"access_token": "minted-jwt"})
            # Authz / quota stubs
            if "/authz/" in url or "/check" in url:
                return httpx.Response(200, json={"allowed": True})
            if "/quota/" in url:
                return httpx.Response(
                    200,
                    json={"allowed": True, "dimension": "agent_sessions", "remaining": 100},
                )
            return httpx.Response(200, json={})

    return _FakeClient


def test_create_run_rejects_missing_virtual_key_when_enforcer_enabled(
    settings: Settings,
    runtime,
    mock_docker_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict,
) -> None:
    """Default enforcer mode refuses to start when the tenant has no gateway key."""
    from agent_scheduler.main import create_app
    from agent_scheduler.api.dependencies import get_runtime

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _make_gateway_client(200, []),  # empty key list
    )
    mock_docker_client.containers.run.return_value = MagicMock(id="cid-gw-always")

    app = create_app(settings)
    app.state.settings = settings
    app.dependency_overrides[get_runtime] = lambda: runtime

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/scheduler/v1/runs",
            headers=auth_headers,
            json={
                "session_id": "ses_01J5SES0000000000000GW010",
                "run_id": "run_01J5RUN0000000000000GW010",
                "task": "gateway url test",
            },
        )

    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"] == "LLM gateway virtual key unavailable for tenant"
    mock_docker_client.containers.run.assert_not_called()


def test_create_run_injects_virtual_key_when_gateway_has_one(
    settings: Settings,
    runtime,
    mock_docker_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict,
) -> None:
    """LLM_VIRTUAL_KEY must be injected when the gateway returns a key."""
    from agent_scheduler.main import create_app
    from agent_scheduler.api.dependencies import get_runtime

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _make_gateway_client(200, [{"key": "vk-tenant-injected", "enabled": True}]),
    )
    mock_docker_client.containers.run.return_value = MagicMock(id="cid-vk-present")

    app = create_app(settings)
    app.state.settings = settings
    app.dependency_overrides[get_runtime] = lambda: runtime

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/scheduler/v1/runs",
            headers=auth_headers,
            json={
                "session_id": "ses_01J5SES0000000000000GW011",
                "run_id": "run_01J5RUN0000000000000GW011",
                "task": "virtual key test",
            },
        )

    assert resp.status_code == 201, resp.text
    env = mock_docker_client.containers.run.call_args.kwargs["environment"]
    assert env.get("LLM_VIRTUAL_KEY") == "vk-tenant-injected"
    assert env.get("LLM_API_KEY") == ""
    assert "LLM_GATEWAY_URL" in env
    assert "llm_gateway_enforcer" in env.get("RUNTIME_EXTENSIONS", "")


def test_create_run_rejects_when_gateway_down(
    settings: Settings,
    runtime,
    mock_docker_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict,
) -> None:
    """Gateway outage must not fall back to direct provider credentials."""
    from agent_scheduler.main import create_app
    from agent_scheduler.api.dependencies import get_runtime

    class _PartiallyDown:
        def __init__(self, *args, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url: str, **kw):
            raise httpx.ConnectError("gateway down")

        async def post(self, url: str, **kw):
            if "service-account-token" in url:
                return httpx.Response(201, json={"access_token": "minted-jwt"})
            if "/authz/" in url or "/check" in url:
                return httpx.Response(200, json={"allowed": True})
            if "/quota/" in url:
                return httpx.Response(
                    200,
                    json={"allowed": True, "dimension": "agent_sessions", "remaining": 100},
                )
            return httpx.Response(200, json={})

    monkeypatch.setattr(httpx, "AsyncClient", _PartiallyDown)
    mock_docker_client.containers.run.return_value = MagicMock(id="cid-gw-down")

    app = create_app(settings)
    app.state.settings = settings
    app.dependency_overrides[get_runtime] = lambda: runtime

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/scheduler/v1/runs",
            headers=auth_headers,
            json={
                "session_id": "ses_01J5SES0000000000000GW012",
                "run_id": "run_01J5RUN0000000000000GW012",
                "task": "gateway down test",
            },
        )

    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"] == "LLM gateway virtual key unavailable for tenant"
    mock_docker_client.containers.run.assert_not_called()
