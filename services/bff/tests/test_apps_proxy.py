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


def test_run_app_webhook_source_forwarded(
    client_and_stub: tuple, authed: dict[str, str]
) -> None:
    """source='webhook' is accepted by run_app and forwarded to the orchestrator."""
    client, fake = client_and_stub
    resp = client.post(
        "/api/bff/v1/apps/app_test/run",
        json={"inputs": {"task": "webhook job"}, "source": "webhook"},
        cookies=authed,
    )
    assert resp.status_code == 201, resp.text
    body = fake.runs_call()
    assert body is not None, "expected a POST .../runs call"
    assert body.get("source") == "webhook"


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


def test_run_app_forwards_forced_mcp_server_ids(
    client_and_stub: tuple, authed: dict[str, str]
) -> None:
    """The App's forced MCP servers must reach the orchestrator run body.

    ``forced_mcp_server_ids`` existed on the App entity but was never consumed
    by any execution layer; this locks the wiring.
    """
    client, fake = client_and_stub
    _APP_STUB["versions"][0]["forced_mcp_server_ids"] = ["mcp_x", "mcp_y"]
    try:
        resp = client.post(
            "/api/bff/v1/apps/app_test/run",
            json={"inputs": {"task": "hello"}},
            cookies=authed,
        )
    finally:
        _APP_STUB["versions"][0]["forced_mcp_server_ids"] = []
    assert resp.status_code == 201, resp.text
    body = fake.runs_call()
    assert body is not None
    assert body["mcp_server_ids"] == ["mcp_x", "mcp_y"]


def test_run_app_omits_mcp_server_ids_when_app_forces_none(
    client_and_stub: tuple, authed: dict[str, str]
) -> None:
    """An App with no forced servers must NOT send the key — backward compat.

    Deliberately asymmetric with ``skills`` (which IS always sent): Apps were
    published before this field was consumed by any execution layer and today
    inherit the tenant's enabled MCP set.  Sending ``[]`` here would silently
    strip every existing App's MCP tools on its next run.  Omitting the key
    preserves the inheritance.

    The accepted cost is that an App cannot express "disable all MCP"; that
    needs a tri-state ``forced_mcp_server_ids`` in app-registry first.
    """
    client, fake = client_and_stub
    resp = client.post(
        "/api/bff/v1/apps/app_test/run",
        json={"inputs": {"task": "hello"}},
        cookies=authed,
    )
    assert resp.status_code == 201, resp.text
    body = fake.runs_call()
    assert body is not None
    assert "mcp_server_ids" not in body
    # ``skills`` stays unconditional — the asymmetry is the point, not a bug.
    assert body["skills"] == []


# ---------------------------------------------------------------------------
# GET /apps/{id}/runs - App execution history proxy
# ---------------------------------------------------------------------------

_APP_ID = "app_01J5TEST00000000000000APP1"

_HISTORY_RUNS = [
    {
        "id": "run_001",
        "session_id": "ses_a",
        "status": "COMPLETED",
        "task_description": "t",
        "result_summary": "All done.",
        "error_message": None,
        "started_at": "2026-07-20T10:00:00Z",
        "finished_at": "2026-07-20T10:05:00Z",
        "created_at": "2026-07-20T09:59:00Z",
        "input_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 30,
        "source": "user",
        "execution_mode": "sandbox",
        "app_snapshot": {
            "app_id": _APP_ID,
            "app_name": "My App",
            "app_version": 1,
            "app_version_id": "ver_1",
            "model": "gpt-test",
            "system_prompt": "SECRET-SHOULD-NOT-LEAK",
            "user_prompt_template": "Do {thing}",
            "rendered_task_preview": "Do hello",
            "sampling_params": {"temperature": 0.7},
            "inputs": [
                {"key": "thing", "label": "Thing", "type": "text", "value": "hello"},
            ],
        },
    },
    {
        "id": "run_002",
        "session_id": "ses_b",
        "status": "RUNNING",
        "task_description": "t2",
        "result_summary": None,
        "error_message": None,
        "started_at": "2026-07-20T11:00:00Z",
        "finished_at": None,
        "created_at": "2026-07-20T10:59:00Z",
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "source": "user",
        "execution_mode": "sandbox",
        "app_snapshot": {
            "app_id": _APP_ID,
            "app_name": "My App",
            "app_version": 1,
            "app_version_id": "ver_1",
            "model": "gpt-test",
            "inputs": [],
        },
    },
]


