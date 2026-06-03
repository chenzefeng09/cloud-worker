"""Test fixtures for agent-scheduler."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_scheduler.config import Settings
from agent_scheduler.main import create_app
from agent_scheduler.services.docker_runtime import DockerRuntime

_TENANT = "tnt_01J5TEST00000000000000TNT1"
_USER = "usr_01J5TEST0000000000000USER1"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        agent_image="hello-world:latest",
        workspace_base="/tmp/agent-platform-test",
        docker_network="none",
        foundation_identity_url="http://identity-test/api/identity/v1",
        scheduler_service_token="fake-service-token",
        otel_enabled=False,
    )


@pytest.fixture
def mock_docker_client() -> MagicMock:
    """A docker.DockerClient stand-in.

    The DockerRuntime accesses ``self.client.containers`` which has
    ``.run`` / ``.get`` / ``.list``; mocking each at call sites in tests
    is more flexible than a full Mock spec.
    """
    return MagicMock()


@pytest.fixture
def runtime(settings: Settings, mock_docker_client: MagicMock) -> DockerRuntime:
    rt = DockerRuntime.__new__(DockerRuntime)
    rt._settings = settings  # noqa: SLF001 — test setup
    rt._client = mock_docker_client  # noqa: SLF001
    return rt


@pytest.fixture
def mock_authz_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch httpx.AsyncClient in the authz_guard module to always allow.

    Applied automatically via app_client fixture so ordinary API tests don't
    need to set up authz mocking themselves.  Tests that specifically want to
    test deny-paths patch the client directly per-test, overriding this.
    """
    _allowed_resp = MagicMock(spec=httpx.Response)
    _allowed_resp.status_code = 200
    _allowed_resp.json = MagicMock(return_value={"allowed": True})

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, *args: Any, **kw: Any) -> MagicMock:
            return _allowed_resp

    monkeypatch.setattr(
        "agent_scheduler.api.authz_guard.httpx.AsyncClient",
        _FakeAsyncClient,
    )


@pytest.fixture
def mock_quota_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch httpx.AsyncClient in quota_guard to always return allowed=true.

    Applied automatically via app_client so non-quota tests aren't blocked
    by the fail-closed billing check.  Quota-specific tests (test_quota_guard.py)
    patch httpx.AsyncClient directly per-test, overriding this effect.
    """
    _allowed_resp = MagicMock(spec=httpx.Response)
    _allowed_resp.status_code = 200
    _allowed_resp.json = MagicMock(
        return_value={"allowed": True, "dimension": "agent_sessions", "remaining": 100}
    )

    class _FakeQuotaClient:
        def __init__(self, *args: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> "_FakeQuotaClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, *args: Any, **kw: Any) -> MagicMock:
            return _allowed_resp

    monkeypatch.setattr(
        "agent_scheduler.services.quota_guard.httpx.AsyncClient",
        _FakeQuotaClient,
    )


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Headers the gateway would inject after JWT verification."""
    return {"X-Tenant-Id": _TENANT, "X-User-Id": _USER}


class FakeIdentity:
    """Stub the identity /sessions/service-account-token endpoint."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response: tuple[int, dict] = (
            201,
            {
                "access_token": "minted-jwt",
                "token_type": "BEARER",
                "expires_at": "2026-12-31T00:00:00+00:00",
                "issued_at": "2026-01-01T00:00:00+00:00",
                "tenant_id": _TENANT,
                "subject": "agent-scheduler:run_test",
                "scopes": ["audit.write"],
                "audience": "foundation",
                "jti": "jti_01J5TEST00000000000000JTI0",
            },
        )

    def respond(self, request_json: dict | None) -> httpx.Response:
        self.calls.append(request_json or {})
        status, body = self.response
        return httpx.Response(status, json=body)


@pytest.fixture
def fake_identity(monkeypatch: pytest.MonkeyPatch) -> FakeIdentity:
    fake = FakeIdentity()

    _authz_allowed_resp = httpx.Response(200, json={"allowed": True})
    _quota_allowed_resp = httpx.Response(200, json={"allowed": True, "dimension": "agent_sessions", "remaining": 100})

    class _AsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> _AsyncClient:
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url: str, **kw) -> httpx.Response:
            # Route calls to the right stub so authz/quota guards still pass
            # even when fake_identity overwrites httpx.AsyncClient.
            if "/authz/" in url or url.endswith("/check"):
                return _authz_allowed_resp
            if "/quota/" in url:
                return _quota_allowed_resp
            return fake.respond(kw.get("json"))

    # Patching httpx.AsyncClient globally; the URL-routing above ensures the
    # authz and quota guards still receive an allowed=true response.
    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    return fake


@pytest.fixture
def app_client(
    settings: Settings,
    runtime: DockerRuntime,
    mock_authz_allowed: None,
    mock_quota_allowed: None,
) -> Iterator[TestClient]:
    """FastAPI TestClient with the docker runtime stubbed out, authz/quota always-allow."""
    app = create_app(settings)
    app.state.settings = settings
    # Override the get_runtime dependency to return our mock-backed runtime.
    from agent_scheduler.api.dependencies import get_runtime
    app.dependency_overrides[get_runtime] = lambda: runtime
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
