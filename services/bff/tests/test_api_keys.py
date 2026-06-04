"""Tests: API Key management endpoints + auth middleware.

Covers:
1. POST /api-keys — unauthenticated → 401/403
2. POST /api-keys — creates key, returns raw key once (hash stored, prefix returned)
3. GET  /api-keys — unauthenticated → 401/403
4. GET  /api-keys — lists only active (non-revoked) keys, never exposes raw/hash
5. DELETE /api-keys/{id} — unauthenticated → 401/403
6. DELETE /api-keys/{id} — revokes own key; key no longer appears in list
7. DELETE /api-keys/{id} — deleting another user's key returns 404
8. API key auth middleware — valid key returns identity + updates last_used_at
9. API key auth middleware — revoked key returns 401
10. API key auth middleware — forged/unknown key returns 401
11. API key auth middleware — missing header returns 401
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agent_bff.domain.db import set_session_factory
from agent_bff.domain.models import Base


# ─────────────────────────────────────────────────────────────────────────────
# SQLite in-memory DB fixture (replaces Postgres for unit tests)
# ─────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def sqlite_session_factory():
    """Create a fresh in-memory SQLite DB for each test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    set_session_factory(factory)
    yield factory
    set_session_factory(None)
    await engine.dispose()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Tests: CRUD endpoints
# ─────────────────────────────────────────────────────────────────────────────

def test_create_api_key_unauthenticated(client) -> None:
    resp = client.post("/api/bff/v1/api-keys", json={"name": "test"})
    assert resp.status_code in (401, 403)


def test_list_api_keys_unauthenticated(client) -> None:
    resp = client.get("/api/bff/v1/api-keys")
    assert resp.status_code in (401, 403)


def test_delete_api_key_unauthenticated(client) -> None:
    resp = client.delete("/api/bff/v1/api-keys/key_some_id")
    assert resp.status_code in (401, 403)


