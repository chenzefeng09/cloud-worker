"""Test fixtures for BFF."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_bff.config import Settings
from agent_bff.main import create_app

# 48-char random-but-fixed string for reproducible tests; satisfies the
# CookieSigner length + placeholder validation.
TEST_COOKIE_SECRET = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKL"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        cookie_secret=TEST_COOKIE_SECRET,
        cookie_secure=False,
        foundation_identity_url="http://identity-test/api/identity/v1",
        session_orchestrator_url="http://orch-test:8006/api/session/v1",
        cors_origins="http://localhost",
        otel_enabled=False,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    # raise_server_exceptions=False lets the catch-all problem-details
    # handler actually run (otherwise TestClient re-raises before the
    # response can be inspected).
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def authed_cookie(settings: Settings) -> dict[str, str]:
    """Return a valid signed session cookie payload for use as ``cookies=`` arg.

    Avoids the full login round-trip when the test only cares about
    post-auth endpoints.
    """
    from foundation_sdk import CookieSigner, CookieSignerSettings

    signer = CookieSigner(
        CookieSignerSettings(
            secret_key=settings.cookie_secret,
            salt="agent-bff-session",
            max_age_seconds=settings.cookie_max_age,
        ),
    )
    token = signer.sign(
        {
            "user_id": "usr_01J5TEST0000000000000USER1",
            "tenant_id": "tnt_01J5TEST00000000000000TNT1",
            "access_token": "fake-jwt-for-test",
        },
    )
    return {settings.cookie_name: token}


class FakeFoundation:
    """Minimal stub of foundation identity / orchestrator HTTP API.

    Tests pass instances of this into ``monkeypatch`` so the BFF talks to
    deterministic responses instead of the real httpx network stack.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.identity_login_response: tuple[int, dict] = (
            201,
            {
                "user_id": "usr_01J5TEST0000000000000USER1",
                "tenant_id": "tnt_01J5TEST00000000000000TNT1",
                "access_token": "fake-jwt",
                "refresh_token": "fake-refresh",
                "display_name": "Test User",
            },
        )
        self.identity_me_response: tuple[int, dict] = (
            200,
            {
                "user_id": "usr_01J5TEST0000000000000USER1",
                "tenant_id": "tnt_01J5TEST00000000000000TNT1",
                "email": "test@example.com",
                "display_name": "Test User",
                "roles": [],
                "mfa_enabled": False,
                "locale": "zh-CN",
            },
        )

    def route(self, method: str, url: str, **kwargs) -> httpx.Response:
        self.calls.append((method, url, kwargs.get("json")))
        if "/sessions/me" in url:
            status, body = self.identity_me_response
        elif "/sessions/current" in url and method == "DELETE":
            status, body = 204, {}
        elif "/sessions" in url and method == "POST" and "identity" in url:
            status, body = self.identity_login_response
        elif "attachments" in url and method == "GET":
            status, body = 200, {"items": []}
        elif "attachments" in url and method == "POST":
            status, body = 201, {
                "id": "att_test",
                "session_id": "ses_abc",
                "filename": "test.txt",
                "mime": "text/plain",
                "size_bytes": 4,
                "blob_key": "tenant/t/sessions/s/attachments/att_test/test.txt",
                "created_at": "2026-06-03T00:00:00Z",
            }
        else:
            status, body = 200, {"ok": True}
        return httpx.Response(status, json=body)


@pytest.fixture
def fake_foundation(monkeypatch: pytest.MonkeyPatch) -> FakeFoundation:
    fake = FakeFoundation()

    class _AsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> _AsyncClient:
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url: str, **kw) -> httpx.Response:
            return fake.route("POST", url, **kw)

        async def get(self, url: str, **kw) -> httpx.Response:
            return fake.route("GET", url, **kw)

        async def delete(self, url: str, **kw) -> httpx.Response:
            return fake.route("DELETE", url, **kw)

        async def patch(self, url: str, **kw) -> httpx.Response:
            return fake.route("PATCH", url, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    return fake
