"""Cold/warm/failed telemetry contract for ``Pool.acquire``.

The KEDA ScaledObject relies on ``sandbox_pool_idle`` and the alerting
SLO relies on ``sandbox_pool_acquire_total{outcome=...}``; these tests
pin both so a future pool refactor can't silently drop them.

Uses a mock runtime so no Docker daemon is required.
"""

from __future__ import annotations

import itertools
from unittest.mock import MagicMock

import pytest
from prometheus_client import CollectorRegistry

from sandbox_pool.config import Settings
from sandbox_pool.errors import PoolExhaustedError
from sandbox_pool.metrics import PoolMetrics, build_metrics
from sandbox_pool.pool import Pool


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
        "sandbox_image": "test-image:latest",
        "docker_network": "test_net",
        # Single "small" bucket with no per-tier keys → legacy watermark
        # overrides flow through via fallback.
        "memory_tier_watermarks": {"small": {}},
    }
    defaults.update(over)
    return Settings(**defaults)  # type: ignore[arg-type]


def _make_mock_runtime() -> MagicMock:
    runtime = MagicMock()
    _counter = itertools.count()
    runtime.start_idle_container.side_effect = (
        lambda template, memory_limit=None: f"{next(_counter):012x}"
    )
    return runtime


def _make_pool(**over: object) -> tuple[Pool, PoolMetrics]:
    """Build a Pool with an isolated registry so counters start at 0."""
    registry = CollectorRegistry()
    metrics = build_metrics(registry)
    pool = Pool(_settings(**over), metrics=metrics, runtime=_make_mock_runtime())
    return pool, metrics


def _counter(
    metrics: PoolMetrics, outcome: str, template: str = "base",
    memory_tier: str = "small",
) -> float:
    return metrics.acquire_total.labels(
        outcome=outcome, template=template, memory_tier=memory_tier,
    )._value.get()


def _histogram_count(
    metrics: PoolMetrics, outcome: str, template: str = "base",
    memory_tier: str = "small",
) -> tuple[float, float]:
    """Total observation count for the latency histogram label combo."""
    child = metrics.acquire_latency.labels(
        outcome=outcome, template=template, memory_tier=memory_tier,
    )
    return child._sum.get(), sum(b.get() for b in child._buckets)


# --------------------------------------------------------------------------- #
# The three outcomes
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_acquire_cold_increments_cold_counter() -> None:
    pool, metrics = _make_pool(idle_low_watermark=0)
    # Pool empty → first acquire cold-spawns.
    await pool.acquire(tenant_id="tnt_a")
    assert _counter(metrics, "cold") == 1.0
    assert _counter(metrics, "warm") == 0.0
    assert _counter(metrics, "failed") == 0.0
    # Latency histogram observed exactly once for this label combo.
    _sum, count = _histogram_count(metrics, "cold")
    assert count == 1


@pytest.mark.asyncio
async def test_acquire_warm_increments_warm_counter() -> None:
    pool, metrics = _make_pool()
    # Pre-warm so there's an IDLE container ready.
    await pool.warmup_tick()
    await pool.acquire(tenant_id="tnt_a")
    assert _counter(metrics, "warm") == 1.0
    assert _counter(metrics, "cold") == 0.0
    assert _counter(metrics, "failed") == 0.0
    _sum, count = _histogram_count(metrics, "warm")
    assert count == 1


@pytest.mark.asyncio
async def test_acquire_failed_increments_failed_counter() -> None:
    pool, metrics = _make_pool(max_total=1, idle_low_watermark=0)
    await pool.acquire(tenant_id="t1")
    with pytest.raises(PoolExhaustedError):
        await pool.acquire(tenant_id="t2")
    assert _counter(metrics, "failed") == 1.0
    # The successful first acquire still counted as cold.
    assert _counter(metrics, "cold") == 1.0
    _sum, count = _histogram_count(metrics, "failed")
    assert count == 1


# --------------------------------------------------------------------------- #
# Gauges track pool state
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_idle_and_total_gauges_track_pool_state() -> None:
    pool, metrics = _make_pool(idle_low_watermark=3, warmup_templates=["base"])
    await pool.warmup_tick()
    assert metrics.idle.labels(template="base", memory_tier="small")._value.get() == 3.0
    assert metrics.total._value.get() == 3.0

    await pool.acquire(tenant_id="tnt_a")  # one IDLE → ACQUIRED
    assert metrics.idle.labels(template="base", memory_tier="small")._value.get() == 2.0
    # total unchanged — warm acquire doesn't spawn.
    assert metrics.total._value.get() == 3.0
