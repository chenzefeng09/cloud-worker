"""Tests: BFF /llm/user-preferences proxy — purpose enum + body validation.

Covers:
1. PUT body validation (purpose enum, required model_id/provider_id) → 422.
2. PUT happy path forwards a validated body to the gateway.
3. DELETE path-param purpose enum validation → 422 / pass-through.
4. resolve query purpose enum validation → 422 / pass-through.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest


class FakeGatewayFoundation:
    """Stubs authz /check (n/a here), identity /sessions/me and llm-gateway."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def route(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, url))
        if "/sessions/me" in url:
            return httpx.Response(200, json={
                "user_id": "usr_01J5TEST0000000000000USER1",
                "tenant_id": "tnt_01J5TEST00000000000000TNT1",
                "email": "user@example.com",
                "display_name": "User",
                "roles": [],
                "mfa_enabled": False,
                "locale": "zh-CN",
            })
        if "/user-preferences" in url and method == "PUT":
            return httpx.Response(200, json={"purpose": "llm", "ok": True})
        if "/user-preferences" in url and method == "DELETE":
            return httpx.Response(200, json={"ok": True})
        if "/user-preferences/resolve" in url and method == "GET":
            return httpx.Response(200, json={"purpose": "llm", "model_id": "m"})
        if url.endswith("/providers") and method == "GET":
            return httpx.Response(200, json={"items": [
                {
                    "id": "prov_enabled",
                    "name": "Enabled Provider",
                    "kind": "openai_compatible",
                    "enabled": True,
                    "base_url": "https://secret.internal/v1",
                    "api_key": "sk-super-secret",
                    "default_model": "gpt-4o",
                    "models": ["gpt-4o", "gpt-4o-mini"],
                    "capabilities": {
                        "model_type_map": {"llm": ["gpt-4o", "gpt-4o-mini"]},
                        "model_settings": {"gpt-4o": {"context_length": 128000}},
                        "internal_secret": "do-not-leak",
                    },
                    "created_at": "2024-01-01T00:00:00Z",
                },
                {
                    "id": "prov_disabled",
                    "name": "Disabled Provider",
                    "kind": "anthropic",
                    "enabled": False,
                    "base_url": "https://other.internal/v1",
                    "api_key": "sk-disabled",
                    "models": ["claude-3"],
                    "capabilities": {"model_type_map": {"llm": ["claude-3"]}},
                },
            ]})
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def fake_gateway(monkeypatch: pytest.MonkeyPatch) -> FakeGatewayFoundation:
    fake = FakeGatewayFoundation()

    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, **kw): return fake.route("GET", url, **kw)
        async def post(self, url, **kw): return fake.route("POST", url, **kw)
        async def put(self, url, **kw): return fake.route("PUT", url, **kw)
        async def patch(self, url, **kw): return fake.route("PATCH", url, **kw)
        async def delete(self, url, **kw): return fake.route("DELETE", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)
    return fake


# ─── GET /llm/models (sanitized, non-admin) ───────────────────────────────────

def test_list_models_accessible_to_normal_user(client, authed_cookie, fake_gateway) -> None:
    # Normal authed user (no admin role) can reach the catalogue endpoint.
    resp = client.get("/api/bff/v1/llm/models", cookies=authed_cookie)
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body and isinstance(body["items"], list)


def test_list_models_only_enabled_providers(client, authed_cookie, fake_gateway) -> None:
    resp = client.get("/api/bff/v1/llm/models", cookies=authed_cookie)
    assert resp.status_code == 200
    items = resp.json()["items"]
    ids = {p["id"] for p in items}
    assert ids == {"prov_enabled"}  # disabled provider filtered out
    assert all(p["enabled"] for p in items)


def test_list_models_redacts_credentials(client, authed_cookie, fake_gateway) -> None:
    resp = client.get("/api/bff/v1/llm/models", cookies=authed_cookie)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items, "expected at least one provider"
    for p in items:
        assert "base_url" not in p
        assert "api_key" not in p
        assert "default_model" not in p
        assert "created_at" not in p
        # Safe catalogue fields are preserved.
        assert set(p) == {"id", "name", "kind", "enabled", "models", "capabilities"}
        caps = p["capabilities"]
        assert set(caps) <= {"model_type_map", "model_settings"}
        assert "internal_secret" not in caps
        assert caps["model_type_map"] == {"llm": ["gpt-4o", "gpt-4o-mini"]}


# ─── PUT body validation ──────────────────────────────────────────────────────

def test_put_user_preferences_happy_path(client, authed_cookie, fake_gateway) -> None:
    resp = client.put(
        "/api/bff/v1/llm/user-preferences",
        json={"purpose": "embedding", "model_id": "text-embed-3", "provider_id": "p1"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 200
    assert any("/user-preferences" in u for _, u in fake_gateway.calls)


def test_put_user_preferences_bad_purpose(client, authed_cookie, fake_gateway) -> None:
    resp = client.put(
        "/api/bff/v1/llm/user-preferences",
        json={"purpose": "image", "model_id": "m", "provider_id": "p"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 422


def test_put_user_preferences_missing_provider(client, authed_cookie, fake_gateway) -> None:
    resp = client.put(
        "/api/bff/v1/llm/user-preferences",
        json={"purpose": "llm", "model_id": "m"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 422


def test_put_user_preferences_blank_model_id(client, authed_cookie, fake_gateway) -> None:
    resp = client.put(
        "/api/bff/v1/llm/user-preferences",
        json={"purpose": "llm", "model_id": "  ", "provider_id": "p"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 422


# ─── DELETE path-param purpose validation ─────────────────────────────────────

def test_delete_user_preference_valid_purpose(client, authed_cookie, fake_gateway) -> None:
    resp = client.delete("/api/bff/v1/llm/user-preferences/llm", cookies=authed_cookie)
    assert resp.status_code == 200


def test_delete_user_preference_invalid_purpose(client, authed_cookie, fake_gateway) -> None:
    resp = client.delete("/api/bff/v1/llm/user-preferences/bogus", cookies=authed_cookie)
    assert resp.status_code == 422
    # Must reject before forwarding to the gateway.
    assert not any("/user-preferences/bogus" in u for _, u in fake_gateway.calls)


# ─── resolve query purpose validation ─────────────────────────────────────────

def test_resolve_user_preferences_valid_purpose(client, authed_cookie, fake_gateway) -> None:
    resp = client.get(
        "/api/bff/v1/llm/user-preferences/resolve?purpose=rerank",
        cookies=authed_cookie,
    )
    assert resp.status_code == 200


def test_resolve_user_preferences_invalid_purpose(client, authed_cookie, fake_gateway) -> None:
    resp = client.get(
        "/api/bff/v1/llm/user-preferences/resolve?purpose=bogus",
        cookies=authed_cookie,
    )
    assert resp.status_code == 422
