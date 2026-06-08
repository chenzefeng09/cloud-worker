"""CubeSandbox runtime — microVM-backed sandbox via the E2B HTTP protocol.

Status
------
**Skeleton.**  Spawn / status / cancel are wired to the E2B endpoints
(``POST /sandboxes`` etc.) but B-M2 still needs:

* CubeProxy URL discovery (env / service-discovery)
* mTLS to the CubeSandbox controller (ADR 0001 follow-up)
* Snapshot-based warm pool integration with ``services/sandbox-pool``
* Hard CPU/mem caps via E2B's ``resources`` payload (vs the
  ``mem_limit`` / ``cpu_shares`` we currently use for Docker)

The class implements the :class:`SandboxRuntime` protocol so
``agent-scheduler.api.v1.runs`` already works against it once
``SCHEDULER_RUNTIME=cube`` is set — calls that hit unimplemented paths
raise :class:`NotImplementedError` with a pointer to the design doc.

References
----------
* `CubeSandbox README <https://github.com/TencentCloud/CubeSandbox>`_ — E2B
  protocol compatibility section.
* ADR 0001 (this repo) — why we picked CubeSandbox + when to fall back.
* ``docs/design/b-m2-sandbox-runtime-v1.md`` — full design.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from agent_scheduler.config import SANDBOX_SERVICE_SCOPES, Settings
from agent_scheduler.errors import ContainerNotFoundError, SandboxSpawnError
from agent_scheduler.services.runtime import RUNTIME_REGISTRY

logger = logging.getLogger(__name__)

_SAFE_ID = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")
_MAX_TASK_LEN = 32 * 1024

# E2B normalises status to its own vocabulary; map to ours so the API
# layer never needs to know which runtime answered.
_E2B_STATUS_MAP: dict[str, str] = {
    "creating": "PENDING",
    "running": "RUNNING",
    "paused": "PAUSED",
    "killed": "CANCELLED",
    "completed": "COMPLETED",
    "errored": "FAILED",
    "not_found": "NOT_FOUND",
}


def _check_ids(**named: str) -> None:
    """Same identifier whitelist as docker_runtime — kept in lockstep."""
    for name, value in named.items():
        if not _SAFE_ID.match(value):
            raise SandboxSpawnError(
                f"{name}={value!r} fails identifier whitelist; refusing to spawn",
            )


class CubeSandboxRuntime:
    """E2B-protocol HTTP client against the CubeSandbox controller.

    Talks to ``settings.cube_controller_url`` (the CubeProxy front door)
    using a single long-lived ``httpx.Client``.  The client is sync to
    match the :class:`SandboxRuntime` protocol; the API layer wraps
    blocking calls with ``asyncio.to_thread`` when needed.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        url = getattr(settings, "cube_controller_url", None)
        if not url:
            # We register the runtime unconditionally so config can
            # decide at startup which one to build, but spawning without
            # a URL is a deployment error — fail clearly when called.
            self._client: httpx.Client | None = None
            return
        self._client = self._make_client(url)

    # --------------------------------------------------------------- transport
    def _make_client(self, base_url: str) -> httpx.Client:
        """Build the httpx.Client with the configured TLS mode.

        Resolution rules (also documented on ``Settings.cube_tls_mode``):

        * ``mtls`` — load client cert+key and CA from settings.  Any of the
          three missing is a deployment error and raises RuntimeError so
          the process refuses to start (failing here is much better than
          silently downgrading to plain TLS in production).
        * ``tls``  — only the CA bundle matters; missing CA also raises.
        * ``off``  — plain HTTP.  No cert material is loaded even if set,
          so an operator can leave certs in env while testing locally.
        """
        mode = self._settings.cube_tls_mode
        timeout = httpx.Timeout(connect=2.0, read=10.0, write=10.0, pool=2.0)

        if mode == "off":
            return httpx.Client(base_url=base_url, timeout=timeout)

        ca = self._settings.cube_ca_cert
        if not ca:
            raise RuntimeError(
                f"cube_tls_mode={mode!r} requires cube_ca_cert "
                "(SCHEDULER_CUBE_CA_CERT); refusing to build CubeSandbox client",
            )

        if mode == "tls":
            return httpx.Client(base_url=base_url, timeout=timeout, verify=ca)

        # mode == "mtls"
        cert = self._settings.cube_client_cert
        key = self._settings.cube_client_key
        if not cert or not key:
            raise RuntimeError(
                "cube_tls_mode='mtls' requires cube_client_cert + cube_client_key "
                "(SCHEDULER_CUBE_CLIENT_CERT / SCHEDULER_CUBE_CLIENT_KEY); "
                "refusing to build CubeSandbox client",
            )
        return httpx.Client(
            base_url=base_url,
            timeout=timeout,
            verify=ca,
            cert=(cert, key),
        )

    # ------------------------------------------------------------------ guard
    def _require_client(self) -> httpx.Client:
        if self._client is None:
            raise SandboxSpawnError(
                "CUBE controller URL not configured "
                "(set SCHEDULER_CUBE_CONTROLLER_URL); "
                "see docs/design/b-m2-sandbox-runtime-v1.md",
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
        llm_gateway_url: str | None = None,
        llm_virtual_key: str | None = None,
        model_override: str | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        _check_ids(
            run_id=run_id, session_id=session_id,
            tenant_id=tenant_id, user_id=user_id,
        )
        if not foundation_token:
            raise SandboxSpawnError(
                "foundation_token is required; mint it via "
                "identity.issue_service_account_token before spawning",
            )
        if len(task) > _MAX_TASK_LEN:
            task = task[: _MAX_TASK_LEN - 32] + "\n…[truncated by scheduler]"

        client = self._require_client()
        extensions = {
            e.strip()
            for e in (self._settings.runtime_extensions or "").split(",")
            if e.strip()
        }
        gateway_mode = "llm_gateway_enforcer" in extensions
        if gateway_mode and not llm_virtual_key:
            raise SandboxSpawnError(
                "llm_gateway_enforcer is enabled but no tenant LLM virtual key "
                "was provided; refusing direct-connect fallback",
            )
        sandbox_llm_api_key = "" if gateway_mode else self._settings.llm_api_key

        # NOTE: CubeSandbox doesn't have docker-style bind mounts, so the
        # task payload is piped through ``env.AGENT_TASK_JSON`` instead of
        # the ``/workspace/task.json`` file the docker runtime uses.  The
        # ``runtime_worker`` checks env first, then file.  See
        # ``agentscope_ext.runtime_worker.__main__`` for the contract.
        import json
        task_json = {
            "task": task,
            "mcp_servers": [],
            "skills": skills or [],
            "files": files or [],
            "memory": memory or "",
            # Per-message LLM selection (model routing key + reasoning effort).
            "model_override": model_override,
            "reasoning_effort": reasoning_effort,
        }
        env = {
            "LLM_API_KEY": sandbox_llm_api_key,
            "LLM_MODEL": self._settings.llm_model,
            "LLM_PROVIDER": self._settings.llm_provider,
            "LLM_BASE_URL": self._settings.llm_base_url,
            "LLM_GATEWAY_URL": llm_gateway_url or self._settings.llm_gateway_url,
            "FOUNDATION_API_URL": self._settings.foundation_api_url,
            "FOUNDATION_API_TOKEN": foundation_token,
            "FOUNDATION_TENANT_ID": tenant_id,
            "FOUNDATION_USER_ID": user_id,
            "OTEL_EXPORTER_OTLP_ENDPOINT": self._settings.otel_exporter_endpoint,
            "MCP_REGISTRY_URL": self._settings.mcp_registry_url,
            "SKILL_STORE_URL": self._settings.skill_store_url,
            "WEB_FETCH_GATEWAY_URL": self._settings.web_fetch_gateway_url,
            "RUNTIME_EXTENSIONS": self._settings.runtime_extensions,
            "FOUNDATION_SERVICE_SCOPES": SANDBOX_SERVICE_SCOPES,
            "RUN_ID": run_id,
            "AGENT_TASK_JSON": json.dumps(task_json, ensure_ascii=False),
        }
        if llm_virtual_key:
            env["LLM_VIRTUAL_KEY"] = llm_virtual_key

        body: dict[str, Any] = {
            # E2B-compatible fields
            "template_id": self._settings.agent_image,
            "metadata": {
                # CubeSandbox stores these as VM labels; they're how we
                # later filter by tenant on list / find owner on cancel.
                "agent-platform.run-id": run_id,
                "agent-platform.session-id": session_id,
                "agent-platform.tenant-id": tenant_id,
            },
            "env": env,
            # AgentScope worker entry — matches the docker runtime's CMD line.
            "cmd": ["python", "-m", "agentscope_ext.runtime_worker"],
            "timeout_seconds": self._settings.sandbox_timeout,
            "resources": {
                "memory_mb": _parse_memory_mb(self._settings.sandbox_memory_limit),
                "cpu_shares": self._settings.sandbox_cpu_shares,
            },
        }
        try:
            resp = client.post("/sandboxes", json=body)
        except httpx.HTTPError as exc:
            raise SandboxSpawnError(f"CubeSandbox unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise SandboxSpawnError(
                f"CubeSandbox refused spawn (HTTP {resp.status_code})",
            )
        sandbox_id: str = str(resp.json().get("sandbox_id") or "")
        if not sandbox_id:
            raise SandboxSpawnError("CubeSandbox returned no sandbox_id")
        logger.info(
            "cube sandbox spawned",
            extra={
                "sandbox_id": sandbox_id,
                "run_id": run_id,
                "tenant_id": tenant_id,
            },
        )
        return sandbox_id

    # --------------------------------------------------------------- inspect
    def get_run_status(self, container_id: str) -> dict[str, Any]:
        client = self._require_client()
        try:
            resp = client.get(f"/sandboxes/{container_id}")
        except httpx.HTTPError as exc:
            raise ContainerNotFoundError(container_id) from exc
        if resp.status_code == 404:
            return {"status": "NOT_FOUND", "exit_code": None}
        data = resp.json()
        return {
            "status": _E2B_STATUS_MAP.get(data.get("status", ""), "UNKNOWN"),
            "exit_code": data.get("exit_code"),
            "started_at": data.get("started_at"),
            "finished_at": data.get("finished_at"),
        }

    def get_container_tenant(self, container_id: str) -> str:
        client = self._require_client()
        try:
            resp = client.get(f"/sandboxes/{container_id}")
        except httpx.HTTPError as exc:
            raise ContainerNotFoundError(container_id) from exc
        if resp.status_code == 404:
            raise ContainerNotFoundError(container_id)
        tenant = resp.json().get("metadata", {}).get("agent-platform.tenant-id", "")
        if not tenant:
            raise ContainerNotFoundError(container_id)
        return str(tenant)

    # ---------------------------------------------------------------- cancel
    def cancel_run(self, container_id: str) -> None:
        client = self._require_client()
        try:
            resp = client.delete(f"/sandboxes/{container_id}")
        except httpx.HTTPError as exc:
            raise ContainerNotFoundError(container_id) from exc
        if resp.status_code == 404:
            raise ContainerNotFoundError(container_id)
        if resp.status_code >= 400:
            # Best-effort — treat any 4xx other than 404 as "already gone";
            # CubeSandbox returns 409 when killing an already-killed VM.
            logger.warning(
                "cube cancel returned non-404 error; treating as gone",
                extra={"sandbox_id": container_id, "status": resp.status_code},
            )

    # ------------------------------------------------------------------ list
    def list_containers(self, *, tenant_id: str) -> list[dict[str, Any]]:
        if not tenant_id:
            raise ValueError("tenant_id is required for list_containers")
        client = self._require_client()
        # E2B uses metadata-based filtering — pass the label as a query
        # parameter the controller will match.
        resp = client.get(
            "/sandboxes",
            params={"metadata.agent-platform.tenant-id": tenant_id},
        )
        items: list[dict[str, Any]] = []
        for vm in resp.json().get("items", []):
            meta = vm.get("metadata", {})
            if meta.get("agent-platform.tenant-id") != tenant_id:
                continue  # belt + braces
            items.append({
                "container_id": vm.get("sandbox_id", ""),
                "run_id": meta.get("agent-platform.run-id", ""),
                "session_id": meta.get("agent-platform.session-id", ""),
                "tenant_id": tenant_id,
                "status": _E2B_STATUS_MAP.get(vm.get("status", ""), "UNKNOWN"),
                "image": vm.get("template_id", ""),
                "created_at": vm.get("created_at", ""),
            })
        return items


def _parse_memory_mb(limit: str) -> int:
    """Translate ``"2g"`` / ``"512m"`` (docker style) into integer MiB.

    CubeSandbox / E2B expect a raw MiB integer in the ``resources``
    payload; reuse the docker-style string from Settings rather than
    introduce a new config knob.
    """
    limit = limit.strip().lower()
    if limit.endswith("g"):
        return int(float(limit[:-1]) * 1024)
    if limit.endswith("m"):
        return int(limit[:-1])
    # Plain number → assume MiB
    return int(limit)


RUNTIME_REGISTRY["cube"] = CubeSandboxRuntime
