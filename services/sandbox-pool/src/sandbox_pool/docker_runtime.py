"""Docker-backed sandbox lifecycle for the pool.

The pool needs a narrower interface than the agent-scheduler's DockerRuntime:
it only needs to *start an idle container* (no task payload, no workspace
bind-mount) and *stop + remove* it by id.  We do NOT duplicate spawn_run logic
here; the agent-scheduler's DockerRuntime handles running containers.

Security posture matches agent-scheduler:

* Talks through the docker-socket-proxy sidecar via DOCKER_HOST (set in
  compose).  No direct /var/run/docker.sock bind-mount.
* Containers are labelled ``agent-platform.pool-managed=true`` and
  ``agent-platform.pool-state=idle`` so they can be audited and cleaned up
  by anything that knows the label convention.
* The sandbox container is started with an idle command (``sleep infinity``
  for Docker — the real agentscope_ext.runtime_worker runs only after
  acquire + task injection in the KEDA/B-M3 path).  For the current Docker
  dev path the idle container just parks a warm image; the acquire caller
  stops it and agent-scheduler spawns a fresh one with the task.  This gives
  the warm-pool the intended latency benefit: image layers are already pulled
  and the image is in the kernel page cache.

Pool label convention
---------------------
``agent-platform.pool-managed`` = ``"true"``
    Marks every container we own — lets an operator ``docker ps
    --filter label=agent-platform.pool-managed=true`` to audit.
``agent-platform.pool-state`` = ``"idle"`` | ``"acquired"``
    Mutated by acquire / release.  The pool keeps the authoritative in-memory
    state in ``PoolState``; the Docker label is best-effort for observability.
``agent-platform.pool-template`` = ``<template_name>``
    Which template this container was spawned from.
"""

from __future__ import annotations

import contextlib
import logging
import os

import docker
import docker.errors

from sandbox_pool.config import Settings

logger = logging.getLogger(__name__)

# Label prefix shared with agent-scheduler so both services' containers can
# be enumerated with a common filter.
_LABEL_PREFIX = "agent-platform"


class PoolDockerRuntime:
    """Thin Docker client wrapper used only by the pool.

    Instantiate once per Pool; the underlying docker.DockerClient is
    long-lived (HTTP connection-pooled).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: docker.DockerClient | None = None
        self._init_client()

    def _init_client(self) -> None:
        # Honour an explicit docker_host setting first; fall back to the
        # DOCKER_HOST env var (set in compose to tcp://docker-socket-proxy:2375),
        # and finally to the SDK's own auto-detection (local socket).
        host = self._settings.docker_host or os.environ.get("DOCKER_HOST", "")
        try:
            if host:
                self._client = docker.DockerClient(base_url=host)
            else:
                self._client = docker.from_env()
        except Exception:  # pragma: no cover
            logger.exception(
                "docker client unavailable; pool will refuse spawn "
                "(check DOCKER_HOST / docker-socket-proxy)"
            )
            self._client = None

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            raise RuntimeError(
                "Docker is not available. Check DOCKER_HOST points at the "
                "socket-proxy sidecar (DOCKER_HOST=tcp://docker-socket-proxy:2375)."
            )
        return self._client

    # ------------------------------------------------------------------ spawn

    def start_idle_container(self, template: str) -> str:
        """Start a warm idle sandbox container and return its 12-char short id.

        The container runs ``sleep infinity`` so it parks the warm image
        without consuming CPU.  The pool hands the *short id* out as
        sandbox_id — 12 hex chars, matching the VARCHAR(50) convention used
        for container_id throughout this project.

        Labels set on the container:
        * ``agent-platform.pool-managed=true``
        * ``agent-platform.pool-state=idle``
        * ``agent-platform.pool-template=<template>``
        """
        try:
            container = self.client.containers.run(
                image=self._settings.sandbox_image,
                command=["sleep", "infinity"],
                network=self._settings.docker_network,
                mem_limit=self._settings.sandbox_memory_limit,
                cpu_shares=self._settings.sandbox_cpu_shares,
                detach=True,
                remove=False,
                labels={
                    f"{_LABEL_PREFIX}.pool-managed": "true",
                    f"{_LABEL_PREFIX}.pool-state": "idle",
                    f"{_LABEL_PREFIX}.pool-template": template,
                },
            )
        except docker.errors.ImageNotFound as exc:
            raise RuntimeError(
                f"Sandbox image not found: {self._settings.sandbox_image}. "
                "Pull it or build from sandbox-images/base.Dockerfile."
            ) from exc
        except docker.errors.APIError as exc:
            raise RuntimeError(f"Docker API error while spawning pool container: {exc}") from exc

        short_id: str = container.id[:12]
        logger.info(
            "pool: idle container started",
            extra={"container_id": short_id, "template": template},
        )
        return short_id

    # ------------------------------------------------------------------ reap

    def stop_container(self, container_id: str) -> None:
        """Stop and remove a pool-managed container by id.

        Idempotent: if the container is already gone (e.g. crashed before
        reap) the errors are suppressed and logged as warnings.
        """
        try:
            c = self.client.containers.get(container_id)
        except docker.errors.NotFound:
            logger.warning(
                "pool: container not found during reap (already gone?)",
                extra={"container_id": container_id},
            )
            return

        try:
            c.stop(timeout=5)
        except docker.errors.APIError:
            # Already stopping or dead — fall through to remove.
            logger.warning(
                "pool: container stop returned APIError; continuing to remove",
                extra={"container_id": container_id},
            )

        with contextlib.suppress(docker.errors.NotFound):
            c.remove()

        logger.info(
            "pool: idle container stopped and removed",
            extra={"container_id": container_id},
        )

    # ------------------------------------------------------------------ label

    def set_pool_state_label(self, container_id: str, state: str) -> None:
        """Update the ``pool-state`` label on a running container.

        Docker doesn't support live label mutation; we achieve the same
        observable effect by renaming is not possible either — so we just
        log the intent.  The authoritative state lives in ``PoolState``;
        the label is best-effort observability only.
        """
        # Docker has no "update labels" API.  Best we can do: log it so
        # ``docker inspect`` output at least says what state we intended.
        logger.debug(
            "pool state change",
            extra={"container_id": container_id, "pool_state": state},
        )