class FakeAppHistoryFoundation:
    """HTTP stub for GET /apps/{id}/runs.

    * ``GET {registry}/apps/{id}`` -> 200 (app exists) or 404 for unknown ids.
    * ``GET {orchestrator}/runs?app_id=...`` -> the canned history payload,
      capturing the query params so the proxy's forwarding is assertable.
    """

    def __init__(self) -> None:
        self.get_runs_calls: list[dict | None] = []
        self.app_get_calls: list[str] = []

    def route(self, method: str, url: str, **kwargs) -> httpx.Response:
        if method == "GET" and "/apps/" in url and "/runs" not in url:
            self.app_get_calls.append(url)
            # Unknown app -> 404 so the 404-propagation test is meaningful.
            if "app_unknown" in url:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(200, json=_APP_STUB)
        if method == "GET" and "/runs" in url:
            self.get_runs_calls.append(kwargs.get("params"))
            return httpx.Response(200, json={"items": _HISTORY_RUNS})
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def client_and_history_stub(
    settings_for_apps: Settings, monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, FakeAppHistoryFoundation]:
    fake = FakeAppHistoryFoundation()

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


def test_list_app_runs_requires_auth(client: TestClient) -> None:
    resp = client.get(f"/api/bff/v1/apps/{_APP_ID}/runs")
    assert resp.status_code == 401


def test_list_app_runs_proxies_to_orchestrator_with_app_id(
    client_and_history_stub: tuple, authed: dict[str, str],
) -> None:
    """The endpoint calls orchestrator GET /runs?app_id=<id> and registry GET /apps/<id>."""
    client, fake = client_and_history_stub
    resp = client.get(f"/api/bff/v1/apps/{_APP_ID}/runs", cookies=authed)
    assert resp.status_code == 200, resp.text

    # Light validation: app-existence check against the registry.
    assert len(fake.app_get_calls) == 1
    assert _APP_ID in fake.app_get_calls[0]
    # History fetch: orchestrator /runs with app_id param.
    assert len(fake.get_runs_calls) == 1
    params = fake.get_runs_calls[0] or {}
    assert params.get("app_id") == _APP_ID


def test_list_app_runs_trims_snapshot_to_inputs_summary(
    client_and_history_stub: tuple, authed: dict[str, str],
) -> None:
    """Response carries a short inputs summary, not the full app_snapshot."""
    client, _ = client_and_history_stub
    resp = client.get(f"/api/bff/v1/apps/{_APP_ID}/runs", cookies=authed)
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert [r["id"] for r in data["items"]] == ["run_001", "run_002"]

    first = data["items"][0]
    # The heavy snapshot fields must NOT leak into the history view.
    assert "app_snapshot" not in first
    assert "system_prompt" not in first
    # Compact app context + inputs summary are present.
    assert first["app"]["app_id"] == _APP_ID
    assert first["app"]["model"] == "gpt-test"
    assert first["inputs"] == [
        {"key": "thing", "label": "Thing", "type": "text", "value": "hello"},
    ]
    # result_summary is surfaced for a finished run.
    assert first["result_summary"] == "All done."
    assert first["finished_at"] == "2026-07-20T10:05:00Z"


def test_list_app_runs_forwards_limit_offset(
    client_and_history_stub: tuple, authed: dict[str, str],
) -> None:
    """?limit / ?offset are forwarded to the orchestrator and echoed back."""
    client, fake = client_and_history_stub
    resp = client.get(
        f"/api/bff/v1/apps/{_APP_ID}/runs",
        params={"limit": 10, "offset": 20},
        cookies=authed,
    )
    assert resp.status_code == 200, resp.text
    params = fake.get_runs_calls[0] or {}
    assert params.get("limit") == 10
    assert params.get("offset") == 20
    body = resp.json()
    assert body["limit"] == 10
    assert body["offset"] == 20


def test_list_app_runs_unknown_app_returns_404(
    client_and_history_stub: tuple, authed: dict[str, str],
) -> None:
    """A nonexistent app surfaces the registry's 404 (no orchestrator call)."""
    client, fake = client_and_history_stub
    resp = client.get("/api/bff/v1/apps/app_unknown/runs", cookies=authed)
    assert resp.status_code == 404
    # The history fetch must not have happened.
    assert fake.get_runs_calls == []


