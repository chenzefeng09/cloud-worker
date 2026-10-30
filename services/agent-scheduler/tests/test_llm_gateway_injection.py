"""Unit tests for M1-P3 LLM gateway env injection.

Covers:
* ``_fetch_tenant_virtual_key`` — key found, key absent, gateway unreachable.
* ``create_run`` end-to-end — sandbox spawn is fail-closed unless the
  gateway returns a tenant virtual key (gateway mode is unconditional).
* No direct provider key ever reaches the sandbox.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_scheduler.api.v1.runs import (
    _fetch_tenant_virtual_key,
    _mint_run_virtual_key,
    _revoke_run_virtual_keys,
)
from agent_scheduler.config import Settings
from agent_scheduler.errors import LlmNotConfiguredError

# Mirrors conftest._TENANT — the tenant header the gateway injects.
_TENANT = "tnt_01J5TEST00000000000000TNT1"


def _read_run_secrets(
    workspace_base: str, *, tenant_id: str, session_id: str, run_id: str
) -> dict[str, str]:
    """Secrets are delivered via /workspace/.secrets.json (0600), not env."""
    path = (
        Path(workspace_base) / tenant_id / session_id / run_id / ".secrets.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))

# ---------------------------------------------------------------------------
# _fetch_tenant_virtual_key unit tests
# ---------------------------------------------------------------------------

class _MockHTTPX:
    """Minimal async context-manager httpx.AsyncClient stand-in."""

    def __init__(self, status_code: int, body: Any) -> None:
        self._status_code = status_code
        self._body = body

    async def __aenter__(self) -> _MockHTTPX:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return httpx.Response(self._status_code, json=self._body)

    async def post(self, url: str, **kw: Any) -> httpx.Response:
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
# _mint_run_virtual_key unit tests (short-lived, run-scoped keys)
# ---------------------------------------------------------------------------


def _gw_settings() -> Settings:
    return Settings(
        env="test",
        scheduler_service_token="tok",
        otel_enabled=False,
        llm_gateway_url="http://llm-gateway:8011/api/llm-gateway/v1",
    )


@pytest.mark.asyncio
async def test_mint_run_key_returns_ephemeral_virtual_key(monkeypatch):
    """A successful mint returns the gateway's ephemeral virtual_key."""
    captured: dict[str, Any] = {}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, url, **kw):
            captured["url"] = url
            captured["json"] = kw.get("json")
            return httpx.Response(
                201,
                json={
                    "virtual_key": "sk-foundation-eph-XYZ",
                    "run_id": "run_MINT1",
                    "ephemeral": True,
                    "expires_at": "2026-06-15T00:10:00+00:00",
                },
            )

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())

    result = await _mint_run_virtual_key(
        settings=_gw_settings(),
        tenant_id="tnt_test",
        run_id="run_MINT1",
        bearer="tok",
    )
    assert result == "sk-foundation-eph-XYZ"
    assert captured["url"].endswith("/tenant-keys/ephemeral")
    assert captured["json"]["run_id"] == "run_MINT1"
    # TTL is derived from sandbox_timeout (+grace) and clamped to a sane window.
    assert captured["json"]["ttl_seconds"] >= 60


@pytest.mark.asyncio
async def test_mint_run_key_returns_none_on_404_older_gateway(monkeypatch):
    """Older gateway without the ephemeral endpoint (404) → None → caller falls back."""
    mock_client = _MockHTTPX(404, {"detail": "not found"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: mock_client)

    result = await _mint_run_virtual_key(
        settings=_gw_settings(),
        tenant_id="tnt_test",
        run_id="run_OLD",
        bearer="tok",
    )
    assert result is None


@pytest.mark.asyncio
async def test_mint_run_key_raises_when_tenant_has_no_provider(monkeypatch):
    """The gateway's own 404 (a problem body with its code, unlike an older
    gateway's bare 404) means the tenant has no enabled provider: no fallback
    key could route a model call either."""
    mock_client = _MockHTTPX(404, {
        "status": 404,
        "code": "FOUNDATION.LLM_GATEWAY.PROVIDER.NOT_FOUND",
        "detail": "No enabled provider to bind ephemeral key for tenant='tnt_test'",
    })
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: mock_client)

    with pytest.raises(LlmNotConfiguredError):
        await _mint_run_virtual_key(
            settings=_gw_settings(),
            tenant_id="tnt_test",
            run_id="run_NOPROV",
            bearer="tok",
        )


