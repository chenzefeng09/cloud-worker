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
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.api.authz_guard import require_permission
from agent_session_orchestrator.api.dependencies import TDbSession, TTenantId
from agent_session_orchestrator.domain.db import get_session as acquire_db_session
from agent_session_orchestrator.domain.models import Run
from agent_session_orchestrator.schemas.common import CursorPage
from agent_session_orchestrator.schemas.message import MessageCreate, MessageOut
from agent_session_orchestrator.services.session_service import SessionService

router = APIRouter(tags=["Messages"])
logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 1.0
_MAX_POLLS = 300  # 5 minutes wall-clock at 1 s/poll
_MAX_TOOL_TEXT_CHARS = 8000
# Grace window for the FIRST token when tailing an active run.  The agent worker
# runs in a freshly-spawned Docker container that cold-starts (pull/import/model
# connect) for several seconds before writing its first delta.  Without this,
# _stream_tokens would give up after ~2 idle reads and the client would never see
# the live token stream — only the all-at-once DB-polled reply.  Far larger than a
# real cold start; it only guards against a worker that never emits anything.
_WORKER_COLDSTART_GRACE_SECONDS = 90.0


def _run_token_stream_key(run_id: str) -> str:
    return f"agent:run:{run_id}:tokens"


def _sanitize_tool_text(value: Any, *, limit: int = _MAX_TOOL_TEXT_CHARS) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\x00", "")
    if len(text) > limit:
        omitted = len(text) - limit
        text = f"{text[:limit]}\n...[truncated {omitted} chars]"
    return text


def _sanitize_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_tool_text(value)
    if isinstance(value, list):
        return [_sanitize_json_value(item) for item in value]
    if isinstance(value, dict):
        return {
            _sanitize_tool_text(key): _sanitize_json_value(val)
            for key, val in value.items()
        }
    return value


