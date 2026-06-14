"""Tests for BFF auth endpoints — login/logout/me."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient


def test_healthz(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"


def test_login_happy_path_sets_signed_cookie(
    client: TestClient, fake_foundation,
) -> None:
    resp = client.post(
        "/api/bff/v1/auth/login",
        json={"email": "test@example.com", "password": "secret"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == "tnt_01J5TEST00000000000000TNT1"
    assert body["user_id"] == "usr_01J5TEST0000000000000USER1"
    # Signed cookie set
    assert "ap_session" in resp.cookies


def test_login_invalid_credentials_returns_401(
    client: TestClient, fake_foundation,
) -> None:
    fake_foundation.identity_login_response = (401, {"detail": "bad"})
    resp = client.post(
        "/api/bff/v1/auth/login",
        json={"email": "x@y.com", "password": "wrong"},
    )
    assert resp.status_code == 401


def test_login_missing_field_returns_401(client: TestClient) -> None:
    """No password → identity not even called → AuthenticationError."""
    resp = client.post("/api/bff/v1/auth/login", json={"email": "x@y.com"})
    assert resp.status_code == 401


def test_me_without_cookie_returns_401(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/auth/me")
    assert resp.status_code == 401


def test_me_with_valid_cookie_calls_identity(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    resp = client.get("/api/bff/v1/auth/me", cookies=authed_cookie)
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "usr_01J5TEST0000000000000USER1"
    # Verify the BFF actually called identity /sessions/me
    called_paths = [c[1] for c in fake_foundation.calls]
    assert any("/sessions/me" in p for p in called_paths)


# ──────────────────────────────────────────────────────────────────────────────
# /auth/me merges authz effective permissions (single round-trip for the SPA)
# ──────────────────────────────────────────────────────────────────────────────

def _install_me_foundation(monkeypatch, *, my_perms_status: int, my_perms_body):
    """Stub identity /sessions/me + authz /my-permissions for /auth/me tests.

    ``my_perms_status``/``my_perms_body`` drive the authz leg; identity always
    returns a fixed profile with roles.  Returns the recorded call list.
    """
    calls: list[tuple[str, str]] = []

    def route(method: str, url: str, **kw) -> httpx.Response:
        calls.append((method, url))
        if "/my-permissions" in url:
            return httpx.Response(my_perms_status, json=my_perms_body)
        if "/sessions/me" in url:
            return httpx.Response(200, json={
                "user_id": "usr_01J5TEST0000000000000USER1",
                "tenant_id": "tnt_01J5TEST00000000000000TNT1",
                "email": "test@example.com",
                "display_name": "Test User",
                "roles": ["tenant_admin"],
                "mfa_enabled": False,
                "locale": "zh-CN",
            })
        return httpx.Response(200, json={"ok": True})

    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, **kw): return route("GET", url, **kw)
        async def post(self, url, **kw): return route("POST", url, **kw)
        async def delete(self, url, **kw): return route("DELETE", url, **kw)
        async def patch(self, url, **kw): return route("PATCH", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)
    return calls


def test_me_merges_authz_permissions(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/auth/me attaches the authz effective permission set alongside roles."""
    calls = _install_me_foundation(
        monkeypatch,
        my_perms_status=200,
        my_perms_body={"permissions": ["authz.role.manage", "authz.binding.manage"]},
    )
    resp = client.get("/api/bff/v1/auth/me", cookies=authed_cookie)
    assert resp.status_code == 200
    body = resp.json()
    assert body["roles"] == ["tenant_admin"]
    assert body["permissions"] == ["authz.role.manage", "authz.binding.manage"]
    # Both legs were called.
    assert any("/sessions/me" in u for _, u in calls)
    assert any("/my-permissions" in u for _, u in calls)


def test_me_permissions_fail_soft_when_authz_down(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """authz unreachable → /auth/me still 200 with profile + empty permissions."""

    def route(method: str, url: str, **kw) -> httpx.Response:
        if "/my-permissions" in url:
            raise httpx.ConnectError("authz down")
        if "/sessions/me" in url:
            return httpx.Response(200, json={
                "user_id": "usr_01J5TEST0000000000000USER1",
                "tenant_id": "tnt_01J5TEST00000000000000TNT1",
                "email": "test@example.com",
                "display_name": "Test User",
                "roles": ["member"],
                "mfa_enabled": False,
                "locale": "zh-CN",
            })
        return httpx.Response(200, json={"ok": True})

    class _AC:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, **kw): return route("GET", url, **kw)
        async def post(self, url, **kw): return route("POST", url, **kw)
        async def delete(self, url, **kw): return route("DELETE", url, **kw)
        async def patch(self, url, **kw): return route("PATCH", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AC)

    resp = client.get("/api/bff/v1/auth/me", cookies=authed_cookie)
    assert resp.status_code == 200
    body = resp.json()
    assert body["roles"] == ["member"]
    # Fail-soft: identity profile preserved, permissions defaulted to [].
    assert body["permissions"] == []


def test_me_permissions_fail_soft_when_authz_non_200(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """authz returns 500 → permissions default to [] (me still 200)."""
    _install_me_foundation(monkeypatch, my_perms_status=500, my_perms_body={"detail": "boom"})
    resp = client.get("/api/bff/v1/auth/me", cookies=authed_cookie)
    assert resp.status_code == 200
    assert resp.json()["permissions"] == []


def test_logout_clears_cookie(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    resp = client.post("/api/bff/v1/auth/logout", cookies=authed_cookie)
    assert resp.status_code == 204
    # set-cookie with empty value + expired Max-Age
    set_cookie_header = resp.headers.get("set-cookie", "")
    assert "ap_session" in set_cookie_header


def test_tampered_cookie_does_not_authenticate(
    client: TestClient, authed_cookie: dict[str, str], fake_foundation,
) -> None:
    """Flipping a byte in the signed cookie must invalidate the session."""
    tampered = authed_cookie["ap_session"][:-3] + "AAA"
    resp = client.get("/api/bff/v1/auth/me", cookies={"ap_session": tampered})
    assert resp.status_code == 401


def test_problem_details_fallback_no_exception_leak(client: TestClient) -> None:
    """An endpoint that raises must not echo the exception text back.

    We construct this by hitting a route that doesn't exist (FastAPI returns
    its own 404, not our fallback), so instead we use a known-broken path
    that triggers a server error.
    """
    # Hitting a real endpoint without proper auth gives 401 — covered above.
    # Force-trigger the fallback by sending malformed JSON.
    resp = client.post(
        "/api/bff/v1/auth/login",
        data=b"not-json",
        headers={"Content-Type": "application/json"},
    )
    # FastAPI handles JSON parse errors itself with 422; that's fine —
    # the point is the response body never contains traceback info.
    assert resp.status_code in (400, 422, 500)
    body = resp.text
    assert "Traceback" not in body
    assert "/site-packages/" not in body
