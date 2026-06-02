"""Health check endpoints."""

from fastapi import APIRouter

from agent_session_orchestrator.schemas.common import HealthStatus

router = APIRouter(tags=["Health"])


@router.get("/healthz", response_model=HealthStatus)
async def healthz() -> HealthStatus:
    return HealthStatus()


@router.get("/readyz", response_model=HealthStatus)
async def readyz() -> HealthStatus:
    try:
        from agent_session_orchestrator.domain.db import _engine
        if _engine is not None:
            return HealthStatus()
    except Exception:
        pass
    return HealthStatus(status="DEGRADED")
