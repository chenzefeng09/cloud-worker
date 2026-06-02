"""Docker runtime — spawns and manages sandbox containers (AgentScope worker).

Security posture for B-M1
-------------------------
The scheduler still uses Docker for B-M1 (CubeSandbox migrates in B-M2).
Three hardening rules apply until then:

1. **No direct ``/var/run/docker.sock`` mount.**  The agent-scheduler container
   talks to a ``tecnativa/docker-socket-proxy`` sidecar over a private
   network; the proxy whitelists only the API verbs we actually need
   (containers, images, networks).  This client therefore honours the
   ``DOCKER_HOST`` env var instead of calling ``docker.from_env()`` with no
   arguments — see ``deploy/docker-compose/agent-platform.yml``.

2. **No global service token in container env.**  ``spawn_run`` requires a
   ``foundation_token`` argument that the caller (``api/v1/runs.py``) must
   mint per-run via ``foundation_sdk.identity.issue_service_account_token``.
   The token's TTL is bounded by ``Settings.sandbox_timeout``, scope is the
   minimum the agent needs, and the audience is ``foundation``.  Token
   never appears in env or logs of the scheduler itself.

3. **Task payload is a file, not argv.**  The user task is written to
   ``/workspace/task.json`` (UTF-8 JSON) for the AgentScope worker to
   read; we never splice user-controlled text into the container's
   command line.  The file lives inside the per-run workspace dir so
   it's cleaned up when the workspace is torn down (no stray files under
   ``/tmp`` building up indefinitely).
"""

from __future__ import annotations

import contextlib
import logging
import re
from pathlib import Path

import docker
from docker.types import Mount

from agent_scheduler.config import Settings
from agent_scheduler.errors import ContainerNotFoundError, SandboxSpawnError
from agent_scheduler.services.runtime import RUNTIME_REGISTRY

logger = logging.getLogger(__name__)

_CONTAINER_STATUS_MAP = {
    "running": "RUNNING",
    "exited": "COMPLETED",
    "paused": "PAUSED",
    "dead": "FAILED",
    "created": "PENDING",
    "restarting": "RUNNING",
    "removing": "FAILED",
}

# Conservative identifier whitelist for anything that gets spliced into
# filesystem paths or container labels.  ULIDs (26 base32 chars) and
# kebab/underscore IDs both fit; if a future ID format breaks this the
# explicit failure is the desired behaviour — extend the regex deliberately.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")

# Hard cap on task strings so a malicious or runaway prompt cannot bloat
# the workspace task.json or our log files.  32 KiB is generous for
# natural language and matches the limit advertised in contracts/openapi.
_MAX_TASK_LEN = 32 * 1024


def _check_ids(**named: str) -> None:
    """Reject if any of the supplied IDs fail :data:`_SAFE_ID`.

    Batch form avoids both the call-site noise of four sequential
    ``_check_id(x, name="x")`` calls and the per-call function overhead
    on the spawn hot path.
    """
    for name, value in named.items():
        if not _SAFE_ID.match(value):
            raise SandboxSpawnError(
                f"{name}={value!r} fails identifier whitelist; refusing to spawn",
            )


