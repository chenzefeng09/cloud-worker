"""Health probes — never auth-gated."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["Health"])


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "OK", "service": "sandbox-pool", "version": "0.1.0a1"}


@router.get("/readyz")
async def readyz(request: Request) -> dict:
    """Ready when the in-memory pool is reachable.

    Once we wire the CubeSandbox controller, this probe should additionally
    do a cheap ping (e.g. ``GET /version``) and downgrade to ``DEGRADED``
    when the upstream is flapping but the pool still has warm VMs to hand
    out.
    """
    snap = request.app.state.pool.snapshot()
    return {
        "status": "OK",
        "service": "sandbox-pool",
        "pool": snap,
    }
