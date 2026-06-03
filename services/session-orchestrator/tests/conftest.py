"""Pytest fixtures for session-orchestrator.

Two flavours of tests:

* **Mock-DB tests** patch ``init_db`` / override the FastAPI dependency
  so the API + service code runs without a real Postgres.  Fast, runs
  anywhere.
* **Live-DB tests** (marked ``@pytest.mark.live_db``) expect a postgres
  instance on ``SESSION_ORCHESTRATOR_DB_DSN`` and run real RLS /
  migrations end-to-end.  CI brings up postgres:16 as a service container
  for these.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_session_orchestrator.config import Settings
from agent_session_orchestrator.main import create_app
from tests._helpers import TEST_TENANT, TEST_USER


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "live_db: requires a running Postgres on SESSION_ORCHESTRATOR_DB_DSN",
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        otel_enabled=False,
    )


@pytest.fixture
def mock_db() -> AsyncMock:
    """Stand-in for an AsyncSession with awaitable execute/flush/refresh."""
    mock = AsyncMock()
    mock.execute = AsyncMock()
    mock.flush = AsyncMock()
    mock.refresh = AsyncMock()
    mock.commit = AsyncMock()
    return mock


@pytest.fixture
def mock_authz_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch httpx.AsyncClient in the authz_guard module to always allow.

    Applied automatically to the app_client fixture so ordinary API tests
    don't need to set up authz mocking themselves.  Tests that specifically
    want to test deny-paths (test_authz_guard.py) patch the client directly
    per-test, overriding this fixture's effect.
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
        "agent_session_orchestrator.api.authz_guard.httpx.AsyncClient",
        _FakeAsyncClient,
    )


@pytest.fixture
def app_client(
    settings: Settings,
    mock_db: AsyncMock,
    mock_authz_allowed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """API client with the DB dependency mocked out and authz always-allow."""
    from agent_session_orchestrator.domain import db as db_module

    async def _noop_init(*args: Any, **kw: Any) -> None:
        return None

    async def _noop_close() -> None:
        return None

    monkeypatch.setattr(db_module, "init_db", _noop_init)
    monkeypatch.setattr(db_module, "close_db", _noop_close)

    app = create_app(settings)

    from agent_session_orchestrator.api.dependencies import db_session

    async def _override_db() -> AsyncIterator[AsyncMock]:
        yield mock_db

    app.dependency_overrides[db_session] = _override_db

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-Tenant-Id": TEST_TENANT, "X-User-Id": TEST_USER}
