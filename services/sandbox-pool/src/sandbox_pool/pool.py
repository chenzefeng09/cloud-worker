"""In-memory pool state.

This is the skeleton implementation — single-process, no persistence.
B-M2 replaces it with a KEDA-driven operator that reads pool state from
the actual CubeSandbox controller (so a pod restart doesn't lose the
warm VMs we've already paid to boot).  See
``docs/design/b-m2-sandbox-runtime-v1.md §5``.

Why the dict-based skeleton lives here at all:

* It lets us land the HTTP contract first (``POST /sandboxes/acquire``,
  ``DELETE /sandboxes/{id}``), so agent-scheduler and tests can target
  the real endpoints before we have a KEDA setup.
* It exercises the watermark logic in unit tests without spinning up a
  controller.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from sandbox_pool.config import Settings
from sandbox_pool.errors import PoolExhaustedError, SandboxNotFoundError

logger = logging.getLogger(__name__)


@dataclass
class PoolEntry:
    """One pre-warmed VM (or one in-use VM after acquire)."""

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
    """Skeleton pool with the public contract callers will use.

    The real implementation will replace ``_spawn_one`` /
    ``_reap_one`` with calls to the CubeSandbox controller; this version
    fabricates fake sandbox ids so the API + watermark logic can be
    exercised end-to-end without a running controller.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._state = PoolState()
        # ``_lock`` guards mutations to ``_state``.  asyncio.Lock is fine for
        # the single-process skeleton; the KEDA operator will replace this
        # with the controller's own atomic operations.
        self._lock = asyncio.Lock()

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
        """Hand out one VM to a caller, spawning if no warm one is available.

        TODO(B-M2): wait up to ``acquire_timeout_seconds`` for a warm VM
        before falling back to a sync cold spawn.  Skeleton always
        cold-spawns when the pool is empty.
        """
        async with self._lock:
            if self._state.total_count() >= self._settings.max_total:
                raise PoolExhaustedError()
            # Prefer warm
            for entry in self._state.entries.values():
                if entry.state == "IDLE" and entry.template == template:
                    entry.state = "ACQUIRED"
                    entry.tenant_id = tenant_id
                    logger.info(
                        "acquire warm",
                        extra={"sandbox_id": entry.sandbox_id, "tenant_id": tenant_id},
                    )
                    return entry
            # Cold spawn
            entry = self._spawn_one(template)
            entry.state = "ACQUIRED"
            entry.tenant_id = tenant_id
            logger.info(
                "acquire cold",
                extra={"sandbox_id": entry.sandbox_id, "tenant_id": tenant_id},
            )
            return entry

    # ------------------------------------------------------------------ release
    async def release(self, sandbox_id: str) -> None:
        async with self._lock:
            entry = self._state.entries.get(sandbox_id)
            if entry is None:
                raise SandboxNotFoundError(sandbox_id)
            # Skeleton: reap on release; production will recycle to IDLE
            # when sandbox is fast to rewind (snapshot-based reset).
            self._reap_one(sandbox_id)

    # ------------------------------------------------------------------ warm-up
    async def warmup_tick(self) -> int:
        """Background task that brings idle count up to ``idle_low_watermark``.

        Returns how many VMs were spawned this tick (useful for tests).
        Called from a periodic scheduler the operator will run.
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
                    self._spawn_one(template)
                    spawned += 1
        return spawned

    async def reap_excess_tick(self) -> int:
        """Trim idle VMs above ``idle_high_watermark`` for each template."""
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
        return reaped

    # ============================================================== internals
    def _spawn_one(self, template: str) -> PoolEntry:
        # B-M2 follow-up: actually POST to ``cube_controller_url``.
        # Skeleton: just fabricate an id so watermark math works in tests.
        entry = PoolEntry(
            sandbox_id=f"sb_{uuid.uuid4().hex[:24]}",
            template=template,
            state="IDLE",
        )
        self._state.entries[entry.sandbox_id] = entry
        return entry

    def _reap_one(self, sandbox_id: str) -> None:
        # B-M2 follow-up: DELETE from controller.
        self._state.entries.pop(sandbox_id, None)
