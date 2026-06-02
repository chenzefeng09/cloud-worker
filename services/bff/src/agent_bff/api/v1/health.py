"""Health check endpoint."""

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["Health"])


class HealthStatus(BaseModel):
    status: str = "OK"
    service: str = "agent-bff"
    version: str = "0.1.0a1"


@router.get("/healthz", response_model=HealthStatus)
async def healthz() -> HealthStatus:
    return HealthStatus()


@router.get("/readyz", response_model=HealthStatus)
async def readyz() -> HealthStatus:
    return HealthStatus()
