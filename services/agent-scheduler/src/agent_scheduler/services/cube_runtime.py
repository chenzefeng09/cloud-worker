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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from agent_scheduler.config import SANDBOX_SERVICE_SCOPES, Settings
from agent_scheduler.errors import ContainerNotFoundError, SandboxSpawnError
from agent_scheduler.services import artifact_store, egress_policy
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
# ``SECRET_FILE_KEYS`` in ``worker-claude-sdk/src/main.ts``.  We duplicate
# rather than import from ``docker_runtime`` on purpose: that module imports
# the ``docker`` SDK at top level, and the Cube (microVM) path must not take a
# hard dependency on Docker being installed.  The Cube env only ever sets a
# subset of these (FOUNDATION_API_TOKEN, and conditionally
# TOOL_BROKER_LEASE / LLM_VIRTUAL_KEY); ``pop`` over the full set is safe since
# absent keys are simply skipped.
_SECRET_ENV_KEYS: frozenset[str] = frozenset(
    {
        "FOUNDATION_API_TOKEN",
        "TOOL_BROKER_LEASE",
        "LLM_VIRTUAL_KEY",
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


def _sandbox_records(payload: Any) -> list[dict[str, Any]]:
    """Normalize Cube list responses across controller versions."""
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = payload.get("items", [])
    else:
        return []
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)]


# Map a worker-facing gateway's *internal* container port to the host-published
# port that the cube microVM uses through its configured reachable host.
# The scheduler config holds container-name URLs (e.g. http://tool-broker:8024
# /..., http://llm-gateway:8011/...) whose host is unresolvable inside a microVM;
# we keep the path/query intact but swap host -> {reachable_host}:{host_port}.
#   * tool-broker:  container 8024  -> host 8024
#   * llm-gateway:  container 8011  -> host 8021
_CONTAINER_PORT_TO_HOST_PORT: dict[int, int] = {
    8024: 8024,  # tool-broker
    8011: 8021,  # llm-gateway
}
# Default host port when the source URL has no explicit port (rare; keeps the
# rewrite total rather than producing a portless public URL).
_DEFAULT_HOST_PORT: int = 8024


# The reachable host must be a bare IPv4/IPv6 or DNS hostname, never an HTML error
# page or a URL. We validate before use because the value is interpolated into
# a CubeVS ``allowOut`` CIDR (``{host}/32``); a bad value (e.g. an upstream
# proxy's HTML error body returned by an IP-echo probe) would otherwise be sent
# as a CIDR and the cube-api rejects the whole spawn (error 130547).
_PUBLIC_HOST_RE = re.compile(
    r"^(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9A-Fa-f:]+|[A-Za-z0-9.\-]{1,253})$"
)


def _valid_public_host(value: str) -> bool:
    value = (value or "").strip()
    if not value or len(value) > 253:
        return False
    return bool(_PUBLIC_HOST_RE.match(value))


def _detect_public_host(settings: Settings) -> str:
    """Resolve the host the cube microVM uses to reach host-published gateways.

    The setting name is retained for compatibility and may contain a private
    node IP. Order: explicit ``cube_public_host`` wins; otherwise probe a public
    echo endpoint, then fall back to the controller URL host. A private node IP
    should be pinned when the cloud's public NAT does not support hairpinning.
    Every candidate is validated by ``_valid_public_host`` before it is accepted
    — a probe that returns an HTML error page (common when the scheduler's own
    egress is brokered/blocked) is rejected, never used as a CIDR target.
    """
    pinned = (getattr(settings, "cube_public_host", "") or "").strip()
    if pinned:
        if _valid_public_host(pinned):
            return pinned
        logger.warning(
            "cube_public_host is set but not a valid IP/hostname; ignoring",
            extra={"public_host": pinned[:64]},
        )
    for url in ("http://ifconfig.me", "https://api.ipify.org"):
        try:
            ip = httpx.get(url, timeout=5.0).text.strip()
        except httpx.HTTPError as exc:  # noqa: PERF203
            logger.warning(
                "cube public-host probe failed",
                extra={"probe": url, "err": str(exc)},
            )
            continue
        if _valid_public_host(ip):
            logger.info(
                "cube public host auto-detected", extra={"public_host": ip}
            )
            return ip
        logger.warning(
            "cube public-host probe returned a non-IP body; ignoring",
            extra={"probe": url, "body_prefix": ip[:64]},
        )
    controller = getattr(settings, "cube_controller_url", "") or ""
    host = urlsplit(controller).hostname or ""
    if host and _valid_public_host(host):
        logger.warning(
            "cube public host falling back to controller host; "
            "set SCHEDULER_CUBE_PUBLIC_HOST to a VM-reachable node IP",
            extra={"public_host": host},
        )
        return host
    return ""


