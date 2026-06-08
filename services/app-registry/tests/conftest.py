"""Pytest fixtures for app-registry.

Uses in-memory SQLite to fully exercise the API + service layer without
external services (authz is mocked to always-allow).
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

from app_registry.config import Settings
from app_registry.domain import db as db_module
from app_registry.domain.models import Base
from app_registry.main import create_app

TEST_TENANT = "tnt_01J5TEST00000000000000TNT1"
TEST_TENANT_OTHER = "tnt_01J5TEST00000000000000TNT2"
TEST_USER = "usr_01J5TEST0000000000000USER1"
TEST_USER_OTHER = "usr_01J5TEST0000000000000USER2"


@pytest.fixture
def settings() -> Settings:
    return Settings(env="test", log_level="WARNING", db_dsn="sqlite+aiosqlite:///:memory:")


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
    """Patch httpx.AsyncClient in authz_guard so /check + is_reviewer allow."""
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
        "app_registry.api.authz_guard.httpx.AsyncClient",
        _FakeAsyncClient,
    )


@pytest.fixture
def app_client(
    settings: Settings,
    db_engine_and_factory: tuple[object, async_sessionmaker[AsyncSession]],
    mock_authz_allowed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    from app_registry import main as main_module

    async def _noop_init(*args: object, **kw: object) -> None:
        return None

    async def _noop_close() -> None:
        return None

    monkeypatch.setattr(main_module, "init_db", _noop_init)
    monkeypatch.setattr(main_module, "close_db", _noop_close)

    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def tenant_headers() -> dict[str, str]:
    return {"X-Tenant-Id": TEST_TENANT, "X-User-Id": TEST_USER}


@pytest.fixture
def other_user_headers() -> dict[str, str]:
    return {"X-Tenant-Id": TEST_TENANT, "X-User-Id": TEST_USER_OTHER}


@pytest.fixture
def internal_headers() -> dict[str, str]:
    return {
        "X-Tenant-Id": TEST_TENANT,
        "X-Service-Scopes": "app.read_internal",
        "X-User-Id": "svc_bff_run_resolver",
    }
