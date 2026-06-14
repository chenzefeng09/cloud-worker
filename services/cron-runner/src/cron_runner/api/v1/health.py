"""Health endpoints."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel


class HealthStatus(BaseModel):
    status: str = "OK"


router = APIRouter(tags=["Health"])


@router.get("/healthz", response_model=HealthStatus)
async def healthz() -> HealthStatus:
    return HealthStatus()


@router.get("/readyz", response_model=HealthStatus)
async def readyz() -> HealthStatus:
    from cron_runner.domain.db import _engine

    if _engine is not None:
        return HealthStatus()
    return HealthStatus(status="DEGRADED")
