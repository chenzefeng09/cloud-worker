"""Session API endpoints."""

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.api.authz_guard import require_permission
from agent_session_orchestrator.api.dependencies import TDbSession, TTenantId, TUserId
from agent_session_orchestrator.schemas.common import CursorPage
from agent_session_orchestrator.schemas.session import SessionCreate, SessionOut, SessionUpdate
from agent_session_orchestrator.services.session_service import SessionService

router = APIRouter(tags=["Sessions"])


@router.get("/sessions", response_model=CursorPage,
            dependencies=[Depends(require_permission("session.read", "session"))])
async def list_sessions(
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
    cursor: str | None = None,
    limit: int = 50,
) -> CursorPage:
    # IDOR guard: a normal caller may only list *their own* sessions.  The
    # admin replay path (BFF, gated on a tenant_admin permission) forwards
    # ``X-Actor: ADMIN`` — the same bypass signal ``require_permission`` honours
    # — and is allowed to list every session in the tenant.  We never widen
    # past the tenant boundary in either case.
    is_admin = request.headers.get("X-Actor") == "ADMIN"
    owner_filter = None if is_admin else user_id
    svc = SessionService(db, tenant_id=tenant_id)
    items, next_cursor = await svc.list_sessions(
        user_id=owner_filter, cursor=cursor, limit=limit
    )
    return CursorPage(
        items=[SessionOut.model_validate(s).model_dump(mode="json") for s in items],
        next_cursor=next_cursor,
    )


@router.post("/sessions", status_code=201, response_model=SessionOut,
             dependencies=[Depends(require_permission("session.create", "session"))])
async def create_session(
    body: SessionCreate,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> SessionOut:
    quota_guard = request.app.state.quota_guard
    await quota_guard.check(tenant_id=tenant_id, dimension="sessions", requested=1)

    svc = SessionService(db, tenant_id=tenant_id)
    session = await svc.create_session(
        user_id=user_id,
        title=body.title,
        metadata=body.agent_config,
    )

    await quota_guard.record(
        tenant_id=tenant_id,
        dimension="sessions",
        amount=1.0,
        description=f"create session {session.id}",
    )
    return SessionOut.model_validate(session)


@router.get("/sessions/{session_id}", response_model=SessionOut,
            dependencies=[Depends(require_permission("session.read", "session"))])
async def get_session(
    session_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> SessionOut:
    svc = SessionService(db, tenant_id=tenant_id)
    session = await svc.get_session(session_id)
    return SessionOut.model_validate(session)


@router.patch("/sessions/{session_id}", response_model=SessionOut,
              dependencies=[Depends(require_permission("session.update", "session"))])
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


@router.delete("/sessions/{session_id}", status_code=204,
               dependencies=[Depends(require_permission("session.delete", "session"))])
async def delete_session(
    session_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> Response:
    svc = SessionService(db, tenant_id=tenant_id)
    await svc.delete_session(session_id)
    return Response(status_code=204)
