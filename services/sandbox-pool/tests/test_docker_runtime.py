"""Unit tests for ``PoolDockerRuntime`` — FIX B4 kernel hardening.

The warm-pool spawns idle containers via ``containers.run(...)``; every such
container must carry the same kernel-hardening kwargs the agent-scheduler
applies to per-run sandboxes (no-new-privileges, cap_drop=ALL, pids_limit, a
nofile ulimit).  A fake docker client captures the ``run`` kwargs so we can
assert on them without a Docker daemon.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import docker

from sandbox_pool.config import Settings
from sandbox_pool.docker_runtime import PoolDockerRuntime


def _settings(**over: object) -> Settings:
    base: dict[str, object] = {
        "env": "test",
        "log_level": "WARNING",
        "sandbox_image": "test-image:latest",
        "docker_network": "test_net",
        "otel_enabled": False,
    }
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


def _runtime(mock_client: MagicMock) -> PoolDockerRuntime:
    rt = PoolDockerRuntime.__new__(PoolDockerRuntime)
    rt._settings = _settings()  # noqa: SLF001 — test setup
    rt._client = mock_client  # noqa: SLF001
    return rt


def test_start_idle_container_applies_kernel_hardening() -> None:
    """Warm-pool idle containers must carry the B4 kernel-hardening kwargs."""
    mock_client = MagicMock()
    mock_client.containers.run.return_value = MagicMock(id="0123456789abcdef")
    rt = _runtime(mock_client)

    rt.start_idle_container("base")

    _, kwargs = mock_client.containers.run.call_args
    assert kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["pids_limit"] == 512
    ulimits = kwargs["ulimits"]
    assert len(ulimits) == 1
    nofile = ulimits[0]
    assert isinstance(nofile, docker.types.Ulimit)
    assert nofile["Name"] == "nofile"
    assert nofile["Soft"] == 4096
    assert nofile["Hard"] == 8192
    # B4 explicitly does NOT flip the rootfs read-only in this pass.
    assert kwargs.get("read_only") in (None, False)
    # Sanity: the idle command + label convention are unchanged.
    assert kwargs["command"] == ["sleep", "infinity"]
    assert kwargs["labels"]["agent-platform.pool-managed"] == "true"