def _coerce_redis_entry(entry: Any) -> dict[str, Any]:
    raw = entry if isinstance(entry, dict) else dict(entry)

    out: dict[str, Any] = {}
    for key, value in raw.items():
        k = key.decode("utf-8") if isinstance(key, bytes) else str(key)
        if isinstance(value, bytes):
            out[k] = value.decode("utf-8")
        else:
            out[k] = value
    return out


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _stream_tokens(
    *,
    redis_url: str,
    run_id: str,
    deadline: float,
    wait_for_worker: bool = False,
) -> AsyncGenerator[str, None]:
    """Tail a run token stream from Redis and yield frontend SSE events.

    Failure-open: Redis is a hot-path convenience. If it is unavailable, the
    caller falls back to DB message polling and final run reconciliation.

    ``wait_for_worker`` controls the no-token-yet early-exit:

    * ``False`` (default, replay of an already-terminal run): the stream is
      fully written, so bail out fast after a couple of idle reads.
    * ``True`` (run is still RUNNING/PENDING): the worker container may still be
      cold-starting, so keep waiting for the first token up to
      ``_WORKER_COLDSTART_GRACE_SECONDS`` (or ``deadline``).  Without this the
      live token stream is lost to the cold-start race.
    """
    try:
        import redis.asyncio as aioredis  # type: ignore[import-not-found]

        client = aioredis.Redis.from_url(
            redis_url,
            socket_connect_timeout=2,
            socket_timeout=2,
            decode_responses=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis token stream connect failed: %s", exc)
        return

    stream_key = _run_token_stream_key(run_id)
    last_id = "0-0"
    saw_entry = False
    idle_reads = 0
    started_at = asyncio.get_event_loop().time()
    try:
        while asyncio.get_event_loop().time() < deadline:
            batches = await client.xread({stream_key: last_id}, block=1000, count=100)
            if not batches:
                idle_reads += 1
                # Once tokens have started flowing, a short run of idle reads
                # means the worker has stalled or finished without an `end`
                # marker — end the stream so the SSE connection doesn't hang.
                if saw_entry and idle_reads >= 5:
                    return
                if not saw_entry:
                    if wait_for_worker:
                        # Active run: the worker may still be cold-starting.
                        # Keep waiting for the first token up to the grace cap.
                        if (
                            asyncio.get_event_loop().time() - started_at
                            >= _WORKER_COLDSTART_GRACE_SECONDS
                        ):
                            return
                    elif idle_reads >= 2:
                        # Replay of a terminal run: the stream is already fully
                        # written, so nothing-yet means nothing-coming.
                        return
                continue

            for _key, entries in batches:
                for entry_id, raw_entry in entries:
                    saw_entry = True
                    idle_reads = 0
                    last_id = (
                        entry_id.decode("utf-8")
                        if isinstance(entry_id, bytes)
                        else str(entry_id)
                    )
                    entry = _coerce_redis_entry(raw_entry)
                    entry_type = entry.get("type")

                    if entry_type == "end":
                        return
                    if entry_type == "delta":
                        text = entry.get("text") or ""
                        if text:
                            yield _sse("token", {"run_id": run_id, "delta": text})
                    elif entry_type == "tool_call":
                        yield _sse(
                            "tool",
                            {
                                "run_id": run_id,
                                "kind": "call",
                                "tool_call_id": entry.get("tool_call_id") or "",
                                "name": entry.get("name") or "tool",
                                "args": entry.get("args") or "",
                            },
                        )
                    elif entry_type == "tool_result":
                        yield _sse(
                            "tool",
                            {
                                "run_id": run_id,
                                "kind": "result",
                                "tool_call_id": entry.get("tool_call_id") or "",
                                "name": entry.get("name") or "tool",
                                "result": _sanitize_tool_text(entry.get("result")),
                                "state": entry.get("state") or "",
                            },
                        )
                    elif entry_type == "thinking":
                        text = entry.get("text") or ""
                        if text:
                            yield _sse("thinking", {"run_id": run_id, "text": text})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis token stream read failed: %s", exc)
        return
    finally:
        with suppress(Exception):
            await client.aclose()


async def _read_ordered_blocks_from_redis(
    *,
    redis_url: str,
    run_id: str,
) -> list[dict[str, Any]]:
    """Replay Redis token/tool entries into ordered persistable blocks."""
    try:
        import redis.asyncio as aioredis  # type: ignore[import-not-found]

        client = aioredis.Redis.from_url(
            redis_url,
            socket_connect_timeout=2,
            socket_timeout=2,
            decode_responses=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis ordered replay connect failed: %s", exc)
        return []

    blocks: list[dict[str, Any]] = []
    text_parts: list[str] = []
    tools_by_id: dict[str, dict[str, Any]] = {}

    def flush_text() -> None:
        if text_parts:
            text = "".join(text_parts)
            if text:
                blocks.append({"kind": "text", "text": text})
            text_parts.clear()

    try:
        entries = await client.xrange(_run_token_stream_key(run_id), min="-", max="+")
        for _entry_id, raw_entry in entries:
            entry = _coerce_redis_entry(raw_entry)
            entry_type = entry.get("type")
            if entry_type == "end":
                break
            if entry_type == "delta":
                text_parts.append(str(entry.get("text") or ""))
                continue
            if entry_type == "thinking":
                # Do NOT flush_text() here. Thinking is interleaved reasoning
                # that does not break the surrounding assistant text — and it is
                # not persisted as a message row below. Flushing on thinking
                # split one continuous reply (e.g. "你好" + "！我是…") into two
                # adjacent assistant rows with nothing between them. Only a tool
                # call must flush, because its block is ordered between text.
                text = str(entry.get("text") or "")
                if text:
                    blocks.append({"kind": "thinking", "text": text})
                continue
            if entry_type == "tool_call":
                flush_text()
                tool_call_id = str(entry.get("tool_call_id") or "")
                args = entry.get("args") or "{}"
                try:
                    parsed_args = json.loads(str(args))
                    input_payload = (
                        parsed_args if isinstance(parsed_args, dict)
                        else {"value": parsed_args}
                    )
                except Exception:  # noqa: BLE001
                    input_payload = {"value": str(args)}

                block = {
                    "kind": "tool",
                    "tool_call_id": tool_call_id,
                    "name": str(entry.get("name") or "tool"),
                    "input": input_payload,
                    "result": None,
                    "state": None,
                }
                tools_by_id[tool_call_id] = block
                blocks.append(block)
                continue
            if entry_type == "tool_result":
                tool_call_id = str(entry.get("tool_call_id") or "")
                block = tools_by_id.get(tool_call_id)
                if block is None:
                    flush_text()
                    block = {
                        "kind": "tool",
                        "tool_call_id": tool_call_id,
                        "name": str(entry.get("name") or "tool"),
                        "input": {},
                        "result": None,
                        "state": None,
                    }
                    tools_by_id[tool_call_id] = block
                    blocks.append(block)
                block["result"] = _sanitize_tool_text(entry.get("result"))
                block["state"] = entry.get("state") or ""

        flush_text()
        return blocks
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis ordered replay failed: %s", exc)
        return []
    finally:
        with suppress(Exception):
            await client.aclose()


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
    user_id: str,
    run: Run,
    svc: SessionService,
    knowledge_ingest_url: str = "",
    redis_url: str | None = None,
) -> tuple[bool, list[Any]]:
    """Poll the scheduler for a RUNNING run; persist result on terminal exit.

    Returns ``(terminal_reached, plan_steps)`` where:
    * ``terminal_reached`` is True when the run reached a terminal state and
      the DB has been updated (so the SSE loop can emit run_complete on the
      same iteration).
    * ``plan_steps`` is the list of structured plan steps read from
      result.json (available both while RUNNING and after completion).

    Best-effort: scheduler errors are logged and treated as "still running"
    so the next poll retries.
    """
    if not run.sandbox_id or run.status not in ("RUNNING", "PENDING"):
        return run.status in ("COMPLETED", "FAILED", "CANCELLED"), []

    # Scheduler's ``GET /runs/{id}`` uses the path segment as the Docker
    # container id (its ``sandbox_id``), falling back to ``run_id`` only
    # when an explicit body provides ``sandbox_id``.  Passing the short
    # container id directly skips that body-only path and works with
    # httpx's GET (which doesn't send a JSON body).
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{scheduler_url}/runs/{run.sandbox_id}",
                headers={"X-Tenant-Id": tenant_id, "X-User-Id": user_id},
            )
    except httpx.HTTPError as exc:
        logger.warning("scheduler poll failed", extra={"run_id": run.id, "err": str(exc)})
        return False, []

    if resp.status_code != 200:
        return False, []

    payload = resp.json()
    # Always extract plan_steps — available during RUNNING and after terminal.
    plan_steps: list[Any] = payload.get("plan_steps") or []

    sched_status = payload.get("status")
    if sched_status not in ("COMPLETED", "FAILED"):
        # Run is still in progress; surface plan_steps to the SSE caller.
        return False, plan_steps

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

    persisted_from_stream = False
    already_persisted_for_run = False
    if redis_url:
        existing_messages, _ = await svc.list_messages(run.session_id, limit=200)
        already_persisted_for_run = any(
            m.run_id == run.id and (m.role or "").lower() in {"assistant", "tool", "thinking"}
            for m in existing_messages
        )
        if not already_persisted_for_run:
            blocks = await _read_ordered_blocks_from_redis(
                redis_url=redis_url,
                run_id=run.id,
            )
            for block in blocks:
                if block.get("kind") == "thinking":
                    # Persist reasoning as a dedicated role='thinking' row so the
                    # UI can show a collapsible "已思考" section after the run
                    # (DeepSeek/Claude-style). It is ordered before the assistant
                    # text in `blocks`, so the thinking row gets a lower sequence
                    # and renders above the answer. role is a free String(10)
                    # column (no enum/CHECK), so 'thinking' is accepted.
                    text = str(block.get("text") or "")
                    if text:
                        await svc.create_message(
                            session_id=run.session_id,
                            role="thinking",
                            content=text,
                            tool_calls=None,
                            run_id=run.id,
                        )
                        persisted_from_stream = True
                elif block.get("kind") == "text":
                    text = str(block.get("text") or "")
                    if text:
                        await svc.create_message(
                            session_id=run.session_id,
                            role="assistant",
                            content=text,
                            tool_calls=None,
                            run_id=run.id,
                        )
                        persisted_from_stream = True
                elif block.get("kind") == "tool":
                    tool_call = {
                        "id": _sanitize_tool_text(block.get("tool_call_id") or ""),
                        "name": _sanitize_tool_text(block.get("name") or "tool"),
                        "input": _sanitize_json_value(block.get("input") or {}),
                        "result": _sanitize_tool_text(block.get("result")),
                        "state": _sanitize_tool_text(block.get("state")),
                    }
                    await svc.create_message(
                        session_id=run.session_id,
                        role="tool",
                        content=_sanitize_tool_text(block.get("name") or "tool"),
                        tool_calls=[tool_call],
                        run_id=run.id,
                    )
                    persisted_from_stream = True

    # Write an assistant message so the UI sees the reply. If Redis replay
    # already produced ordered text/tool blocks, avoid appending a duplicate
    # summary row for the same run.
    if summary and not persisted_from_stream and not already_persisted_for_run:
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

    return True, plan_steps


