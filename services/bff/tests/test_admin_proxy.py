"""Tests: BFF admin proxy + require_super_admin_bff guard.

These tests cover:
1. Unauthenticated requests are rejected with 401 / 403.
2. Authenticated callers with authz denied are rejected with 403.
3. Authenticated super_admin callers are proxied to foundation.
4. /auth/me includes 'roles' field (passthrough from identity).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

class FakeAdminFoundation:
    """Fake foundation responses for admin tests."""

    def __init__(self, *, authz_allowed: bool = True) -> None:
        self.calls: list[tuple[str, str]] = []
        self.authz_allowed = authz_allowed

    def route(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, url))

        # authz /check
        if "/check" in url:
            if self.authz_allowed:
                return httpx.Response(200, json={"allowed": True, "decision_path": "RBAC_HIT"})
            else:
                return httpx.Response(200, json={"allowed": False, "decision_path": "DEFAULT_DENY"})

        # identity /sessions/me
        if "/sessions/me" in url:
            return httpx.Response(200, json={
                "user_id": "usr_01J5TEST0000000000000USER1",
                "tenant_id": "tnt_01J5TEST00000000000000TNT1",
                "email": "admin@example.com",
                "display_name": "Admin User",
                "roles": ["super_admin"],
                "mfa_enabled": False,
                "locale": "zh-CN",
            })

        # identity /platform/tenants
        if "/platform/tenants" in url and method == "GET" and "{" not in url:
            return httpx.Response(200, json={"items": [], "next_cursor": None})

        if "/platform/tenants" in url and method == "POST":
            return httpx.Response(201, json={
                "tenant_id": "tnt_NEWONE0000000000000000001",
                "admin_user_id": "usr_NEWADMIN0000000000000001",
                "root_ou_id": None,
                "subscription_id": None,
                "steps": ["tenant_row_created"],
            })

        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def fake_admin_foundation_allowed(monkeypatch: pytest.MonkeyPatch) -> FakeAdminFoundation:
    fake = FakeAdminFoundation(authz_allowed=True)

    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, **kw): return fake.route("GET", url, **kw)
        async def post(self, url, **kw): return fake.route("POST", url, **kw)
        async def patch(self, url, **kw): return fake.route("PATCH", url, **kw)
        async def delete(self, url, **kw): return fake.route("DELETE", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)
    return fake


@pytest.fixture
def fake_admin_foundation_denied(monkeypatch: pytest.MonkeyPatch) -> FakeAdminFoundation:
    fake = FakeAdminFoundation(authz_allowed=False)

    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, **kw): return fake.route("GET", url, **kw)
        async def post(self, url, **kw): return fake.route("POST", url, **kw)
        async def patch(self, url, **kw): return fake.route("PATCH", url, **kw)
        async def delete(self, url, **kw): return fake.route("DELETE", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)
    return fake


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────

def test_admin_tenants_unauthenticated(client) -> None:
    """No cookie → 401 or 403."""
    resp = client.get("/api/bff/v1/admin/tenants")
    assert resp.status_code in (401, 403)


def test_admin_tenants_authz_denied(
    client,
    authed_cookie: dict,
    fake_admin_foundation_denied,
) -> None:
    """Authenticated but authz returns allowed=False → 403."""
    resp = client.get("/api/bff/v1/admin/tenants", cookies=authed_cookie)
    assert resp.status_code == 403


def test_admin_tenants_super_admin_allowed(
    client,
    authed_cookie: dict,
    fake_admin_foundation_allowed: FakeAdminFoundation,
) -> None:
    """Authenticated super_admin → proxied to foundation, 200 returned."""
    resp = client.get("/api/bff/v1/admin/tenants", cookies=authed_cookie)
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body


def test_admin_create_tenant_super_admin(
    client,
    authed_cookie: dict,
    fake_admin_foundation_allowed: FakeAdminFoundation,
) -> None:
    """POST /admin/tenants → proxied to foundation provisioning."""
    resp = client.post(
        "/api/bff/v1/admin/tenants",
        json={"name": "Test Corp", "tier": "FREE", "admin_email": "admin@test.com"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "tenant_id" in body


def test_auth_me_includes_roles(
    client,
    authed_cookie: dict,
    fake_admin_foundation_allowed: FakeAdminFoundation,
) -> None:
    """GET /auth/me must transparently pass through 'roles' from identity."""
    resp = client.get("/api/bff/v1/auth/me", cookies=authed_cookie)
    assert resp.status_code == 200
    body = resp.json()
    # roles must be present (identity /sessions/me returns it; BFF passes through)
    assert "roles" in body
    assert isinstance(body["roles"], list)
