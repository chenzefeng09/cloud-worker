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
    }
    defaults.update(over)
    return Settings(**defaults)  # type: ignore[arg-type]


def _make_pool(**over: object) -> tuple[Pool, MagicMock]:
    """Build a Pool with a mock runtime.

    The mock's ``start_idle_container`` returns unique 12-char hex ids so
    the pool's ``sandbox_id`` values look like real short container ids.
    The mock's ``stop_container`` is a no-op (assertable).
    """
    runtime = MagicMock()
    _counter = itertools.count()
    runtime.start_idle_container.side_effect = (
        lambda template: f"{next(_counter):012x}"
    )
    pool = Pool(_settings(**over), runtime=runtime)
    return pool, runtime


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
    # Real spawn must have been called
    runtime.start_idle_container.assert_called_once_with("base")


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
    # Cold spawn happened
    runtime.start_idle_container.assert_called_once_with("base")