@pytest.mark.asyncio
async def test_mint_run_key_returns_none_when_unreachable(monkeypatch):
    """Network error during mint → None (caller falls back to the tenant key)."""

    class _Unreachable:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, *a, **kw):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Unreachable())

    result = await _mint_run_virtual_key(
        settings=_gw_settings(),
        tenant_id="tnt_test",
        run_id="run_DOWN",
        bearer="tok",
    )
    assert result is None


@pytest.mark.asyncio
async def test_mint_run_key_returns_none_when_url_empty():
    """Empty gateway URL short-circuits to None."""
    settings = Settings(
        env="test", scheduler_service_token="tok",
        otel_enabled=False, llm_gateway_url="",
    )
    result = await _mint_run_virtual_key(
        settings=settings, tenant_id="tnt", run_id="run_X", bearer="tok",
    )
    assert result is None


@pytest.mark.asyncio
async def test_revoke_run_keys_swallows_errors(monkeypatch):
    """Revoke is best-effort: a gateway failure must not raise (TTL still applies)."""

    class _Unreachable:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, *a, **kw):
            raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Unreachable())

    # Must not raise.
    await _revoke_run_virtual_keys(
        settings=_gw_settings(),
        tenant_id="tnt_test",
        run_id="run_REVOKE",
        bearer="tok",
    )


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


def test_create_run_rejects_missing_virtual_key(
    settings: Settings,
    runtime,
    mock_docker_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict,
) -> None:
    """create_run refuses to start when the tenant has no gateway key."""
    from agent_scheduler.api.dependencies import get_runtime
    from agent_scheduler.main import create_app

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
    assert resp.json()["code"] == "AGENT.SCHEDULER.LLM_GATEWAY_UNAVAILABLE"
    mock_docker_client.containers.run.assert_not_called()


def test_create_run_injects_virtual_key_when_gateway_has_one(
    settings: Settings,
    runtime,
    mock_docker_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict,
) -> None:
    """LLM_VIRTUAL_KEY must be injected when the gateway returns a key."""
    from agent_scheduler.api.dependencies import get_runtime
    from agent_scheduler.main import create_app

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
    # The virtual key is a secret → delivered via /workspace/.secrets.json,
    # never the container env.  No direct provider key exists at all.
    assert "LLM_VIRTUAL_KEY" not in env
    assert "LLM_API_KEY" not in env
    secrets = _read_run_secrets(
        settings.workspace_base,
        tenant_id=_TENANT,
        session_id="ses_01J5SES0000000000000GW011",
        run_id="run_01J5RUN0000000000000GW011",
    )
    assert secrets["LLM_VIRTUAL_KEY"] == "vk-tenant-injected"
    assert "LLM_API_KEY" not in secrets
    # Non-secret gateway routing stays in env.
    assert "LLM_GATEWAY_URL" in env
    assert "RUNTIME_EXTENSIONS" not in env


def _make_minting_gateway_client(
    *, mint_status: int, mint_body: Any, tenant_keys: Any,
):
    """Gateway stub that distinguishes ephemeral mint from the tenant-key list.

    ``POST /tenant-keys/ephemeral`` → (mint_status, mint_body).
    ``GET  /tenant-keys``           → (200, tenant_keys)  [fallback path].
    Identity/authz/quota POSTs are stubbed as usual.
    """

    class _FakeClient:
        def __init__(self, *args, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url: str, **kw):
            return httpx.Response(200, json=tenant_keys)

        async def post(self, url: str, **kw):
            if url.endswith("/tenant-keys/ephemeral"):
                return httpx.Response(mint_status, json=mint_body)
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

    return _FakeClient


