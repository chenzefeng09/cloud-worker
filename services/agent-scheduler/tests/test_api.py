"""Integration tests for the scheduler HTTP API — tenant isolation, token minting."""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient


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
    # And the spawn used the *minted* token, not the scheduler's own.
    spawn_env = mock_docker_client.containers.run.call_args.kwargs["environment"]
    assert spawn_env["FOUNDATION_API_TOKEN"] == "minted-jwt"
    assert spawn_env["LLM_VIRTUAL_KEY"] == "vk-test-default"
    assert spawn_env["LLM_API_KEY"] == ""


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
