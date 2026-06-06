"""Health endpoints — /healthz and /readyz."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel


class HealthStatus(BaseModel):
    status: str = "OK"
    service: str = "web-fetch-gateway"
    version: str = "0.1.0a1"


router = APIRouter(tags=["Health"])


@router.get("/healthz", response_model=HealthStatus)
async def healthz() -> HealthStatus:
    return HealthStatus()


@router.get("/readyz", response_model=HealthStatus)
async def readyz() -> HealthStatus:
    # No DB dependency — readyz is equivalent to healthz.
    return HealthStatus()
