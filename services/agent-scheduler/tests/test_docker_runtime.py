"""Unit tests for DockerRuntime — ID whitelist, TOML safety, ownership."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_scheduler.errors import ContainerNotFoundError, SandboxSpawnError
from agent_scheduler.services.docker_runtime import (
    _SAFE_ID,
    DockerRuntime,
    _check_ids,
)


def test_safe_id_accepts_valid_ulid() -> None:
    assert _SAFE_ID.match("run_01J5TEST00000000000000RUN")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "has spaces",
        "has/slash",
        "has\"quote",
        "x" * 41,  # too long
        "with;semicolon",
    ],
)
def test_safe_id_rejects_bad(bad: str) -> None:
    assert not _SAFE_ID.match(bad)


def test_check_ids_passes_for_clean_inputs() -> None:
    _check_ids(run_id="abc", session_id="def", tenant_id="ghi")  # no raise


def test_check_ids_names_offending_field() -> None:
    with pytest.raises(SandboxSpawnError, match="session_id="):
        _check_ids(run_id="ok", session_id="BAD ID")


def test_spawn_requires_foundation_token(runtime: DockerRuntime) -> None:
    with pytest.raises(SandboxSpawnError, match="foundation_token"):
        runtime.spawn_run(
            run_id="run_01J5RUN0000000000000000001",
            session_id="ses_01J5SES0000000000000000001",
            task="hello",
            tenant_id="tnt_01J5TNT0000000000000000001",
            user_id="usr_01J5USR0000000000000000001",
            foundation_token="",
        )


def test_get_container_tenant_raises_when_missing_label(
    runtime: DockerRuntime, mock_docker_client: MagicMock,
) -> None:
    container = MagicMock()
    container.labels = {}  # no agent-platform.tenant-id
    mock_docker_client.containers.get.return_value = container
    with pytest.raises(ContainerNotFoundError):
        runtime.get_container_tenant("c1")


def test_get_container_tenant_returns_label_value(
    runtime: DockerRuntime, mock_docker_client: MagicMock,
) -> None:
    container = MagicMock()
    container.labels = {"agent-platform.tenant-id": "tnt_abc"}
    mock_docker_client.containers.get.return_value = container
    assert runtime.get_container_tenant("c1") == "tnt_abc"


def test_list_containers_rejects_empty_tenant(runtime: DockerRuntime) -> None:
    with pytest.raises(ValueError, match="tenant_id is required"):
        runtime.list_containers(tenant_id="")


def test_cancel_run_swallows_already_stopped(
    runtime: DockerRuntime, mock_docker_client: MagicMock,
) -> None:
    """``stop`` may have raced; cancel should still attempt remove and finish."""
    import docker as _docker
    container = MagicMock()
    container.stop.side_effect = _docker.errors.APIError("already stopped")
    container.remove.return_value = None
    mock_docker_client.containers.get.return_value = container
    runtime.cancel_run("c1")  # no raise
    container.remove.assert_called_once()


def test_cancel_run_raises_when_container_unknown(
    runtime: DockerRuntime, mock_docker_client: MagicMock,
) -> None:
    import docker as _docker
    mock_docker_client.containers.get.side_effect = _docker.errors.NotFound("nope")
    with pytest.raises(ContainerNotFoundError):
        runtime.cancel_run("c1")


def test_get_run_status_completed_with_exit_code_marks_failed(
    runtime: DockerRuntime, mock_docker_client: MagicMock,
) -> None:
    container = MagicMock()
    container.status = "exited"
    container.attrs = {
        "State": {"ExitCode": 137, "StartedAt": "a", "FinishedAt": "b"},
    }
    mock_docker_client.containers.get.return_value = container
    result = runtime.get_run_status("c1")
    assert result["status"] == "FAILED"
    assert result["exit_code"] == 137


def test_get_run_status_not_found(
    runtime: DockerRuntime, mock_docker_client: MagicMock,
) -> None:
    import docker as _docker
    mock_docker_client.containers.get.side_effect = _docker.errors.NotFound("nope")
    result = runtime.get_run_status("c1")
    assert result["status"] == "NOT_FOUND"


def test_list_containers_filters_by_label(
    runtime: DockerRuntime, mock_docker_client: MagicMock,
) -> None:
    c1 = MagicMock()
    c1.id = "c1"
    c1.labels = {
        "agent-platform.tenant-id": "tnt_a",
        "agent-platform.run-id": "r1",
        "agent-platform.session-id": "s1",
    }
    c1.status = "running"
    c1.image.tags = ["agentscope-sandbox:latest"]
    c1.attrs = {"Created": "2026-01-01"}
    mock_docker_client.containers.list.return_value = [c1]
    items = runtime.list_containers(tenant_id="tnt_a")
    assert len(items) == 1
    assert items[0]["status"] == "RUNNING"
    # Verify the docker filter actually included the tenant label
    _, kwargs = mock_docker_client.containers.list.call_args
    assert "agent-platform.tenant-id=tnt_a" in kwargs["filters"]["label"]


# ---------------------------------------------------------------------------
# B-M3: task.json injection tests
# ---------------------------------------------------------------------------


def test_write_task_file_embeds_empty_skills_by_default(
    runtime: DockerRuntime,
) -> None:
    """``_write_task_file`` without skills → task.json has empty skills list."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = runtime._write_task_file(Path(tmpdir), task="do something")
        payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["task"] == "do something"
    assert payload["mcp_servers"] == []
    assert payload["skills"] == []


