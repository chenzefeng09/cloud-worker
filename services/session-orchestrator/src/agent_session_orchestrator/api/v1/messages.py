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
import datetime as dt
import json
import logging

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.api.authz_guard import require_permission
from agent_session_orchestrator.api.dependencies import TDbSession, TTenantId
from agent_session_orchestrator.domain.db import get_session as acquire_db_session
from agent_session_orchestrator.schemas.common import CursorPage
from agent_session_orchestrator.schemas.message import MessageCreate, MessageOut
from agent_session_orchestrator.services.session_service import SessionService

router = APIRouter(tags=["Messages"])
logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 1.0
_MAX_POLLS = 300  # 5 minutes wall-clock at 1 s/poll


async def _write_memory(
    *,
    knowledge_ingest_url: str,
    tenant_id: str,
    task_description: str,
    summary: str,
) -> None:
    """Write a memory document to knowledge-ingest after run completion.

    The document is a brief Q&A pair: user task + assistant summary.  It is
    uploaded as ``text/plain``, then immediately ingested (embedded) so it
    becomes searchable.  Failure-open: any error is logged at DEBUG level
    and the caller is not affected.

    Two HTTP calls:
      1. ``POST /documents`` (multipart) → document_id
      2. ``POST /documents/{id}/ingest`` → triggers embedding
    """
    if not (knowledge_ingest_url and task_description and summary):
        return
    # Truncate to keep the document small.
    snippet_task = task_description[:300].strip()
    snippet_summary = summary[:600].strip()
    content = f"用户任务：{snippet_task}\n\n助理回答：{snippet_summary}"
    title = f"[memory] {snippet_task[:80]}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            headers = {"X-Tenant-Id": tenant_id, "X-User-Id": "SYSTEM"}
            upload_resp = await client.post(
                f"{knowledge_ingest_url}/documents",
                headers=headers,
                data={"title": title, "mime": "text/plain"},
                files={"file": ("memory.txt", content.encode("utf-8"), "text/plain")},
            )
            if upload_resp.status_code not in (200, 201):
                logger.debug(
                    "memory upload failed HTTP %s", upload_resp.status_code,
                )
                return
            doc_id = upload_resp.json().get("id")
            if not doc_id:
                return
            ingest_resp = await client.post(
                f"{knowledge_ingest_url}/documents/{doc_id}/ingest",
                headers=headers,
            )
            if ingest_resp.status_code not in (200, 201):
                logger.debug(
                    "memory ingest failed HTTP %s", ingest_resp.status_code,
                )
    except Exception as exc:  # noqa: BLE001
        logger.debug("memory write failed (failure-open): %s", exc)


async def _reconcile_run_completion(
    *,
    scheduler_url: str,
    tenant_id: str,
    run,
    svc: SessionService,
    knowledge_ingest_url: str = "",
) -> bool:
    """Poll the scheduler for a RUNNING run; persist result on terminal exit.

    Returns ``True`` when the run reached a terminal state and the DB has
    been updated (so the SSE loop can emit the run_complete event on the
    same iteration).  Best-effort: scheduler errors are logged and treated
    as "still running" so the next poll retries.
    """
    if not run.sandbox_id or run.status not in ("RUNNING", "PENDING"):
        return run.status in ("COMPLETED", "FAILED", "CANCELLED")

    # Scheduler's ``GET /runs/{id}`` uses the path segment as the Docker
    # container id (its ``sandbox_id``), falling back to ``run_id`` only
    # when an explicit body provides ``sandbox_id``.  Passing the short
    # container id directly skips that body-only path and works with
    # httpx's GET (which doesn't send a JSON body).
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{scheduler_url}/runs/{run.sandbox_id}",
                headers={"X-Tenant-Id": tenant_id, "X-User-Id": "SYSTEM"},
            )
    except httpx.HTTPError as exc:
        logger.warning("scheduler poll failed", extra={"run_id": run.id, "err": str(exc)})
        return False

    if resp.status_code != 200:
        return False

    payload = resp.json()
    sched_status = payload.get("status")
    if sched_status not in ("COMPLETED", "FAILED"):
        return False

    summary = payload.get("result_summary")
    error = payload.get("result_error")
    artifacts = payload.get("artifacts") or []

    run.status = sched_status
    run.result_summary = summary
    run.error_message = error
    run.finished_at = dt.datetime.now(tz=dt.UTC)

    # Write an assistant message so the UI sees the reply.
    if summary:
        await svc.create_message(
            session_id=run.session_id,
            role="assistant",
            content=summary,
            tool_calls=None,
            run_id=run.id,
        )

    # Persist artifact metadata as output attachments (kind=output).
    # Failure-open: any error is logged; run completion is not affected.
    if artifacts:
        await _persist_artifacts(
            artifacts=artifacts,
            run=run,
            svc=svc,
            tenant_id=tenant_id,
        )

    # Persist a memory document to knowledge-ingest so future runs can
    # retrieve relevant context.  Awaited inline (failure-open): any error
    # is caught inside _write_memory and logged at DEBUG level, so this
    # never blocks or raises on the reconcile caller.
    if summary and knowledge_ingest_url:
        await _write_memory(
            knowledge_ingest_url=knowledge_ingest_url,
            tenant_id=tenant_id,
            task_description=run.task_description or "",
            summary=summary,
        )

    return True


