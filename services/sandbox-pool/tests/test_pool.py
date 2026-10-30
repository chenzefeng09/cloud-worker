"""Pool — watermark math + acquire/release contract.

All tests inject a ``MockRuntime`` so no Docker daemon is required.
"""

from __future__ import annotations

import itertools
from unittest.mock import MagicMock

import pytest

from sandbox_pool.config import Settings
from sandbox_pool.errors import PoolExhaustedError, SandboxNotFoundError
from sandbox_pool.pool import Pool

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _settings(**over: object) -> Settings:
    defaults: dict[str, object] = {
        "env": "test",
        "log_level": "WARNING",
        "idle_low_watermark": 2,
        "idle_high_watermark": 5,
        "max_total": 10,
        "acquire_timeout_seconds": 0,
        "warmup_templates": ["base"],
        "otel_enabled": False,
        # Minimal docker-related settings (not used — runtime is mocked)
        "sandbox_image": "test-image:latest",
        "docker_network": "test_net",
        # Single-bucket by default: one "small" tier with no per-tier keys, so
        # the legacy idle_low_watermark/idle_high_watermark/max_total overrides
        # flow through (via fallback) and legacy single-tier tests keep their
        # expected counts.  Multi-tier tests override memory_tier_watermarks.
        "memory_tier_watermarks": {"small": {}},
    }
    defaults.update(over)
    return Settings(**defaults)  # type: ignore[arg-type]


def _make_pool(**over: object) -> tuple[Pool, MagicMock]:
    """Build a Pool with a mock runtime.

    The mock's ``start_idle_container`` returns unique 12-char hex ids so
    the pool's ``sandbox_id`` values look like real short container ids
    (``000000000000``, ``000000000001``, … in spawn order).
    The mock's ``stop_container`` is a no-op (assertable).
    The mock's ``alive_ids`` reports every id alive; see ``_kill``.
    """
    runtime = MagicMock()
    _counter = itertools.count()
    runtime.start_idle_container.side_effect = (
        lambda template, memory_limit=None: f"{next(_counter):012x}"
    )
    runtime.alive_ids.side_effect = set
    runtime.running_pool_ids.return_value = set()
    pool = Pool(_settings(**over), runtime=runtime)
    return pool, runtime


def _kill(runtime: MagicMock, *dead: str) -> None:
    """Make the mock runtime report ``dead`` ids as no longer alive."""
    runtime.alive_ids.side_effect = lambda ids: set(ids) - set(dead)


# --------------------------------------------------------------------------- #
# Acquire / release contract
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_acquire_cold_spawns_when_empty() -> None:
    pool, runtime = _make_pool()
    entry = await pool.acquire(tenant_id="tnt_a")
    assert entry.state == "ACQUIRED"
    assert entry.tenant_id == "tnt_a"
    assert entry.template == "base"
    # Real spawn must have been called (small tier → 1g)
    runtime.start_idle_container.assert_called_once_with("base", memory_limit="1g")


@pytest.mark.asyncio
async def test_acquire_reuses_warm_vm() -> None:
    """Warm acquire must not start a new container."""
    pool, runtime = _make_pool()
    spawned = await pool.warmup_tick()
    assert spawned == 2  # idle_low_watermark = 2
    runtime.start_idle_container.reset_mock()

    snap_before = pool.snapshot()
    entry = await pool.acquire(tenant_id="tnt_a")
    snap_after = pool.snapshot()

    assert snap_before["total"] == snap_after["total"], (
        "warm acquire must not change total VM count"
    )
    assert entry.state == "ACQUIRED"
    # No additional container started
    runtime.start_idle_container.assert_not_called()


@pytest.mark.asyncio
async def test_acquire_warm_returns_real_container_id() -> None:
    """The sandbox_id handed back must be a 12-char hex string (short id)."""
    pool, _runtime = _make_pool()
    await pool.warmup_tick()
    entry = await pool.acquire(tenant_id="tnt_b")
    assert len(entry.sandbox_id) == 12
    assert all(c in "0123456789abcdef" for c in entry.sandbox_id)


