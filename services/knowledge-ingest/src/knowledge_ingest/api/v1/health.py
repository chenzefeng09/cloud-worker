"""Health endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from knowledge_ingest.schemas.common import HealthStatus

router = APIRouter(tags=["Health"])


@router.get("/healthz", response_model=HealthStatus)
async def healthz() -> HealthStatus:
    return HealthStatus()


@router.get("/readyz", response_model=HealthStatus)
async def readyz() -> HealthStatus:
    from knowledge_ingest.domain.db import _engine

    if _engine is not None:
        return HealthStatus()
    return HealthStatus(status="DEGRADED")
