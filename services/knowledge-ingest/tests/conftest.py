"""Pytest fixtures for knowledge-ingest.

Embedding calls are stubbed out with a deterministic fake so unit tests
don't need a running llm-gateway, and pgvector is sidestepped by running
against SQLite (the ``VectorColumn`` TypeDecorator falls back to a JSON
list there).
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import MagicMock

import httpx
import numpy as np
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from knowledge_ingest.blob.memory import InMemoryBlobStore
from knowledge_ingest.config import Settings
from knowledge_ingest.domain import db as db_module
from knowledge_ingest.domain.models import EMBEDDING_DIM, Base
from knowledge_ingest.main import create_app

TEST_TENANT = "tnt_01J5TEST00000000000000TNT1"
TEST_TENANT_OTHER = "tnt_01J5TEST00000000000000TNT2"
TEST_USER = "usr_01J5TEST0000000000000USER1"


class FakeEmbeddings:
    """Deterministic embedding stub.

    Maps each input to a unit-norm vector seeded by sha256 of the text,
    so two calls with the same text return the same vector (important for
    tests that round-trip ingest → search).
    """

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        out: list[list[float]] = []
        for t in texts:
            seed = int.from_bytes(hashlib.sha256(t.encode()).digest()[:8], "big")
            rng = np.random.default_rng(seed)
            v = rng.standard_normal(self.dim)
            v /= np.linalg.norm(v) or 1.0
            out.append(v.tolist())
        return out


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        db_dsn="sqlite+aiosqlite:///:memory:",
        chunk_size=64,
        chunk_overlap=8,
    )


@pytest.fixture
def blob_store() -> InMemoryBlobStore:
    return InMemoryBlobStore()


@pytest.fixture
def embeddings() -> FakeEmbeddings:
    return FakeEmbeddings()


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
        "knowledge_ingest.api.authz_guard.httpx.AsyncClient",
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
        return_value={"allowed": True, "dimension": "storage_gb", "remaining": 1024}
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
        "knowledge_ingest.services.quota_guard.httpx.AsyncClient",
        _FakeQuotaClient,
    )


@pytest.fixture
def app_client(
    settings: Settings,
    db_engine_and_factory: tuple[object, async_sessionmaker[AsyncSession]],
    blob_store: InMemoryBlobStore,
    embeddings: FakeEmbeddings,
    mock_authz_allowed: None,
    mock_quota_allowed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    from knowledge_ingest import main as main_module

    async def _noop_init(*args: object, **kw: object) -> None:
        return None

    async def _noop_close() -> None:
        return None

    monkeypatch.setattr(main_module, "init_db", _noop_init)
    monkeypatch.setattr(main_module, "close_db", _noop_close)

    app = create_app(settings, blob_store=blob_store, embeddings=embeddings)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def tenant_headers() -> dict[str, str]:
    return {"X-Tenant-Id": TEST_TENANT, "X-User-Id": TEST_USER}


@pytest.fixture
def other_tenant_headers() -> dict[str, str]:
    return {"X-Tenant-Id": TEST_TENANT_OTHER, "X-User-Id": TEST_USER}
