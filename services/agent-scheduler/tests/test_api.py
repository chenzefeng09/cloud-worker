"""Integration tests for the scheduler HTTP API — tenant isolation, token minting."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

# Mirrors conftest._TENANT / _USER (the headers the gateway injects).
_TENANT = "tnt_01J5TEST00000000000000TNT1"


def _read_run_secrets(
    workspace_base: str, *, tenant_id: str, session_id: str, run_id: str
) -> dict[str, str]:
    """Secrets are delivered out-of-band via /workspace/.secrets.json (0600),
    not the container env (which leaks via /proc/1/environ)."""
    path = (
        Path(workspace_base) / tenant_id / session_id / run_id / ".secrets.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_healthz(app_client: TestClient) -> None:
    resp = app_client.get("/api/scheduler/v1/healthz")
    assert resp.status_code == 200


def test_sandboxes_list_requires_tenant_header(app_client: TestClient) -> None:
    # authz guard fires before the current_tenant dependency: missing tenant
    # is caught at the guard level (403) before the 401 dependency runs.
    resp = app_client.get("/api/scheduler/v1/sandboxes")
    assert resp.status_code in (401, 403)


def test_sandboxes_list_returns_empty_for_clean_tenant(
    app_client: TestClient, mock_docker_client: MagicMock, auth_headers,
) -> None:
    mock_docker_client.containers.list.return_value = []
    resp = app_client.get("/api/scheduler/v1/sandboxes", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_stop_sandbox_rejects_cross_tenant(
    app_client: TestClient, mock_docker_client: MagicMock, auth_headers,
) -> None:
    """Container owned by tnt_other → 404 to the caller in tnt_test."""
    container = MagicMock()
    container.labels = {"agent-platform.tenant-id": "tnt_OTHER"}
    mock_docker_client.containers.get.return_value = container
    resp = app_client.post(
        "/api/scheduler/v1/sandboxes/c_alien/stop", headers=auth_headers,
    )
    assert resp.status_code == 404


def test_create_run_mints_token_then_spawns(
    app_client: TestClient,
    mock_docker_client: MagicMock,
    fake_identity,
    auth_headers,
) -> None:
    spawned = MagicMock()
    spawned.id = "container-abc"
    mock_docker_client.containers.run.return_value = spawned

    resp = app_client.post(
        "/api/scheduler/v1/runs",
        headers=auth_headers,
        json={
            "session_id": "ses_01J5SES0000000000000000001",
            "run_id": "run_01J5RUN0000000000000000001",
            "task": "hello world",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["sandbox_id"] == "container-abc"

    # Identity was actually consulted, with the minimum scope set.
    assert fake_identity.calls, "identity was not called to mint a token"
    minted = fake_identity.calls[0]
    assert minted["tenant_id"] == auth_headers["X-Tenant-Id"]
    assert "llm-gateway.invoke" in minted["scopes"]
    # And the spawn used the *minted* token, not the scheduler's own.  Secrets
    # are now delivered via /workspace/.secrets.json, not the container env, so
    # they must NOT appear in environment= but MUST be in the file.
    spawn_env = mock_docker_client.containers.run.call_args.kwargs["environment"]
    assert "FOUNDATION_API_TOKEN" not in spawn_env
    assert "LLM_VIRTUAL_KEY" not in spawn_env
    assert "LLM_API_KEY" not in spawn_env
    secrets = _read_run_secrets(
        "/tmp/agent-platform-test",
        tenant_id=_TENANT,
        session_id="ses_01J5SES0000000000000000001",
        run_id="run_01J5RUN0000000000000000001",
    )
    assert secrets["FOUNDATION_API_TOKEN"] == "minted-jwt"
    assert secrets["LLM_VIRTUAL_KEY"] == "vk-test-default"
    assert secrets["LLM_API_KEY"] == ""


def test_create_run_forwards_effective_clearance_to_sandbox_env(
    app_client: TestClient,
    mock_docker_client: MagicMock,
    fake_identity,
    auth_headers,
) -> None:
    """``effective_clearance`` in the request body must land in the sandbox env
    as FOUNDATION_USER_CLEARANCE (plain env, not the secrets file) so the
    in-sandbox knowledge_tool can send X-User-Clearance to knowledge-ingest."""
    spawned = MagicMock()
    spawned.id = "container-clearance"
    mock_docker_client.containers.run.return_value = spawned

    resp = app_client.post(
        "/api/scheduler/v1/runs",
        headers=auth_headers,
        json={
            "session_id": "ses_01J5SES000000000000CLR0001",
            "run_id": "run_01J5RUN000000000000CLR0001",
            "task": "clearance flow",
            "effective_clearance": "L3",
        },
    )
    assert resp.status_code == 201, resp.text

    spawn_env = mock_docker_client.containers.run.call_args.kwargs["environment"]
    assert spawn_env["FOUNDATION_USER_CLEARANCE"] == "L3"
    # Also wired: the ki URL + the knowledge_tool extension name.
    assert "KNOWLEDGE_INGEST_URL" in spawn_env
    assert "knowledge_tool" in spawn_env["RUNTIME_EXTENSIONS"]
    # Clearance is NOT secret -> must not ride in the secrets file.
    secrets = _read_run_secrets(
        "/tmp/agent-platform-test",
        tenant_id=_TENANT,
        session_id="ses_01J5SES000000000000CLR0001",
        run_id="run_01J5RUN000000000000CLR0001",
    )
    assert "FOUNDATION_USER_CLEARANCE" not in secrets


def test_create_run_large_attachment_uses_large_memory_tier(
    app_client: TestClient,
    mock_docker_client: MagicMock,
    fake_identity,
    auth_headers,
) -> None:
    """total_attachment_bytes >= threshold → large memory tier (4g)."""
    spawned = MagicMock()
    spawned.id = "container-large"
    mock_docker_client.containers.run.return_value = spawned

    resp = app_client.post(
        "/api/scheduler/v1/runs",
        headers=auth_headers,
        json={
            "session_id": "ses_01J5SES0000000000000000001",
            "run_id": "run_01J5RUN0000000000000000001",
            "task": "hello world",
            # 60 MiB >= 50 MiB threshold.
            "total_attachment_bytes": 60 * 1024 * 1024,
        },
    )
    assert resp.status_code == 201, resp.text
    mem_limit = mock_docker_client.containers.run.call_args.kwargs["mem_limit"]
    assert mem_limit == "4g"


def test_create_run_small_attachment_uses_small_memory_tier(
    app_client: TestClient,
    mock_docker_client: MagicMock,
    fake_identity,
    auth_headers,
) -> None:
    """total_attachment_bytes below threshold (and the default 0) → small tier (1g)."""
    spawned = MagicMock()
    spawned.id = "container-small"
    mock_docker_client.containers.run.return_value = spawned

    resp = app_client.post(
        "/api/scheduler/v1/runs",
        headers=auth_headers,
        json={
            "session_id": "ses_01J5SES0000000000000000001",
            "run_id": "run_01J5RUN0000000000000000001",
            "task": "hello world",
            # 1 MiB < 50 MiB threshold.
            "total_attachment_bytes": 1 * 1024 * 1024,
        },
    )
    assert resp.status_code == 201, resp.text
    mem_limit = mock_docker_client.containers.run.call_args.kwargs["mem_limit"]
    assert mem_limit == "1g"


def test_create_run_validation(app_client: TestClient, auth_headers) -> None:
    resp = app_client.post(
        "/api/scheduler/v1/runs",
        headers=auth_headers,
        json={"session_id": "x", "run_id": "y"},  # missing task
    )
    assert resp.status_code == 422


def test_create_run_refuses_without_scheduler_token(
    app_client: TestClient,
    mock_docker_client: MagicMock,
    fake_identity,
    settings,
    auth_headers,
) -> None:
    settings.scheduler_service_token = ""
    resp = app_client.post(
        "/api/scheduler/v1/runs",
        headers=auth_headers,
        json={
            "session_id": "ses_01J5SES0000000000000000001",
            "run_id": "run_01J5RUN0000000000000000001",
            "task": "hi",
        },
    )
    assert resp.status_code == 500
    assert "SCHEDULER_SERVICE_TOKEN" in resp.text


def test_get_run_requires_tenant_ownership(
    app_client: TestClient, mock_docker_client: MagicMock, auth_headers,
) -> None:
    other = MagicMock()
    other.labels = {"agent-platform.tenant-id": "tnt_OTHER"}
    mock_docker_client.containers.get.return_value = other
    resp = app_client.get(
        "/api/scheduler/v1/runs/run_unknown", headers=auth_headers,
    )
    assert resp.status_code == 404


def test_get_run_returns_status_for_owner(
    app_client: TestClient, mock_docker_client: MagicMock, auth_headers,
) -> None:
    container = MagicMock()
    container.labels = {"agent-platform.tenant-id": auth_headers["X-Tenant-Id"]}
    container.status = "running"
    container.attrs = {
        "State": {"ExitCode": None, "StartedAt": "2026", "FinishedAt": None},
    }
    mock_docker_client.containers.get.return_value = container
    resp = app_client.get(
        "/api/scheduler/v1/runs/c_owned", headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "RUNNING"


def test_cancel_run_for_owner(
    app_client: TestClient, mock_docker_client: MagicMock, auth_headers,
) -> None:
    container = MagicMock()
    container.labels = {"agent-platform.tenant-id": auth_headers["X-Tenant-Id"]}
    mock_docker_client.containers.get.return_value = container
    resp = app_client.post(
        "/api/scheduler/v1/runs/c_owned/cancel", headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"
    container.stop.assert_called_once()


# --------------------------------------------------------------------------- #
# GET /runs — tenant-scoped list, proxied from session-orchestrator
# --------------------------------------------------------------------------- #


class _FakeOrchClient:
    """Stub httpx.AsyncClient used by the runs router's list_runs proxy.

    Records the GET call so tests can assert forwarded headers/params, and
    returns a configurable status + JSON body or raises a connection error.
    """

    last_call: dict | None = None

    def __init__(self, status_code: int = 200, body: dict | None = None,
                 raise_connect: bool = False) -> None:
        self._status = status_code
        self._body = body if body is not None else {"items": []}
        self._raise = raise_connect

    def __call__(self, *args, **kwargs):  # mimic httpx.AsyncClient(timeout=...)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get(self, url, **kwargs):
        import httpx as _httpx
        type(self).last_call = {"url": url, **kwargs}
        if self._raise:
            raise _httpx.ConnectError("orchestrator down")
        return _httpx.Response(self._status, json=self._body)

    async def post(self, url, **kwargs):
        # Patching the runs module's httpx.AsyncClient also swaps the shared
        # httpx.AsyncClient used by the authz/quota guards (same module object).
        # Route their POST /check calls to an allowed response so the guards
        # still pass; only GET is the orchestrator proxy under test.
        import httpx as _httpx
        return _httpx.Response(
            200,
            json={"allowed": True, "dimension": "agent_sessions", "remaining": 100},
        )


def _patch_orch(monkeypatch, **kw) -> _FakeOrchClient:
    fake = _FakeOrchClient(**kw)
    _FakeOrchClient.last_call = None
    monkeypatch.setattr(
        "agent_scheduler.api.v1.runs.httpx.AsyncClient", fake,
    )
    return fake


def test_list_runs_proxies_items_and_forwards_tenant(
    app_client: TestClient, auth_headers, monkeypatch,
) -> None:
    payload = {"items": [{"id": "run_1", "status": "RUNNING"}]}
    fake = _patch_orch(monkeypatch, status_code=200, body=payload)

    resp = app_client.get(
        "/api/scheduler/v1/runs?status=RUNNING&limit=10", headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    # Body is returned verbatim, no re-wrapping.
    assert resp.json() == payload

    call = fake.last_call
    assert call is not None
    assert call["headers"]["X-Tenant-Id"] == auth_headers["X-Tenant-Id"]
    assert call["headers"]["X-User-Id"] == auth_headers["X-User-Id"]
    assert call["params"]["status"] == "RUNNING"
    assert call["params"]["limit"] == 10


def test_list_runs_normalises_status_to_upper(
    app_client: TestClient, auth_headers, monkeypatch,
) -> None:
    fake = _patch_orch(monkeypatch, status_code=200, body={"items": []})

    resp = app_client.get(
        "/api/scheduler/v1/runs?status=running", headers=auth_headers,
    )
    assert resp.status_code == 200
    assert fake.last_call["params"]["status"] == "RUNNING"


def test_list_runs_requires_tenant_header(app_client: TestClient) -> None:
    # No X-Tenant-Id: authz guard / tenant dependency rejects before proxying.
    resp = app_client.get("/api/scheduler/v1/runs")
    assert resp.status_code in (401, 403)


def test_list_runs_orchestrator_500_maps_to_502(
    app_client: TestClient, auth_headers, monkeypatch,
) -> None:
    _patch_orch(monkeypatch, status_code=500, body={"detail": "boom"})
    resp = app_client.get("/api/scheduler/v1/runs", headers=auth_headers)
    assert resp.status_code == 502


def test_list_runs_orchestrator_unreachable_maps_to_502(
    app_client: TestClient, auth_headers, monkeypatch,
) -> None:
    _patch_orch(monkeypatch, raise_connect=True)
    resp = app_client.get("/api/scheduler/v1/runs", headers=auth_headers)
    assert resp.status_code == 502
