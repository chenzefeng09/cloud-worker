"""The at-rest backfill covers every secret-bearing column and never stores plaintext."""

from __future__ import annotations

from typing import Any

import pytest

from mcp_registry.domain.models import McpServer
from mcp_registry.errors import SecretEncryptionUnavailableError
from mcp_registry.scripts import backfill_encrypt_auth as backfill
from mcp_registry.services import secret_crypto as sc
from tests.unit.test_secret_crypto import _FakeCryptoClient, _FakeFailClient


def _server(**columns: Any) -> McpServer:
    return McpServer(
        id="mcp_1", tenant_id="t1", name="s", transport="http",
        env=columns.get("env", {}), headers=columns.get("headers", {}),
        auth=columns.get("auth"),
    )


async def _encrypt(server: McpServer) -> bool:
    return await backfill._encrypt_row(  # noqa: SLF001
        None, server, crypto_url="http://crypto", crypto_timeout=1,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_backfill_encrypts_auth_env_and_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeCryptoClient()
    monkeypatch.setattr(sc.httpx, "AsyncClient", lambda *a, **kw: fake)
    server = _server(
        env={"API_KEY": "sk-env"},
        headers={"Authorization": "Bearer hdr"},
        auth={"type": "bearer", "token": "tok"},
    )
    assert await _encrypt(server)
    assert sc.is_envelope(server.env["API_KEY"])
    assert sc.is_envelope(server.headers["Authorization"])
    assert server.auth is not None and sc.is_envelope(server.auth["token"])
    # Re-running is a no-op.
    assert not await _encrypt(server)


@pytest.mark.asyncio
async def test_backfill_skips_rows_without_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeCryptoClient()
    monkeypatch.setattr(sc.httpx, "AsyncClient", lambda *a, **kw: fake)
    assert not await _encrypt(_server())
    assert fake.posts == []


@pytest.mark.asyncio
async def test_backfill_leaves_the_row_alone_when_crypto_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sc.httpx, "AsyncClient", lambda *a, **kw: _FakeFailClient())
    server = _server(env={"API_KEY": "sk-env"})
    with pytest.raises(SecretEncryptionUnavailableError):
        await _encrypt(server)
    assert server.env == {"API_KEY": "sk-env"}
