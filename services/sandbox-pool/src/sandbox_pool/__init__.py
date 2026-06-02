"""sandbox-pool — CubeSandbox microVM pre-warm pool.

See ``docs/design/b-m2-sandbox-runtime-v1.md`` for the full design.  This
service exists separately from ``agent-scheduler`` because the pool's
lifecycle (slow warm-up, long-running idle VMs) doesn't fit the scheduler's
short-lived per-request shape.
"""
