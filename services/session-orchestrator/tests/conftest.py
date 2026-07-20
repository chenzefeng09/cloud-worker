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
from contextvars import ContextVar
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import Request, Response
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

# ── foundation_sdk compatibility shim (test env only) ───────────────────────
# The real container image builds ``foundation_sdk`` fresh at image-build time,
# so it always exposes ``get_clearance`` (the per-user DLP clearance helper that
# api/middleware.py + api/dependencies.py import). A locally-installed SDK can
# lag that build and lack the symbol, breaking test collection with
# ``ImportError: cannot import name 'get_clearance'``. Inject it before any app
# module imports it. No-op when the installed SDK already has it.
from foundation_sdk import fastapi as _fsdk_fastapi  # noqa: E402

# Module-level clearance contextvar + shim flag. The real container's SDK
# exposes ``get_clearance`` AND its ``RequestContextMiddleware`` reads
# ``X-User-Clearance`` into the clearance var. A locally-installed SDK can
# lag that build and lack both; we bridge the getter here and bridge the
# header->var wiring via ``_ClearanceHeaderMiddleware`` (installed in
# ``app_client``). In a real-SDK env this var is simply unused.
_clearance_var: ContextVar[str] = ContextVar("foundation.clearance", default="")
_CLEARANCE_SHIMMED: bool = not hasattr(_fsdk_fastapi, "get_clearance")

if _CLEARANCE_SHIMMED:
    def _get_clearance() -> str:
        return _clearance_var.get()

    _fsdk_fastapi.get_clearance = _get_clearance  # type: ignore[attr-defined]
    try:
        from foundation_sdk import _fastapi as _fsdk_internal

        if not hasattr(_fsdk_internal, "get_clearance"):
            _fsdk_internal.get_clearance = _get_clearance  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - internal module is best-effort
        pass


class _ClearanceHeaderMiddleware(BaseHTTPMiddleware):
    """Test-only: read ``X-User-Clearance`` into ``_clearance_var``.

    Mirrors what the real ``RequestContextMiddleware`` does in the container
    for the tenant/user vars. Only installed when the shim is active.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: Any,
    ) -> Response:
        _clearance_var.set(request.headers.get("X-User-Clearance", ""))
        return await call_next(request)

from agent_session_orchestrator.config import Settings  # noqa: E402
from agent_session_orchestrator.main import create_app  # noqa: E402
from tests._helpers import TEST_TENANT, TEST_USER  # noqa: E402


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

        async def __aenter__(self) -> _FakeAsyncClient:
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

    # When the local foundation_sdk lacks its own X-User-Clearance wiring, bridge
    # the header into the shim's clearance contextvar so ``TClearance`` resolves
    # the caller's real tier (tests override the header per-case). Must be added
    # before the TestClient builds the middleware stack.
    if _CLEARANCE_SHIMMED:
        app.add_middleware(_ClearanceHeaderMiddleware)

    from agent_session_orchestrator.api.dependencies import db_session

    async def _override_db() -> AsyncIterator[AsyncMock]:
        yield mock_db

    app.dependency_overrides[db_session] = _override_db

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def auth_headers() -> dict[str, str]:
    # ``X-User-Clearance: L2`` bakes in the conservative non-privileged tier so
    # every test exercises the clearance path deterministically; tests that need
    # a higher tier override the header explicitly.
    return {
        "X-Tenant-Id": TEST_TENANT,
        "X-User-Id": TEST_USER,
        "X-User-Clearance": "L2",
    }
