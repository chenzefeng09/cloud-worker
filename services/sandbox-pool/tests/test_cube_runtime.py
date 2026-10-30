"""Unit tests for ``PoolCubeRuntime`` — warm-VM spawn request shape + liveness.

The pool pre-warms microVMs from the Claude Agent SDK worker image, parked on
``sleep infinity`` until a task is injected; a stub httpx client captures the
POST body so the image and cmd are pinned without a CubeSandbox controller,
and answers the ``GET /sandboxes`` list the liveness probe reads.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from sandbox_pool.config import Settings
from sandbox_pool.cube_runtime import IDLE_CMD, PoolCubeRuntime

_CLAUDE_SDK_IMAGE = "ghcr.io/agent-platform/claude-sdk-sandbox:0.1"


def _runtime(settings: Settings, client: MagicMock) -> PoolCubeRuntime:
    rt = PoolCubeRuntime.__new__(PoolCubeRuntime)
    rt._settings = settings  # noqa: SLF001 — test setup
    rt._client = client  # noqa: SLF001
    return rt


def test_sandbox_image_defaults_to_claude_sdk_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SANDBOX_POOL_SANDBOX_IMAGE", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.sandbox_image == _CLAUDE_SDK_IMAGE


@pytest.mark.parametrize("created", [{"sandboxID": "sb_warm"}, {"sandbox_id": "sb_warm"}])
def test_start_idle_container_parks_a_claude_sdk_vm(created: dict[str, str]) -> None:
    """E2B answers ``sandboxID`` (what agent-scheduler reads); ``sandbox_id``
    from older controller builds still works."""
    client = MagicMock()
    client.post.return_value = httpx.Response(
        201, json=created, request=httpx.Request("POST", "/x"),
    )
    settings = Settings(
        env="test",
        log_level="WARNING",
        otel_enabled=False,
        sandbox_image=_CLAUDE_SDK_IMAGE,
        owner_id="pool-a",
    )
    rt = _runtime(settings, client)

    sid = rt.start_idle_container("base", memory_limit="4g")

    assert sid == "sb_warm"
    _, kwargs = client.post.call_args
    body = kwargs["json"]
    assert body["template_id"] == _CLAUDE_SDK_IMAGE
    # Not the worker: it exits without a RUN_ID and a real task.
    assert body["cmd"] == ["sleep", "infinity"]
    assert body["cmd"] == IDLE_CMD
    assert body["resources"]["memory_mb"] == 4096
    assert body["metadata"]["agent-platform.pool-managed"] == "true"
    assert body["metadata"]["agent-platform.pool-owner"] == "pool-a"
    # Nothing in the VM reads a task until one is injected.
    assert "AGENT_TASK_JSON" not in body.get("env", {})


def _list_response(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, json=payload, request=httpx.Request("GET", "/sandboxes"),
    )


def _probe_settings() -> Settings:
    return Settings(env="test", log_level="WARNING", otel_enabled=False, owner_id="pool-a")


def test_alive_ids_reads_controller_state_in_one_list_call() -> None:
    client = MagicMock()
    client.get.return_value = _list_response({"items": [
        {"sandboxID": "sb_running", "state": "running"},
        {"sandboxID": "sb_paused", "state": "paused"},
        {"sandboxID": "sb_killed", "state": "killed"},
    ]})
    rt = _runtime(_probe_settings(), client)

    alive = rt.alive_ids(["sb_running", "sb_paused", "sb_killed", "sb_gone"])

    # Absent (sb_gone) and terminal (sb_killed) VMs are dead; a paused VM is
    # resumable, so it still counts as alive.
    assert alive == {"sb_running", "sb_paused"}
    client.get.assert_called_once_with("/sandboxes")


def test_alive_ids_accepts_bare_list_payload() -> None:
    client = MagicMock()
    client.get.return_value = _list_response(
        [{"sandbox_id": "sb_a", "status": "running"}],
    )
    rt = _runtime(_probe_settings(), client)

    assert rt.alive_ids(["sb_a", "sb_b"]) == {"sb_a"}


def test_alive_ids_raises_when_controller_errors() -> None:
    """A failed list must raise, never read as "every warm VM died"."""
    client = MagicMock()
    client.get.return_value = _list_response({"error": "unavailable"}, status=503)
    rt = _runtime(_probe_settings(), client)

    with pytest.raises(RuntimeError):
        rt.alive_ids(["sb_running"])


def test_running_pool_ids_lists_only_live_pool_vms() -> None:
    """A VM without the pool metadata may be a run, and one with another
    owner belongs to a peer replica: neither is ever included."""
    pool_meta = {"agent-platform.pool-managed": "true", "agent-platform.pool-owner": "pool-a"}
    peer_meta = {"agent-platform.pool-managed": "true", "agent-platform.pool-owner": "pool-b"}
    client = MagicMock()
    client.get.return_value = _list_response({"items": [
        {"sandboxID": "sb_warm", "state": "running", "metadata": pool_meta},
        {"sandboxID": "sb_peer", "state": "running", "metadata": peer_meta},
        {"sandboxID": "sb_old", "state": "running",
         "metadata": {"agent-platform.pool-managed": "true"}},
        {"sandboxID": "sb_dead", "state": "killed", "metadata": pool_meta},
        {"sandboxID": "sb_run", "state": "running", "metadata": {"run_id": "run_1"}},
        {"sandboxID": "sb_bare", "state": "running"},
    ]})
    rt = _runtime(_probe_settings(), client)

    assert rt.running_pool_ids() == {"sb_warm"}


def test_running_pool_ids_raises_when_controller_errors() -> None:
    client = MagicMock()
    client.get.side_effect = httpx.ConnectError("refused")
    rt = _runtime(_probe_settings(), client)

    with pytest.raises(RuntimeError):
        rt.running_pool_ids()
