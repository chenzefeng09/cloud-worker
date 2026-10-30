"""In-memory pool state with real Docker-backed spawn/reap.

Design
------
The pool maintains a set of WARM idle sandbox containers (pre-started via
``PoolDockerRuntime.start_idle_container``).  ``acquire()`` hands one out
instantly when available; callers never pay cold-start latency on a warm hit.

Lifecycle
~~~~~~~~~
* **reap_orphans()** runs once at startup: the pool's state lives in memory,
  so warm sandboxes an earlier process of this pool owner left running (a
  crash) are stopped instead of being left to pile up.  Another replica's
  sandboxes carry its own owner id and are never touched.
* **reconcile_tick()** runs first each tick: one runtime liveness probe over
  the IDLE entries; any whose container died is dropped and its container
  removed, so it stops counting as warm and the next warmup replaces it.
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
* **drain_idle()** runs on a graceful shutdown and stops this process's warm
  spares: a rolling update brings the pool back under a new owner id (a new
  pod name), which would not reap them.

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
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from prometheus_client import CollectorRegistry

from sandbox_pool.config import Settings
from sandbox_pool.docker_runtime import PoolDockerRuntime
from sandbox_pool.errors import PoolExhaustedError, SandboxNotFoundError

# PoolCubeRuntime is imported lazily inside _build_runtime() so that
# importing cube_runtime (which imports httpx) does not fail in environments
# where httpx is not installed but only Docker mode is used.
from sandbox_pool.metrics import PoolMetrics, build_metrics


class PoolRuntime(Protocol):
    """Minimal interface required by Pool — satisfied by PoolDockerRuntime and test mocks."""

    def start_idle_container(
        self, template: str, *, memory_limit: str | None = None,
    ) -> str: ...

    def stop_container(self, container_id: str) -> None: ...

    # Which of these ids are still alive, in one batched probe.  Must raise
    # when the runtime can't be queried rather than report everything dead.
    def alive_ids(self, container_ids: Iterable[str]) -> set[str]: ...

    # Every running sandbox this pool owner (``Settings.pool_owner``) started;
    # never a peer replica's.
    def running_pool_ids(self) -> set[str]: ...

logger = logging.getLogger(__name__)


@dataclass
class PoolEntry:
    """One pre-warmed container (or one in-use container after acquire).

    ``memory_tier`` is the bucket this warm sandbox belongs to ("small" or
    "large").  Because microVM warm sandboxes pre-allocate their RAM, a
    "large" (4g) warm VM cannot serve a "small" (1g) run and vice-versa, so
    the bucket key is ``(template, memory_tier)`` and acquire matches on both.
    """

    sandbox_id: str
    template: str
    state: str  # IDLE | ACQUIRED | RECYCLING
    tenant_id: str | None = None  # populated on acquire
    memory_tier: str = "small"  # "small" | "large"; default for back-compat


@dataclass
class PoolState:
    entries: dict[str, PoolEntry] = field(default_factory=dict)

    def idle_count(self, template: str, memory_tier: str | None = None) -> int:
        """Count IDLE entries for a template, optionally scoped to one tier.

        ``memory_tier=None`` counts across all tiers (legacy single-bucket
        behaviour); pass a tier to count that bucket only.
        """
        return sum(
            1 for e in self.entries.values()
            if e.template == template
            and e.state == "IDLE"
            and (memory_tier is None or e.memory_tier == memory_tier)
        )

    def total_count(self, memory_tier: str | None = None) -> int:
        if memory_tier is None:
            return len(self.entries)
        return sum(1 for e in self.entries.values() if e.memory_tier == memory_tier)


class Pool:
    """Pool with warm Docker containers.

    ``_spawn_one`` starts a real idle container via ``PoolDockerRuntime``.
    ``_reap_one`` stops + removes the container.
    ``reconcile_tick()`` drops idle entries whose container has died.
    ``acquire()`` polls for a warm container up to ``acquire_timeout_seconds``
    before falling back to a synchronous cold spawn.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        metrics: PoolMetrics | None = None,
        metrics_registry: CollectorRegistry | None = None,
        runtime: PoolRuntime | None = None,
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

        self._runtime: PoolRuntime
        if runtime is not None:
            self._runtime = runtime
        else:
            self._runtime = _build_runtime(settings)

    @property
    def metrics(self) -> PoolMetrics:
        return self._metrics

    # ------------------------------------------------------------------ inspect
    def snapshot(self) -> dict[str, object]:
        """For ``/healthz`` + metrics.  Cheap, lock-free read.

        ``by_memory_tier`` is the bucket breakdown the admin console reads: a
        flood of small idles must not mask zero large idles, so each tier's
        ``idle``/``total`` is reported independently.  ``by_template`` is kept
        for back-compat.
        """
        entries = self._state.entries.values()
        return {
            "total": self._state.total_count(),
            "by_template": {
                tmpl: {
                    "idle": self._state.idle_count(tmpl),
                    "total": sum(1 for e in entries if e.template == tmpl),
                }
                for tmpl in self._settings.warmup_templates
            },
            "by_memory_tier": {
                tier: {
                    "idle": sum(
                        1 for e in entries
                        if e.memory_tier == tier and e.state == "IDLE"
                    ),
                    "total": sum(1 for e in entries if e.memory_tier == tier),
                }
                for tier in self._settings.memory_tiers
            },
        }

    # ------------------------------------------------------------------ acquire
    async def acquire(
        self, *, tenant_id: str, template: str = "base", memory_tier: str = "small",
    ) -> PoolEntry:
        """Hand out one sandbox to a caller.

        Fast path: if a warm IDLE entry exists for the requested template AND
        memory tier, claim it immediately (no new container is started).

        The bucket key is ``(template, memory_tier)``: a run requesting "large"
        is NEVER served a "small" warm sandbox (and vice-versa), because a
        microVM pre-allocates its RAM and so a 4g warm VM can't act as a 1g one
        (wastes RAM) nor a 1g VM as a 4g run (too little RAM).

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
            tier_max = self._settings.tier_max_total(memory_tier)

            # --- fast path: warm entry available right now ---
            async with self._lock:
                if self._state.total_count(memory_tier) >= tier_max:
                    raise PoolExhaustedError()
                for entry in self._state.entries.values():
                    if (
                        entry.state == "IDLE"
                        and entry.template == template
                        and entry.memory_tier == memory_tier
                    ):
                        entry.state = "ACQUIRED"
                        entry.tenant_id = tenant_id
                        outcome = "warm"
                        logger.info(
                            "acquire warm",
                            extra={
                                "sandbox_id": entry.sandbox_id,
                                "tenant_id": tenant_id,
                                "memory_tier": memory_tier,
                            },
                        )
                        return entry

            # --- slow path: poll for a warm entry ---
            deadline = time.perf_counter() + self._settings.acquire_timeout_seconds
            _poll_interval = 0.25  # seconds between polls

            while time.perf_counter() < deadline:
                await asyncio.sleep(_poll_interval)
                async with self._lock:
                    if self._state.total_count(memory_tier) >= tier_max:
                        raise PoolExhaustedError()
                    for entry in self._state.entries.values():
                        if (
                            entry.state == "IDLE"
                            and entry.template == template
                            and entry.memory_tier == memory_tier
                        ):
                            entry.state = "ACQUIRED"
                            entry.tenant_id = tenant_id
                            outcome = "warm"
                            logger.info(
                                "acquire warm (after poll)",
                                extra={
                                    "sandbox_id": entry.sandbox_id,
                                    "tenant_id": tenant_id,
                                    "memory_tier": memory_tier,
                                },
                            )
                            return entry

            # --- cold spawn: no warm entry within timeout ---
            async with self._lock:
                if self._state.total_count(memory_tier) >= tier_max:
                    raise PoolExhaustedError()
                entry = await asyncio.to_thread(self._spawn_one, template, memory_tier)
                entry.state = "ACQUIRED"
                entry.tenant_id = tenant_id
                outcome = "cold"
                logger.info(
                    "acquire cold",
                    extra={
                        "sandbox_id": entry.sandbox_id,
                        "tenant_id": tenant_id,
                        "memory_tier": memory_tier,
                    },
                )
                return entry

        finally:
            elapsed = time.perf_counter() - start
            self._metrics.acquire_total.labels(
                outcome=outcome, template=template, memory_tier=memory_tier,
            ).inc()
            self._metrics.acquire_latency.labels(
                outcome=outcome, template=template, memory_tier=memory_tier,
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

    # ------------------------------------------------------------------ startup
    async def reap_orphans(self) -> int:
        """Stop warm sandboxes that an earlier process of this pool left running.

        Pool state lives in memory, so after a crash the previous process's
        warm spares are unknown here: nothing would ever hand them out or
        stop them, and they keep holding memory (``sleep infinity`` never
        exits).  Only sandboxes stamped with this pool's owner id are
        considered, so a replica starting next to its peers never stops
        theirs.  Exited ones are left alone: they hold no CPU or memory and
        their logs may explain a crash.  If the listing fails nothing is
        stopped.  Returns how many were stopped.

        A sandbox handed out by ``acquire`` is in-memory state too, so it would
        be stopped here as well.  Nothing acquires from the pool yet (the
        scheduler spawns its own containers); wiring that in needs the
        ACQUIRED entries to outlive a pool restart first.
        """
        try:
            running = await asyncio.to_thread(self._runtime.running_pool_ids)
        except Exception:
            logger.warning("pool: could not list leftover warm sandboxes", exc_info=True)
            return 0
        async with self._lock:
            orphans = sorted(running - set(self._state.entries))
        stopped = 0
        for sandbox_id in orphans:
            try:
                await asyncio.to_thread(self._runtime.stop_container, sandbox_id)
            except Exception:
                logger.warning(
                    "pool: could not stop a leftover warm sandbox",
                    extra={"sandbox_id": sandbox_id},
                    exc_info=True,
                )
            else:
                stopped += 1
        return stopped

    # ----------------------------------------------------------------- shutdown
    async def drain_idle(self) -> int:
        """Stop this process's warm spares on a graceful shutdown.

        Nothing else would: the next pool process reaps only leftovers carrying
        its own owner id, and under Kubernetes a replaced pod comes back under
        a new name.  Handed-out (ACQUIRED) sandboxes are left alone.  The stops
        run concurrently; returns how many succeeded.
        """
        async with self._lock:
            idle = [e.sandbox_id for e in self._state.entries.values() if e.state == "IDLE"]
            for sandbox_id in idle:
                self._state.entries.pop(sandbox_id, None)
        results = await asyncio.gather(
            *(asyncio.to_thread(self._runtime.stop_container, sid) for sid in idle),
            return_exceptions=True,
        )
        stopped = 0
        for sandbox_id, result in zip(idle, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "pool: could not stop a warm sandbox on shutdown",
                    extra={"sandbox_id": sandbox_id},
                    exc_info=result,
                )
            else:
                stopped += 1
        self._refresh_gauges()
        return stopped

    # ------------------------------------------------------------------ liveness
    async def reconcile_tick(self) -> int:
        """Drop IDLE entries whose sandbox is no longer alive.

        A warm container can die under the pool (crash, OOM kill, daemon
        restart, manual ``docker rm``).  Left alone its entry keeps counting as
        warm, so warmup never replaces it and ``acquire`` would hand out a dead
        id.  One ``alive_ids`` probe covers every idle entry, and the lock is
        held from probe to drop so ``acquire`` can never claim an entry the
        pool already knows is dead.  ACQUIRED entries are left to ``release``.

        If the probe fails nothing is dropped: an unreachable runtime must not
        read as "every warm sandbox died".  Returns how many entries were
        dropped; the next ``warmup_tick`` replenishes them.
        """
        dead: list[str] = []
        async with self._lock:
            idle_ids = [
                e.sandbox_id for e in self._state.entries.values() if e.state == "IDLE"
            ]
            if idle_ids:
                try:
                    alive = await asyncio.to_thread(self._runtime.alive_ids, idle_ids)
                except Exception:
                    logger.warning(
                        "pool: liveness probe failed; keeping idle entries this tick",
                        exc_info=True,
                    )
                    alive = set(idle_ids)
                dead = [sid for sid in idle_ids if sid not in alive]
                for sid in dead:
                    logger.warning(
                        "pool: idle sandbox died; dropping it from the pool",
                        extra={"sandbox_id": sid},
                    )
                    self._reap_one(sid)
        self._refresh_gauges()
        return len(dead)

    # ------------------------------------------------------------------ warm-up
    async def warmup_tick(self) -> int:
        """Background task that brings idle count up to ``idle_low_watermark``.

        Returns how many containers were spawned this tick.
        Called from the periodic scheduler in lifespan().
        """
        spawned = 0
        async with self._lock:
            for template in self._settings.warmup_templates:
                for tier in self._settings.memory_tiers:
                    deficit = (
                        self._settings.tier_idle_low(tier)
                        - self._state.idle_count(template, tier)
                    )
                    # Cap by this tier's own budget AND the global hard cap.
                    deficit = min(
                        deficit,
                        self._settings.tier_max_total(tier)
                        - self._state.total_count(tier),
                        self._settings.max_total - self._state.total_count(),
                    )
                    for _ in range(max(deficit, 0)):
                        await asyncio.to_thread(self._spawn_one, template, tier)
                        spawned += 1
        self._refresh_gauges()
        return spawned

    async def reap_excess_tick(self) -> int:
        """Trim idle containers above ``idle_high_watermark`` per (template, tier).

        Reaping is per-tier: a flood of small idles must not cause large warm
        VMs to be reaped (or mask that the large bucket is below its target).
        """
        reaped = 0
        async with self._lock:
            for template in self._settings.warmup_templates:
                for tier in self._settings.memory_tiers:
                    excess = (
                        self._state.idle_count(template, tier)
                        - self._settings.tier_idle_high(tier)
                    )
                    victims = [
                        e for e in self._state.entries.values()
                        if e.template == template
                        and e.memory_tier == tier
                        and e.state == "IDLE"
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
            for tier in self._settings.memory_tiers:
                self._metrics.idle.labels(template=tmpl, memory_tier=tier).set(
                    self._state.idle_count(tmpl, tier),
                )

    def _spawn_one(self, template: str, memory_tier: str = "small") -> PoolEntry:
        """Start a real idle container and register it in the pool state.

        Called from warmup_tick (via asyncio.to_thread) and the cold-spawn
        path in acquire (also via asyncio.to_thread) so the blocking Docker
        API call doesn't stall the event loop.

        The container is warmed at the tier's memory size (small→1g, large→4g)
        so a warm sandbox can actually serve runs of that tier.

        Returns the PoolEntry (state=IDLE) so callers can immediately
        transition it to ACQUIRED if needed.
        """
        container_id = self._runtime.start_idle_container(
            template, memory_limit=self._settings.tier_memory_size(memory_tier),
        )
        entry = PoolEntry(
            sandbox_id=container_id,
            template=template,
            state="IDLE",
            memory_tier=memory_tier,
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
