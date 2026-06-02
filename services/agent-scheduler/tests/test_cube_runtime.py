"""Unit tests for CubeSandboxRuntime — exercises the E2B HTTP protocol path.

We stub ``httpx.Client`` rather than the runtime class itself so the
request/response shapes are pinned: any future change to the request body
or the parsing of E2B's response will break these tests, surfacing a
contract drift early.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from agent_scheduler.config import Settings
from agent_scheduler.errors import ContainerNotFoundError, SandboxSpawnError
from agent_scheduler.services.cube_runtime import (
    CubeSandboxRuntime,
    _parse_memory_mb,
)


def _make_runtime(client: object | None = None) -> CubeSandboxRuntime:
    settings = Settings(
        env="test",
        log_level="WARNING",
        otel_enabled=False,
        runtime="cube",
        cube_controller_url="http://cube-test:8443",
    )
    rt = CubeSandboxRuntime.__new__(CubeSandboxRuntime)
    rt._settings = settings  # noqa: SLF001 — test setup
    rt._client = client  # noqa: SLF001
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
    )
    assert sid == "sb_xyz"
    # The request body carries the tenant metadata we'll later filter by.
    _, kwargs = client.post.call_args
    body = kwargs["json"]
    assert body["metadata"]["agent-platform.tenant-id"] == "tnt_01J5TNT0000000000000000001"
    assert body["env"]["FOUNDATION_API_TOKEN"] == "ft"
    assert body["resources"]["memory_mb"] > 0


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
        )


def test_spawn_treats_network_error_as_spawn_error() -> None:
    client = MagicMock()
    client.post.side_effect = httpx.ConnectError("nope")
    rt = _make_runtime(client)
    with pytest.raises(SandboxSpawnError, match="unreachable"):
        rt.spawn_run(
            run_id="r1", session_id="s1", task="t",
            tenant_id="t1", user_id="u1", foundation_token="ft",
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
# Registry self-registration
# --------------------------------------------------------------------------- #


def test_cube_runtime_registers_itself() -> None:
    """Import side-effect contract: ``cube`` is wired into the registry."""
    from agent_scheduler.services.runtime import RUNTIME_REGISTRY
    assert RUNTIME_REGISTRY.get("cube") is CubeSandboxRuntime