def test_write_task_file_embeds_skill_names(
    runtime: DockerRuntime,
) -> None:
    """``_write_task_file`` with skills → skill names appear in task.json."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = runtime._write_task_file(
            Path(tmpdir),
            task="analyse data",
            skills=["csv-tools", "pandas-helper"],
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["skills"] == ["csv-tools", "pandas-helper"]
    assert payload["mcp_servers"] == []


def test_spawn_run_env_contains_mcp_and_skill_urls(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """``spawn_run`` must pass MCP_REGISTRY_URL, SKILL_STORE_URL, RUNTIME_EXTENSIONS
    in the container environment so in-sandbox extensions can reach the services."""
    mock_docker_client.containers.run.return_value = MagicMock(id="container-abc")

    runtime.spawn_run(
        run_id="run_01J5RUN0000000000000000001",
        session_id="ses_01J5SES0000000000000000001",
        task="hello",
        tenant_id="tnt_01J5TNT0000000000000000001",
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="tok_test",
        skills=["csv-tools"],
    )

    _, kwargs = mock_docker_client.containers.run.call_args
    env = kwargs["environment"]
    assert "MCP_REGISTRY_URL" in env, "MCP_REGISTRY_URL missing from container env"
    assert "SKILL_STORE_URL" in env, "SKILL_STORE_URL missing from container env"
    assert "RUNTIME_EXTENSIONS" in env, "RUNTIME_EXTENSIONS missing from container env"
    assert "mcp_tenant_injector" in env["RUNTIME_EXTENSIONS"]
    assert "skills_loader" in env["RUNTIME_EXTENSIONS"]


def test_spawn_run_env_contains_foundation_service_scopes(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """``spawn_run`` must inject FOUNDATION_SERVICE_SCOPES so the in-sandbox
    extensions can forward it as X-Service-Scopes to the internal endpoints."""
    mock_docker_client.containers.run.return_value = MagicMock(id="container-scope-test")

    runtime.spawn_run(
        run_id="run_01J5RUN0000000000000000003",
        session_id="ses_01J5SES0000000000000000003",
        task="test scopes",
        tenant_id="tnt_01J5TNT0000000000000000001",
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="tok_scope",
    )

    _, kwargs = mock_docker_client.containers.run.call_args
    env = kwargs["environment"]
    assert "FOUNDATION_SERVICE_SCOPES" in env, (
        "FOUNDATION_SERVICE_SCOPES missing from container env; "
        "mcp_tenant_injector/skills_loader will get 403 from internal endpoints"
    )
    scopes = env["FOUNDATION_SERVICE_SCOPES"]
    assert "mcp.read_internal" in scopes, "mcp.read_internal missing from FOUNDATION_SERVICE_SCOPES"
    assert "skill.read_internal" in scopes, "skill.read_internal missing from FOUNDATION_SERVICE_SCOPES"


def test_spawn_run_task_json_contains_skills(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """Skills passed to ``spawn_run`` must land in the workspace task.json."""
    mock_docker_client.containers.run.return_value = MagicMock(id="container-xyz")

    with tempfile.TemporaryDirectory() as tmpdir:
        runtime._settings.workspace_base = tmpdir
        runtime.spawn_run(
            run_id="run_01J5RUN0000000000000000002",
            session_id="ses_01J5SES0000000000000000002",
            task="crunch numbers",
            tenant_id="tnt_01J5TNT0000000000000000001",
            user_id="usr_01J5USR0000000000000000001",
            foundation_token="tok_test2",
            skills=["csv-tools", "pandas-helper"],
        )
        task_path = (
            Path(tmpdir)
            / "tnt_01J5TNT0000000000000000001"
            / "ses_01J5SES0000000000000000002"
            / "run_01J5RUN0000000000000000002"
            / "task.json"
        )
        payload = json.loads(task_path.read_text(encoding="utf-8"))

    assert payload["task"] == "crunch numbers"
    assert payload["skills"] == ["csv-tools", "pandas-helper"]
    assert payload["mcp_servers"] == []


# ---------------------------------------------------------------------------
# Hot-state: REDIS_URL + RUN_ID env injection
# ---------------------------------------------------------------------------


def test_spawn_run_env_contains_redis_url_and_run_id(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """``spawn_run`` must pass RUN_ID and REDIS_URL in the container environment
    so the in-sandbox runtime_worker can write hot-state checkpoints to Redis."""
    from agent_scheduler.config import Settings

    # Give the runtime a settings with a REDIS_URL configured.
    runtime._settings = Settings(
        env="test",
        log_level="WARNING",
        agent_image="hello-world:latest",
        workspace_base="/tmp/agent-platform-test",
        docker_network="none",
        foundation_identity_url="http://identity-test/api/identity/v1",
        scheduler_service_token="fake-service-token",
        otel_enabled=False,
        redis_url="redis://:redis_change_me@foundation-redis:6379/0",
    )
    mock_docker_client.containers.run.return_value = MagicMock(id="container-redis-test")

    run_id = "run_01J5RUN0000000000000REDIS"
    runtime.spawn_run(
        run_id=run_id,
        session_id="ses_01J5SES0000000000000REDIS",
        task="hello redis",
        tenant_id="tnt_01J5TNT0000000000000000001",
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="tok_redis",
    )

    _, kwargs = mock_docker_client.containers.run.call_args
    env = kwargs["environment"]

    assert "REDIS_URL" in env, "REDIS_URL missing from container env"
    assert env["REDIS_URL"] == "redis://:redis_change_me@foundation-redis:6379/0"

    assert "RUN_ID" in env, "RUN_ID missing from container env"
    assert env["RUN_ID"] == run_id, (
        f"RUN_ID in container env ({env['RUN_ID']!r}) does not match "
        f"the spawned run_id ({run_id!r})"
    )
