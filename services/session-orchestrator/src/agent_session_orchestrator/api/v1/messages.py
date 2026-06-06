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

Token-level streaming
---------------------
When a run is RUNNING/PENDING the generator first tails the Redis Stream
``agent:run:{run_id}:tokens`` (written by the in-sandbox runtime_worker)
using ``XREAD BLOCK`` with a short timeout so it yields ``event: token``
SSE frames for every delta in real-time.  Once the stream's ``type=end``
sentinel is received (or Redis is unavailable), the generator falls back
to the original polling / reconcile loop that emits the persisted
``event: message`` + ``event: run_complete`` + ``event: done`` frames.

The ``run_id`` used to build the Redis key is ``run.id`` (the orchestrator's
internal UUID), which is the same value sent to the scheduler/sandbox as
the ``run_id`` field in the ``POST /runs`` body and exposed inside the
sandbox as the ``RUN_ID`` environment variable.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from collections.abc import AsyncIterator

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

# XREAD BLOCK timeout in milliseconds — short so we can check overall deadline.
_XREAD_BLOCK_MS = 1500
# Maximum seconds we stay in the token-streaming phase before giving up and
# falling through to the reconcile / polling phase.
_TOKEN_STREAM_MAX_SECONDS = 300.0


async def _stream_tokens(
    *,
    redis_url: str,
    run_id: str,
    deadline: float,
) -> AsyncIterator[str]:
    """Yield ``event: token`` SSE lines from the Redis token stream.

    Tails ``agent:run:{run_id}:tokens`` using ``XREAD BLOCK`` until:
    * a ``type=end`` entry is received, or
    * ``deadline`` (monotonic epoch) is exceeded, or
    * a Redis error occurs (failure-open: logs and returns).

    Each yielded string is a complete SSE frame ready for the wire.
    """
    try:
        import redis.asyncio as aioredis  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("redis.asyncio not available — skipping token stream")
        return

    stream_key = f"agent:run:{run_id}:tokens"
    last_id = "0-0"  # start from the beginning of the stream
    client = aioredis.Redis.from_url(
        redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
        decode_responses=True,
    )
    try:
        while asyncio.get_event_loop().time() < deadline:
            try:
                # XREAD BLOCK returns: {stream_key: [(entry_id, {field: val}), ...]}
                # or None on timeout.
                results = await client.xread(
                    {stream_key: last_id},
                    block=_XREAD_BLOCK_MS,
                    count=100,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Redis XREAD error for run %s (failure-open): %s", run_id, exc
                )
                return

            if not results:
                # BLOCK timeout — loop and check deadline.
                continue

            # results is a list of (stream_key, entries) tuples.
            for _key, entries in results:
                for entry_id, fields in entries:
                    last_id = entry_id
                    entry_type = fields.get("type", "")
                    if entry_type == "end":
                        return
                    elif entry_type == "delta":
                        text = fields.get("text", "")
                        payload = json.dumps({"run_id": run_id, "delta": text})
                        yield f"event: token\ndata: {payload}\n\n"
                    elif entry_type == "tool_call":
                        payload = json.dumps({
                            "run_id": run_id,
                            "kind": "call",
                            "tool_call_id": fields.get("tool_call_id", ""),
                            "name": fields.get("name", ""),
                            "args": fields.get("args", ""),
                        })
                        yield f"event: tool\ndata: {payload}\n\n"
                    elif entry_type == "tool_result":
                        payload = json.dumps({
                            "run_id": run_id,
                            "kind": "result",
                            "tool_call_id": fields.get("tool_call_id", ""),
                            "name": fields.get("name", ""),
                            "result": fields.get("result", ""),
                            "state": fields.get("state", ""),
                        })
                        yield f"event: tool\ndata: {payload}\n\n"
                    elif entry_type == "thinking":
                        text = fields.get("text", "")
                        payload = json.dumps({"run_id": run_id, "text": text})
                        yield f"event: thinking\ndata: {payload}\n\n"
                    # unknown types are silently skipped
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass


