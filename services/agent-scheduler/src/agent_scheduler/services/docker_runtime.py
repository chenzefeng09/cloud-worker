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
import mimetypes
import re
from pathlib import Path
from typing import Any

import docker
from docker.types import Mount

from agent_scheduler.config import SANDBOX_SERVICE_SCOPES, Settings
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
        skills: list[str] | None = None,
        files: list[dict] | None = None,
        memory: str | None = None,
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
            skills: optional list of skill names to activate in the sandbox.
                Embedded in task.json; consumed by ``skills_loader`` extension.
            files: optional list of ``{"key": s3key, "filename": name, "mime": mime}``
                dicts describing user-uploaded attachments.  Written to task.json
                ``files`` field; the runtime_worker downloads them to
                ``/workspace/inputs/`` before starting the agent.
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
        self._write_task_file(
            workspace_dir,
            task=task,
            skills=skills or [],
            files=files or [],
            memory=memory or "",
        )

        env_vars = {
            "LLM_API_KEY": self._settings.llm_api_key,
            "LLM_MODEL": self._settings.llm_model,
            # Provider selector + base URL are forwarded so the in-sandbox
            # worker can talk to OpenAI-compatible endpoints (DeepSeek, etc.)
            # without bundling provider-specific knowledge in the image.
            # The worker treats the empty string as "fall back to default".
            "LLM_PROVIDER": self._settings.llm_provider,
            "LLM_BASE_URL": self._settings.llm_base_url,
            "WORKSPACE_BASE": str(workspace_dir),
            "FOUNDATION_API_URL": self._settings.foundation_api_url,
            "FOUNDATION_API_TOKEN": foundation_token,
            "FOUNDATION_TENANT_ID": tenant_id,
            "FOUNDATION_USER_ID": user_id,
            "OTEL_EXPORTER_OTLP_ENDPOINT": self._settings.otel_exporter_endpoint,
            # B-M3: service URLs for the in-sandbox extensions.
            # mcp_tenant_injector reads MCP_REGISTRY_URL;
            # skills_loader reads SKILL_STORE_URL.
            # RUNTIME_EXTENSIONS activates them in declaration order.
            "MCP_REGISTRY_URL": self._settings.mcp_registry_url,
            "SKILL_STORE_URL": self._settings.skill_store_url,
            "RUNTIME_EXTENSIONS": self._settings.runtime_extensions,
            # Dev-mode shim: in prod the API gateway translates the verified
            # Bearer JWT into X-Service-Scopes before requests reach internal
            # endpoints.  In this deployment (no gateway between sandbox and
            # these services) the scheduler injects the scopes directly so the
            # mcp_tenant_injector / skills_loader extensions can set the header.
            # TODO: remove once the gateway enforces scope injection in prod.
            "FOUNDATION_SERVICE_SCOPES": SANDBOX_SERVICE_SCOPES,
            # S3/MinIO credentials — consumed by runtime_worker to download
            # user-uploaded attachments listed in task.json ``files``.
            "S3_ENDPOINT_URL": self._settings.s3_endpoint_url,
            "S3_BUCKET": self._settings.s3_bucket,
            "S3_REGION": self._settings.s3_region,
            "S3_ACCESS_KEY_ID": self._settings.s3_access_key_id,
            "S3_SECRET_ACCESS_KEY": self._settings.s3_secret_access_key,
            # Hot-state layer: stable run identifier + Redis URL so the
            # in-sandbox worker can write step/plan/cursor checkpoints.
            # The sandbox is on saas-foundation-dev_foundation network (same
            # as foundation-redis) so it reaches foundation-redis:6379 directly
            # (plan a — no sidecar needed).
            "RUN_ID": run_id,
            "REDIS_URL": self._settings.redis_url,
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

        # On terminal states, pull the worker's ``result.json`` from the
        # per-run workspace so callers (session-orchestrator) can persist
        # the assistant summary without having to bind-mount workspaces.
        # Best-effort: the file may be missing if the worker crashed before
        # it could write it; we surface a None summary in that case.
        if status in ("COMPLETED", "FAILED"):
            labels = c.labels or {}
            run_id = labels.get("agent-platform.run-id", "")
            session_id = labels.get("agent-platform.session-id", "")
            tenant_id = labels.get("agent-platform.tenant-id", "")
            if run_id and session_id and tenant_id:
                workspace = (
                    Path(self._settings.workspace_base)
                    / tenant_id
                    / session_id
                    / run_id
                )
                result_path = workspace / "result.json"
                if result_path.is_file():
                    try:
                        import json

                        payload = json.loads(result_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        payload = {}
                    result["result_summary"] = payload.get("summary")
                    result["result_error"] = payload.get("error")
                    result["result_status"] = payload.get("status")
                    # If the worker reported FAILED via result.json (exit 0
                    # with status="FAILED"), promote that over the docker
                    # exit-code-based status so callers see the real reason.
                    if payload.get("status") == "FAILED" and result["status"] == "COMPLETED":
                        result["status"] = "FAILED"

                # Upload artifacts to S3 and attach the list to the result.
                # Failure-open: any upload error is logged; the run result
                # is still returned without the failed artifact.
                result["artifacts"] = self._upload_artifacts(
                    workspace=workspace,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    run_id=run_id,
                )
        return result

    # ---------------------------------------------------------------- artifacts
    _EXCLUDED_NAMES: frozenset[str] = frozenset({
        "task.json", "result.json", "events.jsonl",
    })

    def _upload_artifacts(
        self,
        *,
        workspace: Path,
        tenant_id: str,
        session_id: str,
        run_id: str,
    ) -> list[dict[str, Any]]:
        """Scan ``workspace`` for output files and upload them to S3.

        Excluded: task.json, result.json, events.jsonl, and everything under
        inputs/ (user-uploaded originals).  Returns a list of artifact dicts::

            [{"filename": str, "s3_key": str, "size": int, "mime": str}, ...]

        Failure-open: individual file errors are logged and skipped.
        """
        if not workspace.is_dir():
            return []

        s3_client = self._make_s3_client()
        if s3_client is None:
            return []

        artifacts: list[dict[str, Any]] = []
        inputs_dir = workspace / "inputs"

        for fpath in workspace.rglob("*"):
            if not fpath.is_file():
                continue
            # Skip inputs/ subdirectory.
            try:
                fpath.relative_to(inputs_dir)
                continue  # inside inputs/ — skip
            except ValueError:
                pass
            # Skip system files at workspace root.
            if fpath.parent == workspace and fpath.name in self._EXCLUDED_NAMES:
                continue

            relpath = fpath.relative_to(workspace).as_posix()
            s3_key = (
                f"tenant/{tenant_id}/sessions/{session_id}"
                f"/runs/{run_id}/artifacts/{relpath}"
            )
            try:
                data = fpath.read_bytes()
                mime, _ = mimetypes.guess_type(fpath.name)
                mime = mime or "application/octet-stream"
                s3_client.put_object(
                    Bucket=self._settings.s3_bucket,
                    Key=s3_key,
                    Body=data,
                    ContentType=mime,
                )
                artifacts.append({
                    "filename": fpath.name,
                    "s3_key": s3_key,
                    "size": len(data),
                    "mime": mime,
                })
                logger.info(
                    "artifact uploaded",
                    extra={"s3_key": s3_key, "size": len(data), "run_id": run_id},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "artifact upload failed (failure-open)",
                    extra={"path": str(fpath), "err": str(exc)},
                )

        return artifacts

    def _make_s3_client(self) -> Any | None:
        """Build a boto3 S3 client using S3_* settings (not AWS_* env vars).

        Returns None if boto3 is unavailable or credentials are unset.
        """
        try:
            import boto3  # type: ignore[import-not-found]
            client = boto3.client(
                "s3",
                endpoint_url=self._settings.s3_endpoint_url,
                region_name=self._settings.s3_region,
                aws_access_key_id=self._settings.s3_access_key_id,
                aws_secret_access_key=self._settings.s3_secret_access_key,
            )
            return client
        except Exception as exc:  # noqa: BLE001
            logger.warning("S3 client unavailable: %s", exc)
            return None

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
            # ``c.image`` triggers an image inspect under the hood; if the
            # image was pruned/replaced (e.g. the sandbox base image was
            # rebuilt after this container started) the inspect 404s with
            # ImageNotFound.  That must NOT crash the whole listing — fall
            # back to the image id recorded on the container, or "".
            try:
                tags = c.image.tags
                image = tags[0] if tags else (c.attrs.get("Image", ""))
            except Exception:  # noqa: BLE001 -- pruned/missing image, best-effort
                image = c.attrs.get("Image", "")
            result.append({
                "container_id": c.id,
                "run_id": labels.get("agent-platform.run-id", ""),
                "session_id": labels.get("agent-platform.session-id", ""),
                "tenant_id": tenant_id,
                "status": _CONTAINER_STATUS_MAP.get(c.status, "UNKNOWN"),
                "image": image,
                "created_at": c.attrs.get("Created", ""),
            })
        return result

    # ------------------------------------------------------------ task write
    def _write_task_file(
        self,
        workspace_dir: Path,
        *,
        task: str,
        skills: list[str] | None = None,
        files: list[dict] | None = None,
        memory: str | None = None,
    ) -> Path:
        """Write the task payload ``runtime_worker`` will read.

        Schema (pinned by ``agentscope_ext.runtime_worker.__main__``):

            {
              "task": str,
              "mcp_servers": [],   # always empty — mcp_tenant_injector fetches live
              "skills": [str, ...] # skill names; skills_loader fetches manifests
              "files": [{"key": s3key, "filename": name, "mime": mime}, ...]
              "memory": str        # pre-fetched memory snippets (may be empty)
            }

        ``mcp_servers`` stays empty because the in-sandbox ``mcp_tenant_injector``
        extension fetches the enabled server list directly from ``mcp-registry``
        at runtime using ``MCP_REGISTRY_URL`` + ``FOUNDATION_TENANT_ID``.

        ``skills`` carries the caller-supplied list of skill names; the
        ``skills_loader`` extension uses them to resolve manifests from
        ``skill-store`` at runtime.

        ``files`` carries user-uploaded attachments; the runtime_worker
        downloads them to ``/workspace/inputs/`` before starting the agent.

        ``memory`` carries pre-fetched relevant memory snippets retrieved by
        the orchestrator from knowledge-ingest before spawning.  The worker
        prepends them to the system prompt so the agent has context from past
        interactions.  May be an empty string if no memories matched.

        Putting the file under the workspace means it's cleaned up when
        the per-run workspace is torn down (vs the old ``tempfile``
        approach which leaked one file per run under ``/tmp``).
        """
        import json
        payload = {
            "task": task,
            "mcp_servers": [],
            "skills": list(skills) if skills else [],
            "files": list(files) if files else [],
            "memory": memory or "",
        }
        task_path = workspace_dir / "task.json"
        task_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return task_path


# Register as the ``docker`` runtime kind.  This is the dev/CI default
# (CONTRIBUTING contributors don't need KVM); production overrides via
# ``SCHEDULER_RUNTIME=cube``.  See ADR 0001.
RUNTIME_REGISTRY["docker"] = DockerRuntime
