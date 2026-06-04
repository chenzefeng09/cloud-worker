"""FastAPI dependencies for agent-scheduler.

``current_tenant`` deliberately refuses every fallback to query / body
parameters: tenant id MUST arrive in the ``X-Tenant-Id`` header that the API
gateway injects from the verified bearer token.  Letting a client supply
``?tenant_id=victim`` would defeat the whole tenant model — the scheduler
operates on sandbox containers and a misrouted call can list, stop, or
spawn sandboxes in another tenant's namespace.

The runtime injected into request handlers is resolved through
``SandboxRuntime.build_runtime(settings.runtime)`` — Docker for dev/CI,
CubeSandbox for production.  See ADR 0001.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from agent_scheduler.api.middleware import get_tenant_id, get_user_id

# Side-effect imports: each one registers itself in RUNTIME_REGISTRY at
# import time.  Importing them here keeps registration centralised so the
# ``build_runtime`` lookup later doesn't depend on whatever happens to
# have been imported elsewhere.
from agent_scheduler.services import (
    cube_runtime,  # noqa: F401
    docker_runtime,  # noqa: F401
)
from agent_scheduler.services.runtime import SandboxRuntime, build_runtime


def get_runtime(request: Request) -> SandboxRuntime:
    # Cached on app.state by ``main.create_app`` so we don't rebuild the
    # httpx / docker client on every request.
    runtime: SandboxRuntime = request.app.state.runtime
    return runtime


async def current_tenant() -> str:
    """Return the tenant id from the contextvar; 401 if absent.

    No query / body fallback — see module docstring.
    """
    tid = get_tenant_id()
    if not tid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Tenant-Id header required (must be injected by the API gateway)",
        )
    return tid


async def current_user() -> str:
    """Return user id from contextvar; ``SYSTEM`` only for internal jobs."""
    return get_user_id() or "SYSTEM"


# Re-exported here so test code can monkeypatch ``build_runtime`` if it
# needs a fake runtime, without having to know the registry's import path.
__all__ = [
    "build_runtime",
    "current_tenant",
    "current_user",
    "get_runtime",
    "SandboxRuntime",
    "TRuntime",
    "TTenantId",
    "TUserId",
]


TRuntime = Depends(get_runtime)
TTenantId = Depends(current_tenant)
TUserId = Depends(current_user)
