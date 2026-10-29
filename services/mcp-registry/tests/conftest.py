"""Pytest fixtures for mcp-registry.

Uses an in-memory SQLite database to exercise the real SQLAlchemy ORM
without requiring Postgres.  RLS is a Postgres-only concern so it's
skipped in tests; the service code is dialect-aware (see
``domain/db.get_session``).
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from mcp_registry.config import Settings
from mcp_registry.domain import db as db_module
from mcp_registry.domain.models import Base
from mcp_registry.main import create_app

TEST_TENANT = "tnt_01J5TEST00000000000000TNT1"
TEST_TENANT_OTHER = "tnt_01J5TEST00000000000000TNT2"
TEST_USER = "usr_01J5TEST0000000000000USER1"

# Secret fields encrypted/decrypted by the API layer.  Mirrors the table in
# ``services/secret_crypto.py`` so the reversible test fake stays in sync.
_SECRET_FIELDS = ("token", "api_key", "client_secret")


def _is_envelope(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        obj = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(obj, dict) and "ciphertext_b64" in obj


def _reversible_encrypt(value: str) -> str:
    return json.dumps({"ciphertext_b64": base64.b64encode(value.encode()).decode()})


def _reversible_decrypt(value: str) -> str:
    return base64.b64decode(json.loads(value)["ciphertext_b64"]).decode()


async def _reversible_encrypt_auth(
    auth: dict[str, Any] | None,
    *,
    crypto_url: str,
    timeout: float,
    tenant_id: str,
    allow_plaintext: bool = False,
) -> dict[str, Any] | None:
    """Test-only reversible encrypt: envelope = {"ciphertext_b64": b64(pt)}."""
    if not auth:
        return auth
    out = dict(auth)
    for field in _SECRET_FIELDS:
        v = out.get(field)
        if isinstance(v, str) and v and not _is_envelope(v):
            out[field] = _reversible_encrypt(v)
    return out


async def _reversible_decrypt_auth(
    auth: dict[str, Any] | None,
    *,
    crypto_url: str,
    timeout: float,
    tenant_id: str,
) -> dict[str, Any] | None:
    """Test-only reversible decrypt - inverse of ``_reversible_encrypt_auth``."""
    if not auth:
        return auth
    out = dict(auth)
    for field in _SECRET_FIELDS:
        v = out.get(field)
        if isinstance(v, str) and v and _is_envelope(v):
            out[field] = _reversible_decrypt(v)
    return out


async def _reversible_encrypt_values(
    values: dict[str, Any] | None, *, allow_plaintext: bool = False, **_ctx: Any,
) -> dict[str, Any]:
    """Test-only reversible ``encrypt_values`` (same envelope as above)."""
    return {
        k: _reversible_encrypt(v) if isinstance(v, str) and v and not _is_envelope(v) else v
        for k, v in (values or {}).items()
    }


async def _reversible_decrypt_values(
    values: dict[str, Any] | None, **_ctx: Any,
) -> dict[str, Any]:
    """Test-only reversible ``decrypt_values``."""
    return {
        k: _reversible_decrypt(v) if isinstance(v, str) and _is_envelope(v) else v
        for k, v in (values or {}).items()
    }



@pytest.fixture
def settings() -> Settings:
    return Settings(env="test", log_level="WARNING", db_dsn="sqlite+aiosqlite:///:memory:")


@pytest_asyncio.fixture
async def db_engine_and_factory(
    settings: Settings,
) -> AsyncIterator[tuple[object, async_sessionmaker[AsyncSession]]]:
    """Fresh in-memory SQLite engine + session factory for each test."""
    engine = create_async_engine(settings.db_dsn, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    db_module._engine = engine  # noqa: SLF001 — test wiring
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

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, *args: Any, **kw: Any) -> MagicMock:
            return _allowed_resp

    monkeypatch.setattr(
        "mcp_registry.api.authz_guard.httpx.AsyncClient",
        _FakeAsyncClient,
    )


@pytest.fixture
def mock_crypto_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the API-layer crypto helpers with a reversible in-memory transform.

    This avoids real HTTP calls to the foundation crypto service in every
    test while still exercising the full encrypt-on-write / decrypt-on-read
    round-trip: the DB row holds an envelope (``ciphertext_b64``) and every
    API/internal response returns plaintext.  Tests that need to assert on
    ciphertext-at-rest read the ORM row directly (bypassing the API decrypt).
    """
    import mcp_registry.api.v1.mcp_servers as api_module

    monkeypatch.setattr(api_module, "encrypt_auth", _reversible_encrypt_auth)
    monkeypatch.setattr(api_module, "decrypt_auth", _reversible_decrypt_auth)
    monkeypatch.setattr(api_module, "encrypt_values", _reversible_encrypt_values)
    monkeypatch.setattr(api_module, "decrypt_values", _reversible_decrypt_values)


@pytest.fixture
def app_client(
    settings: Settings,
    db_engine_and_factory: tuple[object, async_sessionmaker[AsyncSession]],
    mock_authz_allowed: None,
    mock_crypto_roundtrip: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """API client backed by the in-memory SQLite engine, authz always-allow.

    The lifespan in ``mcp_registry.main`` imports ``init_db`` / ``close_db``
    as locals, so monkeypatching the ``domain.db`` attributes is not enough
    — we have to patch the symbols rebound inside ``main``.
    """
    from mcp_registry import main as main_module

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
def other_tenant_headers() -> dict[str, str]:
    return {"X-Tenant-Id": TEST_TENANT_OTHER, "X-User-Id": TEST_USER}


@pytest.fixture
def internal_headers() -> dict[str, str]:
    return {
        "X-Tenant-Id": TEST_TENANT,
        "X-Service-Scopes": "mcp.read_internal",
        "X-User-Id": "svc_mcp_injector",
    }
