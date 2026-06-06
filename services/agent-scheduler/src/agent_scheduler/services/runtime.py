"""``SandboxRuntime`` — the seam between agent-scheduler and the actual
sandbox technology.

Rationale
---------
B-M1 hard-coded Docker container orchestration inside ``docker_runtime.py``;
B-M2 needs to replace it with CubeSandbox (microVM) by default while
keeping Docker as the dev/CI fallback (no KVM required for contributors)
and Kata Containers as the production fallback (see ADR 0001).

Rather than fork the call sites in ``api/v1/runs.py`` and
``api/v1/sandboxes.py``, we declare a small protocol here that every
runtime implements; the API layer talks to ``SandboxRuntime`` and the
``RUNTIME_REGISTRY`` decides which concrete class loads based on the
``SCHEDULER_RUNTIME`` env var.

The protocol is intentionally **synchronous** because the B-M1 docker SDK
is sync, and async wrapping inside the runtime hides where the I/O really
happens.  ``api/v1/runs.py`` uses ``await asyncio.to_thread(...)`` when
the call needs to be non-blocking.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# The dict types stay loose for now (``dict``) rather than TypedDict because
# the shape is still settling.  Once CubeSandbox lands and we know what
# event fields are stable across runtimes we can tighten this.

__all__ = [
    "SandboxRuntime",
    "RuntimeKind",
    "RUNTIME_REGISTRY",
    "build_runtime",
]


# Stays a plain Literal-ish alias; we don't make it an Enum because env-var
# parsing is easier this way and the set is small.
RuntimeKind = str  # "docker" | "cube" | "kata"


@runtime_checkable
class SandboxRuntime(Protocol):
    """Spawn / inspect / cancel sandbox containers for one tenant.

    Implementations:

    * :class:`agent_scheduler.services.docker_runtime.DockerRuntime` —
      dev/CI default; spawns Docker containers through a socket-proxy.
    * :class:`agent_scheduler.services.cube_runtime.CubeSandboxRuntime` —
      production default in B-M2+; spawns microVMs via the CubeSandbox
      controller (containerd shim + E2B HTTP protocol).
    * ``KataRuntime`` (future) — fallback; spawns Kata Containers via the
      same containerd shim with a Cloud Hypervisor VMM.

    All implementations enforce:

    * ID whitelist on tenant/session/run/user (no shell-injectable
      characters spliced into config files or container labels).
    * ``foundation_token`` is non-empty and short-lived — see
      ``ADR 0001`` and ``docs/CONVENTIONS.md §3.7``.
    * ``list_containers`` / ``get_container_tenant`` enforce tenant
      ownership, so the API layer can refuse cross-tenant probes.
    """

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
    ) -> str:
        """Spawn a sandbox for one agent run; return the runtime-specific id.

        ``run_id`` / ``session_id`` / ``tenant_id`` / ``user_id`` are
        validated against the implementation's identifier whitelist; bad
        input raises :class:`SandboxSpawnError`.  ``foundation_token`` is
        the short-lived per-run JWT minted via
        ``IdentityResource.issue_service_account_token`` — never the
        scheduler's own service-account token.
        """

    def get_run_status(self, container_id: str) -> dict:
        """Return ``{status, exit_code, started_at, finished_at}``.

        ``status`` is normalised across runtimes to one of
        ``PENDING|RUNNING|COMPLETED|FAILED|CANCELLED|NOT_FOUND``.
        """

    def get_container_tenant(self, container_id: str) -> str:
        """Return the ``agent-platform.tenant-id`` of a container.

        Raises :class:`ContainerNotFoundError` when the container doesn't
        exist or doesn't carry the label (i.e. wasn't spawned by us).
        The API layer uses this to refuse cross-tenant destructive ops.
        """

    def cancel_run(self, container_id: str) -> None:
        """Stop + remove a container.  Idempotent on already-gone containers."""

    def list_containers(self, *, tenant_id: str) -> list[dict]:
        """List agent-platform containers belonging to ``tenant_id``.

        Required ``tenant_id`` — passing an empty string raises
        ``ValueError`` rather than widening the filter, so an accidental
        internal caller (cron, broken middleware) can't leak cross-tenant
        data.
        """


# --------------------------------------------------------------------------- #
# Registry — populated by submodules at import time
# --------------------------------------------------------------------------- #

RUNTIME_REGISTRY: dict[RuntimeKind, type] = {}


def build_runtime(kind: RuntimeKind, settings) -> SandboxRuntime:  # noqa: ANN001
    """Instantiate the runtime registered for ``kind``.

    Called once at app startup (and per request in tests that
    monkeypatch).  Unknown ``kind`` raises ``ValueError`` listing the
    available registrations to make misconfiguration loud.
    """
    if kind not in RUNTIME_REGISTRY:
        available = ", ".join(sorted(RUNTIME_REGISTRY)) or "<none registered>"
        raise ValueError(
            f"Unknown SCHEDULER_RUNTIME={kind!r}; registered: {available}",
        )
    return RUNTIME_REGISTRY[kind](settings)