async def _persist_artifacts(
    *,
    artifacts: list[dict],
    run,
    svc: SessionService,
    tenant_id: str,
) -> None:
    """Store artifact metadata as kind=output Attachment rows.

    ``artifacts`` is the list returned by the scheduler:
        [{"filename": str, "s3_key": str, "size": int, "mime": str}, ...]

    Failure-open: any exception is logged and swallowed.
    """
    try:
        for art in artifacts:
            filename = art.get("filename", "artifact")
            s3_key = art.get("s3_key", "")
            size = int(art.get("size", 0))
            mime = art.get("mime", "application/octet-stream")
            if not s3_key:
                continue
            await svc.create_attachment(
                run.session_id,
                user_id="SYSTEM",
                filename=filename,
                mime=mime,
                size_bytes=size,
                blob_key=s3_key,
                kind="output",
                run_id=run.id,
            )
        logger.debug(
            "persisted %d artifact(s) for run %s", len(artifacts), run.id
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("artifact persist failed (failure-open): %s", exc)


@router.get("/sessions/{session_id}/messages", response_model=CursorPage,
            dependencies=[Depends(require_permission("session.message.read", "session"))])
async def list_messages(
    session_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    cursor: str | None = None,
    limit: int = 50,
) -> CursorPage:
    svc = SessionService(db, tenant_id=tenant_id)
    # Opportunistically reconcile any RUNNING run on the session before
    # returning the transcript.  This is how a page refresh after the SSE
    # stream closed early still surfaces the assistant reply: the next GET
    # /messages picks up the terminal status from the scheduler and inserts
    # the assistant message.
    try:
        runs = await svc.list_runs(session_id)
        scheduler_url = request.app.state.settings.agent_scheduler_url
        knowledge_ingest_url = request.app.state.settings.knowledge_ingest_url
        for run in runs:
            await _reconcile_run_completion(
                scheduler_url=scheduler_url,
                tenant_id=tenant_id,
                run=run,
                svc=svc,
                knowledge_ingest_url=knowledge_ingest_url,
            )
        # db session is managed by the dependency wrapper; flushing makes the
        # newly-added assistant message visible to the subsequent list_messages
        # query without us double-committing the outer transaction.
        await db.flush()
    except Exception as exc:  # best-effort — never block the transcript fetch
        logger.warning("reconcile in list_messages failed", extra={"err": str(exc)})
    items, next_cursor = await svc.list_messages(session_id, cursor=cursor, limit=limit)
    return CursorPage(
        items=[MessageOut.model_validate(m).model_dump(mode="json") for m in items],
        next_cursor=next_cursor,
    )


@router.post("/sessions/{session_id}/messages", status_code=201, response_model=MessageOut,
             dependencies=[Depends(require_permission("session.message.create", "session"))])
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


@router.get("/sessions/{session_id}/messages/stream",
            dependencies=[Depends(require_permission("session.message.read", "session"))])
async def stream_messages(
    session_id: str,
    request: Request,
    tenant_id: str = TTenantId,
) -> StreamingResponse:
    """Poll-based SSE — surfaces new messages and run completion."""
    scheduler_url = request.app.state.settings.agent_scheduler_url
    knowledge_ingest_url = request.app.state.settings.knowledge_ingest_url

    async def event_generator():
        last_seq = 0
        try:
            for _ in range(_MAX_POLLS):
                # Per-poll DB session: collect events into a local buffer,
                # let the surrounding ``async with session.begin()`` commit
                # cleanly, and ONLY THEN yield to the SSE client.  Yielding
                # inside the ``async for db`` would let a client disconnect
                # raise GeneratorExit mid-transaction -- starlette/anyio
                # would invoke ``aclose()`` on the generator, the ``async
                # with`` would see an exception, and SQLAlchemy would
                # ``ROLLBACK`` the just-written assistant message.
                events: list[str] = []
                terminal_seen = False
                async for db in acquire_db_session(tenant_id=tenant_id):
                    svc = SessionService(db, tenant_id=tenant_id)

                    # Before reading state, reconcile RUNNING runs against
                    # the scheduler so terminal exits get persisted (status,
                    # result_summary) and an assistant message lands in the
                    # messages table for the UI.
                    runs = await svc.list_runs(session_id)
                    for r in runs:
                        if r.status in ("RUNNING", "PENDING"):
                            await _reconcile_run_completion(
                                scheduler_url=scheduler_url,
                                tenant_id=tenant_id,
                                run=r,
                                svc=svc,
                                knowledge_ingest_url=knowledge_ingest_url,
                            )

                    items, _ = await svc.list_messages(
                        session_id, cursor=str(last_seq) if last_seq else None, limit=50
                    )
                    for msg in items:
                        # model_dump_json combines validate + JSON encode in
                        # one pass; the previous model_dump(mode="json") +
                        # json.dumps doubled the serialisation cost.
                        msg_json = MessageOut.model_validate(msg).model_dump_json()
                        events.append(f"event: message\ndata: {msg_json}\n\n")
                        last_seq = msg.sequence

                    # Run-completion bookkeeping (re-read after reconcile).
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
                        events.append(f"event: run_complete\ndata: {run_payload}\n\n")
                        events.append("event: done\ndata: {}\n\n")
                        terminal_seen = True

                # DB session has now closed + committed (or rolled back on
                # an exception); safe to flush the buffered events to the
                # client.
                for ev in events:
                    yield ev
                if terminal_seen:
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