@pytest.mark.asyncio
async def test_acquire_refuses_above_max_total() -> None:
    pool, _runtime = _make_pool(max_total=2, idle_low_watermark=0)
    await pool.acquire(tenant_id="t1")
    await pool.acquire(tenant_id="t2")
    with pytest.raises(PoolExhaustedError):
        await pool.acquire(tenant_id="t3")


@pytest.mark.asyncio
async def test_release_unknown_raises_not_found() -> None:
    pool, _runtime = _make_pool()
    with pytest.raises(SandboxNotFoundError):
        await pool.release("sb_does_not_exist", tenant_id="t1")


@pytest.mark.asyncio
async def test_release_calls_stop_container() -> None:
    """release() must delegate to runtime.stop_container()."""
    pool, runtime = _make_pool(idle_low_watermark=0)
    entry = await pool.acquire(tenant_id="t1")
    sid = entry.sandbox_id
    await pool.release(sid, tenant_id="t1")
    assert pool.snapshot()["total"] == 0
    runtime.stop_container.assert_called_once_with(sid)


@pytest.mark.asyncio
async def test_release_removes_from_pool() -> None:
    pool, _runtime = _make_pool(idle_low_watermark=0)
    entry = await pool.acquire(tenant_id="t1")
    await pool.release(entry.sandbox_id, tenant_id="t1")
    assert pool.snapshot()["total"] == 0


@pytest.mark.asyncio
async def test_release_rejects_wrong_tenant() -> None:
    pool, runtime = _make_pool(idle_low_watermark=0)
    entry = await pool.acquire(tenant_id="t1")
    with pytest.raises(SandboxNotFoundError):
        await pool.release(entry.sandbox_id, tenant_id="t2")
    assert pool.snapshot()["total"] == 1
    runtime.stop_container.assert_not_called()


# --------------------------------------------------------------------------- #
# Watermark + background ticks
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_warmup_tick_spawns_real_containers() -> None:
    pool, runtime = _make_pool(idle_low_watermark=3)
    spawned = await pool.warmup_tick()
    assert spawned == 3
    assert runtime.start_idle_container.call_count == 3
    # All calls used the "base" template
    for call in runtime.start_idle_container.call_args_list:
        assert call.args[0] == "base"
        assert call.kwargs["memory_limit"] == "1g"


@pytest.mark.asyncio
async def test_warmup_tick_respects_max_total() -> None:
    pool, _runtime = _make_pool(idle_low_watermark=10, max_total=3)
    spawned = await pool.warmup_tick()
    assert spawned == 3, "warmup must cap at max_total"


@pytest.mark.asyncio
async def test_reap_excess_tick_calls_stop() -> None:
    """reap_excess_tick() must call stop_container for each victim."""
    pool, runtime = _make_pool(idle_low_watermark=8, idle_high_watermark=3, max_total=20)
    await pool.warmup_tick()  # spawns 8
    runtime.stop_container.reset_mock()

    reaped = await pool.reap_excess_tick()
    assert reaped == 5
    assert runtime.stop_container.call_count == 5


@pytest.mark.asyncio
async def test_reap_excess_tick_drops_above_high_watermark() -> None:
    pool, _runtime = _make_pool(idle_low_watermark=8, idle_high_watermark=3, max_total=20)
    await pool.warmup_tick()  # spawns up to idle_low = 8
    reaped = await pool.reap_excess_tick()
    # idle 8 → high 3 → reap 5
    assert reaped == 5


# --------------------------------------------------------------------------- #
# Liveness reconciliation
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_reconcile_reaps_dead_idle_and_warmup_replaces_it() -> None:
    """A warm container that died is dropped, removed, and re-warmed."""
    pool, runtime = _make_pool(idle_low_watermark=2)
    await pool.warmup_tick()
    dead_id, live_id = "000000000000", "000000000001"
    _kill(runtime, dead_id)
    runtime.start_idle_container.reset_mock()

    dropped = await pool.reconcile_tick()

    assert dropped == 1
    # One batched probe covers every idle entry.
    runtime.alive_ids.assert_called_once()
    (probed,), _ = runtime.alive_ids.call_args
    assert sorted(probed) == [dead_id, live_id]
    runtime.stop_container.assert_called_once_with(dead_id)
    assert pool.snapshot()["by_template"]["base"] == {"idle": 1, "total": 1}

    spawned = await pool.warmup_tick()

    assert spawned == 1
    runtime.start_idle_container.assert_called_once_with("base", memory_limit="1g")
    assert pool.snapshot()["by_template"]["base"] == {"idle": 2, "total": 2}