async def _read_ordered_blocks_from_redis(
    *,
    redis_url: str,
    run_id: str,
) -> list[dict]:
    """Read the run's token stream and return an ordered list of blocks.

    Processes ``agent:run:{run_id}:tokens`` entries **in stream order** and
    groups them into three kinds of blocks:

    * ``{"kind": "text", "text": <accumulated delta text>}`` — one block per
      run of consecutive ``type=delta`` entries.  A run ends when a non-delta
      entry is encountered.
    * ``{"kind": "tool", "tool_call_id", "name", "args", "input", "result",
      "state"}`` — one block per ``type=tool_call`` entry, with the matching
      ``type=tool_result`` fields merged in if present (they arrive immediately
      after in the stream).
    * ``{"kind": "thinking", "text"}`` — one block per ``type=thinking`` entry.

    Returns ``[]`` on any Redis error (failure-open).
    """
    try:
        import redis.asyncio as aioredis  # type: ignore[import-not-found]
    except ImportError:
        return []

    stream_key = f"agent:run:{run_id}:tokens"
    client = aioredis.Redis.from_url(
        redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
        decode_responses=True,
    )
    try:
        # XRANGE "-" "+" reads the whole stream in one call.
        entries = await client.xrange(stream_key, min="-", max="+")
    except Exception as exc:  # noqa: BLE001
        logger.debug("XRANGE for ordered blocks failed run %s: %s", run_id, exc)
        return []
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass

    blocks: list[dict] = []
    current_text: list[str] = []  # accumulator for current text block

    def _flush_text() -> None:
        """Seal the current text accumulator into a text block if non-empty."""
        if current_text:
            blocks.append({"kind": "text", "text": "".join(current_text)})
            current_text.clear()

    for _entry_id, fields in entries:
        entry_type = fields.get("type", "")

        if entry_type == "delta":
            current_text.append(fields.get("text", ""))

        elif entry_type == "tool_call":
            _flush_text()
            tc_id = fields.get("tool_call_id", "")
            raw_args = fields.get("args", "")
            # Parse args JSON to object if possible; keep raw string on failure.
            try:
                parsed_input = json.loads(raw_args) if raw_args else {}
            except (json.JSONDecodeError, TypeError):
                parsed_input = raw_args
            blocks.append({
                "kind": "tool",
                "tool_call_id": tc_id,
                "name": fields.get("name", ""),
                "args": raw_args,
                "input": parsed_input,
                # result/state filled in by the paired tool_result below
            })

        elif entry_type == "tool_result":
            # Pair with the most recent unresolved tool block in-place.
            tc_id = fields.get("tool_call_id", "")
            for blk in reversed(blocks):
                if blk.get("kind") == "tool" and blk.get("tool_call_id") == tc_id:
                    blk["result"] = fields.get("result", "")
                    blk["state"] = fields.get("state", "")
                    break

        elif entry_type == "thinking":
            _flush_text()
            blocks.append({"kind": "thinking", "text": fields.get("text", "")})

        elif entry_type == "end":
            break

    _flush_text()
    return blocks


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
    redis_url: str | None = None,
) -> bool:
    """Poll the scheduler for a RUNNING run; persist result on terminal exit.

    Returns ``True`` when the run reached a terminal state and the DB has
    been updated (so the SSE loop can emit the run_complete event on the
    same iteration).  Best-effort: scheduler errors are logged and treated
    as "still running" so the next poll retries.
    """
    if not run.sandbox_id or run.status not in ("RUNNING", "PENDING"):
        return run.status in ("COMPLETED", "FAILED", "CANCELLED")

    # The scheduler's RBAC guard rejects ``X-User-Id: SYSTEM`` (fail-closed:
    # it only accepts real ``usr_*`` principals).  This is a background
    # reconcile poll with no inbound user request, so we authenticate as the
    # user who started the run — Session carries ``user_id`` and Run links to
    # Session.  Reusing the initiator's identity keeps the existing RBAC chain
    # intact (no SYSTEM bypass) and keeps the audit trail attributed to them.
    try:
        creator = (await svc.get_session(run.session_id)).user_id
    except Exception as exc:  # noqa: BLE001 — failure-open; retry next poll
        logger.warning(
            "reconcile could not resolve run initiator",
            extra={"run_id": run.id, "err": str(exc)},
        )
        return False

    # Scheduler's ``GET /runs/{id}`` uses the path segment as the Docker
    # container id (its ``sandbox_id``), falling back to ``run_id`` only
    # when an explicit body provides ``sandbox_id``.  Passing the short
    # container id directly skips that body-only path and works with
    # httpx's GET (which doesn't send a JSON body).
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{scheduler_url}/runs/{run.sandbox_id}",
                headers={"X-Tenant-Id": tenant_id, "X-User-Id": creator},
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

    usage = payload.get("usage") or {}
    run.status = sched_status
    run.result_summary = summary
    run.error_message = error
    run.finished_at = dt.datetime.now(tz=dt.UTC)
    run.input_tokens = usage.get("input_tokens")
    run.output_tokens = usage.get("output_tokens")
    run.total_tokens = usage.get("total_tokens")

    # --- Interleaved multi-message persistence ---
    #
    # Strategy: read the Redis stream in full and reconstruct the ordered
    # block list (text / tool / thinking).  Persist each block as its own
    # DB message in stream order so that a page-refresh hydrates the
    # transcript in the same interleaved sequence the user saw live.
    #
    # Idempotency: if ANY assistant or tool message for this run_id already
    # exists in the DB we treat the entire batch as already persisted and
    # skip all creation.  This covers repeated reconcile calls from both
    # list_messages and the SSE poll loop.
    #
    # Fallback: if Redis returns no blocks (stream expired / empty), fall
    # back to the old behaviour — create a single assistant message from
    # result_summary so there is always at least a final reply.

    # --- Check idempotency first ---
    existing_messages, _ = await svc.list_messages(run.session_id, limit=100)
    already_persisted = any(
        getattr(m, "role", "").upper() in ("ASSISTANT", "TOOL")
        and getattr(m, "run_id", None) == run.id
        for m in existing_messages
    )
    if already_persisted:
        # Batch already in DB — nothing to do.
        pass
    else:
        # Try to build the ordered block list from Redis.
        blocks: list[dict] = []
        if redis_url:
            try:
                blocks = await _read_ordered_blocks_from_redis(
                    redis_url=redis_url,
                    run_id=run.id,
                )
            except Exception as exc:  # noqa: BLE001 — failure-open
                logger.debug(
                    "ordered blocks read skipped (failure-open) for run %s: %s",
                    run.id, exc,
                )

        if blocks:
            # Persist each block as a separate message in stream order.
            # Serial awaits guarantee sequence numbers are assigned in order.
            for blk in blocks:
                kind = blk.get("kind")
                if kind == "text":
                    text = blk.get("text", "")
                    if not text:
                        continue
                    await svc.create_message(
                        session_id=run.session_id,
                        role="assistant",
                        content=text,
                        tool_calls=None,
                        run_id=run.id,
                    )
                elif kind == "tool":
                    name = blk.get("name", "")
                    tool_record = {
                        "id": blk.get("tool_call_id", ""),
                        "name": name,
                        "input": blk.get("input", blk.get("args", "")),
                        "result": blk.get("result", ""),
                        "state": blk.get("state", ""),
                    }
                    await svc.create_message(
                        session_id=run.session_id,
                        role="tool",
                        content=name,
                        tool_calls=[tool_record],
                        run_id=run.id,
                    )
                elif kind == "thinking":
                    # Thinking blocks are skipped for DB persistence.
                    # Rationale: the frontend renders thinking in real-time via
                    # SSE; after a page reload the thinking content is not
                    # critical to reconstruct (users can live without it), and
                    # storing large thinking blobs inflates the transcript.
                    # text/tool interleaving is the higher-priority goal.
                    continue
                # Unknown kinds silently skipped.
        else:
            # Fallback: Redis empty / unavailable — write a single assistant
            # message from the scheduler's result_summary so there is always
            # a final reply.
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
        redis_url_for_reconcile = getattr(request.app.state.settings, "redis_url", None)
        for run in runs:
            await _reconcile_run_completion(
                scheduler_url=scheduler_url,
                tenant_id=tenant_id,
                run=run,
                svc=svc,
                knowledge_ingest_url=knowledge_ingest_url,
                redis_url=redis_url_for_reconcile,
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


@router.delete("/sessions/{session_id}/messages",
               dependencies=[Depends(require_permission("session.message.delete", "session"))])
async def delete_messages_from_sequence(
    session_id: str,
    from_sequence: int,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> dict:
    svc = SessionService(db, tenant_id=tenant_id)
    deleted_count = await svc.delete_messages_from_sequence(session_id, from_sequence)
    return {"deleted_count": deleted_count}


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
        attachment_ids=body.attachment_ids or [],
    )
    return MessageOut.model_validate(msg)


@router.get("/sessions/{session_id}/messages/stream",
            dependencies=[Depends(require_permission("session.message.read", "session"))])
async def stream_messages(
    session_id: str,
    request: Request,
    tenant_id: str = TTenantId,
) -> StreamingResponse:
    """SSE stream — real-time token deltas followed by persisted message + completion events.

    Phase 1 (token streaming):
        If there is an active (RUNNING/PENDING) run *and* Redis is configured,
        tail ``agent:run:{run_id}:tokens`` and emit ``event: token`` frames for
        every delta.  This phase ends when the stream's ``type=end`` sentinel
        arrives, the deadline is hit, or Redis is unavailable (failure-open:
        falls straight through to phase 2).

    Phase 2 (reconcile + persistent message):
        Poll the scheduler at ``_POLL_INTERVAL_SECONDS`` intervals, reconcile
        run completion (persist status + assistant message), and emit
        ``event: message`` (full message with DB id), ``event: run_complete``,
        and ``event: done``.  This guarantees the client always receives a
        complete, id-bearing message it can use to dedup/replace any
        optimistic token-stream placeholder.
    """
    scheduler_url = request.app.state.settings.agent_scheduler_url
    knowledge_ingest_url = request.app.state.settings.knowledge_ingest_url
    redis_url: str | None = request.app.state.settings.redis_url

    async def event_generator():  # noqa: C901 — complexity is inherent here
        last_seq = 0
        try:
            # ------------------------------------------------------------------
            # Phase 1: token-level streaming via Redis Stream
            # ------------------------------------------------------------------
            # Resolve the run_id to tail *before* streaming.
            #
            # Selection logic (handles the "fast-run race"):
            #   1. Take the most-recent run for this session (list_runs returns
            #      created_at DESC so runs[0] is newest).
            #   2. Tail its token stream only when EITHER:
            #      a) the run is still active (RUNNING / PENDING) — the normal
            #         path for longer tasks, or
            #      b) the run just completed but its assistant message is not yet
            #         in the messages table — the fast-run race condition: the
            #         sandbox finished in < 1 s, so by the time we connect the
            #         run is already COMPLETED but the token stream (TTL ~1800 s)
            #         still holds all deltas; replaying it gives the user the
            #         streaming feel.
            #   3. If the run is terminal AND its assistant message already
            #      exists in the DB we are dealing with a re-opened historical
            #      session (reconnect / page refresh after completion).  Skip
            #      Phase 1 entirely so we don't replay a wall of old tokens.
            active_run_id: str | None = None
            if redis_url:
                try:
                    async for db in acquire_db_session(tenant_id=tenant_id):
                        svc = SessionService(db, tenant_id=tenant_id)
                        runs = await svc.list_runs(session_id)
                        # list_runs returns created_at DESC — runs[0] is newest.
                        latest_run = runs[0] if runs else None
                        if latest_run is not None:
                            if latest_run.status in ("RUNNING", "PENDING"):
                                # Normal active run: always tail.
                                active_run_id = latest_run.id
                            elif latest_run.status in ("COMPLETED", "FAILED", "CANCELLED"):
                                # Terminal run: only tail if no assistant message
                                # for this run exists yet (fast-run race window).
                                messages, _ = await svc.list_messages(session_id, limit=50)
                                has_assistant_msg = any(
                                    m.role in ("assistant", "ASSISTANT")
                                    and getattr(m, "run_id", None) == latest_run.id
                                    for m in messages
                                )
                                if not has_assistant_msg:
                                    # Fast-run race: token stream likely has
                                    # deltas but the message hasn't landed in DB
                                    # yet.  Replay the stream so the user sees
                                    # streaming output; the end sentinel ensures
                                    # we exit Phase 1 quickly.
                                    active_run_id = latest_run.id
                                # else: historical re-open — skip Phase 1.
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "token-stream: could not resolve active run (failure-open): %s", exc
                    )

            if active_run_id and redis_url:
                deadline = (
                    asyncio.get_event_loop().time() + _TOKEN_STREAM_MAX_SECONDS
                )
                async for token_event in _stream_tokens(
                    redis_url=redis_url,
                    run_id=active_run_id,
                    deadline=deadline,
                ):
                    yield token_event

            # ------------------------------------------------------------------
            # Phase 2: reconcile + poll loop (original logic, unchanged)
            # ------------------------------------------------------------------
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
                                redis_url=redis_url,
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