class DockerRuntime:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        try:
            # docker.from_env() picks up DOCKER_HOST automatically, which is
            # how we route through the socket-proxy sidecar in compose.
            self._client = docker.from_env()
        except Exception:  # pragma: no cover — environment misconfiguration
            logger.exception("docker client unavailable; runtime will refuse spawn")
            self._client = None

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            raise SandboxSpawnError(
                "Docker is not available. Check DOCKER_HOST points at the "
                "socket-proxy sidecar."
            )
        return self._client

    # ------------------------------------------------------------------ spawn
    def spawn_run(
        self,
        run_id: str,
        session_id: str,
        task: str,
        *,
        tenant_id: str,
        user_id: str,
        foundation_token: str,
    ) -> str:
        """Spawn a sandbox container for one agent run.

        Args:
            run_id, session_id, tenant_id, user_id: validated against
                ``_SAFE_ID``; rejected request rather than tolerated.
            task: free-form user instruction; truncated at ``_MAX_TASK_LEN``
                with a sentinel suffix so the rejection is observable.
            foundation_token: short-lived, scope-limited JWT minted by the
                caller via ``IdentityResource.issue_service_account_token``.
                MUST be non-empty — passing the empty string defeats the
                purpose of per-run tokens, so we reject it outright.
        """
        _check_ids(
            run_id=run_id,
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        if not foundation_token:
            raise SandboxSpawnError(
                "foundation_token is required; mint it via "
                "identity.issue_service_account_token before spawning"
            )
        if len(task) > _MAX_TASK_LEN:
            task = task[: _MAX_TASK_LEN - 32] + "\n…[truncated by scheduler]"

        workspace_dir = Path(self._settings.workspace_base) / tenant_id / session_id / run_id
        workspace_dir.mkdir(parents=True, exist_ok=True)

        # The task arrives as a file the worker reads, not an argv arg,
        # so we never splice user-controlled text into a shell command.
        # Schema is pinned by ``agentscope_ext.runtime_worker.__main__``.
        self._write_task_file(workspace_dir, task=task)

        env_vars = {
            "LLM_API_KEY": self._settings.llm_api_key,
            "LLM_MODEL": self._settings.llm_model,
            "WORKSPACE_BASE": str(workspace_dir),
            "FOUNDATION_API_URL": self._settings.foundation_api_url,
            "FOUNDATION_API_TOKEN": foundation_token,
            "FOUNDATION_TENANT_ID": tenant_id,
            "FOUNDATION_USER_ID": user_id,
            "OTEL_EXPORTER_OTLP_ENDPOINT": self._settings.otel_exporter_endpoint,
        }

        try:
            container = self.client.containers.run(
                image=self._settings.agent_image,
                # AgentScope worker entrypoint — see agentscope-ext/README.md.
                # Argv-only invocation; the task itself lives in workspace/task.json.
                command=["python", "-m", "agentscope_ext.runtime_worker"],
                environment=env_vars,
                mounts=[
                    Mount(
                        target="/workspace",
                        source=str(workspace_dir),
                        type="bind",
                    ),
                ],
                network=self._settings.docker_network,
                mem_limit=self._settings.sandbox_memory_limit,
                cpu_shares=self._settings.sandbox_cpu_shares,
                detach=True,
                labels={
                    "agent-platform.run-id": run_id,
                    "agent-platform.session-id": session_id,
                    "agent-platform.tenant-id": tenant_id,
                },
                remove=False,
            )
        except docker.errors.ImageNotFound as exc:
            raise SandboxSpawnError(
                f"Sandbox image not found: {self._settings.agent_image}. "
                f"Build it from sandbox-images/base.Dockerfile or pull from the registry."
            ) from exc
        except docker.errors.APIError as exc:
            raise SandboxSpawnError(f"Docker API error: {exc}") from exc

        logger.info(
            "Container spawned",
            extra={
                "container_id": container.id,
                "run_id": run_id,
                "tenant_id": tenant_id,
                # Token deliberately not logged.
            },
        )
        return container.id

    # --------------------------------------------------------------- inspect
    def get_run_status(self, container_id: str) -> dict:
        try:
            c = self.client.containers.get(container_id)
        except docker.errors.NotFound:
            return {"status": "NOT_FOUND", "exit_code": None}

        status = _CONTAINER_STATUS_MAP.get(c.status, "UNKNOWN")
        result: dict = {
            "status": status,
            "exit_code": c.attrs["State"].get("ExitCode"),
            "started_at": c.attrs["State"].get("StartedAt"),
            "finished_at": c.attrs["State"].get("FinishedAt"),
        }
        if status == "COMPLETED" and result["exit_code"] != 0:
            result["status"] = "FAILED"
        return result

    def get_container_tenant(self, container_id: str) -> str:
        """Return the ``agent-platform.tenant-id`` label of a container.

        Used by API handlers to enforce ownership before destructive actions
        like ``stop_sandbox``.  Raises :class:`ContainerNotFoundError` if the
        container doesn't exist or doesn't belong to agent-platform.
        """
        try:
            c = self.client.containers.get(container_id)
        except docker.errors.NotFound as exc:
            raise ContainerNotFoundError(container_id) from exc
        tenant = c.labels.get("agent-platform.tenant-id", "")
        if not tenant:
            raise ContainerNotFoundError(container_id)
        return tenant

    # ---------------------------------------------------------------- cancel
    def cancel_run(self, container_id: str) -> None:
        try:
            c = self.client.containers.get(container_id)
        except docker.errors.NotFound as exc:
            raise ContainerNotFoundError(container_id) from exc
        try:
            c.stop(timeout=10)
        except docker.errors.APIError:
            # Already stopped or stopping — fall through to remove.
            logger.warning("container stop returned APIError; continuing to remove",
                           extra={"container_id": container_id})
        with contextlib.suppress(docker.errors.NotFound):
            # Removed concurrently; nothing to do.
            c.remove()

    # ------------------------------------------------------------------ list
    def list_containers(self, *, tenant_id: str) -> list[dict]:
        """List agent-platform containers belonging to ``tenant_id``.

        The argument is required; callers in the API layer have already
        rejected requests without a tenant header, but enforcing it here
        too means an accidental internal caller (e.g. cron) can't widen
        the filter by passing ``None`` or ``""``.
        """
        if not tenant_id:
            raise ValueError("tenant_id is required for list_containers")

        filters = {"label": f"agent-platform.tenant-id={tenant_id}"}
        containers = self.client.containers.list(filters=filters, all=True)

        result = []
        for c in containers:
            labels = c.labels
            # Belt + braces: trust labels for filtering but double-check the
            # label actually matches the caller's tenant.
            if labels.get("agent-platform.tenant-id") != tenant_id:
                continue
            result.append({
                "container_id": c.id,
                "run_id": labels.get("agent-platform.run-id", ""),
                "session_id": labels.get("agent-platform.session-id", ""),
                "tenant_id": tenant_id,
                "status": _CONTAINER_STATUS_MAP.get(c.status, "UNKNOWN"),
                "image": c.image.tags[0] if c.image.tags else "",
                "created_at": c.attrs.get("Created", ""),
            })
        return result

    # ------------------------------------------------------------ task write
    def _write_task_file(self, workspace_dir: Path, *, task: str) -> Path:
        """Write the task payload ``runtime_worker`` will read.

        Schema is pinned by ``agentscope_ext.runtime_worker.__main__``:

            {"task": str, "mcp_servers": [...], "skills": [...]}

        ``mcp_servers`` / ``skills`` are reserved for B-M3 when the
        ``mcp-registry`` and ``skill-store`` services exist; for now we
        emit empty arrays so the schema is stable.

        Putting the file under the workspace means it's cleaned up when
        the per-run workspace is torn down (vs the old ``tempfile``
        approach which leaked one file per run under ``/tmp``).
        """
        import json
        payload = {
            "task": task,
            "mcp_servers": [],
            "skills": [],
        }
        task_path = workspace_dir / "task.json"
        task_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return task_path


# Register as the ``docker`` runtime kind.  This is the dev/CI default
# (CONTRIBUTING contributors don't need KVM); production overrides via
# ``SCHEDULER_RUNTIME=cube``.  See ADR 0001.
RUNTIME_REGISTRY["docker"] = DockerRuntime
