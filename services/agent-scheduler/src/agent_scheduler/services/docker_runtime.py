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
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import docker
from docker.types import Mount, Ulimit

from agent_scheduler.config import (
    _DEV_LIKE_ENVS,
    SANDBOX_SERVICE_SCOPES,
    Settings,
)
from agent_scheduler.errors import ContainerNotFoundError, SandboxSpawnError
from agent_scheduler.services import artifact_store, egress_policy
from agent_scheduler.services.feature_flag import (
    evaluate_flag,
    runtime_extensions_for_run,
)
from agent_scheduler.services.runtime import RUNTIME_REGISTRY
from agent_scheduler.services.skill_cache import MountedSkill, SkillCache

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


# FIX B4 — kernel hardening applied to EVERY sandbox ``containers.run(...)``.
# The same kwargs are duplicated in ``sandbox_pool.docker_runtime`` (a separate
# service that cannot import this module); keep the two copies identical.
#
# * ``no-new-privileges`` neuters setuid binaries so a compromised worker can't
#   re-escalate via e.g. a stray ``sudo``/``ping`` even if one slips into the
#   image.
# * ``cap_drop=["ALL"]`` — the worker is a non-root python/node process that
#   needs no Linux capabilities; dropping them all removes the usual container
#   escape primitives (CAP_SYS_ADMIN, CAP_NET_RAW, …).  If a test ever reveals a
#   genuinely required cap, add back the MINIMAL set here with a comment.
# * ``pids_limit`` caps the process table inside the sandbox so a uid-1000 fork
#   bomb can't exhaust the host's pid space; 512 is generous for the worker.
# * ``ulimits`` (nofile) bounds open file descriptors so the sandbox cannot
#   exhaust the daemon's fd table.
#
# TODO(security): read_only rootfs + tmpfs — the worker writes outside
# /workspace (npm/pip caches, /tmp) so a read-only rootfs needs separate tmpfs
# wiring; left for a follow-up pass.  Network/mem/cpu limits are handled by the
# egress policy + per-run sizing at the call site, not here.
_SANDBOX_PIDS_LIMIT = 512
_SANDBOX_NOFILE_SOFT = 4096
_SANDBOX_NOFILE_HARD = 8192


def _kernel_hardening_kwargs() -> dict[str, Any]:
    """Return the shared ``containers.run`` kernel-hardening kwargs (FIX B4)."""
    return {
        "security_opt": ["no-new-privileges:true"],
        "cap_drop": ["ALL"],
        "pids_limit": _SANDBOX_PIDS_LIMIT,
        "ulimits": [
            Ulimit(
                name="nofile",
                soft=_SANDBOX_NOFILE_SOFT,
                hard=_SANDBOX_NOFILE_HARD,
            )
        ],
    }