async def _persist_artifacts(
    *,
    artifacts: list[dict[str, Any]],
    run: Run,
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
        redis_url = request.app.state.settings.redis_url
        # Fetch the session's real user_id so the scheduler poll header
        # carries the originating user rather than the "SYSTEM" sentinel
        # (which the scheduler's authz guard rejects with 403).
        session_obj = await svc.get_session(session_id)
        for run in runs:
            await _reconcile_run_completion(
                scheduler_url=scheduler_url,
                tenant_id=tenant_id,
                user_id=session_obj.user_id,
                run=run,
                svc=svc,
                knowledge_ingest_url=knowledge_ingest_url,
                redis_url=redis_url,
            )
            # Note: plan_steps from list_messages are discarded — this path is
            # a page-refresh reconcile; SSE is the real-time channel for plans.
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
        attachment_ids=body.attachment_ids,
    )
    return MessageOut.model_validate(msg)


@router.delete("/sessions/{session_id}/messages",
               dependencies=[Depends(require_permission("session.message.delete", "session"))])
async def delete_messages_from_sequence(
    session_id: str,
    from_sequence: int,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> dict[str, int]:
    """Delete all messages with sequence >= ``from_sequence`` (conversation rewind).

    Lets the frontend rewind a conversation to a specific user turn by removing
    that message and everything after it (including orphaned output attachments).
    """
    svc = SessionService(db, tenant_id=tenant_id)
    deleted_count = await svc.delete_messages_from_sequence(session_id, from_sequence)
    return {"deleted_count": deleted_count}


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

    async def event_generator() -> AsyncGenerator[str, None]:
        last_seq = 0
        redis_url = request.app.state.settings.redis_url
        if redis_url:
            stream_run_id: str | None = None
            # True when stream_run_id is an active (RUNNING/PENDING) run whose
            # worker may still be cold-starting; False for terminal replay.
            stream_run_active = False
            async for db in acquire_db_session(tenant_id=tenant_id):
                svc = SessionService(db, tenant_id=tenant_id)
                runs = await svc.list_runs(session_id)
                active = next(
                    (r for r in runs if r.status in ("RUNNING", "PENDING")),
                    None,
                )
                if active is not None:
                    stream_run_id = active.id
                    stream_run_active = True
                else:
                    terminal = next(
                        (
                            r for r in runs
                            if r.status in ("COMPLETED", "FAILED", "CANCELLED")
                        ),
                        None,
                    )
                    if terminal is not None:
                        existing, _ = await svc.list_messages(session_id, limit=200)
                        already_persisted = any(
                            m.run_id == terminal.id
                            and (m.role or "").lower() in {"assistant", "tool"}
                            for m in existing
                        )
                        if not already_persisted:
                            stream_run_id = terminal.id

            if stream_run_id is not None:
                deadline = asyncio.get_event_loop().time() + (
                    _MAX_POLLS * _POLL_INTERVAL_SECONDS
                )
                async for ev in _stream_tokens(
                    redis_url=redis_url,
                    run_id=stream_run_id,
                    deadline=deadline,
                    wait_for_worker=stream_run_active,
                ):
                    yield ev

        # Runs already terminal when this stream opened. A NEW message's run is
        # not among these, so we must not let a pre-existing completed run from
        # session history fire run_complete/done and end the stream before the
        # current run finishes — that was causing replies to only appear on
        # manual refresh.
        preexisting_terminal: set[str] = set()
        primed = False
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
                    # Collect plan_steps from any running run this poll.
                    latest_plan_steps: list[Any] = []
                    latest_run_id: str = ""
                    running_runs = [
                        r for r in runs if r.status in ("RUNNING", "PENDING")
                    ]
                    session_obj = (
                        await svc.get_session(session_id)
                        if running_runs
                        else None
                    )
                    for r in running_runs:
                        if session_obj is not None:
                            _terminal, _plan = await _reconcile_run_completion(
                                scheduler_url=scheduler_url,
                                tenant_id=tenant_id,
                                user_id=session_obj.user_id,
                                run=r,
                                svc=svc,
                                knowledge_ingest_url=knowledge_ingest_url,
                                redis_url=redis_url,
                            )
                            if _plan:
                                latest_plan_steps = _plan
                                latest_run_id = r.id

                    # Emit a plan SSE event whenever plan_steps are available,
                    # even while the run is still RUNNING.  This lets the
                    # frontend PLAN panel update in real time.
                    if latest_plan_steps:
                        plan_payload = json.dumps({
                            "run_id": latest_run_id,
                            "plan_steps": latest_plan_steps,
                        })
                        events.append(f"event: plan\ndata: {plan_payload}\n\n")

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
                    terminal_statuses = ("COMPLETED", "FAILED", "CANCELLED")
                    active_exists = any(r.status in ("RUNNING", "PENDING") for r in runs)
                    # On the first poll, snapshot runs that were ALREADY terminal
                    # (session history) so they don't end the stream — we only
                    # care about a run completing while this stream is open.
                    if not primed and active_exists:
                        preexisting_terminal = {
                            r.id for r in runs if r.status in terminal_statuses
                        }
                        primed = True
                    terminal = next(
                        (
                            r
                            for r in runs
                            if r.status in terminal_statuses
                            and r.id not in preexisting_terminal
                        ),
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
