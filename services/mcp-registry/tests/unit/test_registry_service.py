"""Service-layer tests for RegistryService against in-memory SQLite."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_registry.errors import McpServerNameConflictError, McpServerNotFoundError
from mcp_registry.services import secret_crypto as sc
from mcp_registry.services.registry_service import RegistryService
from tests.conftest import TEST_TENANT, TEST_TENANT_OTHER


@pytest.fixture
def svc(session: AsyncSession) -> RegistryService:
    return RegistryService(session, tenant_id=TEST_TENANT)


@pytest.mark.asyncio
async def test_create_assigns_tenant_and_id(svc: RegistryService) -> None:
    server = await svc.create(
        name="weather",
        transport="http",
        endpoint="https://x/y",
        command=None,
        env={},
        enabled=True,
    )
    assert server.tenant_id == TEST_TENANT
    assert server.id.startswith("mcp_")


@pytest.mark.asyncio
async def test_duplicate_name_raises_conflict(svc: RegistryService) -> None:
    await svc.create(
        name="weather",
        transport="http",
        endpoint="https://x",
        command=None,
        env={},
        enabled=True,
    )
    with pytest.raises(McpServerNameConflictError):
        await svc.create(
            name="weather",
            transport="http",
            endpoint="https://other",
            command=None,
            env={},
            enabled=True,
        )


@pytest.mark.asyncio
async def test_get_missing_raises(svc: RegistryService) -> None:
    with pytest.raises(McpServerNotFoundError):
        await svc.get("mcp_unknown")


@pytest.mark.asyncio
async def test_update_changes_fields(svc: RegistryService) -> None:
    created = await svc.create(
        name="weather",
        transport="http",
        endpoint="https://x",
        command=None,
        env={},
        enabled=True,
    )
    updated = await svc.update(created.id, enabled=False, endpoint="https://y")
    assert updated.enabled is False
    assert updated.endpoint == "https://y"


@pytest.mark.asyncio
async def test_delete_removes_row(svc: RegistryService) -> None:
    created = await svc.create(
        name="weather",
        transport="http",
        endpoint="https://x",
        command=None,
        env={},
        enabled=True,
    )
    await svc.delete(created.id)
    with pytest.raises(McpServerNotFoundError):
        await svc.get(created.id)


@pytest.mark.asyncio
async def test_list_for_tenant_filters_enabled(session: AsyncSession) -> None:
    svc = RegistryService(session, tenant_id=TEST_TENANT)
    await svc.create(
        name="on",
        transport="http",
        endpoint="https://a",
        command=None,
        env={},
        enabled=True,
    )
    await svc.create(
        name="off",
        transport="http",
        endpoint="https://b",
        command=None,
        env={},
        enabled=False,
    )
    # Other tenant — must not leak.
    other = RegistryService(session, tenant_id=TEST_TENANT_OTHER)
    await other.create(
        name="elsewhere",
        transport="http",
        endpoint="https://c",
        command=None,
        env={},
        enabled=True,
    )

    items = await svc.list_for_tenant(TEST_TENANT)
    names = sorted(s.name for s in items)
    assert names == ["on"]


# ---------------------------------------------------------------------------
# Auth secret encryption at rest (Phase 2)
# ---------------------------------------------------------------------------


class _FakeCryptoResp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status_code = 200
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeCryptoClient:
    """Reversible echo crypto service for at-rest verification."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeCryptoClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        data: Any = None,
    ) -> _FakeCryptoResp:
        if url.endswith("/encrypt"):
            return _FakeCryptoResp(
                {"envelope": {"ciphertext_b64": (json or {})["plaintext_b64"]}}
            )
        if url.endswith("/decrypt"):
            env = (json or {})["envelope"]
            return _FakeCryptoResp({"plaintext_b64": env["ciphertext_b64"]})
        return _FakeCryptoResp({})


@pytest.mark.asyncio
async def test_registry_stores_encrypted_auth_and_decrypt_recovers_plaintext(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The service stores whatever auth dict it is given; with the API-layer
    encrypt applied upstream the DB row holds an envelope, and decrypt_auth
    recovers the plaintext.  This proves the at-rest contract: ciphertext in
    the DB, plaintext only on the decrypted read path.
    """
    monkeypatch.setattr(sc.httpx, "AsyncClient", lambda *a, **kw: _FakeCryptoClient())

    crypto_ctx = {
        "crypto_url": "http://crypto:8004/api/crypto/v1",
        "timeout": 5.0,
        "tenant_id": TEST_TENANT,
    }
    plain_auth = {"type": "bearer", "token": "secret-tok"}
    encrypted_auth = await sc.encrypt_auth(plain_auth, **crypto_ctx)
    assert sc.is_envelope(encrypted_auth["token"])  # encrypt produced envelope

    svc = RegistryService(session, tenant_id=TEST_TENANT)
    server = await svc.create(
        name="encrypted-bearer",
        transport="http",
        endpoint="https://x",
        command=None,
        env={},
        enabled=True,
        auth=encrypted_auth,
    )

    # Read the raw ORM row - the DB column holds ciphertext, not plaintext.
    raw = await svc.get(server.id)
    assert sc.is_envelope(raw.auth["token"])
    assert "secret-tok" not in json.dumps(raw.auth)

    # Decrypt recovers the plaintext.
    decrypted = await sc.decrypt_auth(raw.auth, **crypto_ctx)
    assert decrypted == plain_auth