@pytest.mark.asyncio
async def test_acquire_never_returns_known_dead_entry() -> None:
    """Once reconcile finds a warm sandbox dead, acquire must skip it."""
    pool, runtime = _make_pool(idle_low_watermark=2)
    await pool.warmup_tick()
    # The dead one is first in acquire's scan order.
    dead_id = "000000000000"
    _kill(runtime, dead_id)
    await pool.reconcile_tick()
    runtime.start_idle_container.reset_mock()

    first = await pool.acquire(tenant_id="t1")   # the surviving warm one
    second = await pool.acquire(tenant_id="t2")  # no warm left → cold spawn

    assert dead_id not in {first.sandbox_id, second.sandbox_id}
    runtime.start_idle_container.assert_called_once()


@pytest.mark.asyncio
async def test_reconcile_keeps_entries_when_probe_fails() -> None:
    """An unreachable runtime must not read as "every warm sandbox died"."""
    pool, runtime = _make_pool(idle_low_watermark=2)
    await pool.warmup_tick()
    runtime.alive_ids.side_effect = RuntimeError("docker daemon unreachable")

    dropped = await pool.reconcile_tick()

    assert dropped == 0
    runtime.stop_container.assert_not_called()
    assert pool.snapshot()["by_template"]["base"]["idle"] == 2


@pytest.mark.asyncio
async def test_reconcile_leaves_acquired_entries_alone() -> None:
    """Only IDLE entries are probed; an ACQUIRED one belongs to its caller."""
    pool, runtime = _make_pool(idle_low_watermark=2)
    await pool.warmup_tick()
    acquired = await pool.acquire(tenant_id="t1")
    runtime.alive_ids.side_effect = lambda ids: set()  # every container dead

    dropped = await pool.reconcile_tick()

    assert dropped == 1
    (probed,), _ = runtime.alive_ids.call_args
    assert acquired.sandbox_id not in probed
    assert pool.snapshot()["by_template"]["base"] == {"idle": 0, "total": 1}
    # Still tracked, so the caller can release it as usual.
    await pool.release(acquired.sandbox_id, tenant_id="t1")
    assert pool.snapshot()["total"] == 0


@pytest.mark.asyncio
async def test_reconcile_skips_probe_without_idle_entries() -> None:
    pool, runtime = _make_pool(idle_low_watermark=0)
    assert await pool.reconcile_tick() == 0
    runtime.alive_ids.assert_not_called()


# --------------------------------------------------------------------------- #
# Startup: warm sandboxes an earlier pool process left running
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_reap_orphans_stops_running_sandboxes_the_pool_does_not_know() -> None:
    pool, runtime = _make_pool(idle_low_watermark=1)
    await pool.warmup_tick()  # this process's own warm sandbox: 000000000000
    runtime.running_pool_ids.return_value = {"000000000000", "0ldc0ntainer", "0ldc0ntaine2"}

    stopped = await pool.reap_orphans()

    assert stopped == 2
    assert sorted(c.args[0] for c in runtime.stop_container.call_args_list) == [
        "0ldc0ntaine2", "0ldc0ntainer",
    ]
    assert pool.snapshot()["by_template"]["base"]["idle"] == 1


@pytest.mark.asyncio
async def test_reap_orphans_stops_nothing_when_the_listing_fails() -> None:
    pool, runtime = _make_pool()
    runtime.running_pool_ids.side_effect = RuntimeError("docker daemon unreachable")

    assert await pool.reap_orphans() == 0
    runtime.stop_container.assert_not_called()


@pytest.mark.asyncio
async def test_reap_orphans_carries_on_past_a_failed_stop() -> None:
    pool, runtime = _make_pool()
    runtime.running_pool_ids.return_value = {"0ldc0ntainer", "0ldc0ntaine2"}
    runtime.stop_container.side_effect = [RuntimeError("busy"), None]

    # Both are tried; only the one that stopped is counted.
    assert await pool.reap_orphans() == 1
    assert runtime.stop_container.call_count == 2


