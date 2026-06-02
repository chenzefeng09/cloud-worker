"""Message API endpoints.

SSE stream
----------
``GET /sessions/{id}/messages/stream`` is a polling SSE stream that wakes
once per second to surface new messages and run state changes.  Two
discipline points the earlier implementation got wrong:

1. **Each poll opens its own DB session.**  The FastAPI dependency that
   injected ``db: AsyncSession`` was bound to the *outer* request and held
   one transaction (with one ``SET LOCAL``) for the entire 5-minute
   lifetime of the generator.  That violated the transaction model
   established by ``domain.db.get_session`` (``async with session.begin()``)
   and would have leaked DB connections under load.  We now acquire a
   fresh short-lived session per poll via the same ``get_session`` factory.

2. **Errors never leak ``str(exc)``.**  An unhandled exception in the
   generator surfaces only a generic ``"event stream terminated"`` line;
   the real exception is logged by the FastAPI exception handler with the
   request id so ops can correlate.

LISTEN/NOTIFY or Redis pub/sub is the long-term replacement (tracked in
B-M2 backlog) — once the publisher side exists this endpoint can drop the
polling loop entirely.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.api.dependencies import TDbSession, TTenantId
from agent_session_orchestrator.domain.db import get_session as acquire_db_session
from agent_session_orchestrator.schemas.common import CursorPage
from agent_session_orchestrator.schemas.message import MessageCreate, MessageOut
from agent_session_orchestrator.services.session_service import SessionService

router = APIRouter(tags=["Messages"])
logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 1.0
_MAX_POLLS = 300  # 5 minutes wall-clock at 1 s/poll


@router.get("/sessions/{session_id}/messages", response_model=CursorPage)
async def list_messages(
    session_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    cursor: str | None = None,
    limit: int = 50,
) -> CursorPage:
    svc = SessionService(db, tenant_id=tenant_id)
    items, next_cursor = await svc.list_messages(session_id, cursor=cursor, limit=limit)
    return CursorPage(
        items=[MessageOut.model_validate(m).model_dump(mode="json") for m in items],
        next_cursor=next_cursor,
    )


@router.post("/sessions/{session_id}/messages", status_code=201, response_model=MessageOut)
async def create_message(
    session_id: str,
    body: MessageCreate,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> MessageOut:
    svc = SessionService(db, tenant_id=tenant_id)
    msg = await svc.create_message(
        session_id=session_id,
        role=body.role.value if hasattr(body.role, "value") else body.role,
        content=body.content,
        tool_calls=body.tool_calls,
        run_id=body.run_id,
    )
    return MessageOut.model_validate(msg)


@router.get("/sessions/{session_id}/messages/stream")
async def stream_messages(
    session_id: str,
    tenant_id: str = TTenantId,
) -> StreamingResponse:
    """Poll-based SSE — surfaces new messages and run completion."""

    async def event_generator():
        last_seq = 0
        try:
            for _ in range(_MAX_POLLS):
                # Fresh session per poll so each iteration has its own
                # transaction + SET LOCAL.  Pool churn is acceptable at
                # 1 connection/sec/active stream; LISTEN/NOTIFY removes it.
                async for db in acquire_db_session(tenant_id=tenant_id):
                    svc = SessionService(db, tenant_id=tenant_id)

                    items, _ = await svc.list_messages(
                        session_id, cursor=str(last_seq) if last_seq else None, limit=50
                    )
                    for msg in items:
                        # model_dump_json combines validate + JSON encode in
                        # one pass; the previous model_dump(mode="json") +
                        # json.dumps doubled the serialisation cost.
                        msg_json = MessageOut.model_validate(msg).model_dump_json()
                        yield f"event: message\ndata: {msg_json}\n\n"
                        last_seq = msg.sequence

                    # Run-completion bookkeeping
                    runs = await svc.list_runs(session_id)
                    terminal = next(
                        (r for r in runs if r.status in ("COMPLETED", "FAILED", "CANCELLED")),
                        None,
                    )
                    if terminal is not None:
                        run_payload = json.dumps(
                            {
                                "run_id": terminal.id,
                                "status": terminal.status,
                                "result_summary": terminal.result_summary,
                            },
                        )
                        yield f"event: run_complete\ndata: {run_payload}\n\n"
                        yield "event: done\ndata: {}\n\n"
                        return

                await asyncio.sleep(_POLL_INTERVAL_SECONDS)

            yield "event: done\ndata: {}\n\n"
        except asyncio.CancelledError:
            # Client disconnected; nothing to do — the surrounding
            # ``async for`` already closed the last DB session.
            raise
        except Exception:
            logger.exception(
                "messages SSE stream terminated unexpectedly",
                extra={"session_id": session_id, "tenant_id": tenant_id},
            )
            # Do NOT echo str(exc); a 500-class error may include SQL text
            # or path info that the client must not see.
            yield (
                f"event: error\ndata: "
                f"{json.dumps({'detail': 'event stream terminated; see request id'})}\n\n"
            )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
