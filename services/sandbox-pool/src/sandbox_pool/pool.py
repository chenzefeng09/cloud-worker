"""In-memory pool state with real Docker-backed spawn/reap.

Design
------
The pool maintains a set of WARM idle sandbox containers (pre-started via
``PoolDockerRuntime.start_idle_container``).  ``acquire()`` hands one out
instantly when available; callers never pay cold-start latency on a warm hit.

Lifecycle
~~~~~~~~~
* **warmup_tick()** runs in the background, spawning real Docker containers
  up to ``idle_low_watermark`` per template.
* **acquire()** claims an IDLE entry (fast path) or cold-spawns one
  synchronously when none are available (waits up to
  ``acquire_timeout_seconds`` polling for a warm one first).
* **release(sandbox_id)** reaps the container (``sleep infinity`` containers
  are not reusable after task assignment in the current Docker dev path;
  the snapshot-based recycle path is a KEDA/CubeSandbox follow-up).
* **reap_excess_tick()** removes idle containers above
  ``idle_high_watermark``.

Persistence
~~~~~~~~~~~
State is in-process; a restart re-discovers warm containers (future work —
for now it re-spawns).  The KEDA operator (B-M2 follow-up) replaces this
with the CubeSandbox controller as the source of truth.

Runtime injection
~~~~~~~~~~~~~~~~~
``Pool`` accepts an optional ``runtime`` argument so tests can inject a mock
without touching Docker.  Production omits it and ``Pool`` builds a real
``PoolDockerRuntime`` from the settings.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from prometheus_client import CollectorRegistry

from sandbox_pool.config import Settings
from sandbox_pool.docker_runtime import PoolDockerRuntime
from sandbox_pool.errors import PoolExhaustedError, SandboxNotFoundError

# PoolCubeRuntime is imported lazily inside _build_runtime() so that
# importing cube_runtime (which imports httpx) does not fail in environments
# where httpx is not installed but only Docker mode is used.
from sandbox_pool.metrics import PoolMetrics, build_metrics

logger = logging.getLogger(__name__)


@dataclass
class PoolEntry:
    """One pre-warmed container (or one in-use container after acquire)."""

    sandbox_id: str
    template: str
    state: str  # IDLE | ACQUIRED | RECYCLING
    tenant_id: str | None = None  # populated on acquire


@dataclass
class PoolState:
    entries: dict[str, PoolEntry] = field(default_factory=dict)

    def idle_count(self, template: str) -> int:
        return sum(
            1 for e in self.entries.values()
            if e.template == template and e.state == "IDLE"
        )

    def total_count(self) -> int:
        return len(self.entries)


class Pool:
    """Pool with warm Docker containers.

    ``_spawn_one`` starts a real idle container via ``PoolDockerRuntime``.
    ``_reap_one`` stops + removes the container.
    ``acquire()`` polls for a warm container up to ``acquire_timeout_seconds``
    before falling back to a synchronous cold spawn.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        metrics: PoolMetrics | None = None,
        metrics_registry: CollectorRegistry | None = None,
        runtime: object = None,
    ) -> None:
        self._settings = settings
        self._state = PoolState()
        self._lock = asyncio.Lock()

        if metrics is not None:
            self._metrics = metrics
        else:
            self._metrics = build_metrics(
                metrics_registry if metrics_registry is not None
                else CollectorRegistry(),
            )

        if runtime is not None:
            self._runtime = runtime
        else:
            self._runtime = _build_runtime(settings)

    @property
    def metrics(self) -> PoolMetrics:
        return self._metrics

    # ------------------------------------------------------------------ inspect
    def snapshot(self) -> dict[str, object]:
        """For ``/healthz`` + metrics.  Cheap, lock-free read."""
        return {
            "total": self._state.total_count(),
            "by_template": {
                tmpl: {
                    "idle": self._state.idle_count(tmpl),
                    "total": sum(
                        1 for e in self._state.entries.values() if e.template == tmpl
                    ),
                }
                for tmpl in self._settings.warmup_templates
            },
        }

    # ------------------------------------------------------------------ acquire
    async def acquire(self, *, tenant_id: str, template: str = "base") -> PoolEntry:
        """Hand out one sandbox to a caller.

        Fast path: if a warm IDLE entry exists for the requested template,
        claim it immediately (no new container is started).

        Slow path: poll for a warm entry up to ``acquire_timeout_seconds``.
        If no warm entry appears within the timeout, cold-spawn synchronously
        (blocking on the Docker API call via asyncio.to_thread).

        Records three Prometheus signals before returning / raising:

        * ``sandbox_pool_acquire_total{outcome=warm|cold|failed,template=...}``
        * ``sandbox_pool_acquire_latency_seconds`` (same labels)
        * ``sandbox_pool_idle`` / ``sandbox_pool_total`` gauges refreshed.
        """
        start = time.perf_counter()
        outcome = "failed"
        try:
            # --- fast path: warm entry available right now ---
            async with self._lock:
                if self._state.total_count() >= self._settings.max_total:
                    raise PoolExhaustedError()
                for entry in self._state.entries.values():
                    if entry.state == "IDLE" and entry.template == template:
                        entry.state = "ACQUIRED"
                        entry.tenant_id = tenant_id
                        outcome = "warm"
                        logger.info(
                            "acquire warm",
                            extra={
                                "sandbox_id": entry.sandbox_id,
                                "tenant_id": tenant_id,
                            },
                        )
                        return entry

            # --- slow path: poll for a warm entry ---
            deadline = time.perf_counter() + self._settings.acquire_timeout_seconds
            _POLL_INTERVAL = 0.25  # seconds between polls

            while time.perf_counter() < deadline:
                await asyncio.sleep(_POLL_INTERVAL)
                async with self._lock:
                    if self._state.total_count() >= self._settings.max_total:
                        raise PoolExhaustedError()
                    for entry in self._state.entries.values():
                        if entry.state == "IDLE" and entry.template == template:
                            entry.state = "ACQUIRED"
                            entry.tenant_id = tenant_id
                            outcome = "warm"
                            logger.info(
                                "acquire warm (after poll)",
                                extra={
                                    "sandbox_id": entry.sandbox_id,
                                    "tenant_id": tenant_id,
                                },
                            )
                            return entry

            # --- cold spawn: no warm entry within timeout ---
            async with self._lock:
                if self._state.total_count() >= self._settings.max_total:
                    raise PoolExhaustedError()
                entry = await asyncio.to_thread(self._spawn_one, template)
                entry.state = "ACQUIRED"
                entry.tenant_id = tenant_id
                outcome = "cold"
                logger.info(
                    "acquire cold",
                    extra={"sandbox_id": entry.sandbox_id, "tenant_id": tenant_id},
                )
                return entry

        finally:
            elapsed = time.perf_counter() - start
            self._metrics.acquire_total.labels(
                outcome=outcome, template=template,
            ).inc()
            self._metrics.acquire_latency.labels(
                outcome=outcome, template=template,
            ).observe(elapsed)
            self._refresh_gauges()

    # ------------------------------------------------------------------ release
    async def release(self, sandbox_id: str, *, tenant_id: str | None = None) -> None:
        async with self._lock:
            entry = self._state.entries.get(sandbox_id)
            if entry is None:
                raise SandboxNotFoundError(sandbox_id)
            if tenant_id is not None and entry.tenant_id != tenant_id:
                raise SandboxNotFoundError(sandbox_id)
            # Reap on release.  Snapshot-based recycle (→ IDLE) is the
            # KEDA/CubeSandbox follow-up; Docker containers running
            # ``sleep infinity`` are not reusable after a task run anyway.
            self._reap_one(sandbox_id)
        self._refresh_gauges()

    # ------------------------------------------------------------------ warm-up
    async def warmup_tick(self) -> int:
        """Background task that brings idle count up to ``idle_low_watermark``.

        Returns how many containers were spawned this tick.
        Called from the periodic scheduler in lifespan().
        """
        spawned = 0
        async with self._lock:
            for template in self._settings.warmup_templates:
                deficit = self._settings.idle_low_watermark - self._state.idle_count(template)
                deficit = min(
                    deficit,
                    self._settings.max_total - self._state.total_count(),
                )
                for _ in range(max(deficit, 0)):
                    await asyncio.to_thread(self._spawn_one, template)
                    spawned += 1
        self._refresh_gauges()
        return spawned

    async def reap_excess_tick(self) -> int:
        """Trim idle containers above ``idle_high_watermark`` per template."""
        reaped = 0
        async with self._lock:
            for template in self._settings.warmup_templates:
                excess = self._state.idle_count(template) - self._settings.idle_high_watermark
                victims = [
                    e for e in self._state.entries.values()
                    if e.template == template and e.state == "IDLE"
                ][:max(excess, 0)]
                for v in victims:
                    self._reap_one(v.sandbox_id)
                    reaped += 1
        self._refresh_gauges()
        return reaped

    # ============================================================== internals
    def _refresh_gauges(self) -> None:
        self._metrics.total.set(self._state.total_count())
        for tmpl in self._settings.warmup_templates:
            self._metrics.idle.labels(template=tmpl).set(
                self._state.idle_count(tmpl),
            )

    def _spawn_one(self, template: str) -> PoolEntry:
        """Start a real idle container and register it in the pool state.

        Called from warmup_tick (via asyncio.to_thread) and the cold-spawn
        path in acquire (also via asyncio.to_thread) so the blocking Docker
        API call doesn't stall the event loop.

        Returns the PoolEntry (state=IDLE) so callers can immediately
        transition it to ACQUIRED if needed.
        """
        container_id = self._runtime.start_idle_container(template)
        entry = PoolEntry(
            sandbox_id=container_id,
            template=template,
            state="IDLE",
        )
        self._state.entries[entry.sandbox_id] = entry
        return entry

    def _reap_one(self, sandbox_id: str) -> None:
        """Stop + remove the container and remove its entry from pool state."""
        self._state.entries.pop(sandbox_id, None)
        # Stop the real container in a fire-and-forget way.  We've already
        # removed the entry from PoolState so the pool considers it gone;
        # the actual Docker stop/remove is best-effort (the container will
        # be cleaned up by the proxy's TTL or a manual prune if it hangs).
        try:
            self._runtime.stop_container(sandbox_id)
        except Exception:
            logger.warning(
                "pool: reap failed (container may already be gone)",
                extra={"sandbox_id": sandbox_id},
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Runtime factory
# ---------------------------------------------------------------------------

def _build_runtime(settings: Settings) -> PoolDockerRuntime:
    """Select and instantiate the pool runtime based on ``settings.runtime``.

    When ``runtime="cube"`` a :class:`~sandbox_pool.cube_runtime.PoolCubeRuntime`
    is returned; otherwise the default :class:`PoolDockerRuntime` is used.

    The cube runtime is imported lazily so that importing this module does not
    pull in httpx in environments where only the docker runtime is used.
    """
    if settings.runtime == "cube":
        from sandbox_pool.cube_runtime import PoolCubeRuntime  # noqa: PLC0415
        logger.info(
            "pool: using CubeSandbox runtime",
            extra={"cube_controller_url": settings.cube_controller_url},
        )
        return PoolCubeRuntime(settings)  # type: ignore[return-value]

    logger.info("pool: using Docker runtime")
    return PoolDockerRuntime(settings)
