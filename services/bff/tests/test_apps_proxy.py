"""Tests for BFF /apps/{id}/run endpoint — source field forwarding."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_bff.config import Settings
from agent_bff.main import create_app


# ---------------------------------------------------------------------------
# Minimal stub that returns just enough for run_app to succeed end-to-end.
# ---------------------------------------------------------------------------

_APP_STUB = {
    "id": "app_test",
    "name": "Test App",
    "versions": [
        {
            "id": "ver_test",
            "status": "published",
            "version": 1,
            "system_prompt": "You are a test assistant.",
            "user_prompt_template": "Do {task}.",
            "model": "test-model",
            "reasoning_effort": None,
            "sampling_params": {},
            "forced_skills": [],
            "forced_mcp_server_ids": [],
            "input_schema": [
                {"key": "task", "label": "Task", "type": "text", "required": True},
            ],
        }
    ],
}


class FakeRunAppFoundation:
    """HTTP stub for run_app — tracks calls and what JSON body was sent."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self._session_created = False

    def route(self, method: str, url: str, **kwargs) -> httpx.Response:
        self.calls.append((method, url, kwargs.get("json")))
        if "apps/" in url and method == "GET":
            return httpx.Response(200, json=_APP_STUB)
        if "/sessions" in url and method == "POST" and "runs" not in url:
            self._session_created = True
            return httpx.Response(201, json={"id": "ses_new", "status": "ACTIVE"})
        if "/runs" in url and method == "POST":
            return httpx.Response(
                201,
                json={"id": "run_abc", "status": "RUNNING"},
            )
        if "attachments" in url and method == "GET":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={"ok": True})

    def runs_call(self) -> dict | None:
        """Return the JSON body of the POST .../runs call if it happened."""
        for method, url, body in self.calls:
            if method == "POST" and "/runs" in url and "sessions" in url:
                return body
        return None


@pytest.fixture
def settings_for_apps() -> Settings:
    from tests.conftest import TEST_COOKIE_SECRET

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
def client_and_stub(
    settings_for_apps: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, FakeRunAppFoundation]:
    fake = FakeRunAppFoundation()

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

    app = create_app(settings_for_apps)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, fake


@pytest.fixture
def authed(settings_for_apps: Settings) -> dict[str, str]:
    from foundation_sdk import CookieSigner, CookieSignerSettings

    signer = CookieSigner(
        CookieSignerSettings(
            secret_key=settings_for_apps.cookie_secret,
            salt="agent-bff-session",
            max_age_seconds=settings_for_apps.cookie_max_age,
        ),
    )
    token = signer.sign(
        {
            "user_id": "usr_01J5TEST0000000000000USER1",
            "tenant_id": "tnt_01J5TEST00000000000000TNT1",
            "access_token": "fake-jwt-for-test",
        },
    )
    return {settings_for_apps.cookie_name: token}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_app_default_source_is_user(
    client_and_stub: tuple, authed: dict[str, str]
) -> None:
    """When no source is supplied, run_app forwards source='user' to orchestrator."""
    client, fake = client_and_stub
    resp = client.post(
        "/api/bff/v1/apps/app_test/run",
        json={"inputs": {"task": "hello"}},
        cookies=authed,
    )
    assert resp.status_code == 201, resp.text
    body = fake.runs_call()
    assert body is not None, "expected a POST .../runs call"
    assert body.get("source") == "user"


def test_run_app_schedule_source_forwarded(
    client_and_stub: tuple, authed: dict[str, str]
) -> None:
    """When source='schedule', run_app forwards it to the orchestrator runs payload."""
    client, fake = client_and_stub
    resp = client.post(
        "/api/bff/v1/apps/app_test/run",
        json={"inputs": {"task": "scheduled job"}, "source": "schedule"},
        cookies=authed,
    )
    assert resp.status_code == 201, resp.text
    body = fake.runs_call()
    assert body is not None, "expected a POST .../runs call"
    assert body.get("source") == "schedule"


def test_run_app_invalid_source_returns_400(
    client_and_stub: tuple, authed: dict[str, str]
) -> None:
    """An unrecognised source value should return 400 before hitting orchestrator."""
    client, fake = client_and_stub
    resp = client.post(
        "/api/bff/v1/apps/app_test/run",
        json={"inputs": {"task": "anything"}, "source": "bot"},
        cookies=authed,
    )
    assert resp.status_code == 400
    # No runs call should have been made.
    assert fake.runs_call() is None