def _rewrite_to_public(url: str, public_host: str) -> str:
    """Rewrite a container-name gateway URL's host -> {public_host}:{host_port}.

    Keeps scheme/path/query/fragment intact; maps the source container port to
    its host-published port (see ``_CONTAINER_PORT_TO_HOST_PORT``). Returns the
    input unchanged when it is empty, public_host is empty, or the URL has no
    host (so a misconfigured/blank URL never becomes a half-rewritten one).
    """
    if not url or not public_host:
        return url
    parts = urlsplit(url)
    if not parts.hostname:
        return url
    host_port = _CONTAINER_PORT_TO_HOST_PORT.get(
        parts.port or -1, parts.port or _DEFAULT_HOST_PORT
    )
    netloc = f"{public_host}:{host_port}"
    return urlunsplit(
        (parts.scheme or "http", netloc, parts.path, parts.query, parts.fragment)
    )


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
        # VM-reachable host used for this machine's published gateways. The
        # attribute name is retained for compatibility and may hold a private
        # node IP. Resolved once, then reused for allowOut + URL rewrites.
        self._public_host: str = _detect_public_host(settings)

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
        # CubeSandbox's E2B-compatible API authenticates every request with the
        # ``X-API-Key`` header (imprinted from a live SDK capture).
        headers = {"X-API-Key": self._settings.cube_api_key}

        if mode == "off":
            return httpx.Client(base_url=base_url, timeout=timeout, headers=headers)

        ca = self._settings.cube_ca_cert
        if not ca:
            raise RuntimeError(
                f"cube_tls_mode={mode!r} requires cube_ca_cert "
                "(SCHEDULER_CUBE_CA_CERT); refusing to build CubeSandbox client",
            )

        if mode == "tls":
            return httpx.Client(
                base_url=base_url, timeout=timeout, verify=ca, headers=headers
            )

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
            headers=headers,
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
        mcp_server_ids: list[str] | None = None,
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
        user_clearance: str = "",
        workspace_id: str = "",
    ) -> str:
        _check_ids(
            run_id=run_id, session_id=session_id,
            tenant_id=tenant_id, user_id=user_id,
        )
        self._last_workspace_id = workspace_id
        if not foundation_token:
            raise SandboxSpawnError(
                "foundation_token is required; mint it via "
                "identity.issue_service_account_token before spawning",
            )
        if len(task) > _MAX_TASK_LEN:
            task = task[: _MAX_TASK_LEN - 32] + "\n…[truncated by scheduler]"

        client = self._require_client()
        if not llm_virtual_key:
            raise SandboxSpawnError(
                "no tenant LLM virtual key was provided; refusing to spawn a "
                "worker without gateway credentials",
            )

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
                "tool-broker. Set it to the reachable node IP/32 or the "
                "tool-broker + cluster-DNS ClusterIP/32s (see "
                "deploy/cube/README.md).",
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
                clearance=user_clearance,
            )
        except ValueError as exc:
            raise SandboxSpawnError(str(exc)) from exc

        # The worker receives the task through ``env.AGENT_TASK_JSON`` when it
        # is launched via envd below. Do NOT also put this value in Cube's
        # create-time ``envVars``: Cube v0.6 caps an individual create-time env
        # value at roughly 4 KiB, while legitimate user prompts can be larger.
        # Create only boots envd; the worker does not start until
        # ``_exec_start_worker`` passes the complete env to ``subprocess.Popen``.
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
            "workspace_writeback": bool(workspace_id),
        }
        # Per-run MCP selection, presence-carried exactly as in the docker
        # runtime: key absent = tenant default set, [] = disable all, list =
        # only those ids.  ``is not None`` guard, never truthiness.
        if mcp_server_ids is not None:
            task_json["mcp_server_ids"] = [str(s) for s in mcp_server_ids]
        # Cube-specific URL rewrite: container names are unresolvable inside the
        # microVM, so the worker reaches host gateways through the configured
        # node IP/host. CubeSandbox v0.6 can explicitly allow a private address,
        # which is preferred when public NAT lacks hairpinning. Take gateway URLs
        # from ``service_env`` when set (broker mode), else from the raw settings
        # (none/foundation mode blanks them), then rewrite the host. Only the
        # two URLs the worker dials directly for LLM calls are rewritten:
        #   * TOOL_BROKER_URL  (façade for LLM/MCP/state/s3/etc. in broker mode)
        #   * LLM_GATEWAY_URL  (the worker's OpenAI-compatible base)
        # All other service_env URLs (MCP/SKILL/WEBFETCH/DLP) are passed through
        # unchanged: in broker mode they nest under the rewritten TOOL_BROKER_URL
        # host already; in none mode they are blank by design.
        raw_llm_gateway = (
            service_env["LLM_GATEWAY_URL"]
            or (llm_gateway_url or self._settings.llm_gateway_url)
        )
        raw_tool_broker = (
            service_env["TOOL_BROKER_URL"] or self._settings.tool_broker_url
        )
        public_llm_gateway = _rewrite_to_public(raw_llm_gateway, self._public_host)
        public_tool_broker = _rewrite_to_public(raw_tool_broker, self._public_host)
        env = {
            "LLM_MODEL": self._settings.llm_model,
            "LLM_GATEWAY_URL": public_llm_gateway,
            "FOUNDATION_API_URL": self._settings.foundation_api_url,
            "FOUNDATION_API_TOKEN": foundation_token,
            "FOUNDATION_TENANT_ID": tenant_id,
            "FOUNDATION_USER_ID": user_id,
            # Effective clearance (min(user, workspace)); mirrors the docker
            # runtime so the worker's knowledge tool / the broker façade enforce the same
            # visibility gate under cube.
            "FOUNDATION_USER_CLEARANCE": user_clearance,
            "OTEL_EXPORTER_OTLP_ENDPOINT": self._settings.otel_exporter_endpoint,
            "SANDBOX_EGRESS_MODE": service_env["SANDBOX_EGRESS_MODE"],
            "TOOL_BROKER_URL": public_tool_broker,
            "MCP_REGISTRY_URL": service_env["MCP_REGISTRY_URL"],
            "SKILL_STORE_URL": service_env["SKILL_STORE_URL"],
            "WEB_FETCH_GATEWAY_URL": service_env["WEB_FETCH_GATEWAY_URL"],
            "DLP_URL": service_env["DLP_URL"],
            "FOUNDATION_SERVICE_SCOPES": SANDBOX_SERVICE_SCOPES,
            "RUN_ID": run_id,
            # The Claude Agent SDK CLI tries to phone home (Statsig / BigQuery 1P
            # event logging) on a cadence; in the air-gapped cube microVM those
            # POSTs fail and spew errors. Disable all telemetry so they don't
            # compete with the model traffic or add noise.
            "CLAUDE_CODE_ENABLE_TELEMETRY": "0",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
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
        # is absent here too).  See
        # ``docker_runtime`` (same pattern) and the worker's
        # ``loadSecretFileIntoEnv`` / ``SECRET_FILE_KEYS`` (worker-claude-sdk).
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
        # ``{hostPath, mountPath, readOnly}``).  CubeMaster's prepareHostDirVolume
        # only accepts **directories** as mount sources (a single-file hostPath
        # fails with error 130545 "not a directory" — verified against the live
        # cube-api).  So we host-mount the per-run secrets *directory* at
        # ``/workspace``; the worker then reads ``/workspace/.secrets.json`` from
        # it.  Plus any materialised skills at /home/skills/<name>.
        host_mounts: list[dict[str, Any]] = [
            {
                "hostPath": str(secrets_host_dir),
                "mountPath": "/workspace",
                # Writable: the worker reads ``/workspace/.secrets.json`` then
                # unlinks it, and also writes its result/scratch under /workspace.
                "readOnly": False,
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

        # CubeSandbox's behaviour with an empty cmd is unspecified, so the
        # worker's image ENTRYPOINT is passed explicitly (``selected_cmd``).
        selected_image = self._settings.agent_image
        selected_template = selected_image
        memory_tier = ""
        if memory_limit:
            try:
                memory_tier = (
                    "large"
                    if _parse_memory_mb(memory_limit)
                    >= _parse_memory_mb(
                        self._settings.sandbox_memory_limit_large
                    )
                    else "small"
                )
            except ValueError:
                logger.warning(
                    "invalid cube memory limit; using the default template",
                    extra={"run_id": run_id, "memory_limit": memory_limit},
                )
            else:
                tier_template = (
                    self._settings.cube_claude_sdk_template_large
                    if memory_tier == "large"
                    else self._settings.cube_claude_sdk_template_small
                )
                if tier_template:
                    selected_template = tier_template
                    metadata["agent-platform.memory-tier"] = memory_tier
        selected_cmd = ["node", "/app/dist/main.js"]
        logger.info(
            "worker image selected",
            extra={
                "run_id": run_id,
                "tenant_id": tenant_id,
                "image": selected_image,
                "template": selected_template,
                "memory_limit": memory_limit,
                "memory_tier": memory_tier,
            },
        )

        # CubeSandbox v0.6.0 E2B-create contract: camelCase ``templateID`` /
        # ``envVars``, timeout in seconds, and the nested E2B ``lifecycle``
        # object. Top-level ``autoPause`` is not a create option in v0.6 and is
        # silently ignored. ``cmd`` and ``resources`` are NOT part of the create
        # contract — the worker entry (``selected_cmd``) is launched via envd
        # exec after the sandbox is up (phase-4 wiring), not at create.
        # ``AGENT_TASK_JSON`` is deliberately launch-only. Keeping it out of
        # create-time envVars avoids Cube's per-value size limit without
        # changing the worker contract or truncating user input.
        create_env = {k: v for k, v in env.items() if k != "AGENT_TASK_JSON"}
        body: dict[str, Any] = {
            "templateID": selected_template,
            "metadata": metadata,
            "envVars": create_env,
            "timeout": self._settings.sandbox_timeout,
            "lifecycle": {
                "onTimeout": "pause",
                "autoResume": True,
            },
            "secure": True,
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
        #      * ``network={"allowOut": [CIDR, ...]}`` (nested) -> the ONLY
        #        destinations CubeVS forwards.
        #    A CubeSandbox controller that implements CubeVS therefore honours
        #    this directly — no invented label-only hint. Additional CIDRs come
        #    from ``cube_sandbox_allow_out`` (operator-provided reachable node,
        #    tool-broker, or cluster-DNS addresses). CubeVS keys on destination
        #    CIDR, so the scheduler cannot derive every deployment target from
        #    ``TOOL_BROKER_URL``. Parsed + fail-closed above.
        #
        # 2. Belt-and-suspenders, also enforced by URL rewriting: in broker mode
        #    ``egress_policy.service_env`` already rewrote every capability URL
        #    the worker sees (LLM/MCP/skills/web-fetch/state/S3) to the
        #    tool-broker façade and blanked the direct Redis URL + MinIO keys, so
        #    the worker has no *configured* path to infra even before CubeVS.
        #
        # 3. CubeSandbox v0.6 honours explicitly listed private CIDRs. This is
        #    important on clouds whose public NAT does not provide hairpinning:
        #    the private node IP can be both the rewritten gateway host and an
        #    ``allowOut`` destination. The network-policy label remains useful
        #    to host-side admission/fallback glue documented in deploy/cube/.
        # CUBE NETWORK FORM (verified end-to-end against the live cube-api):
        # the microVM reaches host gateways through ``_public_host`` (a legacy
        # name that may hold a private node IP), so inject ``{host}/32`` into
        # CubeVS ``allowOut`` regardless of egress_mode.
        # ``allow_internet_access=false`` keeps default-deny; ``allowOut`` (NOTE:
        # camelCase, CIDR form — L7 domain rules break exec, verified) is the
        # forward allowlist. In broker mode we also forward the operator's
        # configured CIDRs; in none mode the reachable host is the only entry.
        egress_mode = service_env["SANDBOX_EGRESS_MODE"]
        allow_out: list[str] = []
        if self._public_host:
            allow_out.append(f"{self._public_host}/32")
        else:
            logger.warning(
                "cube spawn without a reachable host: the microVM will have no "
                "egress path to the host gateways (set SCHEDULER_CUBE_PUBLIC_HOST)",
                extra={"run_id": run_id},
            )
        if egress_mode == "broker":
            metadata["agent-platform.network-policy"] = "sandbox-broker-only"
            # Also forward the operator's node/broker/DNS allowlist.
            allow_out.extend(c for c in allow_out_cidrs if c not in allow_out)
        elif egress_mode == "none":
            metadata["agent-platform.network-policy"] = "deny-all"
        else:  # foundation
            metadata["agent-platform.network-policy"] = "foundation"
        body["allow_internet_access"] = False
        body["network"] = {"allowOut": allow_out}
        try:
            resp = client.post("/sandboxes", json=body)
        except httpx.HTTPError as exc:
            raise SandboxSpawnError(f"CubeSandbox unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise SandboxSpawnError(
                f"CubeSandbox refused spawn (HTTP {resp.status_code}): "
                f"{resp.text[:500]}",
            )
        sandbox_id: str = str(resp.json().get("sandboxID") or "")
        if not sandbox_id:
            raise SandboxSpawnError("CubeSandbox returned no sandboxID")
        logger.info(
            "cube sandbox spawned",
            extra={
                "sandbox_id": sandbox_id,
                "run_id": run_id,
                "tenant_id": tenant_id,
            },
        )
        # Phase 7: launch the worker inside the now-running microVM via the envd
        # ``/execute`` endpoint. Create only boots the VM (envd) — it does NOT
        # run ``selected_cmd``. We POST a tiny Python bootstrap that
        # ``subprocess.Popen``-spawns the worker DETACHED (non-blocking) so the
        # exec returns immediately while the worker streams for tens of seconds
        # in the background. env is passed explicitly to Popen (carries
        # AGENT_TASK_JSON + gateway URLs); secrets arrive via the host-mounted
        # /workspace/.secrets.json the worker reads itself.
        self._exec_start_worker(sandbox_id, selected_cmd, env, run_id=run_id)
        return sandbox_id

    # --------------------------------------------------------------- exec start
    def _exec_start_worker(
        self,
        sandbox_id: str,
        cmd: list[str],
        env: dict[str, str],
        *,
        run_id: str,
    ) -> None:
        """Start the worker process inside the microVM via envd ``/execute``.

        The envd HTTP surface is published per-sandbox at
        ``https://49999-{sandboxID}.cube.app/execute`` (reached from the
        scheduler container through the cube-proxy on host :443). It runs the
        Python in ``body.code`` and streams ndjson. We keep that Python trivial:
        ``subprocess.Popen`` the worker detached and exit, so ``/execute``
        returns at once and the worker keeps running after the stream closes.

        Failure here is fatal to the run (the VM is up but idle), so we raise
        ``SandboxSpawnError`` — the caller already created the sandbox, but a
        run with no worker is worse than a clean spawn failure.
        """
        # cwd=/workspace is load-bearing, not cosmetic: /workspace is the only
        # path host-mounted back to this host, so it is the only place an agent
        # can write a file we can publish as a run artifact. envd's default cwd
        # is the image's /home/user, where output files die with the microVM.
        bootstrap = (
            "import subprocess, os\n"
            f"env = dict(os.environ)\n"
            f"env.update({env!r})\n"
            "logf = open('/workspace/worker.log', 'wb')\n"
            f"p = subprocess.Popen({cmd!r}, env=env, cwd='/workspace', "
            "stdout=logf, stderr=subprocess.STDOUT, "
            "start_new_session=True)\n"
            "print('worker_started', p.pid)\n"
        )
        exec_url = f"https://49999-{sandbox_id}.cube.app/execute"
        headers = {"X-API-Key": self._settings.cube_api_key}
        try:
            # verify=False: the per-sandbox envd cert is issued by Cube's
            # internal mkcert CA (not in the scheduler trust store). The exec
            # surface is reached over the cube-proxy on the host, not the public
            # internet, so TLS-trust is not the boundary here.
            with httpx.Client(verify=False, timeout=30.0) as exec_client:  # noqa: S501
                resp = exec_client.post(
                    exec_url, json={"code": bootstrap}, headers=headers
                )
        except httpx.HTTPError as exc:
            raise SandboxSpawnError(
                f"cube envd exec unreachable for {sandbox_id}: {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise SandboxSpawnError(
                f"cube envd exec refused (HTTP {resp.status_code}) for "
                f"{sandbox_id}: {resp.text[:500]}",
            )
        logger.info(
            "cube worker exec started",
            extra={
                "sandbox_id": sandbox_id,
                "run_id": run_id,
                "exec_status": resp.status_code,
            },
        )

    # --------------------------------------------------------------- inspect
    def _resolve_sandbox(self, container_id: str) -> dict[str, Any] | None:
        """Return the controller's sandbox record, or ``None`` if not found.

        The cube controller's ``GET /sandboxes/{id}`` only matches the *full*
        32-char sandboxID, but the orchestrator polls with the short 12-char
        container id (the prefix the scheduler returned as ``sandbox_id``). When
        the direct GET 404s we fall back to listing all sandboxes and matching by
        ``sandboxID`` prefix, so both id forms resolve. The list record also
        carries the ``metadata`` labels (tenant/session/run) we need to locate
        the host-mounted ``result.json``.
        """
        client = self._require_client()
        resp = client.get(f"/sandboxes/{container_id}")
        if resp.status_code == 200:
            return resp.json()  # type: ignore[no-any-return]
        if resp.status_code != 404:
            return None
        # 404 on the direct path: fall back to a prefix match over the list.
        try:
            list_resp = client.get("/sandboxes")
        except httpx.HTTPError:
            return None
        if list_resp.status_code != 200:
            return None
        for vm in _sandbox_records(list_resp.json()):
            sid = str(vm.get("sandboxID") or vm.get("sandbox_id") or "")
            if sid == container_id or sid.startswith(container_id):
                return vm  # type: ignore[no-any-return]
        return None

    @staticmethod
    def _run_ids(sandbox: dict[str, Any]) -> tuple[str, str, str] | None:
        """Return ``(tenant_id, session_id, run_id)`` from the VM's metadata.

        ``None`` when any of the three is missing — an externally created VM,
        or one whose metadata the controller no longer carries.
        """
        meta = sandbox.get("metadata") or {}
        tenant_id = meta.get("agent-platform.tenant-id", "")
        session_id = meta.get("agent-platform.session-id", "")
        run_id = meta.get("agent-platform.run-id", "")
        if not (tenant_id and session_id and run_id):
            return None
        return tenant_id, session_id, run_id

    def _run_workspace(self, ids: tuple[str, str, str]) -> Path:
        """Host path of the per-run dir that is host-mounted at ``/workspace``."""
        tenant_id, session_id, run_id = ids
        return Path(self._settings.workspace_base) / tenant_id / session_id / run_id

    def _upload_output_files(
        self, sandbox: dict[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        """Unified output-file scan from the host side of the mount."""
        ids = self._run_ids(sandbox)
        if ids is None:
            return {"workspace_files": [], "artifacts": []}
        tenant_id, session_id, run_id = ids
        workspace_id = getattr(self, "_last_workspace_id", "") or ""
        return artifact_store.upload_run_output_files(
            workspace=self._run_workspace(ids),
            tenant_id=tenant_id,
            session_id=session_id,
            run_id=run_id,
            workspace_id=workspace_id,
            bucket=self._settings.s3_bucket,
            s3_client=artifact_store.make_s3_client(self._settings),
        )

    def _read_result_json(self, sandbox: dict[str, Any]) -> dict[str, Any] | None:
        """Read the worker's ``result.json`` for a run.

        Key fact (verified on the live stack): the cube spawn host-mounts the
        per-run host dir ``{workspace_base}/{tenant}/{session}/{run}`` into the
        microVM at ``/workspace``, so the worker's ``/workspace/result.json``
        lands back on the *scheduler host* at that same path — exactly like the
        docker runtime's bind mount. We therefore read the host file directly
        (robust even after the microVM is destroyed) rather than exec-ing into
        the VM, which would fail precisely when the VM is gone.

        Returns the parsed payload dict, or ``None`` when the file is absent
        (worker still running, or it crashed before writing) / unreadable /
        unparseable. Best-effort: never raises.
        """
        ids = self._run_ids(sandbox)
        if ids is None:
            return None
        result_path = self._run_workspace(ids) / "result.json"
        if not result_path.is_file():
            return None
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.info(
                "cube result.json present but unreadable/unparseable; ignoring",
                extra={"run_id": ids[2]},
            )
            return None
        return payload if isinstance(payload, dict) else None

    def get_run_status(self, container_id: str) -> dict[str, Any]:
        try:
            sandbox = self._resolve_sandbox(container_id)
        except httpx.HTTPError as exc:
            raise ContainerNotFoundError(container_id) from exc
        if sandbox is None:
            # Controller has no record of the VM (reaped). We cannot map the
            # sandbox id back to a run_id without the controller metadata, so we
            # cannot locate the host result.json here — report NOT_FOUND. The
            # orchestrator treats this as terminal-ish and stops polling. The VM
            # must therefore outlive the worker long enough to be polled at least
            # once after result.json is written (governed by the spawn
            # ``timeout``); see the note in ``spawn_run``.
            return {"status": "NOT_FOUND", "exit_code": None}
        data = sandbox
        # The controller reports VM lifecycle under ``state`` (e.g. "running");
        # older shapes used ``status``. Honour either; result.json (below) is the
        # authoritative source for the *worker* outcome and overrides this.
        raw_state = data.get("state") or data.get("status") or ""
        result: dict[str, Any] = {
            "status": _E2B_STATUS_MAP.get(raw_state, "RUNNING"),
            "exit_code": data.get("exit_code"),
            "started_at": data.get("startedAt") or data.get("started_at"),
            "finished_at": data.get("endAt") or data.get("finished_at"),
            # Default: no plan_steps / artifacts unless result.json supplies them.
            "plan_steps": [],
        }

        # Result-collection parity with docker_runtime.get_run_status: read the
        # worker's result.json (from the host bind-mount; see _read_result_json)
        # and map its fields onto the dict the orchestrator's
        # ``_reconcile_run_completion`` expects (result_summary / result_error /
        # result_status / usage / plan_steps).
        #
        # While the worker is mid-run result.json does not exist yet →
        # ``_read_result_json`` returns None → we leave status RUNNING so the
        # orchestrator keeps polling.
        payload = self._read_result_json(data)
        if payload is not None:
            worker_status = payload.get("status")
            result["result_summary"] = payload.get("summary")
            result["result_error"] = payload.get("error")
            # Normalise to the orchestrator vocabulary. The worker writes
            # OK / FAILED / RUNNING / paused (see worker-claude-sdk/src/result.ts).
            # "paused" is passed through verbatim — the orchestrator keys on the
            # literal string "paused" for HITL detection. Anything else maps to
            # COMPLETED/FAILED below.
            result["result_status"] = worker_status
            result["usage"] = payload.get("usage")
            result["plan_steps"] = payload.get("plan_steps") or []

            # Promote run status from the worker's authoritative result.json.
            # The E2B sandbox status reflects the VM ("running" until killed),
            # NOT the worker outcome, so result.json is the source of truth for
            # terminal state — mirroring docker_runtime promoting result.json's
            # FAILED over the container exit code.
            if worker_status == "OK":
                result["status"] = "COMPLETED"
            elif worker_status == "FAILED":
                result["status"] = "FAILED"
            elif worker_status == "paused":
                # Report COMPLETED so the orchestrator enters its terminal
                # branch, where ``result_status == "paused"`` flips it to PAUSED
                # (HITL). Matches the docker path: container exits 0 → COMPLETED,
                # result.json status=paused → PAUSED.
                result["status"] = "COMPLETED"
            # worker_status == "RUNNING" (or anything unexpected): leave the
            # E2B-derived status untouched so polling continues.

            # Artifacts: the per-run host dir is host-mounted into the VM at
            # /workspace, so whatever the agent wrote is already on this host —
            # the same situation the docker runtime uploads from, and readable
            # even after the microVM is gone. Only publish on a terminal status:
            # polling calls this repeatedly, and a mid-run upload would ship
            # half-written files (and re-ship them on every poll).
            if result["status"] in ("COMPLETED", "FAILED"):
                output = self._upload_output_files(data)
                result["artifacts"] = output["artifacts"]
                workspace_id = getattr(self, "_last_workspace_id", "") or ""
                if workspace_id:
                    result["writeback_files"] = output["workspace_files"]

        return result

    def get_container_tenant(self, container_id: str) -> str:
        try:
            sandbox = self._resolve_sandbox(container_id)
        except httpx.HTTPError as exc:
            raise ContainerNotFoundError(container_id) from exc
        if sandbox is None:
            raise ContainerNotFoundError(container_id)
        tenant = sandbox.get("metadata", {}).get("agent-platform.tenant-id", "")
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

    # ------------------------------------------------------------------ reap
    def reap_expired(self, *, max_age_seconds: int) -> int:
        """Delete agent-platform cube sandboxes older than ``max_age_seconds``.

        CubeSandbox v0.6 normally enforces the spawn-time idle ``timeout`` via
        cube-lifecycle-manager and pauses the VM. This is a last-resort safety
        net for controller/CLM failures: a periodic background sweep (see
        ``main.lifespan``) lists ALL sandboxes across tenants and deletes the
        ones we spawned whose ``startedAt`` is older than ``max_age_seconds``
        (set comfortably above the run cap so it never kills a live run).

        Fail-safe by construction:

        * a sandbox with a missing/unparseable ``startedAt`` is SKIPPED (we never
          delete a VM whose age we can't establish);
        * a sandbox without an ``agent-platform.*`` metadata marker is SKIPPED
          (we never touch a foreign VM);
        * per-sandbox HTTP errors are caught and logged so one failure doesn't
          abort the whole sweep;
        * 204 / 404 / 409 on the DELETE are all treated as success (already gone).

        Returns the number of sandboxes deleted.
        """
        client = self._require_client()
        # NO params: list ALL sandboxes across tenants (the leak is host-wide).
        resp = client.get("/sandboxes")
        now = datetime.now(UTC)
        deleted = 0
        for vm in _sandbox_records(resp.json()):
            meta = vm.get("metadata") or {}
            # Only reap VMs WE spawned: require an agent-platform marker.
            if not (
                meta.get("agent-platform.run-id")
                or meta.get("agent-platform.tenant-id")
            ):
                continue
            sandbox_id = str(vm.get("sandboxID") or vm.get("sandbox_id") or "")
            if not sandbox_id:
                continue
            started_raw = vm.get("startedAt") or vm.get("started_at")
            if not started_raw:
                continue  # cannot establish age -> fail safe, skip
            try:
                # ISO8601 may end in ``Z``; fromisoformat needs +00:00.
                started = datetime.fromisoformat(
                    str(started_raw).replace("Z", "+00:00")
                )
            except ValueError:
                continue  # unparseable -> fail safe, skip
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            age = (now - started).total_seconds()
            if age <= max_age_seconds:
                continue
            try:
                del_resp = client.delete(f"/sandboxes/{sandbox_id}")
            except httpx.HTTPError as exc:
                logger.warning(
                    "cube reaper: delete failed; continuing sweep",
                    extra={"sandbox_id": sandbox_id, "err": str(exc)},
                )
                continue
            # 204 / 404 / 409 all mean "gone" — count as a successful reap.
            if del_resp.status_code in (204, 404, 409) or (
                200 <= del_resp.status_code < 300
            ):
                deleted += 1
                logger.info(
                    "cube reaper: deleted expired sandbox",
                    extra={"sandbox_id": sandbox_id, "age_seconds": int(age)},
                )
            else:
                logger.warning(
                    "cube reaper: delete returned unexpected status; skipping",
                    extra={
                        "sandbox_id": sandbox_id,
                        "status": del_resp.status_code,
                    },
                )
        return deleted

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
        for vm in _sandbox_records(resp.json()):
            meta = vm.get("metadata") or {}
            if meta.get("agent-platform.tenant-id") != tenant_id:
                continue  # belt + braces
            items.append({
                "container_id": vm.get("sandboxID") or vm.get("sandbox_id", ""),
                "run_id": meta.get("agent-platform.run-id", ""),
                "session_id": meta.get("agent-platform.session-id", ""),
                "tenant_id": tenant_id,
                "status": _E2B_STATUS_MAP.get(
                    vm.get("state") or vm.get("status", ""),
                    "UNKNOWN",
                ),
                "image": vm.get("templateID") or vm.get("template_id", ""),
                "created_at": vm.get("startedAt") or vm.get("created_at", ""),
            })
        return items


def _parse_memory_mb(limit: str) -> int:
    """Translate a Docker-style memory limit into MiB for tier selection."""
    limit = limit.strip().lower()
    if limit.endswith("g"):
        return int(float(limit[:-1]) * 1024)
    if limit.endswith("m"):
        return int(limit[:-1])
    # Plain number → assume MiB
    return int(limit)


RUNTIME_REGISTRY["cube"] = CubeSandboxRuntime