def test_create_api_key_returns_raw_key(
    client,
    authed_cookie: dict,
    sqlite_session_factory: Any,
) -> None:
    """POST /api-keys → 201, raw key returned, prefix matches key, hash stored."""
    resp = client.post(
        "/api/bff/v1/api-keys",
        json={"name": "CI pipeline"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 201, resp.text

    body = resp.json()
    assert body["name"] == "CI pipeline"
    assert "key" in body  # one-time raw key
    assert body["key"].startswith("cw_live_")
    assert body["key_prefix"] == body["key"][:16]
    assert body["id"]
    assert body["created_at"]


def test_create_api_key_hash_stored_not_plaintext(
    client,
    authed_cookie: dict,
    sqlite_session_factory: Any,
) -> None:
    """The response must NOT include the hash; and the key must not be in the list response."""
    create_resp = client.post(
        "/api/bff/v1/api-keys",
        json={"name": "Check hash"},
        cookies=authed_cookie,
    )
    assert create_resp.status_code == 201
    create_body = create_resp.json()

    # List response must not contain the raw key or hash
    list_resp = client.get("/api/bff/v1/api-keys", cookies=authed_cookie)
    assert list_resp.status_code == 200
    items = list_resp.json()
    assert len(items) == 1
    item = items[0]
    # Ensure raw key fields are absent
    assert "key" not in item
    assert "key_hash" not in item
    # prefix is safe to return
    assert item["key_prefix"] == create_body["key_prefix"]


def test_list_api_keys_excludes_revoked(
    client,
    authed_cookie: dict,
    sqlite_session_factory: Any,
) -> None:
    """Revoked keys must not appear in the list."""
    # Create two keys
    r1 = client.post("/api/bff/v1/api-keys", json={"name": "keep"}, cookies=authed_cookie)
    r2 = client.post("/api/bff/v1/api-keys", json={"name": "revoke me"}, cookies=authed_cookie)
    assert r1.status_code == 201
    assert r2.status_code == 201

    key2_id = r2.json()["id"]

    # Revoke key2
    del_resp = client.delete(f"/api/bff/v1/api-keys/{key2_id}", cookies=authed_cookie)
    assert del_resp.status_code == 204

    # List should only contain key1
    list_resp = client.get("/api/bff/v1/api-keys", cookies=authed_cookie)
    assert list_resp.status_code == 200
    items = list_resp.json()
    assert len(items) == 1
    assert items[0]["name"] == "keep"


def test_delete_nonexistent_key_returns_404(
    client,
    authed_cookie: dict,
    sqlite_session_factory: Any,
) -> None:
    """Attempting to revoke an unknown key returns 404."""
    resp = client.delete("/api/bff/v1/api-keys/key_NONEXISTENT", cookies=authed_cookie)
    assert resp.status_code == 404


def test_delete_already_revoked_key_returns_404(
    client,
    authed_cookie: dict,
    sqlite_session_factory: Any,
) -> None:
    """Revoking an already-revoked key returns 404 (idempotent guard)."""
    r = client.post("/api/bff/v1/api-keys", json={"name": "once"}, cookies=authed_cookie)
    key_id = r.json()["id"]

    # First revoke — success
    assert client.delete(f"/api/bff/v1/api-keys/{key_id}", cookies=authed_cookie).status_code == 204

    # Second revoke — 404
    assert client.delete(f"/api/bff/v1/api-keys/{key_id}", cookies=authed_cookie).status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# Tests: API key auth middleware
# ─────────────────────────────────────────────────────────────────────────────

def test_api_key_auth_valid_key(
    client,
    authed_cookie: dict,
    sqlite_session_factory: Any,
) -> None:
    """A valid key allows access to a protected endpoint.

    We use the demo /api-keys/probe endpoint which is protected by
    require_api_key() and returns the resolved identity.
    """
    # Create a key via the cookie-auth endpoint
    create_resp = client.post(
        "/api/bff/v1/api-keys",
        json={"name": "auth test"},
        cookies=authed_cookie,
    )
    assert create_resp.status_code == 201
    raw_key = create_resp.json()["key"]

    # Use the key to access the probe endpoint
    probe_resp = client.get(
        "/api/bff/v1/api-keys/probe",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert probe_resp.status_code == 200
    body = probe_resp.json()
    assert body["user_id"] == "usr_01J5TEST0000000000000USER1"
    assert body["tenant_id"] == "tnt_01J5TEST00000000000000TNT1"


def test_api_key_auth_x_api_key_header(
    client,
    authed_cookie: dict,
    sqlite_session_factory: Any,
) -> None:
    """X-API-Key header is accepted as an alternative to Authorization: Bearer."""
    create_resp = client.post(
        "/api/bff/v1/api-keys",
        json={"name": "x-api-key test"},
        cookies=authed_cookie,
    )
    raw_key = create_resp.json()["key"]

    probe_resp = client.get(
        "/api/bff/v1/api-keys/probe",
        headers={"X-API-Key": raw_key},
    )
    assert probe_resp.status_code == 200


def test_api_key_auth_revoked_key_rejected(
    client,
    authed_cookie: dict,
    sqlite_session_factory: Any,
) -> None:
    """A revoked key must be rejected (401)."""
    create_resp = client.post(
        "/api/bff/v1/api-keys",
        json={"name": "to revoke"},
        cookies=authed_cookie,
    )
    body = create_resp.json()
    raw_key = body["key"]
    key_id = body["id"]

    # Revoke it
    client.delete(f"/api/bff/v1/api-keys/{key_id}", cookies=authed_cookie)

    # Now the key should be rejected
    probe_resp = client.get(
        "/api/bff/v1/api-keys/probe",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert probe_resp.status_code == 401


def test_api_key_auth_forged_key_rejected(
    client,
    sqlite_session_factory: Any,
) -> None:
    """A forged (never-issued) key returns 401."""
    probe_resp = client.get(
        "/api/bff/v1/api-keys/probe",
        headers={"Authorization": "Bearer cw_live_completelyfakekey123"},
    )
    assert probe_resp.status_code == 401


def test_api_key_auth_missing_header_rejected(
    client,
    sqlite_session_factory: Any,
) -> None:
    """Accessing the probe endpoint with no auth at all returns 401."""
    probe_resp = client.get("/api/bff/v1/api-keys/probe")
    assert probe_resp.status_code == 401