# --------------------------------------------------------------------------- #
# Shutdown: stop this process's warm spares
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_drain_idle_stops_every_warm_sandbox() -> None:
    pool, runtime = _make_pool(idle_low_watermark=2)
    await pool.warmup_tick()

    assert await pool.drain_idle() == 2

    assert sorted(c.args[0] for c in runtime.stop_container.call_args_list) == [
        "000000000000", "000000000001",
    ]
    assert pool.snapshot()["total"] == 0


@pytest.mark.asyncio
async def test_drain_idle_leaves_handed_out_sandboxes_alone() -> None:
    pool, runtime = _make_pool(idle_low_watermark=0)
    acquired = await pool.acquire(tenant_id="tnt_a")

    assert await pool.drain_idle() == 0

    runtime.stop_container.assert_not_called()
    assert pool.snapshot()["total"] == 1
    assert acquired.sandbox_id == "000000000000"


@pytest.mark.asyncio
async def test_drain_idle_carries_on_past_a_failed_stop() -> None:
    pool, runtime = _make_pool(idle_low_watermark=2)
    await pool.warmup_tick()
    runtime.stop_container.side_effect = [RuntimeError("busy"), None]

    # Both are tried and both leave the pool; only the one that stopped counts.
    assert await pool.drain_idle() == 1
    assert runtime.stop_container.call_count == 2
    assert pool.snapshot()["total"] == 0


# --------------------------------------------------------------------------- #
# Warm-pool acquire + timeout
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_acquire_warm_after_warmup_tick() -> None:
    """After warmup_tick the pool has IDLE containers; acquire must be a warm hit."""
    pool, runtime = _make_pool(idle_low_watermark=2)
    await pool.warmup_tick()
    spawn_count_before = runtime.start_idle_container.call_count

    entry = await pool.acquire(tenant_id="tnt_warm")
    assert entry.state == "ACQUIRED"
    # No additional spawn for a warm acquire
    assert runtime.start_idle_container.call_count == spawn_count_before


@pytest.mark.asyncio
async def test_acquire_cold_spawn_when_no_warm_and_no_timeout(monkeypatch) -> None:
    """With acquire_timeout_seconds=0, no polling → immediate cold spawn."""
    pool, runtime = _make_pool(idle_low_watermark=0, acquire_timeout_seconds=0)
    entry = await pool.acquire(tenant_id="tnt_cold")
    assert entry.state == "ACQUIRED"
    # Cold spawn happened (small tier → 1g)
    runtime.start_idle_container.assert_called_once_with("base", memory_limit="1g")


# --------------------------------------------------------------------------- #
# Per-memory-tier buckets (small=1g / large=4g)
# --------------------------------------------------------------------------- #

