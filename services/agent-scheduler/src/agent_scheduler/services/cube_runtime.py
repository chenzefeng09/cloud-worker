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

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

from agent_scheduler.config import SANDBOX_SERVICE_SCOPES, Settings
from agent_scheduler.errors import ContainerNotFoundError, SandboxSpawnError
from agent_scheduler.services import egress_policy
from agent_scheduler.services.feature_flag import evaluate_flag
from agent_scheduler.services.runtime import RUNTIME_REGISTRY
from agent_scheduler.services.skill_cache import MountedSkill, SkillCache

logger = logging.getLogger(__name__)

_SAFE_ID = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")
_MAX_TASK_LEN = 32 * 1024

# Credentials that must NEVER reach the microVM via the ``env`` dict.  A
# microVM exposes pid-1's environ at ``/proc/1/environ``, readable by any
# same-uid in-VM tool subprocess — so an in-sandbox agent could
# ``cat /proc/1/environ`` and recover every secret we passed as env.  These
# keys are instead delivered out-of-band via a 0600 host file that Cube
# host-mounts to ``/workspace/.secrets.json``; the worker reads it into its
# own (userspace) env and unlinks it before any tool subprocess runs, so the
# bytes never enter the kernel environ.  Routing/metadata (URLs, tenant/user/
# run IDs, model, egress mode, scopes) stay in ``env``.
#
# KEEP IN SYNC with ``docker_runtime._SECRET_ENV_KEYS`` and the worker's
# ``runtime_worker.__main__._SECRET_FILE_KEYS`` (same 7 keys).  We duplicate
# rather than import from ``docker_runtime`` on purpose: that module imports
# the ``docker`` SDK at top level, and the Cube (microVM) path must not take a
# hard dependency on Docker being installed.  The Cube env only ever sets a
# subset of these (LLM_API_KEY / FOUNDATION_API_TOKEN, and conditionally
# TOOL_BROKER_LEASE / LLM_VIRTUAL_KEY); ``pop`` over the full set is safe since
# absent keys are simply skipped.
_SECRET_ENV_KEYS: frozenset[str] = frozenset(
    {
        "FOUNDATION_API_TOKEN",
        "TOOL_BROKER_LEASE",
        "LLM_VIRTUAL_KEY",
        "LLM_API_KEY",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        "REDIS_URL",
    }
)

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
        else:
            self._client = self._make_client(url)
        # Lazy — keeps tests that bypass __init__ from constructing a client.
        self._skill_cache: SkillCache | None = None

    def _get_skill_cache(self) -> SkillCache:
        if self._skill_cache is None:
            self._skill_cache = SkillCache(
                cache_base=self._settings.skills_cache_base,
                skill_store_url=self._settings.skill_store_url,
                service_scopes=self._settings.skill_store_service_scopes,
            )
        return self._skill_cache

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
        memory_limit: str | None = None,
        history: list[dict[str, Any]] | None = None,
        llm_gateway_url: str | None = None,
        llm_virtual_key: str | None = None,
        model_override: str | None = None,
        reasoning_effort: str | None = None,
        sys_prompt: str | None = None,
        sampling_params: dict[str, Any] | None = None,
        hitl: bool = True,
        resume: dict[str, Any] | None = None,
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

        # CubeVS egress allowlist (broker mode).  Parse the operator-provided
        # CIDR list now so the fail-closed guard runs alongside the other
        # deployment-config checks above — before any network call.  An empty
        # list in broker mode is a deployment error: ``allow_internet_access``
        # is forced false below, so an empty ``allow_out`` air-gaps the sandbox
        # and it can never reach the tool-broker (every run would fail with a
        # confusing connect error).  We refuse at spawn instead, mirroring how
        # ``_require_client`` refuses without a controller URL.
        allow_out_cidrs = [
            cidr.strip()
            for cidr in self._settings.cube_sandbox_allow_out.split(",")
            if cidr.strip()
        ]
        if (
            self._settings.sandbox_egress_mode == "broker"
            and not allow_out_cidrs
        ):
            raise SandboxSpawnError(
                "broker egress mode requires a CubeVS allow_out CIDR list but "
                "SCHEDULER_CUBE_SANDBOX_ALLOW_OUT is empty; an empty allowlist "
                "with allow_internet_access=false air-gaps the sandbox from the "
                "tool-broker. Set it to the tool-broker + cluster-DNS "
                "ClusterIP/32s (see deploy/cube/README.md).",
            )
        try:
            service_env = egress_policy.service_env(
                self._settings,
                llm_gateway_url=llm_gateway_url,
            )
            egress_metadata = egress_policy.metadata(self._settings)
            tool_broker_lease = egress_policy.mint_tool_broker_lease(
                self._settings,
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=run_id,
                ttl_seconds=self._settings.sandbox_timeout,
            )
        except ValueError as exc:
            raise SandboxSpawnError(str(exc)) from exc

        # The task payload is piped through ``env.AGENT_TASK_JSON``
        # instead of a workspace file because CubeSandbox sandboxes are
        # microVMs without a shared workspace bind by default; the
        # ``runtime_worker`` checks env first, then the file path used
        # by the docker runtime.  See
        # ``agentscope_ext.runtime_worker.__main__`` for the contract.
        #
        # Skill blobs DO get bind-mounted from the scheduler host into
        # the VM via ``metadata.host-mount`` (Cube convention; see
        # CubeSandbox/examples/host-mount) — populated below from the
        # skill_cache materialise step.
        task_json = {
            "task": task,
            "mcp_servers": [],
            "skills": skills or [],
            "files": files or [],
            "memory": memory or "",
            "history": list(history) if history else [],
            # Per-message LLM selection (model routing key + reasoning effort).
            "model_override": model_override,
            "reasoning_effort": reasoning_effort,
            # App-harness fields (empty for ordinary chat runs).
            "sys_prompt": sys_prompt or "",
            "sampling_params": sampling_params or {},
            # HITL gate + resume payload (see docker_runtime._write_task_file).
            "hitl": bool(hitl),
            "resume": resume,
        }
        env = {
            "LLM_API_KEY": sandbox_llm_api_key,
            "LLM_MODEL": self._settings.llm_model,
            "LLM_PROVIDER": self._settings.llm_provider,
            "LLM_BASE_URL": self._settings.llm_base_url,
            "LLM_GATEWAY_URL": service_env["LLM_GATEWAY_URL"],
            "FOUNDATION_API_URL": self._settings.foundation_api_url,
            "FOUNDATION_API_TOKEN": foundation_token,
            "FOUNDATION_TENANT_ID": tenant_id,
            "FOUNDATION_USER_ID": user_id,
            "OTEL_EXPORTER_OTLP_ENDPOINT": self._settings.otel_exporter_endpoint,
            "SANDBOX_EGRESS_MODE": service_env["SANDBOX_EGRESS_MODE"],
            "TOOL_BROKER_URL": service_env["TOOL_BROKER_URL"],
            "MCP_REGISTRY_URL": service_env["MCP_REGISTRY_URL"],
            "SKILL_STORE_URL": service_env["SKILL_STORE_URL"],
            "WEB_FETCH_GATEWAY_URL": service_env["WEB_FETCH_GATEWAY_URL"],
            "DLP_URL": service_env["DLP_URL"],
            "RUNTIME_EXTENSIONS": self._settings.runtime_extensions,
            "FOUNDATION_SERVICE_SCOPES": SANDBOX_SERVICE_SCOPES,
            "RUN_ID": run_id,
            "AGENT_TASK_JSON": json.dumps(task_json, ensure_ascii=False),
            # NB: ``egress_policy.service_env`` is the single source of truth
            # for infra creds.  The Cube microVM path deliberately injects NO
            # direct Redis URL and NO S3 endpoint/keys: prod Cube runs in broker
            # egress, so the worker reaches hot-state and object-store through
            # the tool-broker /state and /s3 façades (which hold the real
            # creds).  Do not add S3_*/REDIS_URL here — that would hand a
            # microVM the standing MinIO root keys this hardening removes.
        }
        if tool_broker_lease:
            env["TOOL_BROKER_LEASE"] = tool_broker_lease
        if llm_virtual_key:
            env["LLM_VIRTUAL_KEY"] = llm_virtual_key

        # Security: move the credential subset of ``env`` OUT of the VM env and
        # deliver it via a 0600 host file that Cube host-mounts at
        # ``/workspace/.secrets.json`` (the worker reads it into its own env and
        # unlinks it before any tool subprocess runs).  Passing these through
        # the VM ``env`` would leak them via the kernel ``/proc/1/environ``
        # interface to any same-uid in-VM tool subprocess.  ``pop`` preserves
        # the conditional semantics above: a key lands in the file only if it
        # was present in ``env`` (so an absent TOOL_BROKER_LEASE / LLM_VIRTUAL_KEY
        # is absent here too), while LLM_API_KEY — always present, possibly ""
        # under the gateway enforcer — is carried verbatim.  See
        # ``docker_runtime`` (same pattern) and the worker's
        # ``_load_secret_file_into_env`` / ``_SECRET_FILE_KEYS``.
        secret_file_payload = {
            k: env.pop(k) for k in _SECRET_ENV_KEYS if k in env
        }
        # The Cube path has no shared ``/workspace`` bind by default, so create a
        # small per-run host dir (mirroring how docker_runtime derives its
        # workspace dir from ``workspace_base``) to hold the secrets file, then
        # host-mount that file into the VM at the worker's expected path.
        secrets_host_dir = (
            Path(self._settings.workspace_base) / tenant_id / session_id / run_id
        )
        secrets_host_path = secrets_host_dir / ".secrets.json"
        try:
            secrets_host_dir.mkdir(parents=True, exist_ok=True)
            secrets_host_path.write_text(
                json.dumps(secret_file_payload, ensure_ascii=False),
                encoding="utf-8",
            )
            os.chmod(secrets_host_path, 0o600)
        except OSError as exc:
            raise SandboxSpawnError(
                f"Failed to write sandbox secrets file at {secrets_host_path}: {exc}"
            ) from exc

        # Materialise skills onto the scheduler host and let CubeSandbox
        # host-mount them into the VM at /home/skills/<name>.  Failure
        # to materialise yields an empty list — sandbox spawn proceeds
        # without that skill rather than failing the run.
        mounted_skills: list[MountedSkill] = []
        try:
            mounted_skills = self._get_skill_cache().materialize(
                list(skills or []),
                tenant_id=tenant_id,
                user_id=user_id,
            )
        except Exception as exc:  # noqa: BLE001 -- failure-open
            logger.warning(
                "skill cache materialise raised; continuing without mounts",
                extra={"run_id": run_id, "err": str(exc)},
            )

        metadata: dict[str, Any] = {
            # CubeSandbox stores these as VM labels; they're how we
            # later filter by tenant on list / find owner on cancel.
            "agent-platform.run-id": run_id,
            "agent-platform.session-id": session_id,
            "agent-platform.tenant-id": tenant_id,
            **egress_metadata,
        }
        # Host-mount entries (Cube convention: a JSON array of
        # ``{hostPath, mountPath, readOnly}``).  Always includes the per-run
        # secrets file at the worker's expected path ``/workspace/.secrets.json``
        # (read-only — the worker reads-then-unlinks it inside the VM's own
        # writable copy), plus any materialised skills at /home/skills/<name>.
        host_mounts: list[dict[str, Any]] = [
            {
                "hostPath": str(secrets_host_path),
                "mountPath": "/workspace/.secrets.json",
                "readOnly": True,
            }
        ]
        host_mounts.extend(
            {
                "hostPath": ms.host_path,
                "mountPath": f"/home/skills/{ms.name}",
                "readOnly": True,
            }
            for ms in mounted_skills
        )
        metadata["host-mount"] = json.dumps(host_mounts)

        # Phase 5: pick the worker image per-run via feature flag.  Failure-open
        # (inside ``evaluate_flag``) falls back to the AgentScope template, so
        # the agentscope path stays byte-for-byte unchanged.  Unlike docker we
        # do NOT rely on an empty cmd for the Claude SDK image: CubeSandbox's
        # behaviour with an empty cmd is unspecified, so we pass the equivalent
        # of the image ENTRYPOINT explicitly.
        use_claude_sdk = evaluate_flag(
            "enable_claude_sdk_worker",
            tenant_id=tenant_id,
            user_id=user_id,
            flag_url=self._settings.feature_flag_url,
            timeout_ms=self._settings.feature_flag_timeout_ms,
        )
        # Global config fallback: when the per-run flag is False/unreachable, a
        # deployment can still route every run to the Claude SDK worker via
        # ``SCHEDULER_USE_CLAUDE_SDK_WORKER_DEFAULT``.  The flag wins when it
        # returns True; the default only applies on the False branch.  Committed
        # default is False, so this is a no-op for existing behaviour.
        if not use_claude_sdk:
            use_claude_sdk = self._settings.use_claude_sdk_worker_default
        selected_image = (
            self._settings.claude_sdk_image
            if use_claude_sdk
            else self._settings.agent_image
        )
        selected_cmd = (
            ["node", "/app/dist/main.js"]
            if use_claude_sdk
            else ["python", "-m", "agentscope_ext.runtime_worker"]
        )
        logger.info(
            "worker image selected",
            extra={
                "run_id": run_id,
                "tenant_id": tenant_id,
                "image": selected_image,
                "claude_sdk": use_claude_sdk,
            },
        )

        body: dict[str, Any] = {
            # E2B-compatible fields
            "template_id": selected_image,
            "metadata": metadata,
            "env": env,
            # AgentScope worker entry — matches the docker runtime's CMD line.
            # Claude SDK path uses the image's node ENTRYPOINT explicitly.
            "cmd": selected_cmd,
            "timeout_seconds": self._settings.sandbox_timeout,
            "resources": {
                "memory_mb": _parse_memory_mb(
                    memory_limit or self._settings.sandbox_memory_limit
                ),
                "cpu_shares": self._settings.sandbox_cpu_shares,
            },
        }
        # ----------------------------------------------------------------- net
        # Per-microVM egress isolation via CubeVS (CubeSandbox's eBPF virtual
        # switch), configured at sandbox-create time.
        #
        # STATE OF ENFORCEMENT (read before "fixing" this):
        #
        # 1. The body now carries the REAL CubeVS create shape — the same one
        #    Cubelet translates into a CubeVS ``MVMOptions`` struct:
        #      * ``allow_internet_access=False`` (top-level)  -> drop all public
        #        egress (default-deny);
        #      * ``network={"allow_out": [CIDR, ...]}`` (nested) -> the ONLY
        #        destinations CubeVS forwards.
        #    A CubeSandbox controller that implements CubeVS therefore honours
        #    this directly — no invented label-only hint.  The ``allow_out``
        #    CIDRs come from ``cube_sandbox_allow_out`` (operator-provided: the
        #    tool-broker + cluster-DNS ClusterIP/32s; CubeVS keys on destination
        #    CIDR, not a label, so the scheduler cannot derive them from
        #    ``TOOL_BROKER_URL`` and they must be configured — see
        #    deploy/cube/README.md).  Parsed + fail-closed above.
        #
        # 2. Belt-and-suspenders, also enforced by URL rewriting: in broker mode
        #    ``egress_policy.service_env`` already rewrote every capability URL
        #    the worker sees (LLM/MCP/skills/web-fetch/state/S3) to the
        #    tool-broker façade and blanked the direct Redis URL + MinIO keys, so
        #    the worker has no *configured* path to infra even before CubeVS.
        #
        # 3. The ``agent-platform.network-policy`` label below still matters: it
        #    drives the host-nftables fallback + any CubeMaster/Cubelet admission
        #    glue in ``deploy/cube/``.  That fallback is NOT dead weight — CubeVS
        #    hardwires a drop of RFC-1918 / link-local ranges (10/8, 127/8,
        #    169.254/16, 172.16/12, 192.168/16) regardless of ``allow_out``, so
        #    if the broker's ClusterIP falls in one of those ranges the CubeVS
        #    allowlist alone air-gaps the sandbox and the nftables fallback (no
        #    such guard) is required.  See deploy/cube/README.md +
        #    docs/design/b-m2-sandbox-runtime-v1.md.
        egress_mode = service_env["SANDBOX_EGRESS_MODE"]
        if egress_mode == "broker":
            metadata["agent-platform.network-policy"] = "sandbox-broker-only"
            # CubeVS: default-deny public egress, forward only the operator's
            # allowlist (tool-broker + DNS).  ``allow_out_cidrs`` is guaranteed
            # non-empty here by the fail-closed guard above.
            body["allow_internet_access"] = False
            body["network"] = {"allow_out": allow_out_cidrs}
        elif egress_mode == "none":
            metadata["agent-platform.network-policy"] = "deny-all"
            # Total air-gap is the intended semantics of ``none`` mode: deny
            # internet AND forward nothing.  Empty allow_out is correct here, so
            # we do NOT fail closed.
            body["allow_internet_access"] = False
            body["network"] = {"allow_out": []}
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
