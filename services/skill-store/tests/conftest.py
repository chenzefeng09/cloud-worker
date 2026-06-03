"""Pytest fixtures for skill-store.

Uses in-memory SQLite + ``InMemoryBlobStore`` to fully exercise the API
without external services.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from skill_store.blob.memory import InMemoryBlobStore
from skill_store.config import Settings
from skill_store.domain import db as db_module
from skill_store.domain.models import Base
from skill_store.main import create_app

TEST_TENANT = "tnt_01J5TEST00000000000000TNT1"
TEST_TENANT_OTHER = "tnt_01J5TEST00000000000000TNT2"
TEST_USER = "usr_01J5TEST0000000000000USER1"


@pytest.fixture
def settings() -> Settings:
    return Settings(env="test", log_level="WARNING", db_dsn="sqlite+aiosqlite:///:memory:")


@pytest.fixture
def blob_store() -> InMemoryBlobStore:
    return InMemoryBlobStore()


@pytest_asyncio.fixture
async def db_engine_and_factory(
    settings: Settings,
) -> AsyncIterator[tuple[object, async_sessionmaker[AsyncSession]]]:
    engine = create_async_engine(settings.db_dsn, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    db_module._engine = engine  # noqa: SLF001
    db_module.set_session_factory(factory)
    try:
        yield engine, factory
    finally:
        db_module.set_session_factory(None)
        db_module._engine = None  # noqa: SLF001
        await engine.dispose()


@pytest_asyncio.fixture
async def session(
    db_engine_and_factory: tuple[object, async_sessionmaker[AsyncSession]],
) -> AsyncIterator[AsyncSession]:
    _, factory = db_engine_and_factory
    async with factory() as s:
        yield s


@pytest.fixture
def mock_authz_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch httpx.AsyncClient in the authz_guard module to always allow.

    Applied automatically via app_client so ordinary API tests don't need
    to set up authz mocking themselves.
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
        "skill_store.api.authz_guard.httpx.AsyncClient",
        _FakeAsyncClient,
    )


@pytest.fixture
def mock_quota_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch httpx.AsyncClient in quota_guard to always return allowed=true.

    Applied automatically via app_client so non-quota tests aren't blocked
    by the fail-closed billing check.
    """
    _allowed_resp = MagicMock(spec=httpx.Response)
    _allowed_resp.status_code = 200
    _allowed_resp.json = MagicMock(
        return_value={"allowed": True, "dimension": "skills", "remaining": 100}
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
        "skill_store.services.quota_guard.httpx.AsyncClient",
        _FakeQuotaClient,
    )


@pytest.fixture
def app_client(
    settings: Settings,
    db_engine_and_factory: tuple[object, async_sessionmaker[AsyncSession]],
    blob_store: InMemoryBlobStore,
    mock_authz_allowed: None,
    mock_quota_allowed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    from skill_store import main as main_module

    async def _noop_init(*args: object, **kw: object) -> None:
        return None

    async def _noop_close() -> None:
        return None

    monkeypatch.setattr(main_module, "init_db", _noop_init)
    monkeypatch.setattr(main_module, "close_db", _noop_close)

    app = create_app(settings, blob_store=blob_store)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def tenant_headers() -> dict[str, str]:
    return {"X-Tenant-Id": TEST_TENANT, "X-User-Id": TEST_USER}


@pytest.fixture
def other_tenant_headers() -> dict[str, str]:
    return {"X-Tenant-Id": TEST_TENANT_OTHER, "X-User-Id": TEST_USER}


@pytest.fixture
def internal_headers() -> dict[str, str]:
    return {"X-Service-Scopes": "skill.read_internal", "X-User-Id": "svc_skills_loader"}