def _multi_tier(**over: object) -> dict[str, object]:
    """Two-bucket watermark config used by the tier tests."""
    base: dict[str, object] = {
        "acquire_timeout_seconds": 0,
        "max_total": 100,
        "memory_tier_watermarks": {
            "small": {"idle_low": 2, "idle_high": 4, "max_total": 50},
            "large": {"idle_low": 1, "idle_high": 3, "max_total": 50},
        },
        "memory_tier_sizes": {"small": "1g", "large": "4g"},
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_large_acquire_does_not_serve_small_idle() -> None:
    """A "large" request must NOT be handed a "small" warm sandbox."""
    pool, runtime = _make_pool(**_multi_tier(
        memory_tier_watermarks={
            "small": {"idle_low": 3, "idle_high": 4, "max_total": 50},
            "large": {"idle_low": 0, "idle_high": 3, "max_total": 50},
        },
    ))
    # Warm only small idles (large idle_low=0 → none warmed).
    await pool.warmup_tick()
    snap = pool.snapshot()
    assert snap["by_memory_tier"]["small"]["idle"] == 3
    assert snap["by_memory_tier"]["large"]["idle"] == 0

    runtime.start_idle_container.reset_mock()
    # Acquiring large must cold-spawn (no large idle to reuse) and leave the
    # small idles untouched.
    entry = await pool.acquire(tenant_id="t1", memory_tier="large")
    assert entry.memory_tier == "large"
    runtime.start_idle_container.assert_called_once_with("base", memory_limit="4g")
    # All 3 small idles still IDLE (none stolen for the large run).
    assert pool.snapshot()["by_memory_tier"]["small"]["idle"] == 3


@pytest.mark.asyncio
async def test_small_acquire_does_not_serve_large_idle() -> None:
    """A "small" request must NOT be handed a "large" warm sandbox."""
    pool, runtime = _make_pool(**_multi_tier(
        memory_tier_watermarks={
            "small": {"idle_low": 0, "idle_high": 4, "max_total": 50},
            "large": {"idle_low": 2, "idle_high": 3, "max_total": 50},
        },
    ))
    await pool.warmup_tick()  # warms only large
    assert pool.snapshot()["by_memory_tier"]["large"]["idle"] == 2

    runtime.start_idle_container.reset_mock()
    entry = await pool.acquire(tenant_id="t1", memory_tier="small")
    assert entry.memory_tier == "small"
    runtime.start_idle_container.assert_called_once_with("base", memory_limit="1g")
    assert pool.snapshot()["by_memory_tier"]["large"]["idle"] == 2


@pytest.mark.asyncio
async def test_large_acquire_reuses_large_idle() -> None:
    """A warm large idle is reused for a large acquire (no new spawn)."""
    pool, runtime = _make_pool(**_multi_tier())
    await pool.warmup_tick()
    runtime.start_idle_container.reset_mock()

    entry = await pool.acquire(tenant_id="t1", memory_tier="large")
    assert entry.memory_tier == "large"
    runtime.start_idle_container.assert_not_called()


@pytest.mark.asyncio
async def test_warmup_brings_each_tier_to_its_own_idle_low() -> None:
    """warmup_tick warms small→idle_low_small and large→idle_low_large."""
    pool, runtime = _make_pool(**_multi_tier())
    spawned = await pool.warmup_tick()
    # small idle_low=2 + large idle_low=1 = 3 spawns.
    assert spawned == 3

    snap = pool.snapshot()
    assert snap["by_memory_tier"]["small"]["idle"] == 2
    assert snap["by_memory_tier"]["large"]["idle"] == 1

    # Each tier warmed at its own memory size.
    sizes = sorted(
        c.kwargs["memory_limit"]
        for c in runtime.start_idle_container.call_args_list
    )
    assert sizes == ["1g", "1g", "4g"]


@pytest.mark.asyncio
async def test_snapshot_reports_per_tier_breakdown() -> None:
    pool, _runtime = _make_pool(**_multi_tier())
    await pool.warmup_tick()
    snap = pool.snapshot()
    assert set(snap["by_memory_tier"]) == {"small", "large"}
    assert snap["by_memory_tier"]["small"] == {"idle": 2, "total": 2}
    assert snap["by_memory_tier"]["large"] == {"idle": 1, "total": 1}
    assert snap["total"] == 3


@pytest.mark.asyncio
async def test_reaping_is_per_tier() -> None:
    """Reaping trims each tier to its own idle_high, independently."""
    pool, _runtime = _make_pool(**_multi_tier(
        memory_tier_watermarks={
            "small": {"idle_low": 6, "idle_high": 2, "max_total": 50},
            "large": {"idle_low": 5, "idle_high": 1, "max_total": 50},
        },
    ))
    await pool.warmup_tick()  # small=6 idle, large=5 idle
    reaped = await pool.reap_excess_tick()
    # small: 6 → 2 → reap 4; large: 5 → 1 → reap 4; total 8.
    assert reaped == 8
    snap = pool.snapshot()
    assert snap["by_memory_tier"]["small"]["idle"] == 2
    assert snap["by_memory_tier"]["large"]["idle"] == 1


@pytest.mark.asyncio
async def test_large_exhaustion_independent_of_small() -> None:
    """A flood of small idles must not let large exceed its own max_total."""
    pool, _runtime = _make_pool(**_multi_tier(
        memory_tier_watermarks={
            "small": {"idle_low": 0, "idle_high": 4, "max_total": 50},
            "large": {"idle_low": 0, "idle_high": 3, "max_total": 1},
        },
    ))
    await pool.acquire(tenant_id="t1", memory_tier="large")
    with pytest.raises(PoolExhaustedError):
        await pool.acquire(tenant_id="t2", memory_tier="large")
