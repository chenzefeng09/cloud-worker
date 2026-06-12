"""Prometheus metrics for the sandbox pool.

Why we export these
-------------------
The KEDA ScaledObject in ``deploy/operator/keda-scaledobject-sandbox-pool.yaml``
keys off ``sandbox_pool_idle`` to decide when to add replicas; the
cold/warm counter + latency histogram are the operator SLO surface
(``> 80%`` of acquires should be warm, p95 cold spawn ``< 3 s``).

Notes
-----
* We instantiate the collectors against a *custom registry* so tests can
  pass their own and start with zero state.  ``REGISTRY`` (the default
  one) carries leftover counters across tests run in the same process,
  which makes ``test_metrics`` and ``test_api`` fight over the same
  series — passing a registry sidesteps the issue cleanly.
* The exported names follow Prometheus conventions: ``_total`` suffix on
  counters, no suffix on gauges, ``_seconds`` suffix on histograms.
"""

from __future__ import annotations

from dataclasses import dataclass

from prometheus_client import REGISTRY as _DEFAULT_REGISTRY
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# Buckets tuned for sandbox acquire latency.  Warm hits land in the first
# 10ms bucket; cold spawns of a microVM sit somewhere around 500ms–3s on
# real CubeSandbox hardware (see ADR 0001 §6).
_LATENCY_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)


@dataclass(frozen=True)
class PoolMetrics:
    """A bundle of collectors so callers can pass one object around."""

    acquire_total: Counter
    idle: Gauge
    total: Gauge
    acquire_latency: Histogram


def build_metrics(registry: CollectorRegistry | None = None) -> PoolMetrics:
    """Construct a fresh set of collectors on the given registry.

    Pass ``registry=None`` (the default) for production — uses the
    process-wide default registry that ``make_asgi_app`` scrapes.  Pass
    a private :class:`CollectorRegistry` in tests for isolation.
    """
    reg = registry if registry is not None else _DEFAULT_REGISTRY
    return PoolMetrics(
        acquire_total=Counter(
            "sandbox_pool_acquire_total",
            "Acquire attempts partitioned by outcome.",
            labelnames=("outcome", "template", "memory_tier"),
            registry=reg,
        ),
        idle=Gauge(
            "sandbox_pool_idle",
            "Number of IDLE (warm) sandboxes per template and memory tier.",
            labelnames=("template", "memory_tier"),
            registry=reg,
        ),
        total=Gauge(
            "sandbox_pool_total",
            "Total sandboxes managed by the pool (IDLE + ACQUIRED + RECYCLING).",
            registry=reg,
        ),
        acquire_latency=Histogram(
            "sandbox_pool_acquire_latency_seconds",
            "Wall-clock time from acquire() entry to return, per outcome.",
            labelnames=("outcome", "template", "memory_tier"),
            buckets=_LATENCY_BUCKETS,
            registry=reg,
        ),
    )
