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
    _SECRET_ENV_KEYS,
    DockerRuntime,
    _check_ids,
)


def _read_secrets_file(
    workspace_base: str | Path,
    *,
    tenant_id: str,
    session_id: str,
    run_id: str,
) -> dict[str, str]:
    """Load the ``.secrets.json`` the scheduler writes into the run workspace.

    Secrets are delivered out-of-band via this 0600 file on the /workspace
    bind mount (NOT container env) so an in-sandbox agent can't recover them
    from ``/proc/1/environ``.
    """
    secrets_path = (
        Path(workspace_base) / tenant_id / session_id / run_id / ".secrets.json"
    )
    return json.loads(secrets_path.read_text(encoding="utf-8"))


def _secrets_file_mode(
    workspace_base: str | Path,
    *,
    tenant_id: str,
    session_id: str,
    run_id: str,
) -> int:
    secrets_path = (
        Path(workspace_base) / tenant_id / session_id / run_id / ".secrets.json"
    )
    return secrets_path.stat().st_mode & 0o777


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
        llm_virtual_key="vk-test",
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
        llm_virtual_key="vk-test",
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


def test_spawn_run_foundation_egress_uses_configured_network(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """Legacy foundation egress keeps the sandbox on the configured Docker network.

    The platform default egress mode is now ``broker``; this test pins
    ``foundation`` explicitly because it asserts the foundation-mode contract
    (sandbox joins ``docker_network``, no network_disabled).
    """
    from agent_scheduler.config import Settings

    runtime._settings = Settings(  # noqa: SLF001 — test setup
        env="test",
        log_level="WARNING",
        agent_image="hello-world:latest",
        workspace_base="/tmp/agent-platform-test",
        docker_network="none",
        foundation_identity_url="http://identity-test/api/identity/v1",
        scheduler_service_token="fake-service-token",
        otel_enabled=False,
        sandbox_egress_mode="foundation",
    )
    mock_docker_client.containers.run.return_value = MagicMock(id="container-egress-foundation")

    runtime.spawn_run(
        run_id="run_01J5RUN0000000000000EG001",
        session_id="ses_01J5SES0000000000000EG001",
        task="egress foundation",
        tenant_id="tnt_01J5TNT0000000000000000001",
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="tok_egress",
        llm_virtual_key="vk-test",
    )

    _, kwargs = mock_docker_client.containers.run.call_args
    assert kwargs["network"] == runtime._settings.docker_network  # noqa: SLF001
    assert "network_disabled" not in kwargs
    assert kwargs["environment"]["SANDBOX_EGRESS_MODE"] == "foundation"
    assert kwargs["labels"]["agent-platform.egress-mode"] == "foundation"


def test_spawn_run_broker_egress_uses_broker_network_and_urls(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """Broker mode removes direct foundation URLs from the sandbox contract."""
    from agent_scheduler.config import Settings

    runtime._settings = Settings(
        env="test",
        log_level="WARNING",
        agent_image="hello-world:latest",
        workspace_base="/tmp/agent-platform-test",
        docker_network="foundation-net",
        sandbox_egress_mode="broker",
        sandbox_broker_network="sandbox-broker-net",
        tool_broker_url="http://tool-broker:8024/api/tool-broker/v1",
        foundation_identity_url="http://identity-test/api/identity/v1",
        scheduler_service_token="fake-service-token",
        otel_enabled=False,
        redis_url="redis://:secret@foundation-redis:6379/0",
        s3_endpoint_url="http://foundation-minio:9000",
        s3_bucket="agent-platform",
        s3_region="us-east-1",
        s3_access_key_id="minio-root",
        s3_secret_access_key="minio-root-secret",
    )
    # The broker network must be internal:true (FIX B5 invariant); model that
    # explicitly so this happy-path test also exercises the egress guard.
    _net = MagicMock()
    _net.attrs = {"Internal": True}
    mock_docker_client.networks.get.return_value = _net
    mock_docker_client.containers.run.return_value = MagicMock(id="container-egress-broker")

    run_id = "run_01J5RUN0000000000000EG002"
    session_id = "ses_01J5SES0000000000000EG002"
    tenant_id = "tnt_01J5TNT0000000000000000001"
    runtime.spawn_run(
        run_id=run_id,
        session_id=session_id,
        task="egress broker",
        tenant_id=tenant_id,
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="tok_egress",
        llm_virtual_key="vk-test",
    )

    _, kwargs = mock_docker_client.containers.run.call_args
    env = kwargs["environment"]
    assert kwargs["network"] == "sandbox-broker-net"
    assert "network_disabled" not in kwargs
    assert env["SANDBOX_EGRESS_MODE"] == "broker"
    assert env["TOOL_BROKER_URL"] == "http://tool-broker:8024/api/tool-broker/v1"
    assert env["LLM_GATEWAY_URL"] == (
        "http://tool-broker:8024/api/tool-broker/v1/llm-gateway/v1"
    )
    assert env["MCP_REGISTRY_URL"] == (
        "http://tool-broker:8024/api/tool-broker/v1/mcp/v1"
    )
    assert env["WEB_FETCH_GATEWAY_URL"] == (
        "http://tool-broker:8024/api/tool-broker/v1/web-fetch/api/web-fetch/v1"
    )
    # Secrets are no longer passed via env — they ride /workspace/.secrets.json.
    assert "REDIS_URL" not in env
    assert "S3_ACCESS_KEY_ID" not in env
    assert "S3_SECRET_ACCESS_KEY" not in env
    assert "TOOL_BROKER_LEASE" not in env
    assert env["S3_ENDPOINT_URL"] == ""
    # Bucket name + region are not secrets and stay populated so the worker can
    # build object keys for the broker /s3/v1/object call.
    assert env["S3_BUCKET"] == "agent-platform"
    assert env["S3_REGION"] == "us-east-1"
    secrets = _read_secrets_file(
        runtime._settings.workspace_base,  # noqa: SLF001
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=run_id,
    )
    # Defense-in-depth: the MinIO root access/secret keys are blanked in broker
    # mode (the broker /s3 façade holds the real creds), but still delivered via
    # the file channel so the worker reads consistent values from one place.
    assert secrets["S3_ACCESS_KEY_ID"] == ""
    assert secrets["S3_SECRET_ACCESS_KEY"] == ""
    assert secrets["REDIS_URL"] == ""
    assert secrets["TOOL_BROKER_LEASE"].startswith("v1.")
    assert kwargs["labels"]["agent-platform.egress-mode"] == "broker"
    assert kwargs["labels"]["agent-platform.tool-broker-url"] == (
        "http://tool-broker:8024/api/tool-broker/v1"
    )


def test_spawn_run_none_egress_disables_docker_network(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """None mode gives Bash no network namespace egress at all."""
    from agent_scheduler.config import Settings

    runtime._settings = Settings(
        env="test",
        log_level="WARNING",
        agent_image="hello-world:latest",
        workspace_base="/tmp/agent-platform-test",
        docker_network="foundation-net",
        sandbox_egress_mode="none",
        foundation_identity_url="http://identity-test/api/identity/v1",
        scheduler_service_token="fake-service-token",
        otel_enabled=False,
        redis_url="redis://:secret@foundation-redis:6379/0",
        s3_endpoint_url="http://foundation-minio:9000",
    )
    mock_docker_client.containers.run.return_value = MagicMock(id="container-egress-none")

    run_id = "run_01J5RUN0000000000000EG003"
    session_id = "ses_01J5SES0000000000000EG003"
    tenant_id = "tnt_01J5TNT0000000000000000001"
    runtime.spawn_run(
        run_id=run_id,
        session_id=session_id,
        task="egress none",
        tenant_id=tenant_id,
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="tok_egress",
        llm_virtual_key="vk-test",
    )

    _, kwargs = mock_docker_client.containers.run.call_args
    env = kwargs["environment"]
    assert kwargs["network_disabled"] is True
    assert "network" not in kwargs
    assert env["SANDBOX_EGRESS_MODE"] == "none"
    assert env["LLM_GATEWAY_URL"] == ""
    assert env["MCP_REGISTRY_URL"] == ""
    assert env["WEB_FETCH_GATEWAY_URL"] == ""
    assert env["S3_ENDPOINT_URL"] == ""
    assert env["S3_BUCKET"] == ""
    assert env["S3_REGION"] == ""
    # Secrets never reach env; in ``none`` mode they are blanked but still
    # delivered via the file channel.
    assert "REDIS_URL" not in env
    assert "S3_ACCESS_KEY_ID" not in env
    assert "S3_SECRET_ACCESS_KEY" not in env
    secrets = _read_secrets_file(
        runtime._settings.workspace_base,  # noqa: SLF001
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=run_id,
    )
    assert secrets["REDIS_URL"] == ""
    assert secrets["S3_ACCESS_KEY_ID"] == ""
    assert secrets["S3_SECRET_ACCESS_KEY"] == ""
    assert kwargs["labels"]["agent-platform.egress-mode"] == "none"


def test_spawn_run_foundation_egress_injects_real_s3_creds(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """Foundation mode keeps the legacy direct S3 creds in the sandbox;
    they are sourced through ``service_env`` (the single source of truth),
    so this confirms the indirection did not drop them in the common path.
    Post security-fix they are delivered via /workspace/.secrets.json rather
    than container env, but the values must be preserved unchanged."""
    from agent_scheduler.config import Settings

    runtime._settings = Settings(  # noqa: SLF001
        env="test",
        log_level="WARNING",
        agent_image="hello-world:latest",
        workspace_base="/tmp/agent-platform-test",
        docker_network="foundation-net",
        sandbox_egress_mode="foundation",
        foundation_identity_url="http://identity-test/api/identity/v1",
        scheduler_service_token="fake-service-token",
        otel_enabled=False,
        s3_endpoint_url="http://foundation-minio:9000",
        s3_bucket="agent-platform",
        s3_region="us-east-1",
        s3_access_key_id="minio-root",
        s3_secret_access_key="minio-root-secret",
        redis_url="redis://:secret@foundation-redis:6379/0",
    )
    mock_docker_client.containers.run.return_value = MagicMock(id="container-foundation-s3")

    run_id = "run_01J5RUN0000000000000EGS31"
    session_id = "ses_01J5SES0000000000000EGS31"
    tenant_id = "tnt_01J5TNT0000000000000000001"
    runtime.spawn_run(
        run_id=run_id,
        session_id=session_id,
        task="foundation s3",
        tenant_id=tenant_id,
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="tok_egress",
        llm_virtual_key="vk-test",
    )

    _, kwargs = mock_docker_client.containers.run.call_args
    env = kwargs["environment"]
    # Non-secret S3 routing stays in env.
    assert env["S3_ENDPOINT_URL"] == "http://foundation-minio:9000"
    assert env["S3_BUCKET"] == "agent-platform"
    assert env["S3_REGION"] == "us-east-1"
    # Secret S3 creds + Redis URL are delivered via the file, not env.
    assert "S3_ACCESS_KEY_ID" not in env
    assert "S3_SECRET_ACCESS_KEY" not in env
    assert "REDIS_URL" not in env
    secrets = _read_secrets_file(
        runtime._settings.workspace_base,  # noqa: SLF001
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=run_id,
    )
    assert secrets["S3_ACCESS_KEY_ID"] == "minio-root"
    assert secrets["S3_SECRET_ACCESS_KEY"] == "minio-root-secret"
    assert secrets["REDIS_URL"] == "redis://:secret@foundation-redis:6379/0"


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
            llm_virtual_key="vk-test",
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
# M1-P3: LLM gateway env injection
# ---------------------------------------------------------------------------


def test_spawn_run_env_contains_llm_gateway_url(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """``spawn_run`` must always inject LLM_GATEWAY_URL regardless of virtual key."""
    mock_docker_client.containers.run.return_value = MagicMock(id="container-gw-url")

    runtime.spawn_run(
        run_id="run_01J5RUN0000000000000GW001",
        session_id="ses_01J5SES0000000000000GW001",
        task="hello gateway",
        tenant_id="tnt_01J5TNT0000000000000000001",
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="tok_gw",
        llm_virtual_key="vk-test",
    )

    _, kwargs = mock_docker_client.containers.run.call_args
    env = kwargs["environment"]
    assert "LLM_GATEWAY_URL" in env, "LLM_GATEWAY_URL missing from container env"
    # Default from Settings
    assert "llm-gateway" in env["LLM_GATEWAY_URL"] or "localhost" in env["LLM_GATEWAY_URL"]


def test_spawn_run_virtual_key_delivered_via_secrets_file_when_provided(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """When a virtual key is supplied it is delivered via /workspace/.secrets.json
    (a secret), NOT the container env where /proc/1/environ would leak it."""
    mock_docker_client.containers.run.return_value = MagicMock(id="container-vk-present")

    run_id = "run_01J5RUN0000000000000GW002"
    session_id = "ses_01J5SES0000000000000GW002"
    tenant_id = "tnt_01J5TNT0000000000000000001"
    runtime.spawn_run(
        run_id=run_id,
        session_id=session_id,
        task="hello with key",
        tenant_id=tenant_id,
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="tok_vk",
        llm_virtual_key="vk-tenant-test-123",
    )

    _, kwargs = mock_docker_client.containers.run.call_args
    env = kwargs["environment"]
    assert "LLM_VIRTUAL_KEY" not in env, "LLM_VIRTUAL_KEY must not be in container env"
    secrets = _read_secrets_file(
        runtime._settings.workspace_base,  # noqa: SLF001
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=run_id,
    )
    assert secrets["LLM_VIRTUAL_KEY"] == "vk-tenant-test-123"


def test_spawn_run_rejects_missing_virtual_key_when_enforcer_enabled(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """Enterprise default is fail-closed: no virtual key means no sandbox."""
    mock_docker_client.containers.run.return_value = MagicMock(id="container-vk-absent")

    with pytest.raises(SandboxSpawnError, match="no tenant LLM virtual key"):
        runtime.spawn_run(
            run_id="run_01J5RUN0000000000000GW003",
            session_id="ses_01J5SES0000000000000GW003",
            task="hello no key",
            tenant_id="tnt_01J5TNT0000000000000000001",
            user_id="usr_01J5USR0000000000000000001",
            foundation_token="tok_novk",
            llm_virtual_key=None,
        )
    mock_docker_client.containers.run.assert_not_called()


def test_spawn_run_blanks_direct_key_when_enforcer_enabled(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """Default enforcer mode must not expose provider secrets to sandbox code."""
    from agent_scheduler.config import Settings

    runtime._settings = Settings(
        env="test",
        log_level="WARNING",
        agent_image="hello-world:latest",
        workspace_base="/tmp/agent-platform-test",
        docker_network="none",
        foundation_identity_url="http://identity-test/api/identity/v1",
        scheduler_service_token="fake-service-token",
        otel_enabled=False,
        llm_api_key="sk-direct-key",
        llm_model="deepseek-chat",
        llm_provider="openai",
        llm_base_url="https://api.deepseek.com/v1",
    )
    mock_docker_client.containers.run.return_value = MagicMock(id="container-direct")

    run_id = "run_01J5RUN0000000000000GW004"
    session_id = "ses_01J5SES0000000000000GW004"
    tenant_id = "tnt_01J5TNT0000000000000000001"
    runtime.spawn_run(
        run_id=run_id,
        session_id=session_id,
        task="direct connect check",
        tenant_id=tenant_id,
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="tok_direct",
        llm_virtual_key="vk-also-injected",
    )

    _, kwargs = mock_docker_client.containers.run.call_args
    env = kwargs["environment"]
    # Secret keys live in the file, not env.
    assert "LLM_API_KEY" not in env
    assert "LLM_VIRTUAL_KEY" not in env
    # Non-secret LLM routing/metadata stays in env.
    assert env.get("LLM_BASE_URL") == "https://api.deepseek.com/v1", "LLM_BASE_URL must be preserved"
    assert env.get("LLM_MODEL") == "deepseek-chat", "LLM_MODEL must be preserved"
    assert env.get("LLM_PROVIDER") == "openai", "LLM_PROVIDER must be preserved"
    assert "LLM_GATEWAY_URL" in env
    secrets = _read_secrets_file(
        runtime._settings.workspace_base,  # noqa: SLF001
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=run_id,
    )
    assert secrets["LLM_API_KEY"] == "", "LLM_API_KEY must be blank in gateway mode"
    assert secrets["LLM_VIRTUAL_KEY"] == "vk-also-injected"


def test_spawn_run_direct_connect_vars_preserved_when_enforcer_disabled(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """Local-only direct-connect mode remains available when explicitly configured."""
    from agent_scheduler.config import Settings

    runtime._settings = Settings(
        env="test",
        log_level="WARNING",
        agent_image="hello-world:latest",
        workspace_base="/tmp/agent-platform-test",
        docker_network="none",
        foundation_identity_url="http://identity-test/api/identity/v1",
        scheduler_service_token="fake-service-token",
        otel_enabled=False,
        llm_api_key="sk-direct-key",
        llm_model="deepseek-chat",
        llm_provider="openai",
        llm_base_url="https://api.deepseek.com/v1",
        runtime_extensions="mcp_tenant_injector,skills_loader,web_fetch",
    )
    mock_docker_client.containers.run.return_value = MagicMock(id="container-direct")

    run_id = "run_01J5RUN0000000000000GW005"
    session_id = "ses_01J5SES0000000000000GW005"
    tenant_id = "tnt_01J5TNT0000000000000000001"
    runtime.spawn_run(
        run_id=run_id,
        session_id=session_id,
        task="direct connect check",
        tenant_id=tenant_id,
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="tok_direct",
    )

    _, kwargs = mock_docker_client.containers.run.call_args
    env = kwargs["environment"]
    # The real provider key is a secret → file channel, not env.
    assert "LLM_API_KEY" not in env
    secrets = _read_secrets_file(
        runtime._settings.workspace_base,  # noqa: SLF001
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=run_id,
    )
    assert secrets["LLM_API_KEY"] == "sk-direct-key"
    # Non-secret routing/metadata stays in env.
    assert env.get("LLM_BASE_URL") == "https://api.deepseek.com/v1"
    assert env.get("LLM_MODEL") == "deepseek-chat"
    assert env.get("LLM_PROVIDER") == "openai"


# ---------------------------------------------------------------------------
# Hot-state: REDIS_URL + RUN_ID env injection
# ---------------------------------------------------------------------------


def test_spawn_run_env_contains_redis_url_and_run_id(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """``spawn_run`` must pass RUN_ID in the container environment and deliver
    REDIS_URL (a secret) via /workspace/.secrets.json so the in-sandbox
    runtime_worker can write hot-state checkpoints to Redis."""
    from agent_scheduler.config import Settings

    # Give the runtime a settings with a REDIS_URL configured.  REDIS_URL is
    # only forwarded into the sandbox in foundation egress mode (broker/none
    # blank it, since direct Redis access is removed); pin foundation here
    # because the platform default egress mode is now ``broker``.
    runtime._settings = Settings(
        env="test",
        log_level="WARNING",
        agent_image="hello-world:latest",
        workspace_base="/tmp/agent-platform-test",
        docker_network="none",
        foundation_identity_url="http://identity-test/api/identity/v1",
        scheduler_service_token="fake-service-token",
        otel_enabled=False,
        sandbox_egress_mode="foundation",
        redis_url="redis://:redis_change_me@foundation-redis:6379/0",
    )
    mock_docker_client.containers.run.return_value = MagicMock(id="container-redis-test")

    run_id = "run_01J5RUN0000000000000REDIS"
    session_id = "ses_01J5SES0000000000000REDIS"
    tenant_id = "tnt_01J5TNT0000000000000000001"
    runtime.spawn_run(
        run_id=run_id,
        session_id=session_id,
        task="hello redis",
        tenant_id=tenant_id,
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="tok_redis",
        llm_virtual_key="vk-test",
    )

    _, kwargs = mock_docker_client.containers.run.call_args
    env = kwargs["environment"]

    # REDIS_URL is a secret → delivered via the file, not container env.
    assert "REDIS_URL" not in env, "REDIS_URL must not be in container env"
    secrets = _read_secrets_file(
        runtime._settings.workspace_base,  # noqa: SLF001
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=run_id,
    )
    assert secrets["REDIS_URL"] == "redis://:redis_change_me@foundation-redis:6379/0"

    # RUN_ID is routing metadata and stays in the container env.
    assert "RUN_ID" in env, "RUN_ID missing from container env"
    assert env["RUN_ID"] == run_id, (
        f"RUN_ID in container env ({env['RUN_ID']!r}) does not match "
        f"the spawned run_id ({run_id!r})"
    )


# ---------------------------------------------------------------------------
# Security: secrets delivered via /workspace/.secrets.json, NOT container env
#
# The kernel exposes a process' environ at /proc/1/environ, world-readable to
# any in-sandbox code.  Passing credentials as ``environment=`` would let an
# agent ``cat /proc/1/environ`` and exfiltrate them, defeating the worker's
# env-scrub defense.  The scheduler therefore strips the secret subset out of
# the container env and writes it to a 0600 file on the /workspace bind mount.
# ---------------------------------------------------------------------------


def test_spawn_run_secrets_moved_to_file_not_env(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """The 7 secret keys must NOT appear in ``environment=`` and MUST land in
    ``/workspace/.secrets.json`` (mode 0600) with correct values, while
    routing/metadata keys stay in the container env."""
    from agent_scheduler.config import Settings

    runtime._settings = Settings(  # noqa: SLF001 — test setup
        env="test",
        log_level="WARNING",
        agent_image="hello-world:latest",
        workspace_base="/tmp/agent-platform-test",
        docker_network="none",
        sandbox_egress_mode="foundation",
        foundation_identity_url="http://identity-test/api/identity/v1",
        scheduler_service_token="fake-service-token",
        otel_enabled=False,
        llm_api_key="sk-direct-key",
        llm_model="deepseek-chat",
        llm_provider="openai",
        llm_base_url="https://api.deepseek.com/v1",
        runtime_extensions="mcp_tenant_injector,skills_loader,web_fetch",
        redis_url="redis://:redis_change_me@foundation-redis:6379/0",
        s3_endpoint_url="http://foundation-minio:9000",
        s3_bucket="agent-platform",
        s3_region="us-east-1",
        s3_access_key_id="minio-root",
        s3_secret_access_key="minio-root-secret",
    )
    mock_docker_client.containers.run.return_value = MagicMock(id="container-secrets")

    tenant_id = "tnt_01J5TNT0000000000000000001"
    session_id = "ses_01J5SES0000000000000SECRT"
    run_id = "run_01J5RUN0000000000000SECRT"
    runtime.spawn_run(
        run_id=run_id,
        session_id=session_id,
        task="secret split check",
        tenant_id=tenant_id,
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="tok-foundation-secret",
        llm_virtual_key="vk-tenant-secret",
    )

    _, kwargs = mock_docker_client.containers.run.call_args
    env = kwargs["environment"]

    # 1. NONE of the 7 secret keys may appear in the container environment.
    for key in _SECRET_ENV_KEYS:
        assert key not in env, (
            f"{key} leaked into container environment= "
            f"(readable via /proc/1/environ)"
        )

    # 2. The secrets file was written with those keys + correct values.
    secrets = _read_secrets_file(
        runtime._settings.workspace_base,  # noqa: SLF001
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=run_id,
    )
    assert secrets["FOUNDATION_API_TOKEN"] == "tok-foundation-secret"
    assert secrets["LLM_VIRTUAL_KEY"] == "vk-tenant-secret"
    assert secrets["LLM_API_KEY"] == "sk-direct-key"  # enforcer off here
    assert secrets["S3_ACCESS_KEY_ID"] == "minio-root"
    assert secrets["S3_SECRET_ACCESS_KEY"] == "minio-root-secret"
    assert secrets["REDIS_URL"] == "redis://:redis_change_me@foundation-redis:6379/0"
    # TOOL_BROKER_LEASE is only minted in broker mode; foundation mode omits it.
    assert "TOOL_BROKER_LEASE" not in secrets

    # 3. The file is 0600 (owner read/write only).
    assert _secrets_file_mode(
        runtime._settings.workspace_base,  # noqa: SLF001
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=run_id,
    ) == 0o600

    # 4. Routing/metadata keys stay in the container env (not secrets).
    assert env["FOUNDATION_TENANT_ID"] == tenant_id
    assert env["RUN_ID"] == run_id
    assert "MCP_REGISTRY_URL" in env
    assert env["S3_BUCKET"] == "agent-platform"
    assert env["S3_REGION"] == "us-east-1"
    assert "FOUNDATION_API_URL" in env


# ---------------------------------------------------------------------------
# FIX B4: kernel hardening on every sandbox containers.run(...)
# ---------------------------------------------------------------------------


def test_spawn_run_applies_kernel_hardening(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """Every spawned sandbox must carry the B4 kernel-hardening kwargs:
    no-new-privileges, cap_drop=ALL, a pids_limit, and a nofile ulimit."""
    import docker as _docker

    mock_docker_client.containers.run.return_value = MagicMock(id="container-harden")

    runtime.spawn_run(
        run_id="run_01J5RUN0000000000000HRD01",
        session_id="ses_01J5SES0000000000000HRD01",
        task="hardening check",
        tenant_id="tnt_01J5TNT0000000000000000001",
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="tok_harden",
        llm_virtual_key="vk-test",
    )

    _, kwargs = mock_docker_client.containers.run.call_args
    assert kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["pids_limit"] == 512
    ulimits = kwargs["ulimits"]
    assert len(ulimits) == 1
    nofile = ulimits[0]
    assert isinstance(nofile, _docker.types.Ulimit)
    # docker-py stores Ulimit fields under a private dict; assert via the
    # public name and the values we configured.
    assert nofile["Name"] == "nofile"
    assert nofile["Soft"] == 4096
    assert nofile["Hard"] == 8192
    # B4 explicitly does NOT flip the rootfs read-only in this pass.
    assert kwargs.get("read_only") in (None, False)


# ---------------------------------------------------------------------------
# FIX B5: broker/none egress must land on an internal-only docker network
# ---------------------------------------------------------------------------


def _fake_network(*, internal: bool) -> MagicMock:
    net = MagicMock()
    net.attrs = {"Internal": internal}
    return net


def _broker_settings():
    from agent_scheduler.config import Settings

    return Settings(
        env="test",
        log_level="WARNING",
        agent_image="hello-world:latest",
        workspace_base="/tmp/agent-platform-test",
        docker_network="foundation-net",
        sandbox_egress_mode="broker",
        sandbox_broker_network="sandbox-broker-net",
        tool_broker_url="http://tool-broker:8024/api/tool-broker/v1",
        foundation_identity_url="http://identity-test/api/identity/v1",
        scheduler_service_token="fake-service-token",
        otel_enabled=False,
    )


def test_spawn_run_broker_rejects_non_internal_network(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """If the broker network is NOT internal:true the spawn must fail closed."""
    runtime._settings = _broker_settings()  # noqa: SLF001
    mock_docker_client.networks.get.return_value = _fake_network(internal=False)
    mock_docker_client.containers.run.return_value = MagicMock(id="should-not-run")

    with pytest.raises(SandboxSpawnError, match="internal:true"):
        runtime.spawn_run(
            run_id="run_01J5RUN0000000000000NET01",
            session_id="ses_01J5SES0000000000000NET01",
            task="non-internal broker net",
            tenant_id="tnt_01J5TNT0000000000000000001",
            user_id="usr_01J5USR0000000000000000001",
            foundation_token="tok_net",
            llm_virtual_key="vk-test",
        )
    # Fail-closed: we never started the container.
    mock_docker_client.containers.run.assert_not_called()
    mock_docker_client.networks.get.assert_called_once_with("sandbox-broker-net")


def test_spawn_run_broker_allows_internal_network(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """An internal:true broker network passes the invariant and spawns."""
    runtime._settings = _broker_settings()  # noqa: SLF001
    mock_docker_client.networks.get.return_value = _fake_network(internal=True)
    mock_docker_client.containers.run.return_value = MagicMock(id="container-internal-ok")

    container_id = runtime.spawn_run(
        run_id="run_01J5RUN0000000000000NET02",
        session_id="ses_01J5SES0000000000000NET02",
        task="internal broker net",
        tenant_id="tnt_01J5TNT0000000000000000001",
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="tok_net",
        llm_virtual_key="vk-test",
    )
    assert container_id == "container-internal-ok"
    mock_docker_client.containers.run.assert_called_once()


def test_spawn_run_broker_tolerates_fake_client_without_networks(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """A minimal fake docker client lacking network inspection must NOT break
    the spawn — the invariant is skipped (real clients always support it)."""
    runtime._settings = _broker_settings()  # noqa: SLF001
    # Simulate a fake whose networks.get(...).attrs access raises AttributeError.
    mock_docker_client.networks.get.return_value.attrs.__getitem__.side_effect = (
        AttributeError("no attrs on this fake")
    )
    mock_docker_client.containers.run.return_value = MagicMock(id="container-tolerant")

    container_id = runtime.spawn_run(
        run_id="run_01J5RUN0000000000000NET03",
        session_id="ses_01J5SES0000000000000NET03",
        task="tolerant fake client",
        tenant_id="tnt_01J5TNT0000000000000000001",
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="tok_net",
        llm_virtual_key="vk-test",
    )
    assert container_id == "container-tolerant"
    mock_docker_client.containers.run.assert_called_once()


# ---------------------------------------------------------------------------
# FIX B6: foundation egress is prod-unsafe unless explicitly opted in
# ---------------------------------------------------------------------------


def _foundation_settings(**over: object):
    from agent_scheduler.config import Settings

    base: dict[str, object] = dict(
        log_level="WARNING",
        agent_image="hello-world:latest",
        workspace_base="/tmp/agent-platform-test",
        docker_network="foundation-net",
        sandbox_egress_mode="foundation",
        foundation_identity_url="http://identity-test/api/identity/v1",
        scheduler_service_token="fake-service-token",
        otel_enabled=False,
    )
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


def test_spawn_run_foundation_egress_rejected_in_prod_without_optin(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """foundation egress in a prod-like env without the opt-in must fail closed."""
    runtime._settings = _foundation_settings(env="prod")  # noqa: SLF001
    mock_docker_client.containers.run.return_value = MagicMock(id="should-not-run")

    with pytest.raises(SandboxSpawnError, match="unsafe in env 'prod'"):
        runtime.spawn_run(
            run_id="run_01J5RUN0000000000000FND01",
            session_id="ses_01J5SES0000000000000FND01",
            task="foundation prod no optin",
            tenant_id="tnt_01J5TNT0000000000000000001",
            user_id="usr_01J5USR0000000000000000001",
            foundation_token="tok_fnd",
            llm_virtual_key="vk-test",
        )
    mock_docker_client.containers.run.assert_not_called()


def test_spawn_run_foundation_egress_allowed_in_prod_with_optin(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """The explicit opt-in re-enables foundation egress in a prod-like env."""
    runtime._settings = _foundation_settings(  # noqa: SLF001
        env="prod",
        allow_insecure_foundation_egress=True,
    )
    mock_docker_client.containers.run.return_value = MagicMock(id="container-fnd-optin")

    container_id = runtime.spawn_run(
        run_id="run_01J5RUN0000000000000FND02",
        session_id="ses_01J5SES0000000000000FND02",
        task="foundation prod with optin",
        tenant_id="tnt_01J5TNT0000000000000000001",
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="tok_fnd",
        llm_virtual_key="vk-test",
    )
    assert container_id == "container-fnd-optin"
    mock_docker_client.containers.run.assert_called_once()


def test_spawn_run_foundation_egress_allowed_in_dev_without_optin(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """Dev-like envs (dev/test/local) are exempt — no opt-in needed."""
    runtime._settings = _foundation_settings(env="dev")  # noqa: SLF001
    mock_docker_client.containers.run.return_value = MagicMock(id="container-fnd-dev")

    container_id = runtime.spawn_run(
        run_id="run_01J5RUN0000000000000FND03",
        session_id="ses_01J5SES0000000000000FND03",
        task="foundation dev",
        tenant_id="tnt_01J5TNT0000000000000000001",
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="tok_fnd",
        llm_virtual_key="vk-test",
    )
    assert container_id == "container-fnd-dev"
    mock_docker_client.containers.run.assert_called_once()


def test_spawn_run_secrets_file_keeps_empty_llm_api_key_under_enforcer(
    runtime: DockerRuntime,
    mock_docker_client: MagicMock,
) -> None:
    """Under the gateway enforcer LLM_API_KEY is "" but still present in the
    dict, so it must be written to the secrets file (not silently dropped) —
    while falsy LLM_VIRTUAL_KEY/TOOL_BROKER_LEASE are absent."""
    mock_docker_client.containers.run.return_value = MagicMock(id="container-enforcer")

    tenant_id = "tnt_01J5TNT0000000000000000001"
    session_id = "ses_01J5SES000000000000ENFRC"
    run_id = "run_01J5RUN000000000000ENFRC"
    runtime.spawn_run(
        run_id=run_id,
        session_id=session_id,
        task="enforcer empty-key check",
        tenant_id=tenant_id,
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="tok-enforcer",
        llm_virtual_key="vk-enforcer",
    )

    _, kwargs = mock_docker_client.containers.run.call_args
    assert "LLM_API_KEY" not in kwargs["environment"]

    secrets = _read_secrets_file(
        runtime._settings.workspace_base,  # noqa: SLF001
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=run_id,
    )
    # Default settings → enforcer active → blanked key, but key present.
    assert "LLM_API_KEY" in secrets
    assert secrets["LLM_API_KEY"] == ""
    assert secrets["LLM_VIRTUAL_KEY"] == "vk-enforcer"
