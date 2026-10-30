"""CubeSandbox-backed pool runtime.

Implements the same narrow interface as :class:`PoolDockerRuntime` but calls
the CubeSandbox controller (E2B-compatible HTTP API) instead of the Docker
daemon.

Interface contract (mirroring PoolDockerRuntime)
-------------------------------------------------
``start_idle_container(template: str) -> str``
    Start a warm idle microVM and return its sandbox_id string.

``stop_container(sandbox_id: str) -> None``
    Kill and delete the microVM.  Idempotent — 404 is silently swallowed.

``alive_ids(sandbox_ids) -> set[str]``
    Which of the given microVMs the controller still has alive — one
    ``GET /sandboxes`` per call.  Raises when the list can't be read.

``running_pool_ids() -> set[str]``
    Every live microVM whose metadata marks it pool-managed by this pool
    owner (``Settings.pool_owner``); a peer replica's VMs are never listed.

``set_pool_state_label(sandbox_id: str, state: str) -> None``
    Best-effort label update (logged only — CubeSandbox metadata is immutable
    after creation, so we log the intent as the Docker runtime does).

Limitations / follow-ups
------------------------
* **Snapshot-based pre-warming is not implemented.**
  CubeSandbox supports snapshotting a running VM so a new microVM can fork
  from a memory checkpoint (cold-start in < 100 ms, even less than a warm
  Docker container).  This requires:
    1. Spawning one "template VM" with the Claude SDK worker in a
       quiescent state.
    2. Calling the CubeSandbox snapshot API (not yet in this codebase — no
       E2B spec for it exists publicly).
    3. Forking from that snapshot on ``start_idle_container``.
  Until that path is available, ``start_idle_container`` issues a regular
  POST /sandboxes (cold spawn) — the pool still gives latency benefits
  because the microVM is already booted when ``acquire()`` is called.
  Snapshot pre-warming is tracked as a B-M2 follow-up.

* **mTLS configuration** is inherited from the Settings fields that mirror
  agent-scheduler's cube_* settings (SANDBOX_POOL_CUBE_* env vars).
  The transport construction logic is identical to
  ``agent_scheduler.services.cube_runtime.CubeSandboxRuntime._make_client``.

References
----------
* ``services/agent-scheduler/src/agent_scheduler/services/cube_runtime.py``
  — the scheduler's CubeSandboxRuntime; this class is the pool mirror of it.
* ``docs/runbook/enable-cubesandbox.md`` — staging enablement guide.
* ADR 0001 — why CubeSandbox + snapshot-based pool is the production target.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import httpx

from sandbox_pool.config import Settings

logger = logging.getLogger(__name__)

# Labels we embed in microVM metadata (analogous to Docker labels).
_LABEL_PREFIX = "agent-platform"

# What a warm microVM runs until a task is injected: nothing.  The worker needs
# a RUN_ID and a real task and exits without them, so starting it here would
# only get the VM dropped and respawned on every pool tick.
IDLE_CMD: list[str] = ["sleep", "infinity"]

# Controller ``state`` values that mean a VM is gone for good — the terminal
# half of agent-scheduler's ``_E2B_STATUS_MAP``.  Anything else (creating,
# running, paused by cube-lifecycle-manager, or a state we don't know) counts
# as alive, so controller vocabulary drift can't churn the warm pool.
_TERMINAL_STATES = frozenset({"killed", "completed", "errored", "not_found"})


class PoolCubeRuntime:
    """Thin CubeSandbox HTTP client for the sandbox pool.

    Communicates with the CubeSandbox controller using the E2B-compatible
    REST API (same protocol as agent-scheduler's CubeSandboxRuntime).
    Implements the ``PoolRuntime`` interface expected by :class:`~sandbox_pool.pool.Pool`.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: httpx.Client = self._make_client()

    # --------------------------------------------------------------- transport

    def _make_client(self) -> httpx.Client:
        """Build the httpx.Client respecting SANDBOX_POOL_CUBE_TLS_MODE.

        Mirrors the TLS resolution logic in agent-scheduler's cube_runtime.py
        so the two services behave identically with respect to the trust boundary.

        Resolution:
          ``mtls`` — load client cert + key + CA; all three required.
          ``tls``  — only CA; no client cert.
          ``off``  — plain HTTP; no cert material (dev/test only).
        """
        mode: str = getattr(self._settings, "cube_tls_mode", "mtls")
        url: str = self._settings.cube_controller_url
        timeout = httpx.Timeout(connect=2.0, read=10.0, write=10.0, pool=2.0)

        if mode == "off":
            return httpx.Client(base_url=url, timeout=timeout)

        ca: str | None = getattr(self._settings, "cube_ca_cert", None)
        if not ca:
            raise RuntimeError(
                f"cube_tls_mode={mode!r} requires SANDBOX_POOL_CUBE_CA_CERT; "
                "refusing to build pool cube client",
            )

        if mode == "tls":
            return httpx.Client(base_url=url, timeout=timeout, verify=ca)

        # mode == "mtls"
        cert: str | None = getattr(self._settings, "cube_client_cert", None)
        key: str | None = getattr(self._settings, "cube_client_key", None)
        if not cert or not key:
            raise RuntimeError(
                "cube_tls_mode='mtls' requires SANDBOX_POOL_CUBE_CLIENT_CERT "
                "+ SANDBOX_POOL_CUBE_CLIENT_KEY; refusing to build pool cube client",
            )
        return httpx.Client(
            base_url=url,
            timeout=timeout,
            verify=ca,
            cert=(cert, key),
        )

    # --------------------------------------------------------------- spawn

    def start_idle_container(
        self, template: str, *, memory_limit: str | None = None,
    ) -> str:
        """Boot a warm idle microVM and return its sandbox_id.

        ``memory_limit`` (docker-style, e.g. ``"1g"`` / ``"4g"``) sizes the
        microVM for its memory tier.  Because a microVM pre-allocates its RAM,
        each tier must warm at its own size; when omitted we fall back to the
        legacy single-bucket ``sandbox_memory_limit``.

        The microVM parks on ``IDLE_CMD`` instead of running the worker, which
        exits without a RUN_ID and a real task.
        The pool will hand the sandbox_id to the scheduler on acquire; the
        scheduler then tells the VM what task to run by other means
        (for CubeSandbox this relies on the snapshot fork path — follow-up).

        NOTE: Snapshot-based pre-warming is NOT implemented.
        This is a cold-spawn that gives the pool one pre-booted VM ready
        to receive a task.  The latency benefit comes from the microVM
        already being booted (OS + Node runtime in memory), not from
        a memory-checkpoint fork.  Snapshot integration is a follow-up.
        """
        body = {
            # E2B-compatible fields
            "template_id": self._settings.sandbox_image,
            "metadata": {
                f"{_LABEL_PREFIX}.pool-managed": "true",
                f"{_LABEL_PREFIX}.pool-state": "idle",
                f"{_LABEL_PREFIX}.pool-template": template,
                f"{_LABEL_PREFIX}.pool-owner": self._settings.pool_owner,
            },
            # Park the VM (CubeSandbox's behaviour with an empty cmd is
            # unspecified, so the command is always passed explicitly).
            "cmd": list(IDLE_CMD),
            "resources": {
                "memory_mb": _parse_memory_mb(
                    memory_limit or self._settings.sandbox_memory_limit,
                ),
                "cpu_shares": self._settings.sandbox_cpu_shares,
            },
        }

        try:
            resp = self._client.post("/sandboxes", json=body)
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"CubeSandbox controller unreachable during pool warm-up: {exc}",
            ) from exc

        if resp.status_code >= 400:
            raise RuntimeError(
                f"CubeSandbox refused pool spawn (HTTP {resp.status_code}): {resp.text[:200]}",
            )

        # E2B answers ``sandboxID`` (what agent-scheduler reads); older
        # controller builds said ``sandbox_id``.
        created = resp.json()
        sandbox_id = str(created.get("sandboxID") or created.get("sandbox_id") or "")
        if not sandbox_id:
            raise RuntimeError("CubeSandbox returned no sandboxID for pool warm-up spawn")

        logger.info(
            "pool: cube idle microVM started",
            extra={"sandbox_id": sandbox_id, "template": template},
        )
        return sandbox_id

    # --------------------------------------------------------------- reap

    def stop_container(self, sandbox_id: str) -> None:
        """Kill and delete a pool-managed microVM.  Idempotent."""
        try:
            resp = self._client.delete(f"/sandboxes/{sandbox_id}")
        except httpx.HTTPError as exc:
            logger.warning(
                "pool: cube stop request failed (treating as gone)",
                extra={"sandbox_id": sandbox_id, "error": str(exc)},
            )
            return

        if resp.status_code == 404:
            logger.warning(
                "pool: cube sandbox not found during reap (already gone?)",
                extra={"sandbox_id": sandbox_id},
            )
            return

        if resp.status_code >= 400:
            # 409 = already killed.  Treat any non-404 4xx/5xx as "gone".
            logger.warning(
                "pool: cube delete returned error; treating as gone",
                extra={"sandbox_id": sandbox_id, "status": resp.status_code},
            )
            return

        logger.info(
            "pool: cube idle microVM stopped",
            extra={"sandbox_id": sandbox_id},
        )

    # --------------------------------------------------------------- liveness

    def alive_ids(self, sandbox_ids: Iterable[str]) -> set[str]:
        """Return the subset of ``sandbox_ids`` the controller still has alive.

        One ``GET /sandboxes`` (the list agent-scheduler's reaper walks) covers
        every id.  A VM is dead only when its record is gone or its ``state``
        is terminal (``_TERMINAL_STATES``).  Raises when the list can't be read
        (unreachable, non-200, unrecognised body): an unreachable controller
        must not look like every warm VM died.
        """
        alive = {_vm_id(vm) for vm in self._live_vms()}
        return {sid for sid in sandbox_ids if sid in alive}

    def running_pool_ids(self) -> set[str]:
        """Ids of every live microVM this pool owner started.

        A VM without the pool metadata is never included: it may be a run the
        scheduler started.  Neither is one another pool owner (a peer replica)
        started.  Raises when the list can't be read.
        """
        owner = self._settings.pool_owner
        return {
            _vm_id(vm)
            for vm in self._live_vms()
            if isinstance(vm.get("metadata"), dict)
            and vm["metadata"].get(f"{_LABEL_PREFIX}.pool-managed") == "true"
            and vm["metadata"].get(f"{_LABEL_PREFIX}.pool-owner") == owner
        }

    def _live_vms(self) -> list[dict]:
        """Every VM record from ``GET /sandboxes`` whose state is not terminal."""
        try:
            resp = self._client.get("/sandboxes")
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"CubeSandbox controller unreachable during pool liveness probe: {exc}",
            ) from exc

        if resp.status_code != 200:
            raise RuntimeError(
                f"CubeSandbox refused pool liveness probe (HTTP {resp.status_code}): "
                f"{resp.text[:200]}",
            )

        payload = resp.json()
        # Controller versions answer with a bare list or ``{"items": [...]}``.
        records = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            raise RuntimeError("CubeSandbox returned an unrecognised sandbox list")
        return [
            vm for vm in records
            if isinstance(vm, dict)
            and (vm.get("state") or vm.get("status")) not in _TERMINAL_STATES
            and _vm_id(vm)
        ]

    # --------------------------------------------------------------- label (no-op)

    def set_pool_state_label(self, sandbox_id: str, state: str) -> None:
        """Log a pool-state change intent.

        CubeSandbox VM metadata is immutable after creation (unlike Docker labels
        which can be updated in flight).  We log the intent so the audit trail
        is consistent; authoritative state lives in PoolState.
        """
        logger.debug(
            "pool state change",
            extra={"sandbox_id": sandbox_id, "pool_state": state},
        )


def _parse_memory_mb(limit: str) -> int:
    """Translate ``"2g"`` / ``"512m"`` (docker style) into integer MiB."""
    limit = limit.strip().lower()
    if limit.endswith("g"):
        return int(float(limit[:-1]) * 1024)
    if limit.endswith("m"):
        return int(limit[:-1])
    return int(limit)


def _vm_id(vm: dict) -> str:
    return str(vm.get("sandboxID") or vm.get("sandbox_id") or "")
