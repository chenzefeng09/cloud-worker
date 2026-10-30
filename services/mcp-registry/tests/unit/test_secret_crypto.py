"""Unit tests for the auth-secret envelope crypto helpers.

The helpers call the foundation crypto service over httpx; these tests patch
``httpx.AsyncClient`` in the ``secret_crypto`` module with a reversible echo
(ciphertext_b64 == plaintext_b64) so we can verify the helper logic -
envelope detection, idempotence, legacy-plaintext passthrough, per-type
field selection, and graceful fallback - without a live crypto service.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from mcp_registry.errors import SecretEncryptionUnavailableError
from mcp_registry.services import secret_crypto as sc


class _FakeResp:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeCryptoClient:
    """Reversible echo: envelope.ciphertext_b64 == request.plaintext_b64."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []

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
    ) -> _FakeResp:
        self.posts.append((url, json or {}))
        if url.endswith("/encrypt"):
            return _FakeResp(
                200,
                {"envelope": {"ciphertext_b64": (json or {})["plaintext_b64"]}},
            )
        if url.endswith("/decrypt"):
            env = (json or {})["envelope"]
            return _FakeResp(200, {"plaintext_b64": env["ciphertext_b64"]})
        return _FakeResp(404, {})


class _FakeFailClient:
    """Always returns 500 - exercises the plaintext fallback path."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeFailClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> _FakeResp:
        return _FakeResp(500, {})


@pytest.fixture
def fake_crypto(monkeypatch: pytest.MonkeyPatch) -> _FakeCryptoClient:
    fake = _FakeCryptoClient()
    monkeypatch.setattr(sc.httpx, "AsyncClient", lambda *a, **kw: fake)
    return fake


@pytest.mark.asyncio
async def test_encrypt_then_decrypt_roundtrip(fake_crypto: _FakeCryptoClient) -> None:
    encrypted = await sc.encrypt_secret(
        "tok-123", crypto_url="http://crypto:8004/api/crypto/v1", timeout=5, tenant_id="t1",
    )
    assert sc.is_envelope(encrypted)
    assert "tok-123" not in encrypted  # plaintext not present in the envelope
    decrypted = await sc.decrypt_secret(
        encrypted, crypto_url="http://crypto:8004/api/crypto/v1", timeout=5, tenant_id="t1",
    )
    assert decrypted == "tok-123"


@pytest.mark.asyncio
async def test_encrypt_is_idempotent(fake_crypto: _FakeCryptoClient) -> None:
    """An already-encrypted value must not be double-wrapped."""
    encrypted = await sc.encrypt_secret(
        "tok", crypto_url="http://crypto:8004/api/crypto/v1", timeout=5, tenant_id="t1",
    )
    re_encrypted = await sc.encrypt_secret(
        encrypted, crypto_url="http://crypto:8004/api/crypto/v1", timeout=5, tenant_id="t1",
    )
    assert re_encrypted == encrypted
    # Only the first call hit the crypto service.
    assert len(fake_crypto.posts) == 1


@pytest.mark.asyncio
async def test_decrypt_legacy_plaintext_passthrough(
    fake_crypto: _FakeCryptoClient,
) -> None:
    """A non-envelope value (legacy plaintext) is returned as-is."""
    result = await sc.decrypt_secret(
        "legacy-plaintext",
        crypto_url="http://crypto:8004/api/crypto/v1", timeout=5, tenant_id="t1",
    )
    assert result == "legacy-plaintext"
    assert fake_crypto.posts == []  # no crypto call for plaintext


@pytest.mark.asyncio
async def test_encrypt_fails_closed_when_crypto_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sc.httpx, "AsyncClient", lambda *a, **kw: _FakeFailClient())
    with pytest.raises(SecretEncryptionUnavailableError):
        await sc.encrypt_secret(
            "tok", crypto_url="http://crypto:8004/api/crypto/v1", timeout=5, tenant_id="t1",
        )


@pytest.mark.asyncio
async def test_encrypt_falls_back_to_plaintext_only_when_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sc.httpx, "AsyncClient", lambda *a, **kw: _FakeFailClient())
    result = await sc.encrypt_secret(
        "tok", crypto_url="http://crypto:8004/api/crypto/v1", timeout=5, tenant_id="t1",
        allow_plaintext=True,
    )
    assert result == "tok"  # dev-only fallback


@pytest.mark.asyncio
async def test_encrypt_values_round_trip_keeps_keys(fake_crypto: _FakeCryptoClient) -> None:
    ctx = {"crypto_url": "http://crypto:8004/api/crypto/v1", "timeout": 5, "tenant_id": "t1"}
    env = {"API_KEY": "sk-env", "EMPTY": "", "PORT": 8080}
    encrypted = await sc.encrypt_values(env, **ctx)
    assert set(encrypted) == {"API_KEY", "EMPTY", "PORT"}
    assert sc.is_envelope(encrypted["API_KEY"])
    assert "sk-env" not in encrypted["API_KEY"]
    # Empty and non-string values have nothing to protect and pass through.
    assert encrypted["EMPTY"] == "" and encrypted["PORT"] == 8080
    assert len(fake_crypto.posts) == 1
    # Idempotent on envelopes, like encrypt_auth.
    assert await sc.encrypt_values(encrypted, **ctx) == encrypted
    assert await sc.decrypt_values(encrypted, **ctx) == env


@pytest.mark.asyncio
async def test_value_helpers_accept_empty_maps(fake_crypto: _FakeCryptoClient) -> None:
    ctx = {"crypto_url": "http://crypto:8004/api/crypto/v1", "timeout": 5, "tenant_id": "t1"}
    assert await sc.encrypt_values({}, **ctx) == {}
    assert await sc.encrypt_values(None, **ctx) == {}
    assert await sc.decrypt_values(None, **ctx) == {}
    assert fake_crypto.posts == []


@pytest.mark.asyncio
async def test_encrypt_values_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sc.httpx, "AsyncClient", lambda *a, **kw: _FakeFailClient())
    with pytest.raises(SecretEncryptionUnavailableError):
        await sc.encrypt_values(
            {"Authorization": "Bearer x"},
            crypto_url="http://crypto:8004/api/crypto/v1", timeout=5, tenant_id="t1",
        )


@pytest.mark.asyncio
async def test_decrypt_falls_back_to_stored_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sc.httpx, "AsyncClient", lambda *a, **kw: _FakeFailClient())
    stored = json.dumps({"ciphertext_b64": base64.b64encode(b"tok").decode()})
    result = await sc.decrypt_secret(
        stored, crypto_url="http://crypto:8004/api/crypto/v1", timeout=5, tenant_id="t1",
    )
    assert result == stored  # stored value returned when decrypt fails


def test_is_envelope_detection() -> None:
    assert sc.is_envelope('{"ciphertext_b64": "abc"}') is True
    assert sc.is_envelope("plain") is False
    assert sc.is_envelope('{"foo": "bar"}') is False
    assert sc.is_envelope(None) is False
    assert sc.is_envelope(123) is False
    assert sc.is_envelope("{not json") is False


@pytest.mark.asyncio
async def test_encrypt_auth_handles_each_type(fake_crypto: _FakeCryptoClient) -> None:
    bearer = await sc.encrypt_auth(
        {"type": "bearer", "token": "btok"},
        crypto_url="http://crypto:8004/api/crypto/v1", timeout=5, tenant_id="t1",
    )
    assert sc.is_envelope(bearer["token"])
    assert bearer["type"] == "bearer"

    api_key = await sc.encrypt_auth(
        {"type": "api_key", "api_key": "k", "header_name": "X-Api-Key"},
        crypto_url="http://crypto:8004/api/crypto/v1", timeout=5, tenant_id="t1",
    )
    assert sc.is_envelope(api_key["api_key"])
    assert api_key["header_name"] == "X-Api-Key"  # non-secret passthrough

    oauth = await sc.encrypt_auth(
        {
            "type": "oauth",
            "client_id": "cid",
            "client_secret": "csec",
            "token_url": "https://oauth.example.com/token",
            "scopes": ["read", "write"],
        },
        crypto_url="http://crypto:8004/api/crypto/v1", timeout=5, tenant_id="t1",
    )
    assert sc.is_envelope(oauth["client_secret"])
    assert oauth["client_id"] == "cid"  # non-secret passthrough
    assert oauth["token_url"] == "https://oauth.example.com/token"
    assert oauth["scopes"] == ["read", "write"]


@pytest.mark.asyncio
async def test_encrypt_auth_none_type_is_noop(fake_crypto: _FakeCryptoClient) -> None:
    out = await sc.encrypt_auth(
        {"type": "none"},
        crypto_url="http://crypto:8004/api/crypto/v1", timeout=5, tenant_id="t1",
    )
    assert out == {"type": "none"}
    assert fake_crypto.posts == []


@pytest.mark.asyncio
async def test_encrypt_auth_passthrough_none_and_empty(
    fake_crypto: _FakeCryptoClient,
) -> None:
    assert await sc.encrypt_auth(
        None, crypto_url="http://c", timeout=5, tenant_id="t",
    ) is None
    # Empty dict is falsy -> passthrough.
    assert await sc.encrypt_auth(
        {}, crypto_url="http://c", timeout=5, tenant_id="t",
    ) == {}


@pytest.mark.asyncio
async def test_decrypt_auth_reverses_each_type(fake_crypto: _FakeCryptoClient) -> None:
    plain = {
        "type": "oauth",
        "client_id": "cid",
        "client_secret": "csec",
        "token_url": "https://oauth.example.com/token",
        "scopes": ["read"],
    }
    encrypted = await sc.encrypt_auth(
        plain, crypto_url="http://crypto:8004/api/crypto/v1", timeout=5, tenant_id="t1",
    )
    decrypted = await sc.decrypt_auth(
        encrypted, crypto_url="http://crypto:8004/api/crypto/v1", timeout=5, tenant_id="t1",
    )
    assert decrypted == plain


@pytest.mark.asyncio
async def test_encrypt_secret_skips_empty(fake_crypto: _FakeCryptoClient) -> None:
    assert await sc.encrypt_secret(
        "", crypto_url="http://c", timeout=5, tenant_id="t",
    ) == ""
    assert fake_crypto.posts == []


# ---------------------------------------------------------------------------
# mask_auth — pure function, no crypto calls
# ---------------------------------------------------------------------------


def test_mask_auth_bearer() -> None:
    result = sc.mask_auth({"type": "bearer", "token": "sk-real"})
    assert result == {
        "type": "bearer",
        "token": sc.MASKED_SENTINEL,
        "has_token": True,
    }


def test_mask_auth_api_key() -> None:
    result = sc.mask_auth({
        "type": "api_key",
        "api_key": "sk-real",
        "header_name": "X-API-Key",
    })
    assert result == {
        "type": "api_key",
        "api_key": sc.MASKED_SENTINEL,
        "has_api_key": True,
        "header_name": "X-API-Key",  # non-secret passed through
    }


def test_mask_auth_oauth() -> None:
    result = sc.mask_auth({
        "type": "oauth",
        "client_id": "cid",
        "client_secret": "csec",
        "token_url": "https://oauth.example.com/token",
        "scopes": ["read"],
    })
    assert result["client_secret"] == sc.MASKED_SENTINEL
    assert result["has_client_secret"] is True
    assert result["client_id"] == "cid"
    assert result["token_url"] == "https://oauth.example.com/token"
    assert result["scopes"] == ["read"]


def test_mask_auth_none_type_unchanged() -> None:
    auth = {"type": "none"}
    assert sc.mask_auth(auth) == {"type": "none"}


def test_mask_auth_none_value() -> None:
    assert sc.mask_auth(None) is None


def test_mask_auth_empty_dict() -> None:
    assert sc.mask_auth({}) == {}


def test_mask_auth_absent_secret_field() -> None:
    """Bearer with no token set -> has_token is False."""
    result = sc.mask_auth({"type": "bearer"})
    assert result["has_token"] is False
    assert "token" not in result or not result.get("token")


def test_mask_values_hides_every_non_empty_value() -> None:
    envelope = json.dumps({"ciphertext_b64": "c2VjcmV0"})
    masked = sc.mask_values({"API_KEY": "sk-plain", "TOKEN": envelope, "EMPTY": "", "NONE": None})
    # Keys stay visible; envelope and legacy plaintext are masked alike; empty
    # values have nothing to hide.
    assert masked == {
        "API_KEY": sc.MASKED_SENTINEL,
        "TOKEN": sc.MASKED_SENTINEL,
        "EMPTY": "",
        "NONE": None,
    }


def test_mask_values_none_or_empty() -> None:
    assert sc.mask_values(None) == {}
    assert sc.mask_values({}) == {}


def test_masked_keys_lists_sentinel_values_only() -> None:
    sent = {"A": sc.MASKED_SENTINEL, "B": "typed", "C": "", "D": sc.MASKED_SENTINEL}
    assert sc.masked_keys(sent) == ["A", "D"]
    assert sc.masked_keys(None) == []
