"""Pool skeleton — watermark math + acquire/release contract."""

from __future__ import annotations

import pytest

from sandbox_pool.config import Settings
from sandbox_pool.errors import PoolExhaustedError, SandboxNotFoundError
from sandbox_pool.pool import Pool


def _settings(**over: object) -> Settings:
    defaults = {
        "env": "test",
        "log_level": "WARNING",
        "idle_low_watermark": 2,
        "idle_high_watermark": 5,
        "max_total": 10,
        "warmup_templates": ["base"],
        "otel_enabled": False,
    }
    defaults.update(over)
    return Settings(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_acquire_cold_spawns_when_empty() -> None:
    pool = Pool(_settings())
    entry = await pool.acquire(tenant_id="tnt_a")
    assert entry.state == "ACQUIRED"
    assert entry.tenant_id == "tnt_a"
    assert entry.template == "base"


@pytest.mark.asyncio
async def test_acquire_reuses_warm_vm() -> None:
    pool = Pool(_settings())
    spawned = await pool.warmup_tick()
    assert spawned == 2  # idle_low_watermark
    snap_before = pool.snapshot()
    entry = await pool.acquire(tenant_id="tnt_a")
    snap_after = pool.snapshot()
    assert snap_before["total"] == snap_after["total"], (
        "warm acquire should not change total VM count"
    )
    assert entry.state == "ACQUIRED"


@pytest.mark.asyncio
async def test_acquire_refuses_above_max_total() -> None:
    pool = Pool(_settings(max_total=2, idle_low_watermark=0))
    await pool.acquire(tenant_id="t1")
    await pool.acquire(tenant_id="t2")
    with pytest.raises(PoolExhaustedError):
        await pool.acquire(tenant_id="t3")


@pytest.mark.asyncio
async def test_release_unknown_raises_not_found() -> None:
    pool = Pool(_settings())
    with pytest.raises(SandboxNotFoundError):
        await pool.release("sb_does_not_exist")


@pytest.mark.asyncio
async def test_release_removes_from_pool() -> None:
    pool = Pool(_settings(idle_low_watermark=0))
    entry = await pool.acquire(tenant_id="t1")
    await pool.release(entry.sandbox_id)
    assert pool.snapshot()["total"] == 0


@pytest.mark.asyncio
async def test_warmup_tick_respects_max_total() -> None:
    pool = Pool(_settings(idle_low_watermark=10, max_total=3))
    spawned = await pool.warmup_tick()
    assert spawned == 3, "warmup must cap at max_total"


@pytest.mark.asyncio
async def test_reap_excess_tick_drops_above_high_watermark() -> None:
    pool = Pool(_settings(idle_low_watermark=8, idle_high_watermark=3, max_total=20))
    await pool.warmup_tick()  # spawns up to idle_low = 8
    reaped = await pool.reap_excess_tick()
    # idle 8 → high 3 → reap 5
    assert reaped == 5
