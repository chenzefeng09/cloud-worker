"""Session API endpoints."""

from fastapi import APIRouter, Response
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.api.dependencies import TDbSession, TTenantId, TUserId
from agent_session_orchestrator.schemas.common import CursorPage
from agent_session_orchestrator.schemas.session import SessionCreate, SessionOut, SessionUpdate
from agent_session_orchestrator.services.session_service import SessionService

router = APIRouter(tags=["Sessions"])


@router.get("/sessions", response_model=CursorPage)
async def list_sessions(
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    cursor: str | None = None,
    limit: int = 50,
) -> CursorPage:
    svc = SessionService(db, tenant_id=tenant_id)
    items, next_cursor = await svc.list_sessions(cursor=cursor, limit=limit)
    return CursorPage(
        items=[SessionOut.model_validate(s).model_dump(mode="json") for s in items],
        next_cursor=next_cursor,
    )


@router.post("/sessions", status_code=201, response_model=SessionOut)
async def create_session(
    body: SessionCreate,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> SessionOut:
    svc = SessionService(db, tenant_id=tenant_id)
    session = await svc.create_session(
        user_id=user_id,
        title=body.title,
        metadata=body.agent_config,
    )
    return SessionOut.model_validate(session)


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def get_session(
    session_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> SessionOut:
    svc = SessionService(db, tenant_id=tenant_id)
    session = await svc.get_session(session_id)
    return SessionOut.model_validate(session)


@router.patch("/sessions/{session_id}", response_model=SessionOut)
async def update_session(
    session_id: str,
    body: SessionUpdate,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> SessionOut:
    svc = SessionService(db, tenant_id=tenant_id)
    updates = body.model_dump(exclude_none=True)
    session = await svc.update_session(session_id, **updates)
    return SessionOut.model_validate(session)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> None:
    svc = SessionService(db, tenant_id=tenant_id)
    await svc.delete_session(session_id)
    return Response(status_code=204)
