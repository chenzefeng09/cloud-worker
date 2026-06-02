"""Unit tests for DockerRuntime — ID whitelist, TOML safety, ownership."""

from __future__ import annotations

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
