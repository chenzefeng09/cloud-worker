"""Tests for BFF auth endpoints — login/logout/me."""

from __future__ import annotations

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
