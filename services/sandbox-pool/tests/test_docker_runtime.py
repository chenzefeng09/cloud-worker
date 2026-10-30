"""Unit tests for ``PoolDockerRuntime`` — FIX B4 kernel hardening, idle entrypoint, liveness.

The warm-pool spawns idle containers via ``containers.run(...)``; every such
container must carry the same kernel-hardening kwargs the agent-scheduler
applies to per-run sandboxes (no-new-privileges, cap_drop=ALL, pids_limit, a
nofile ulimit), and must park on ``sleep infinity`` instead of starting the
image's worker entrypoint.  A fake docker client captures the ``run`` / ``list``
kwargs so we can assert on them without a Docker daemon.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import docker
import pytest

from sandbox_pool.config import Settings
from sandbox_pool.docker_runtime import PoolDockerRuntime


def _settings(**over: object) -> Settings:
    base: dict[str, object] = {
        "env": "test",
        "log_level": "WARNING",
        "sandbox_image": "test-image:latest",
        "docker_network": "test_net",
        "otel_enabled": False,
        "owner_id": "pool-a",
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
    # Sanity: the label convention is unchanged.
    assert kwargs["labels"]["agent-platform.pool-managed"] == "true"


def test_pool_owner_defaults_to_the_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under Kubernetes the hostname is the pod name; compose pins owner_id."""
    monkeypatch.setattr("sandbox_pool.config.socket.gethostname", lambda: "pool-pod-7")
    assert _settings(owner_id="").pool_owner == "pool-pod-7"
    assert _settings(owner_id=" agent-sandbox-pool ").pool_owner == "agent-sandbox-pool"


def test_start_idle_container_stamps_the_pool_owner() -> None:
    """The owner label is what keeps a replica from reaping a peer's containers."""
    mock_client = MagicMock()
    mock_client.containers.run.return_value = MagicMock(id="0123456789abcdef")
    rt = _runtime(mock_client)

    rt.start_idle_container("base")

    _, kwargs = mock_client.containers.run.call_args
    assert kwargs["labels"]["agent-platform.pool-owner"] == "pool-a"


def test_start_idle_container_overrides_worker_entrypoint() -> None:
    """The idle container must park on ``sleep infinity``, not run the worker.

    The sandbox image's ENTRYPOINT is the worker (``node /app/dist/main.js``),
    so a ``command`` would only be appended to it as argv and the worker would
    exit 1 with no RUN_ID.  The entrypoint itself is overridden, no command.
    """
    mock_client = MagicMock()
    mock_client.containers.run.return_value = MagicMock(id="0123456789abcdef")
    rt = _runtime(mock_client)

    rt.start_idle_container("base")

    args, kwargs = mock_client.containers.run.call_args
    assert kwargs["entrypoint"] == ["sleep", "infinity"]
    assert not args
    assert kwargs.get("command") is None
    # docker-init forwards SIGTERM, which ``sleep`` as PID 1 would ignore.
    assert kwargs["init"] is True


def test_alive_ids_is_one_label_filtered_list() -> None:
    """Liveness is one ``containers.list`` of running pool containers, not an inspect per id."""
    mock_client = MagicMock()
    mock_client.containers.list.return_value = [
        MagicMock(id="aaaaaaaaaaaa" + "0" * 52),
        MagicMock(id="cccccccccccc" + "0" * 52),  # running, but not asked about
    ]
    rt = _runtime(mock_client)

    alive = rt.alive_ids(["aaaaaaaaaaaa", "bbbbbbbbbbbb"])

    # bbbbbbbbbbbb is absent from the running list (exited or removed) → dead.
    assert alive == {"aaaaaaaaaaaa"}
    mock_client.containers.list.assert_called_once_with(
        filters={
            "label": [
                "agent-platform.pool-managed=true",
                "agent-platform.pool-owner=pool-a",
            ],
            "status": "running",
        },
        sparse=True,
    )
    mock_client.containers.get.assert_not_called()


def test_alive_ids_propagates_docker_errors() -> None:
    """A daemon error must raise, never read as "every container is dead"."""
    mock_client = MagicMock()
    mock_client.containers.list.side_effect = docker.errors.APIError("daemon down")
    rt = _runtime(mock_client)

    with pytest.raises(docker.errors.APIError):
        rt.alive_ids(["aaaaaaaaaaaa"])


def test_running_pool_ids_lists_running_pool_containers() -> None:
    """Exited pool containers are not listed: they hold no CPU or memory."""
    mock_client = MagicMock()
    mock_client.containers.list.return_value = [MagicMock(id="aaaaaaaaaaaa" + "0" * 52)]
    rt = _runtime(mock_client)

    assert rt.running_pool_ids() == {"aaaaaaaaaaaa"}
    mock_client.containers.list.assert_called_once_with(
        filters={
            "label": [
                "agent-platform.pool-managed=true",
                "agent-platform.pool-owner=pool-a",
            ],
            "status": "running",
        },
        sparse=True,
    )
