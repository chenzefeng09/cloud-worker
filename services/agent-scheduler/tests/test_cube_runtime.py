"""Unit tests for CubeSandboxRuntime — exercises the E2B HTTP protocol path.

We stub ``httpx.Client`` rather than the runtime class itself so the
request/response shapes are pinned: any future change to the request body
or the parsing of E2B's response will break these tests, surfacing a
contract drift early.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from agent_scheduler.config import Settings
from agent_scheduler.errors import ContainerNotFoundError, SandboxSpawnError
from agent_scheduler.services.cube_runtime import (
    CubeSandboxRuntime,
    _parse_memory_mb,
)


# The spawn path now writes a 0600 ``.secrets.json`` under ``workspace_base``;
# the module default (/var/agent-platform/workspaces) is not writable in CI, so
# every test that reaches the POST needs a writable base.  One module-scoped
# tmp dir keeps the existing test bodies untouched apart from passing it in.
_WORKSPACE_BASE = tempfile.mkdtemp(prefix="cube-test-workspace-")


def _make_runtime(client: object | None = None) -> CubeSandboxRuntime:
    settings = Settings(
        env="test",
        log_level="WARNING",
        otel_enabled=False,
        runtime="cube",
        cube_controller_url="http://cube-test:8443",
        # Default egress mode is "broker", which now fail-closes without an
        # allow_out CIDR list; give the default runtime a valid one so the
        # spawn-path happy tests can reach the POST.  Tests that exercise the
        # fail-closed / none / foundation modes override ``_settings`` directly.
        cube_sandbox_allow_out="10.96.0.10/32, 10.96.0.53/32",
        workspace_base=_WORKSPACE_BASE,
    )
    rt = CubeSandboxRuntime.__new__(CubeSandboxRuntime)
    rt._settings = settings  # noqa: SLF001 — test setup
    rt._client = client  # noqa: SLF001
    rt._skill_cache = None  # noqa: SLF001
    return rt


def _ok_response(data: dict, status: int = 200) -> httpx.Response:
    """Build a real httpx.Response so .json()/.status_code behave correctly."""
    return httpx.Response(status, json=data, request=httpx.Request("GET", "/x"))


# --------------------------------------------------------------------------- #
# _parse_memory_mb
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        ("2g", 2048),
        ("512m", 512),
        ("1.5g", 1536),
        ("1024", 1024),
        ("  2G  ", 2048),
    ],
)
def test_parse_memory_mb(limit: str, expected: int) -> None:
    assert _parse_memory_mb(limit) == expected


# --------------------------------------------------------------------------- #
# Guard rails
# --------------------------------------------------------------------------- #


def test_unconfigured_runtime_raises_with_pointer() -> None:
    rt = _make_runtime(client=None)
    with pytest.raises(SandboxSpawnError, match="CUBE controller URL"):
        rt.spawn_run(
            run_id="r", session_id="s", task="t",
            tenant_id="tt", user_id="uu", foundation_token="ft",
        )


def test_spawn_rejects_bad_id() -> None:
    client = MagicMock()
    rt = _make_runtime(client)
    with pytest.raises(SandboxSpawnError, match="session_id="):
        rt.spawn_run(
            run_id="run_x",
            session_id="BAD ID",
            task="t",
            tenant_id="tnt_y",
            user_id="usr_z",
            foundation_token="ft",
        )
    client.post.assert_not_called()


def test_spawn_requires_foundation_token() -> None:
    client = MagicMock()
    rt = _make_runtime(client)
    with pytest.raises(SandboxSpawnError, match="foundation_token"):
        rt.spawn_run(
            run_id="run_a", session_id="ses_b", task="t",
            tenant_id="tnt_c", user_id="usr_d",
            foundation_token="",
        )


def test_list_containers_rejects_empty_tenant() -> None:
    rt = _make_runtime(MagicMock())
    with pytest.raises(ValueError, match="tenant_id is required"):
        rt.list_containers(tenant_id="")


# --------------------------------------------------------------------------- #
# E2B protocol happy paths
# --------------------------------------------------------------------------- #


def test_spawn_posts_to_e2b_endpoint_and_returns_sandbox_id() -> None:
    client = MagicMock()
    client.post.return_value = _ok_response(
        {"sandbox_id": "sb_xyz"}, status=201,
    )
    rt = _make_runtime(client)
    sid = rt.spawn_run(
        run_id="run_01J5RUN0000000000000000001",
        session_id="ses_01J5SES0000000000000000001",
        task="hello",
        tenant_id="tnt_01J5TNT0000000000000000001",
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="ft",
        llm_virtual_key="vk-test",
    )
    assert sid == "sb_xyz"
    # The request body carries the tenant metadata we'll later filter by.
    _, kwargs = client.post.call_args
    body = kwargs["json"]
    assert body["metadata"]["agent-platform.tenant-id"] == "tnt_01J5TNT0000000000000000001"
    # FOUNDATION_API_TOKEN is a secret and is delivered out-of-band via the
    # host-mounted .secrets.json — it must NOT be in the VM env (see the
    # dedicated secret-isolation tests below).
    assert "FOUNDATION_API_TOKEN" not in body["env"]
    assert body["resources"]["memory_mb"] > 0


def test_spawn_broker_egress_posts_broker_metadata_and_urls() -> None:
    client = MagicMock()
    client.post.return_value = _ok_response(
        {"sandbox_id": "sb_broker"}, status=201,
    )
    rt = _make_runtime(client)
    rt._settings = Settings(  # noqa: SLF001
        env="test",
        log_level="WARNING",
        otel_enabled=False,
        runtime="cube",
        cube_controller_url="http://cube-test:8443",
        sandbox_egress_mode="broker",
        tool_broker_url="http://tool-broker:8024/api/tool-broker/v1",
        cube_sandbox_allow_out="10.96.0.10/32, 10.96.0.53/32",
        workspace_base=_WORKSPACE_BASE,
    )

    sid = rt.spawn_run(
        run_id="run_01J5RUN0000000000000000002",
        session_id="ses_01J5SES0000000000000000002",
        task="hello broker",
        tenant_id="tnt_01J5TNT0000000000000000001",
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="ft",
        llm_virtual_key="vk-test",
    )

    assert sid == "sb_broker"
    _, kwargs = client.post.call_args
    body = kwargs["json"]
    env = body["env"]
    meta = body["metadata"]
    assert meta["agent-platform.egress-mode"] == "broker"
    assert meta["agent-platform.tool-broker-url"] == (
        "http://tool-broker:8024/api/tool-broker/v1"
    )
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
    # TOOL_BROKER_LEASE is a secret: it must NOT ride in the VM env, but be
    # delivered via the host-mounted .secrets.json instead (see the dedicated
    # secret-isolation tests).  Assert it's stripped from env and present in
    # the file with the expected ``v1.`` prefix.
    assert "TOOL_BROKER_LEASE" not in env
    host_mounts = json.loads(meta["host-mount"])
    secrets_path = Path(
        next(
            m["hostPath"]
            for m in host_mounts
            if m["mountPath"] == "/workspace/.secrets.json"
        )
    )
    secrets = json.loads(secrets_path.read_text(encoding="utf-8"))
    assert secrets["TOOL_BROKER_LEASE"].startswith("v1.")
    # Broker-mode network isolation: the named policy label (drives the
    # nftables fallback + admission glue) PLUS the real CubeVS create shape —
    # ``allow_internet_access=false`` (top-level) + ``network.allow_out``
    # (nested) carrying the operator-provided tool-broker + DNS CIDRs.  A
    # controller implementing CubeVS honours this directly.
    assert meta["agent-platform.network-policy"] == "sandbox-broker-only"
    assert body["allow_internet_access"] is False
    assert body["network"] == {
        "allow_out": ["10.96.0.10/32", "10.96.0.53/32"]
    }
    # Defense-in-depth: the Cube microVM path injects NO direct infra creds at
    # all (prod Cube runs in broker egress; the worker reaches hot-state and
    # object-store through the tool-broker /state and /s3 façades).  Assert the
    # MinIO root keys and the password-bearing Redis URL never appear.
    assert "S3_ACCESS_KEY_ID" not in env
    assert "S3_SECRET_ACCESS_KEY" not in env
    assert "S3_ENDPOINT_URL" not in env
    assert "REDIS_URL" not in env


def test_spawn_broker_without_allow_out_fails_closed() -> None:
    """Broker mode with an empty allow_out is a deployment error.

    An empty allowlist + ``allow_internet_access=false`` air-gaps the sandbox
    from the tool-broker, so we refuse at spawn (before any POST) with an
    actionable message naming the env var + the README.
    """
    client = MagicMock()
    rt = _make_runtime(client)
    rt._settings = Settings(  # noqa: SLF001
        env="test",
        log_level="WARNING",
        otel_enabled=False,
        runtime="cube",
        cube_controller_url="http://cube-test:8443",
        sandbox_egress_mode="broker",
        tool_broker_url="http://tool-broker:8024/api/tool-broker/v1",
        cube_sandbox_allow_out="",  # empty -> fail closed
    )
    with pytest.raises(
        SandboxSpawnError, match="SCHEDULER_CUBE_SANDBOX_ALLOW_OUT"
    ):
        rt.spawn_run(
            run_id="run_01J5RUN0000000000000000003",
            session_id="ses_01J5SES0000000000000000003",
            task="hello broker",
            tenant_id="tnt_01J5TNT0000000000000000001",
            user_id="usr_01J5USR0000000000000000001",
            foundation_token="ft",
            llm_virtual_key="vk-test",
        )
    client.post.assert_not_called()


def test_spawn_none_egress_air_gaps_without_failing_closed() -> None:
    """``none`` mode wants total air-gap: deny internet, empty allow_out, no raise."""
    client = MagicMock()
    client.post.return_value = _ok_response(
        {"sandbox_id": "sb_none"}, status=201,
    )
    rt = _make_runtime(client)
    rt._settings = Settings(  # noqa: SLF001
        env="test",
        log_level="WARNING",
        otel_enabled=False,
        runtime="cube",
        cube_controller_url="http://cube-test:8443",
        sandbox_egress_mode="none",
        # No allow_out configured — none mode does NOT fail closed.
        cube_sandbox_allow_out="",
        workspace_base=_WORKSPACE_BASE,
    )
    sid = rt.spawn_run(
        run_id="run_01J5RUN0000000000000000004",
        session_id="ses_01J5SES0000000000000000004",
        task="isolated",
        tenant_id="tnt_01J5TNT0000000000000000001",
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="ft",
        llm_virtual_key="vk-test",
    )
    assert sid == "sb_none"
    _, kwargs = client.post.call_args
    body = kwargs["json"]
    assert body["metadata"]["agent-platform.network-policy"] == "deny-all"
    assert body["allow_internet_access"] is False
    assert body["network"] == {"allow_out": []}


def test_spawn_foundation_egress_emits_no_network_field() -> None:
    """``foundation`` (legacy/dev) mode carries no CubeVS network field."""
    client = MagicMock()
    client.post.return_value = _ok_response(
        {"sandbox_id": "sb_foundation"}, status=201,
    )
    rt = _make_runtime(client)
    rt._settings = Settings(  # noqa: SLF001
        env="test",
        log_level="WARNING",
        otel_enabled=False,
        runtime="cube",
        cube_controller_url="http://cube-test:8443",
        sandbox_egress_mode="foundation",
        # allow_out is irrelevant in foundation mode (no fail-closed, no field).
        cube_sandbox_allow_out="",
        workspace_base=_WORKSPACE_BASE,
    )
    sid = rt.spawn_run(
        run_id="run_01J5RUN0000000000000000005",
        session_id="ses_01J5SES0000000000000000005",
        task="legacy",
        tenant_id="tnt_01J5TNT0000000000000000001",
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="ft",
        llm_virtual_key="vk-test",
    )
    assert sid == "sb_foundation"
    _, kwargs = client.post.call_args
    body = kwargs["json"]
    assert "network" not in body
    assert "allow_internet_access" not in body


def test_spawn_propagates_e2b_4xx_as_spawn_error() -> None:
    client = MagicMock()
    client.post.return_value = _ok_response(
        {"detail": "no capacity"}, status=503,
    )
    rt = _make_runtime(client)
    with pytest.raises(SandboxSpawnError, match="HTTP 503"):
        rt.spawn_run(
            run_id="r1", session_id="s1", task="t",
            tenant_id="t1", user_id="u1", foundation_token="ft",
            llm_virtual_key="vk-test",
        )


def test_spawn_treats_network_error_as_spawn_error() -> None:
    client = MagicMock()
    client.post.side_effect = httpx.ConnectError("nope")
    rt = _make_runtime(client)
    with pytest.raises(SandboxSpawnError, match="unreachable"):
        rt.spawn_run(
            run_id="r1", session_id="s1", task="t",
            tenant_id="t1", user_id="u1", foundation_token="ft",
            llm_virtual_key="vk-test",
        )


def test_get_run_status_maps_e2b_state_to_internal() -> None:
    client = MagicMock()
    client.get.return_value = _ok_response(
        {"status": "running", "exit_code": None, "started_at": "a", "finished_at": None},
    )
    rt = _make_runtime(client)
    result = rt.get_run_status("sb_1")
    assert result["status"] == "RUNNING"
    assert result["exit_code"] is None


def test_get_run_status_returns_not_found_on_404() -> None:
    client = MagicMock()
    client.get.return_value = _ok_response({}, status=404)
    rt = _make_runtime(client)
    assert rt.get_run_status("sb_missing") == {"status": "NOT_FOUND", "exit_code": None}


def test_get_container_tenant_returns_metadata() -> None:
    client = MagicMock()
    client.get.return_value = _ok_response(
        {"metadata": {"agent-platform.tenant-id": "tnt_a"}},
    )
    rt = _make_runtime(client)
    assert rt.get_container_tenant("sb_1") == "tnt_a"


def test_get_container_tenant_raises_on_404() -> None:
    client = MagicMock()
    client.get.return_value = _ok_response({}, status=404)
    rt = _make_runtime(client)
    with pytest.raises(ContainerNotFoundError):
        rt.get_container_tenant("sb_missing")


def test_get_container_tenant_raises_when_metadata_blank() -> None:
    client = MagicMock()
    client.get.return_value = _ok_response({"metadata": {}})
    rt = _make_runtime(client)
    with pytest.raises(ContainerNotFoundError):
        rt.get_container_tenant("sb_no_label")


def test_cancel_run_raises_on_404() -> None:
    client = MagicMock()
    client.delete.return_value = _ok_response({}, status=404)
    rt = _make_runtime(client)
    with pytest.raises(ContainerNotFoundError):
        rt.cancel_run("sb_missing")


def test_cancel_run_treats_non_404_errors_as_gone(caplog) -> None:
    """409 / 500 from CubeSandbox just means the VM is already going away."""
    client = MagicMock()
    client.delete.return_value = _ok_response({}, status=409)
    rt = _make_runtime(client)
    rt.cancel_run("sb_dying")  # no raise


def test_list_containers_filters_by_tenant_label() -> None:
    client = MagicMock()
    client.get.return_value = _ok_response({
        "items": [
            {
                "sandbox_id": "sb_a", "status": "running",
                "metadata": {
                    "agent-platform.tenant-id": "tnt_a",
                    "agent-platform.run-id": "r1",
                    "agent-platform.session-id": "s1",
                },
                "template_id": "img", "created_at": "2026-01-01",
            },
            {
                # foreign tenant slipped past server-side filter — belt+braces
                # check should drop it.
                "sandbox_id": "sb_b", "status": "running",
                "metadata": {"agent-platform.tenant-id": "tnt_OTHER"},
            },
        ],
    })
    rt = _make_runtime(client)
    items = rt.list_containers(tenant_id="tnt_a")
    assert [i["container_id"] for i in items] == ["sb_a"]
    # And the URL really included the tenant filter param.
    _, kwargs = client.get.call_args
    assert kwargs["params"]["metadata.agent-platform.tenant-id"] == "tnt_a"


# --------------------------------------------------------------------------- #
# Secret isolation — no secrets in VM env; delivered via host-mounted file
# --------------------------------------------------------------------------- #


_SECRET_KEYS = (
    "FOUNDATION_API_TOKEN",
    "TOOL_BROKER_LEASE",
    "LLM_VIRTUAL_KEY",
    "LLM_API_KEY",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "REDIS_URL",
)


def _make_runtime_with_workspace(client: object, workspace_base: Path) -> CubeSandboxRuntime:
    """Like ``_make_runtime`` but with a real on-disk ``workspace_base``.

    The secret-file path writes a 0600 file under ``workspace_base``; point it
    at a tmp dir so the write actually lands and we can inspect it.  Foundation
    egress mode avoids needing an allow_out CIDR list, and we drop the
    ``llm_gateway_enforcer`` extension so ``LLM_API_KEY`` stays populated (the
    enforcer blanks it) — that gives a non-trivial secrets payload AND removes
    the gateway guard that would otherwise require a per-run virtual key.
    """
    settings = Settings(
        env="test",
        log_level="WARNING",
        otel_enabled=False,
        runtime="cube",
        cube_controller_url="http://cube-test:8443",
        sandbox_egress_mode="foundation",
        cube_sandbox_allow_out="",
        workspace_base=str(workspace_base),
        llm_api_key="sk-real-llm-key",
        runtime_extensions="mcp_tenant_injector,skills_loader,web_fetch",
    )
    rt = CubeSandboxRuntime.__new__(CubeSandboxRuntime)
    rt._settings = settings  # noqa: SLF001
    rt._client = client  # noqa: SLF001
    rt._skill_cache = None  # noqa: SLF001
    return rt


def test_spawn_keeps_secrets_out_of_vm_env(tmp_path: Path) -> None:
    """The env dict handed to the Cube VM must carry NONE of the secret keys."""
    client = MagicMock()
    client.post.return_value = _ok_response({"sandbox_id": "sb_sec"}, status=201)
    rt = _make_runtime_with_workspace(client, tmp_path)

    rt.spawn_run(
        run_id="run_01J5RUN0000000000000000010",
        session_id="ses_01J5SES0000000000000000010",
        task="secrets",
        tenant_id="tnt_01J5TNT0000000000000000001",
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="ft-secret",
        llm_virtual_key="vk-secret",
    )

    _, kwargs = client.post.call_args
    env = kwargs["json"]["env"]
    for key in _SECRET_KEYS:
        assert key not in env, f"{key} leaked into the Cube VM env dict"
    # Routing / metadata that is NOT a secret stays in env.
    assert env["FOUNDATION_TENANT_ID"] == "tnt_01J5TNT0000000000000000001"
    assert env["FOUNDATION_USER_ID"] == "usr_01J5USR0000000000000000001"
    assert env["FOUNDATION_API_URL"]
    assert env["RUN_ID"] == "run_01J5RUN0000000000000000010"
    assert "AGENT_TASK_JSON" in env


def test_spawn_writes_and_mounts_secrets_file(tmp_path: Path) -> None:
    """Secrets land in a 0600 host file host-mounted at /workspace/.secrets.json."""
    client = MagicMock()
    client.post.return_value = _ok_response({"sandbox_id": "sb_sec2"}, status=201)
    rt = _make_runtime_with_workspace(client, tmp_path)

    rt.spawn_run(
        run_id="run_01J5RUN0000000000000000011",
        session_id="ses_01J5SES0000000000000000011",
        task="secrets",
        tenant_id="tnt_01J5TNT0000000000000000001",
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="ft-secret",
        llm_virtual_key="vk-secret",
    )

    _, kwargs = client.post.call_args
    body = kwargs["json"]

    # 1. A host-mount entry targets exactly the path the worker reads.
    host_mounts = json.loads(body["metadata"]["host-mount"])
    secret_mounts = [
        m for m in host_mounts if m["mountPath"] == "/workspace/.secrets.json"
    ]
    assert len(secret_mounts) == 1, host_mounts
    mount = secret_mounts[0]
    assert mount["readOnly"] is True
    host_path = Path(mount["hostPath"])

    # 2. The file was actually written under workspace_base, mode 0600.
    expected_dir = (
        tmp_path
        / "tnt_01J5TNT0000000000000000001"
        / "ses_01J5SES0000000000000000011"
        / "run_01J5RUN0000000000000000011"
    )
    assert host_path == expected_dir / ".secrets.json"
    assert host_path.exists()
    assert stat.S_IMODE(os.stat(host_path).st_mode) == 0o600

    # 3. The file content carries the real secret values that were stripped
    #    from env (present-only: FOUNDATION_API_TOKEN, LLM_API_KEY,
    #    LLM_VIRTUAL_KEY, TOOL_BROKER_LEASE in foundation mode; no S3_/REDIS).
    payload = json.loads(host_path.read_text(encoding="utf-8"))
    assert payload["FOUNDATION_API_TOKEN"] == "ft-secret"
    assert payload["LLM_VIRTUAL_KEY"] == "vk-secret"
    assert payload["LLM_API_KEY"] == "sk-real-llm-key"
    # Keys never set on the Cube path must not be invented into the file.
    assert "S3_ACCESS_KEY_ID" not in payload
    assert "REDIS_URL" not in payload


def test_spawn_omits_absent_optional_secrets_from_file(tmp_path: Path) -> None:
    """Optional secrets (LLM_VIRTUAL_KEY) absent from env stay out of the file."""
    client = MagicMock()
    client.post.return_value = _ok_response({"sandbox_id": "sb_sec3"}, status=201)
    rt = _make_runtime_with_workspace(client, tmp_path)

    rt.spawn_run(
        run_id="run_01J5RUN0000000000000000012",
        session_id="ses_01J5SES0000000000000000012",
        task="secrets",
        tenant_id="tnt_01J5TNT0000000000000000001",
        user_id="usr_01J5USR0000000000000000001",
        foundation_token="ft-secret",
        # no llm_virtual_key -> not set in env -> must not be in the file
    )

    _, kwargs = client.post.call_args
    host_mounts = json.loads(kwargs["json"]["metadata"]["host-mount"])
    host_path = Path(
        next(
            m["hostPath"]
            for m in host_mounts
            if m["mountPath"] == "/workspace/.secrets.json"
        )
    )
    payload = json.loads(host_path.read_text(encoding="utf-8"))
    assert "LLM_VIRTUAL_KEY" not in payload
    assert payload["FOUNDATION_API_TOKEN"] == "ft-secret"


# --------------------------------------------------------------------------- #
# Registry self-registration
# --------------------------------------------------------------------------- #


def test_cube_runtime_registers_itself() -> None:
    """Import side-effect contract: ``cube`` is wired into the registry."""
    from agent_scheduler.services.runtime import RUNTIME_REGISTRY
    assert RUNTIME_REGISTRY.get("cube") is CubeSandboxRuntime
