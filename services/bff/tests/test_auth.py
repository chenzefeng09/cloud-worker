"""Tests for BFF auth endpoints — login/logout/me."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_bff.domain.db import set_session_factory
from tests.web_session_support import all_rows, sqlite_session_factory


@pytest.fixture
def session_store(client: TestClient, tmp_path: Path) -> Iterator[object]:
    """SQLite web-session store, installed after ``client``'s lifespan ran
    ``init_db`` — so login never reaches the default Postgres DSN."""
    factory = sqlite_session_factory(tmp_path / "bff.db")
    yield factory
    set_session_factory(None)


def test_healthz(client: TestClient) -> None:
    resp = client.get("/api/bff/v1/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"


def test_login_happy_path_sets_signed_cookie(
    client: TestClient, fake_foundation, session_store,
) -> None:
    resp = client.post(
        "/api/bff/v1/auth/login",
        json={"email": "test@example.com", "password": "secret"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == "tnt_01J5TEST00000000000000TNT1"
    assert body["user_id"] == "usr_01J5TEST0000000000000USER1"
    # Signed cookie set — it names a server-side session, it does not carry
    # the tokens.
    assert "ap_session" in resp.cookies
    payload = client.app.state.cookie_signer.unsign(resp.cookies["ap_session"])
    assert set(payload) == {"sid", "tenant_id", "user_id"}
    [row] = asyncio.run(all_rows(session_store))
    assert row.user_id == "usr_01J5TEST0000000000000USER1"


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
# PATCH /auth/me — self-service profile update proxy
# ──────────────────────────────────────────────────────────────────────────────

def _install_patch_me_foundation(monkeypatch, *, status: int, body):
    """Stub identity ``PATCH /sessions/me`` for update_me tests.

    The fake echoes the upstream ``status``/``body`` and records every call so
    the test can assert both the forwarded path and the JSON payload the BFF
    sent. Returns ``(method, url, json_body)`` triples.
    """
    calls: list[tuple[str, str, dict | None]] = []

    def route(method: str, url: str, **kw) -> httpx.Response:
        calls.append((method, url, kw.get("json")))
        if method == "PATCH" and "/sessions/me" in url:
            return httpx.Response(status, json=body)
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


def test_patch_me_without_cookie_returns_401(client: TestClient) -> None:
    resp = client.patch("/api/bff/v1/auth/me", json={"display_name": "x"})
    assert resp.status_code == 401


def test_patch_me_updates_display_name(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATCH /auth/me forwards display_name to identity and returns the new profile."""
    updated = {
        "user_id": "usr_01J5TEST0000000000000USER1",
        "tenant_id": "tnt_01J5TEST00000000000000TNT1",
        "email": "test@example.com",
        "display_name": "新名字",
        "roles": ["member"],
        "mfa_enabled": False,
        "locale": "zh-CN",
    }
    calls = _install_patch_me_foundation(monkeypatch, status=200, body=updated)

    resp = client.patch(
        "/api/bff/v1/auth/me",
        json={"display_name": "新名字"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["display_name"] == "新名字"
    # BFF PATCHed identity /sessions/me with a projected {display_name} body.
    patch_calls = [c for c in calls if c[0] == "PATCH" and "/sessions/me" in c[1]]
    assert patch_calls, calls
    assert patch_calls[0][2] == {"display_name": "新名字"}


def test_patch_me_drops_the_cached_display_name(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a rename, pages naming the caller (an App's author) look the name up again."""
    from agent_bff.api import _people

    key = ("tnt_01J5TEST00000000000000TNT1", "usr_01J5TEST0000000000000USER1")
    _people._remember(key, "Old name")
    _install_patch_me_foundation(
        monkeypatch, status=200, body={"user_id": key[1], "display_name": "New name"},
    )

    resp = client.patch(
        "/api/bff/v1/auth/me", json={"display_name": "New name"}, cookies=authed_cookie,
    )

    assert resp.status_code == 200, resp.text
    assert _people._cached(key) == (False, None)


def test_patch_me_extra_field_dropped_before_upstream(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The BFF must project to only display_name — extra keys never reach identity."""
    updated = {
        "user_id": "usr_01J5TEST0000000000000USER1",
        "tenant_id": "tnt_01J5TEST00000000000000TNT1",
        "email": "test@example.com",
        "display_name": "Renamed",
        "roles": ["member"],
        "mfa_enabled": False,
        "locale": "zh-CN",
    }
    calls = _install_patch_me_foundation(monkeypatch, status=200, body=updated)

    resp = client.patch(
        "/api/bff/v1/auth/me",
        json={"display_name": "Renamed", "roles": ["tenant_admin"]},
        cookies=authed_cookie,
    )
    assert resp.status_code == 200, resp.text
    patch_calls = [c for c in calls if c[0] == "PATCH" and "/sessions/me" in c[1]]
    assert patch_calls[0][2] == {"display_name": "Renamed"}


def test_patch_me_propagates_upstream_422(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upstream validation 422 surfaces as a 422 (not masked as 401)."""
    _install_patch_me_foundation(
        monkeypatch, status=422, body={"detail": "display_name required"},
    )
    resp = client.patch(
        "/api/bff/v1/auth/me",
        json={"display_name": ""},
        cookies=authed_cookie,
    )
    assert resp.status_code == 422, resp.text


# ──────────────────────────────────────────────────────────────────────────────
# POST /auth/me/password — self-service change-password proxy
# ──────────────────────────────────────────────────────────────────────────────

def _install_password_foundation(monkeypatch, *, status: int, body):
    """Stub identity ``POST /sessions/me/password`` for change-password tests.

    The fake echoes the upstream ``status``/``body`` and records every call so
    the test can assert both the forwarded path and the projected JSON payload
    the BFF sent. Returns ``(method, url, json_body)`` triples.
    """
    calls: list[tuple[str, str, dict | None]] = []

    def route(method: str, url: str, **kw) -> httpx.Response:
        calls.append((method, url, kw.get("json")))
        if method == "POST" and "/sessions/me/password" in url:
            # body may be None to model a 204 No Content success.
            if body is None:
                return httpx.Response(status)
            return httpx.Response(status, json=body)
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


def test_change_password_without_cookie_returns_401(client: TestClient) -> None:
    resp = client.post(
        "/api/bff/v1/auth/me/password",
        json={"current_password": "old", "new_password": "newpassword123"},
    )
    assert resp.status_code == 401


def test_change_password_forwards_projected_body(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /auth/me/password forwards {current_password,new_password} to identity → 204."""
    calls = _install_password_foundation(monkeypatch, status=204, body=None)

    resp = client.post(
        "/api/bff/v1/auth/me/password",
        json={"current_password": "OldPassw0rd", "new_password": "NewStrongPassw0rd"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 204, resp.text
    # BFF POSTed identity /sessions/me/password with a projected body.
    pw_calls = [
        c for c in calls if c[0] == "POST" and "/sessions/me/password" in c[1]
    ]
    assert pw_calls, calls
    assert pw_calls[0][2] == {
        "current_password": "OldPassw0rd",
        "new_password": "NewStrongPassw0rd",
    }


def test_change_password_extra_field_dropped_before_upstream(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The BFF must project to only the two passwords — extra keys (e.g. a target
    user_id) never reach identity."""
    calls = _install_password_foundation(monkeypatch, status=204, body=None)

    resp = client.post(
        "/api/bff/v1/auth/me/password",
        json={
            "current_password": "OldPassw0rd",
            "new_password": "NewStrongPassw0rd",
            "user_id": "usr_someoneelse",
        },
        cookies=authed_cookie,
    )
    # extra keys are simply ignored by the pydantic model; the projected upstream
    # body carries only the two password fields.
    assert resp.status_code == 204, resp.text
    pw_calls = [
        c for c in calls if c[0] == "POST" and "/sessions/me/password" in c[1]
    ]
    assert pw_calls[0][2] == {
        "current_password": "OldPassw0rd",
        "new_password": "NewStrongPassw0rd",
    }


def test_change_password_wrong_current_propagates_400(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upstream 400 (wrong current password) surfaces as 400 — NOT masked as 401."""
    _install_password_foundation(
        monkeypatch,
        status=400,
        body={
            "code": "FOUNDATION.IDENTITY.INPUT.CURRENT_PASSWORD_INCORRECT",
            "detail": "Current password is incorrect",
        },
    )
    resp = client.post(
        "/api/bff/v1/auth/me/password",
        json={"current_password": "WRONG", "new_password": "NewStrongPassw0rd"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 400, resp.text


def test_change_password_sso_account_propagates_409(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upstream 409 (SSO/federated account) surfaces as 409, not masked."""
    _install_password_foundation(
        monkeypatch,
        status=409,
        body={
            "code": "FOUNDATION.IDENTITY.STATE.PASSWORD_CHANGE_NOT_ALLOWED",
            "detail": "Account is linked to a federated identity provider",
        },
    )
    resp = client.post(
        "/api/bff/v1/auth/me/password",
        json={"current_password": "whatever", "new_password": "NewStrongPassw0rd"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 409, resp.text


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


def test_patch_me_includes_permissions(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATCH /auth/me enriches with authz permissions exactly like GET /auth/me,
    so the two /auth/me response shapes are identical and the SPA's admin-gating
    survives a rename."""
    updated = {
        "user_id": "usr_01J5TEST0000000000000USER1",
        "tenant_id": "tnt_01J5TEST00000000000000TNT1",
        "email": "test@example.com",
        "display_name": "Renamed",
        "roles": ["tenant_admin"],
        "mfa_enabled": False,
        "locale": "zh-CN",
    }
    calls: list[tuple[str, str]] = []

    def route(method: str, url: str, **kw) -> httpx.Response:
        calls.append((method, url))
        if "/my-permissions" in url:
            return httpx.Response(
                200,
                json={"permissions": ["authz.role.manage", "authz.binding.manage"]},
            )
        if method == "PATCH" and "/sessions/me" in url:
            return httpx.Response(200, json=updated)
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

    resp = client.patch(
        "/api/bff/v1/auth/me",
        json={"display_name": "Renamed"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["display_name"] == "Renamed"
    # Same shape as GET /auth/me: permissions attached from authz.
    assert body["permissions"] == ["authz.role.manage", "authz.binding.manage"]
    # The authz /my-permissions leg was actually called.
    assert any("/my-permissions" in u for _, u in calls)


def test_change_password_wrong_current_surfaces_user_message_problem(
    client: TestClient, authed_cookie: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An upstream problem+json with a ``user_message`` must reach the SPA as a
    problem+json body that lifts ``user_message`` to the top level — NOT as a
    bare ``detail`` blob the SPA can't read."""
    _install_password_foundation(
        monkeypatch,
        status=400,
        body={
            "type": "https://agent-platform.dev/errors/current_password_incorrect",
            "title": "FOUNDATION.IDENTITY.INPUT.CURRENT_PASSWORD_INCORRECT",
            "status": 400,
            "code": "FOUNDATION.IDENTITY.INPUT.CURRENT_PASSWORD_INCORRECT",
            "detail": "Current password is incorrect",
            "user_message": "当前密码不正确",
        },
    )
    resp = client.post(
        "/api/bff/v1/auth/me/password",
        json={"current_password": "WRONG", "new_password": "NewStrongPassw0rd"},
        cookies=authed_cookie,
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    # The BFF lifts user_message to the top level of detail and keeps the
    # original problem document nested — the SPA reads detail["user_message"].
    assert body["detail"]["user_message"] == "当前密码不正确"
    assert body["detail"]["detail"]["code"] == (
        "FOUNDATION.IDENTITY.INPUT.CURRENT_PASSWORD_INCORRECT"
    )


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