def test_list_app_runs_limit_out_of_range_rejected(
    client_and_history_stub: tuple, authed: dict[str, str],
) -> None:
    """limit is constrained ge=1, le=200 by FastAPI Query validation."""
    client, _ = client_and_history_stub
    assert client.get(
        f"/api/bff/v1/apps/{_APP_ID}/runs",
        params={"limit": 0}, cookies=authed,
    ).status_code == 422
    assert client.get(
        f"/api/bff/v1/apps/{_APP_ID}/runs",
        params={"limit": 201}, cookies=authed,
    ).status_code == 422


# ---------------------------------------------------------------------------
# GET /apps + POST /apps - proxy forwarding of scope / tag / tags body
# ---------------------------------------------------------------------------

class FakeAppsCrudFoundation:
    """HTTP stub for the BFF list/create App proxies.

    Captures the query params (GET /apps) and JSON body (POST /apps) the BFF
    forwards to app-registry so the proxy contract is assertable.
    """

    def __init__(self) -> None:
        self.list_calls: list[dict | None] = []
        self.create_calls: list[dict | None] = []

    def route(self, method: str, url: str, **kwargs) -> httpx.Response:
        if method == "GET" and url.endswith("/apps"):
            self.list_calls.append(kwargs.get("params"))
            return httpx.Response(200, json={"items": [], "total": 0})
        if method == "POST" and url.endswith("/apps"):
            self.create_calls.append(kwargs.get("json"))
            return httpx.Response(201, json={"id": "app_new", "tags": []})
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def client_and_crud_stub(
    settings_for_apps: Settings, monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, FakeAppsCrudFoundation]:
    fake = FakeAppsCrudFoundation()

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


def _params_to_multimap(params: object) -> dict[str, list[str]]:
    """Normalize forwarded params (list[tuple] | dict | QueryParams) -> multimap."""
    out: dict[str, list[str]] = {}
    if params is None:
        return out
    if isinstance(params, dict):
        for k, v in params.items():
            out.setdefault(k, []).extend(v if isinstance(v, list) else [v])
    else:  # list[tuple[str, str]] / httpx.QueryParams (iterable of pairs)
        for k, v in params:
            out.setdefault(k, []).append(v)
    return out


def test_list_apps_forwards_scope_and_tag_params(
    client_and_crud_stub: tuple, authed: dict[str, str],
) -> None:
    """?scope=mine&tag=foo&tag=bar is forwarded as repeated query params."""
    client, fake = client_and_crud_stub
    resp = client.get(
        "/api/bff/v1/apps",
        params={"scope": "mine", "tag": ["foo", "bar"]},
        cookies=authed,
    )
    assert resp.status_code == 200, resp.text
    assert len(fake.list_calls) == 1
    mm = _params_to_multimap(fake.list_calls[0])
    assert mm.get("scope") == ["mine"]
    # Repeated tag params survive forwarding (OR semantics rely on this).
    assert sorted(mm.get("tag", [])) == ["bar", "foo"]


def test_list_apps_no_params_when_none_passed(
    client_and_crud_stub: tuple, authed: dict[str, str],
) -> None:
    """A bare GET /apps forwards no scope/tag params to app-registry."""
    client, fake = client_and_crud_stub
    resp = client.get("/api/bff/v1/apps", cookies=authed)
    assert resp.status_code == 200, resp.text
    mm = _params_to_multimap(fake.list_calls[0])
    assert "scope" not in mm
    assert "tag" not in mm


def test_create_app_forwards_tags_in_body(
    client_and_crud_stub: tuple, authed: dict[str, str],
) -> None:
    """The ``tags`` field in the POST body is passed through to app-registry."""
    client, fake = client_and_crud_stub
    body = {
        "name": "New App",
        "tags": ["legal", "contract"],
        "version": {
            "system_prompt": "x",
            "user_prompt_template": "y",
            "model": "m",
        },
    }
    resp = client.post("/api/bff/v1/apps", json=body, cookies=authed)
    assert resp.status_code == 201, resp.text
    assert len(fake.create_calls) == 1
    forwarded = fake.create_calls[0] or {}
    assert forwarded.get("tags") == ["legal", "contract"]
    assert forwarded.get("name") == "New App"
