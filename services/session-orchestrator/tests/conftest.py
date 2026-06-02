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
from unittest.mock import AsyncMock

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
def app_client(
    settings: Settings,
    mock_db: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """API client with the DB dependency mocked out."""
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