def test_create_run_prefers_minted_ephemeral_key(
    settings: Settings,
    runtime,
    mock_docker_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict,
) -> None:
    """When the gateway mints an ephemeral key, the sandbox gets THAT key,
    never the long-lived tenant key."""
    from agent_scheduler.api.dependencies import get_runtime
    from agent_scheduler.main import create_app

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _make_minting_gateway_client(
            mint_status=201,
            mint_body={"virtual_key": "sk-foundation-eph-RUN", "ephemeral": True},
            # A long-lived tenant key is available too; it must NOT be chosen.
            tenant_keys=[{"key": "vk-tenant-LONGLIVED", "enabled": True}],
        ),
    )
    mock_docker_client.containers.run.return_value = MagicMock(id="cid-eph")

    app = create_app(settings)
    app.state.settings = settings
    app.dependency_overrides[get_runtime] = lambda: runtime

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/scheduler/v1/runs",
            headers=auth_headers,
            json={
                "session_id": "ses_01J5SES0000000000000EPH01",
                "run_id": "run_01J5RUN0000000000000EPH01",
                "task": "ephemeral key preferred",
            },
        )

    assert resp.status_code == 201, resp.text
    secrets = _read_run_secrets(
        settings.workspace_base,
        tenant_id=_TENANT,
        session_id="ses_01J5SES0000000000000EPH01",
        run_id="run_01J5RUN0000000000000EPH01",
    )
    # The short-lived ephemeral key is injected; the long-lived tenant key is not.
    assert secrets["LLM_VIRTUAL_KEY"] == "sk-foundation-eph-RUN"


def test_create_run_falls_back_to_tenant_key_when_mint_unavailable(
    settings: Settings,
    runtime,
    mock_docker_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict,
) -> None:
    """Older gateway (mint 404) → fall back to the long-lived tenant key so
    runs don't break."""
    from agent_scheduler.api.dependencies import get_runtime
    from agent_scheduler.main import create_app

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _make_minting_gateway_client(
            mint_status=404,
            mint_body={"detail": "not found"},
            tenant_keys=[{"key": "vk-tenant-FALLBACK", "enabled": True}],
        ),
    )
    mock_docker_client.containers.run.return_value = MagicMock(id="cid-fallback")

    app = create_app(settings)
    app.state.settings = settings
    app.dependency_overrides[get_runtime] = lambda: runtime

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/scheduler/v1/runs",
            headers=auth_headers,
            json={
                "session_id": "ses_01J5SES0000000000000FBK01",
                "run_id": "run_01J5RUN0000000000000FBK01",
                "task": "fallback to tenant key",
            },
        )

    assert resp.status_code == 201, resp.text
    secrets = _read_run_secrets(
        settings.workspace_base,
        tenant_id=_TENANT,
        session_id="ses_01J5SES0000000000000FBK01",
        run_id="run_01J5RUN0000000000000FBK01",
    )
    assert secrets["LLM_VIRTUAL_KEY"] == "vk-tenant-FALLBACK"


def test_create_run_refuses_tenant_without_llm_provider(
    settings: Settings,
    runtime,
    mock_docker_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict,
) -> None:
    """A tenant with no enabled provider gets a typed, non-retryable refusal,
    not the gateway-outage 502, and no fallback to a long-lived key."""
    from agent_scheduler.api.dependencies import get_runtime
    from agent_scheduler.main import create_app

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _make_minting_gateway_client(
            mint_status=404,
            mint_body={
                "status": 404,
                "code": "FOUNDATION.LLM_GATEWAY.PROVIDER.NOT_FOUND",
                "detail": "No enabled provider to bind ephemeral key",
            },
            # A key bound to a since-disabled provider: it must not be used.
            tenant_keys=[{"key": "vk-tenant-ORPHAN", "enabled": True}],
        ),
    )
    mock_docker_client.containers.run.return_value = MagicMock(id="cid-noprov")

    app = create_app(settings)
    app.state.settings = settings
    app.dependency_overrides[get_runtime] = lambda: runtime

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/scheduler/v1/runs",
            headers=auth_headers,
            json={
                "session_id": "ses_01J5SES0000000000000NOP01",
                "run_id": "run_01J5RUN0000000000000NOP01",
                "task": "tenant without a provider",
            },
        )

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "AGENT.SCHEDULER.LLM_NOT_CONFIGURED"
    mock_docker_client.containers.run.assert_not_called()


def test_create_run_rejects_when_gateway_down(
    settings: Settings,
    runtime,
    mock_docker_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict,
) -> None:
    """Gateway outage must not fall back to direct provider credentials."""
    from agent_scheduler.api.dependencies import get_runtime
    from agent_scheduler.main import create_app

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
    assert resp.json()["code"] == "AGENT.SCHEDULER.LLM_GATEWAY_UNAVAILABLE"
    mock_docker_client.containers.run.assert_not_called()