# Credentials that must NEVER reach the container via ``environment=``.  The
# kernel exposes a process' full environ at ``/proc/1/environ``, which is
# world-readable to any code running inside the sandbox — so an in-sandbox
# agent could ``cat /proc/1/environ`` and recover every secret we passed as
# env, defeating the worker's env-scrub defense.  These keys are instead
# delivered out-of-band via a 0600 file on the ``/workspace`` bind mount
# (see ``.secrets.json`` below), which the worker reads into its own env and
# unlinks before any tool subprocess runs.  Routing/metadata (URLs, tenant/
# user/run IDs, model, egress mode, scopes, bucket/region) stay in env.
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
        # Lazy: built on first spawn so test fixtures that bypass __init__
        # (see tests/conftest.py) don't try to construct an httpx.Client.
        self._skill_cache: SkillCache | None = None

    def _get_skill_cache(self) -> SkillCache:
        if self._skill_cache is None:
            self._skill_cache = SkillCache(
                cache_base=self._settings.skills_cache_base,
                skill_store_url=self._settings.skill_store_url,
                service_scopes=self._settings.skill_store_service_scopes,
            )
        return self._skill_cache

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
        mcp_server_ids: list[str] | None = None,
        files: list[dict[str, Any]] | None = None,
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
            mcp_server_ids: optional per-run MCP server selection, carried with
                PRESENCE semantics.  ``None`` -> the ``mcp_server_ids`` key is
                OMITTED from task.json and the in-sandbox ``mcp_tenant_injector``
                keeps injecting every enabled tenant server; ``[]`` -> the key is
                written as ``[]``, meaning no MCP servers for this run; a
                non-empty list -> only those server ids.  ``None`` is never
                serialized as ``null``.
            files: optional list of ``{"key": s3key, "filename": name, "mime": mime}``
                dicts describing user-uploaded attachments.  Written to task.json
                ``files`` field; the runtime_worker downloads them to
                ``/workspace/inputs/`` before starting the agent.
            llm_gateway_url: M1-P3 — LLM gateway base URL (with API prefix).
                Injected as ``LLM_GATEWAY_URL``.  Defaults to
                ``settings.llm_gateway_url`` when omitted.  The sandbox
                ignores this env if ``llm_gateway_enforcer`` is not active.
            llm_virtual_key: tenant virtual key from the gateway.  Required
                when ``llm_gateway_enforcer`` is active; omitted only for an
                explicit local direct-connect configuration.
            user_clearance: the run's effective clearance (``min(user,
                workspace)``), forwarded by the orchestrator.  Injected into
                the sandbox env as ``FOUNDATION_USER_CLEARANCE`` so the
                ``knowledge_tool`` extension can send ``X-User-Clearance`` to
                knowledge-ingest (foundation mode) and signed into the broker
                lease so the broker re-injects it for the ki façade (broker
                mode).  Not secret -> plain env, like tenant/user id.
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
            # Pass through UNCHANGED (no ``or []``): ``None`` and ``[]`` are
            # different instructions and the writer distinguishes them.
            mcp_server_ids=mcp_server_ids,
            files=files or [],
            memory=memory or "",
            history=history or [],
            model_override=model_override,
            reasoning_effort=reasoning_effort,
            sys_prompt=sys_prompt,
            sampling_params=sampling_params,
            hitl=hitl,
            resume=resume,
            workspace_writeback=bool(workspace_id),
        )

        # When the ``llm_gateway_enforcer`` extension is active, the sandbox
        # routes all LLM traffic through foundation llm-gateway using a
        # per-tenant virtual key.  The real upstream provider key lives inside
        # the gateway and must not be exposed to sandbox code.
        _extensions = {
            e.strip()
            for e in (self._settings.runtime_extensions or "").split(",")
            if e.strip()
        }
        _gateway_mode = "llm_gateway_enforcer" in _extensions
        if _gateway_mode and not llm_virtual_key:
            raise SandboxSpawnError(
                "llm_gateway_enforcer is enabled but no tenant LLM virtual key "
                "was provided; refusing direct-connect fallback",
            )
        _sandbox_llm_api_key = "" if _gateway_mode else self._settings.llm_api_key
        try:
            service_env = egress_policy.service_env(
                self._settings,
                llm_gateway_url=llm_gateway_url,
            )
            network_kwargs = egress_policy.docker_run_network_kwargs(self._settings)
            egress_labels = egress_policy.metadata(self._settings)
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

        # Canvas v2 (Phase B): append ``canvas_tool`` to RUNTIME_EXTENSIONS when
        # the per-run ``enable_canvas_tool`` flag is on.  Failure-open (handled
        # inside ``evaluate_flag``): a down/slow flag service leaves the base
        # extensions unchanged, so the flag-off path is byte-for-byte identical
        # to pre-canvas behaviour.
        runtime_extensions = runtime_extensions_for_run(
            base_extensions=self._settings.runtime_extensions,
            tenant_id=tenant_id,
            user_id=user_id,
            flag_url=self._settings.feature_flag_url,
            timeout_ms=self._settings.feature_flag_timeout_ms,
        )

        # NOTE: this dict is assembled with every value (including secrets)
        # for clarity, but the ``_SECRET_ENV_KEYS`` subset is popped out below
        # (after the conditional appends) and delivered via
        # ``/workspace/.secrets.json`` instead of ``environment=`` — see the
        # comment on ``_SECRET_ENV_KEYS``.  Only routing/metadata reaches the
        # container env.
        env_vars = {
            "LLM_API_KEY": _sandbox_llm_api_key,
            "LLM_MODEL": self._settings.llm_model,
            # Provider selector + base URL are forwarded so the in-sandbox
            # worker can talk to OpenAI-compatible endpoints (DeepSeek, etc.)
            # without bundling provider-specific knowledge in the image.
            # The worker treats the empty string as "fall back to default".
            "LLM_PROVIDER": self._settings.llm_provider,
            "LLM_BASE_URL": self._settings.llm_base_url,
            # LLM gateway env vars.  The enforcer is enabled by default and
            # refuses to build a model if either URL or virtual key is absent.
            "LLM_GATEWAY_URL": service_env["LLM_GATEWAY_URL"],
            "WORKSPACE_BASE": str(workspace_dir),
            "FOUNDATION_API_URL": self._settings.foundation_api_url,
            "FOUNDATION_API_TOKEN": foundation_token,
            "FOUNDATION_TENANT_ID": tenant_id,
            "FOUNDATION_USER_ID": user_id,
            # Effective clearance (min(user, workspace)) for the run.  The
            # ``knowledge_tool`` extension sends this as X-User-Clearance to
            # knowledge-ingest so ki enforces the visibility gate; in broker
            # mode the same value is signed into the lease and the broker
            # re-injects it (trusted) for the ki façade.  Not secret -> plain
            # env, consistent with FOUNDATION_TENANT_ID / FOUNDATION_USER_ID.
            "FOUNDATION_USER_CLEARANCE": user_clearance,
            "OTEL_EXPORTER_OTLP_ENDPOINT": self._settings.otel_exporter_endpoint,
            "SANDBOX_EGRESS_MODE": service_env["SANDBOX_EGRESS_MODE"],
            "TOOL_BROKER_URL": service_env["TOOL_BROKER_URL"],
            # B-M3: service URLs for the in-sandbox extensions.
            # mcp_tenant_injector reads MCP_REGISTRY_URL;
            # skills_loader reads SKILL_STORE_URL.
            # RUNTIME_EXTENSIONS activates them in declaration order.
            "MCP_REGISTRY_URL": service_env["MCP_REGISTRY_URL"],
            "SKILL_STORE_URL": service_env["SKILL_STORE_URL"],
            "RUNTIME_EXTENSIONS": runtime_extensions,
            # Dev-mode shim: in prod the API gateway translates the verified
            # Bearer JWT into X-Service-Scopes before requests reach internal
            # endpoints.  In this deployment (no gateway between sandbox and
            # these services) the scheduler injects the scopes directly so the
            # mcp_tenant_injector / skills_loader extensions can set the header.
            # TODO: remove once the gateway enforces scope injection in prod.
            "FOUNDATION_SERVICE_SCOPES": SANDBOX_SERVICE_SCOPES,
            # S3/MinIO credentials — consumed by runtime_worker to download
            # user-uploaded attachments listed in task.json ``files``.  ALL of
            # these come from ``service_env`` (not ``self._settings``) so the
            # egress mode is the single source of truth: in broker mode the
            # access/secret keys are blanked (the broker /s3 façade holds the
            # real MinIO creds) while bucket/region stay populated.
            "S3_ENDPOINT_URL": service_env["S3_ENDPOINT_URL"],
            "S3_BUCKET": service_env["S3_BUCKET"],
            "S3_REGION": service_env["S3_REGION"],
            "S3_ACCESS_KEY_ID": service_env["S3_ACCESS_KEY_ID"],
            "S3_SECRET_ACCESS_KEY": service_env["S3_SECRET_ACCESS_KEY"],
            # Hot-state layer: stable run identifier + Redis URL so the
            # in-sandbox worker can write step/plan/cursor checkpoints.
            "RUN_ID": run_id,
            "REDIS_URL": service_env["REDIS_URL"],
            # Web-fetch gateway — the web_fetch extension reads this to
            # POST fetch requests to the gateway sidecar.  The gateway is
            # either reached directly (foundation mode) or through tool-broker.
            "WEB_FETCH_GATEWAY_URL": service_env["WEB_FETCH_GATEWAY_URL"],
            # Foundation DLP — the worker POSTs tool-call content to
            # ${DLP_URL}/core/scan for scanning.  Reached directly (foundation
            # mode) or through the tool-broker /dlp façade (broker mode).
            "DLP_URL": service_env["DLP_URL"],
            # Knowledge-ingest (KB v1) - the knowledge_tool extension GETs
            # ${KNOWLEDGE_INGEST_URL}/search?scope=knowledge.  Reached directly
            # (foundation mode) or through the tool-broker /knowledge/v1
            # facade (broker mode, which re-injects X-User-Clearance from the
            # signed lease).  Blank in none mode -> tool self-gates off.
            "KNOWLEDGE_INGEST_URL": service_env["KNOWLEDGE_INGEST_URL"],
        }
        if tool_broker_lease:
            env_vars["TOOL_BROKER_LEASE"] = tool_broker_lease

        # M1-P3: inject LLM_VIRTUAL_KEY only when present (omit rather than
        # blank so the enforcer can reliably detect "no key configured").
        if llm_virtual_key:
            env_vars["LLM_VIRTUAL_KEY"] = llm_virtual_key

        # Security: the credential subset of ``env_vars`` is intentionally
        # moved OUT of the container environment here and delivered via
        # ``/workspace/.secrets.json`` (0600, read-then-unlinked by the
        # worker).  Passing them as ``environment=`` would leak them through
        # the kernel ``/proc/1/environ`` interface to any in-sandbox code.
        # ``pop`` preserves the existing conditional semantics: a key is only
        # written to the file if it was present in ``env_vars`` (so falsy
        # LLM_VIRTUAL_KEY / TOOL_BROKER_LEASE are absent here too), while
        # LLM_API_KEY — which is always present, possibly "" under the
        # gateway enforcer — is carried through verbatim.
        secret_file_payload = {
            k: env_vars.pop(k) for k in _SECRET_ENV_KEYS if k in env_vars
        }
        secrets_path = workspace_dir / ".secrets.json"
        try:
            secrets_path.write_text(
                json.dumps(secret_file_payload, ensure_ascii=False),
                encoding="utf-8",
            )
            os.chmod(secrets_path, 0o600)
        except OSError as exc:
            raise SandboxSpawnError(
                f"Failed to write sandbox secrets file at {secrets_path}: {exc}"
            ) from exc

        # Stage-2 block B: materialise the run's skills into the
        # content-addressed host cache and prepare read-only bind mounts
        # at /home/skills/<name>.  Failure-open: any cache error is
        # logged and yields an empty mount list — the sandbox simply
        # sees no skills, never failing the spawn over a missing one.
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

        mounts = [
            Mount(
                target="/workspace",
                source=str(workspace_dir),
                type="bind",
            ),
        ]
        for ms in mounted_skills:
            mounts.append(
                Mount(
                    target=f"/home/skills/{ms.name}",
                    source=ms.host_path,
                    type="bind",
                    read_only=True,
                )
            )

        # Phase 5: pick the worker image per-run via feature flag.  Failure-open
        # (handled inside ``evaluate_flag``) means a down/slow flag service
        # falls back to the AgentScope image, so the agentscope path here is
        # byte-for-byte unchanged from before this feature landed.
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
        # ``command=None`` lets docker use the image ENTRYPOINT (the Claude SDK
        # image's node entry).  AgentScope keeps its explicit argv unchanged.
        selected_cmd = (
            None
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

        # FIX B5/B6 — fail-closed spawn-time egress invariants, checked here
        # where we hold ``self.client``, before we ever start the container:
        #   B6: ``foundation`` egress is prod-unsafe unless explicitly opted in.
        #   B5: broker/none mode MUST land on an internal-only docker network so
        #       a compromised worker has no direct route off-box.
        self._assert_foundation_egress_allowed()
        self._assert_isolated_network(network_kwargs)

        try:
            container = self.client.containers.run(
                image=selected_image,
                # AgentScope worker entrypoint — see agentscope-ext/README.md.
                # Argv-only invocation; the task itself lives in workspace/task.json.
                # Claude SDK path uses command=None to run the image ENTRYPOINT.
                command=selected_cmd,
                environment=env_vars,
                mounts=mounts,
                **network_kwargs,
                # FIX B4 — kernel hardening (no-new-privileges, cap_drop=ALL,
                # pids_limit, nofile ulimit).  See ``_kernel_hardening_kwargs``.
                **_kernel_hardening_kwargs(),
                mem_limit=memory_limit or self._settings.sandbox_memory_limit,
                cpu_shares=self._settings.sandbox_cpu_shares,
                detach=True,
                labels={
                    "agent-platform.run-id": run_id,
                    "agent-platform.session-id": session_id,
                    "agent-platform.tenant-id": tenant_id,
                    **({"agent-platform.workspace-id": workspace_id} if workspace_id else {}),
                    **egress_labels,
                },
                remove=False,
            )
        except docker.errors.ImageNotFound as exc:
            raise SandboxSpawnError(
                f"Sandbox image not found: {selected_image}. "
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
        return str(container.id)

    # ----------------------------------------------------------- egress guard
    def _assert_foundation_egress_allowed(self) -> None:
        """FIX B6 — gate prod-unsafe ``foundation`` egress behind an opt-in.

        ``foundation`` mode puts the sandbox directly on the shared foundation
        network (un-brokered reach to internal services / the internet).  That
        is intentional for dev/test/local, but in any other environment it is a
        "silent open" risk, so it must be explicitly enabled via
        ``allow_insecure_foundation_egress``.  Broker/none modes are unaffected.
        """
        mode = str(
            getattr(self._settings, "sandbox_egress_mode", "foundation")
            or "foundation"
        )
        if mode != "foundation":
            return
        env = str(getattr(self._settings, "env", "") or "")
        if env in _DEV_LIKE_ENVS:
            return
        if getattr(self._settings, "allow_insecure_foundation_egress", False):
            return
        raise SandboxSpawnError(
            f"sandbox_egress_mode 'foundation' is unsafe in env {env!r}; it "
            "puts the sandbox directly on the foundation network. Set "
            "SCHEDULER_ALLOW_INSECURE_FOUNDATION_EGRESS=true to opt in, or use "
            "'broker' egress (the default). Refusing to spawn."
        )

    def _assert_isolated_network(self, network_kwargs: dict[str, Any]) -> None:
        """FIX B5/B6 — refuse to spawn onto a non-internal egress network.

        ``egress_policy.docker_run_network_kwargs`` chooses a named docker
        network for broker (and, in principle, none) mode, but cannot verify
        the network is actually isolated because it has no docker client.  We
        do that here, fail-closed: a named broker/none network MUST be
        ``Internal == True`` (no default gateway → no direct off-box egress),
        otherwise a misconfigured network would silently give the sandbox open
        internet access.

        ``foundation`` mode is exempt — it deliberately joins the shared
        foundation network for legacy/dev direct service access.

        Tolerance for test fakes: when the docker client can't inspect the
        network (a minimal fake without ``.networks.get(...).attrs``), we skip
        the assertion rather than break.  A real client always supports it, so
        the invariant holds in every deployment.
        """
        mode = str(
            getattr(self._settings, "sandbox_egress_mode", "foundation")
            or "foundation"
        )
        if mode == "foundation":
            return
        network = network_kwargs.get("network")
        if not network:
            # ``none`` mode uses ``network_disabled=True`` (no named network) —
            # already isolated, nothing to verify.
            return
        try:
            net = self.client.networks.get(network)
            is_internal = bool(net.attrs["Internal"])
        except AttributeError:
            # Minimal fake docker client without a ``networks`` collection /
            # ``.attrs`` — can't verify, so don't enforce (real clients always
            # support this).
            return
        except docker.errors.NotFound:
            # Network doesn't exist yet; let ``containers.run`` surface the
            # real error rather than masking it with a confusing message.
            return
        except (KeyError, docker.errors.APIError):
            # Malformed inspect payload or transient API error — don't block
            # the spawn on an inspect hiccup (the network choice itself is
            # already pinned by the egress policy).
            return
        if not is_internal:
            raise SandboxSpawnError(
                f"egress mode {mode!r} requires network {network!r} to be "
                "internal:true, refusing to spawn"
            )

    # --------------------------------------------------------------- inspect
    def get_run_status(self, container_id: str) -> dict[str, Any]:
        try:
            c = self.client.containers.get(container_id)
        except docker.errors.NotFound:
            return {"status": "NOT_FOUND", "exit_code": None}

        status = _CONTAINER_STATUS_MAP.get(c.status, "UNKNOWN")
        result: dict[str, Any] = {
            "status": status,
            "exit_code": c.attrs["State"].get("ExitCode"),
            "started_at": c.attrs["State"].get("StartedAt"),
            "finished_at": c.attrs["State"].get("FinishedAt"),
        }
        if status == "COMPLETED" and result["exit_code"] != 0:
            result["status"] = "FAILED"

        # Read result.json for both terminal AND running states.
        # For RUNNING runs we only extract plan_steps (written continuously
        # by the worker after each ToolCallEndEvent) so the orchestrator's
        # SSE poll can relay real-time plan progress to the frontend.
        # For terminal states we additionally pull summary/error/status/usage.
        # Best-effort: missing or malformed file returns empty plan_steps.
        labels = c.labels or {}
        run_id = labels.get("agent-platform.run-id", "")
        session_id = labels.get("agent-platform.session-id", "")
        tenant_id = labels.get("agent-platform.tenant-id", "")

        workspace = None
        if run_id and session_id and tenant_id:
            workspace = (
                Path(self._settings.workspace_base)
                / tenant_id
                / session_id
                / run_id
            )

        # Always try to read plan_steps (available during RUNNING too).
        result["plan_steps"] = []
        if workspace is not None:
            result_path = workspace / "result.json"
            if result_path.is_file():
                try:
                    import json as _json

                    payload = _json.loads(result_path.read_text(encoding="utf-8"))
                    result["plan_steps"] = payload.get("plan_steps") or []
                except (OSError, ValueError):
                    pass

        if status in ("COMPLETED", "FAILED") and workspace is not None:
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
                result["usage"] = payload.get("usage")
                result["plan_steps"] = payload.get("plan_steps") or []
                # If the worker reported FAILED via result.json (exit 0
                # with status="FAILED"), promote that over the docker
                # exit-code-based status so callers see the real reason.
                if payload.get("status") == "FAILED" and result["status"] == "COMPLETED":
                    result["status"] = "FAILED"

            # Unified output-file scan: one pass uploads all files to S3 and
            # returns two lists — workspace_files (for workspace persistence)
            # and artifacts (outputs/ subset, for chat output-file cards).
            workspace_id = labels.get("agent-platform.workspace-id", "")
            output = artifact_store.upload_run_output_files(
                workspace=workspace,
                tenant_id=tenant_id,
                session_id=session_id,
                run_id=run_id,
                workspace_id=workspace_id,
                bucket=self._settings.s3_bucket,
                s3_client=self._make_s3_client(),
                excluded_names=self._EXCLUDED_NAMES,
            )
            result["artifacts"] = output["artifacts"]
            if workspace_id:
                result["writeback_files"] = output["workspace_files"]
        return result

    _EXCLUDED_NAMES: frozenset[str] = artifact_store.EXCLUDED_NAMES

    def _make_s3_client(self) -> Any | None:
        """Build a boto3 S3 client using S3_* settings (not AWS_* env vars)."""
        return artifact_store.make_s3_client(self._settings)

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
        return str(tenant)

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

    # ------------------------------------------------------------------ reap
    def reap_expired(self, *, max_age_seconds: int) -> int:
        """Docker containers are reaped by their own lifecycle; no-op here."""
        return 0

    # ------------------------------------------------------------------ list
    def list_containers(self, *, tenant_id: str) -> list[dict[str, Any]]:
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
        mcp_server_ids: list[str] | None = None,
        files: list[dict[str, Any]] | None = None,
        memory: str | None = None,
        history: list[dict[str, Any]] | None = None,
        model_override: str | None = None,
        reasoning_effort: str | None = None,
        sys_prompt: str | None = None,
        sampling_params: dict[str, Any] | None = None,
        hitl: bool = True,
        resume: dict[str, Any] | None = None,
        workspace_writeback: bool = False,
    ) -> Path:
        """Write the task payload ``runtime_worker`` will read.

        Schema (pinned by ``agentscope_ext.runtime_worker.__main__``):

            {
              "task": str,
              "mcp_servers": [],   # always empty — mcp_tenant_injector fetches live
              "mcp_server_ids": [str, ...]  # OPTIONAL key — see below
              "skills": [str, ...] # skill names; skills_loader fetches manifests
              "files": [{"key": s3key, "filename": name, "mime": mime}, ...]
              "memory": str        # pre-fetched memory snippets (may be empty)
              "history": [...]     # prior-turn conversation the worker restores
                                   # into AgentState.context for multi-turn memory;
                                   # items: {"role","type",...} incl. tool calls
              "sys_prompt": str    # App-harness system prompt the worker appends
                                   # to identity/soul/user prompts (empty for chat)
              "sampling_params": {} # App-harness per-run LLM sampling overrides
                                   # (e.g. temperature/top_p/max_tokens); empty for
                                   # chat runs; applied by the gateway model builder
            }

        ``mcp_servers`` stays empty because the in-sandbox ``mcp_tenant_injector``
        extension fetches the enabled server list directly from ``mcp-registry``
        at runtime using ``MCP_REGISTRY_URL`` + ``FOUNDATION_TENANT_ID``.

        ``mcp_server_ids`` is the per-run narrowing of that live fetch, and it
        is the one key in this payload whose PRESENCE is load-bearing:

        * key ABSENT  — the run expressed no MCP choice; the injector keeps its
          historical behaviour and injects every enabled tenant server.
        * key ``[]``  — the run explicitly disabled MCP; the injector must
          inject nothing.
        * non-empty   — the injector keeps only the listed server ids.

        A ``None`` selection therefore omits the key rather than writing
        ``null``; ``json.dumps`` would otherwise erase the distinction between
        "no choice" and "disable everything".

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
            "history": list(history) if history else [],
            # Per-message LLM selection: model routing key + reasoning effort.
            # The gateway model builder reads these; both may be None (defaults).
            "model_override": model_override,
            "reasoning_effort": reasoning_effort,
            # App-harness fields (empty for ordinary chat runs).  The worker
            # appends ``sys_prompt`` to the system prompt and applies
            # ``sampling_params`` per-run in the gateway model builder.
            "sys_prompt": sys_prompt or "",
            "sampling_params": sampling_params or {},
            # HITL: tool-confirmation gate (False for scheduled/background
            # runs) and the resume payload for continuing a PAUSED run
            # ({"kind","tool_call_id","confirmed","answer","agent_state"}).
            "hitl": bool(hitl),
            "resume": resume,
            "workspace_writeback": bool(workspace_writeback),
        }
        # Presence-carried: only write the key when the run made a choice.
        # ``is not None`` — NOT truthiness — because ``[]`` ("disable all MCP")
        # is a real selection that must reach the sandbox.
        if mcp_server_ids is not None:
            payload["mcp_server_ids"] = [str(s) for s in mcp_server_ids]
        task_path = workspace_dir / "task.json"
        task_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return task_path


# Register as the ``docker`` runtime kind.  This is the dev/CI default
# (CONTRIBUTING contributors don't need KVM); production overrides via
# ``SCHEDULER_RUNTIME=cube``.  See ADR 0001.
RUNTIME_REGISTRY["docker"] = DockerRuntime
