"""Message API endpoints.

SSE stream
----------
``GET /sessions/{id}/stream`` is the session-scoped multiplexing SSE stream
that holds the connection open for the full lifetime of the session view and
surfaces events from whatever run is currently active (including future runs
started after the connection is opened).

Design notes:

1. **Each poll opens its own DB session.**  We acquire a fresh short-lived
   session per poll via the ``get_session`` factory (``async with
   session.begin()``), rather than holding a single transaction for the
   entire 5-minute stream lifetime.  This prevents connection leaks under load.

2. **Errors never leak ``str(exc)``.**  Unhandled exceptions surface only a
   generic ``"event stream terminated"`` line; the real exception is logged
   with the request id for ops correlation.

3. **Buffer-then-yield discipline.**  All events collected inside an
   ``async for db in acquire_db_session(...)`` block are buffered in a local
   list and yielded only *after* the block closes.  This prevents a client
   disconnect (``GeneratorExit`` / ``CancelledError``) from rolling back a
   just-committed assistant message row.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import suppress
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.api.authz_guard import require_permission
from agent_session_orchestrator.api.dependencies import TDbSession, TTenantId
from agent_session_orchestrator.api.dlp_guard import scan_and_tag
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
# Active-run idle handling.  Once tokens have started flowing, silence is the
# model thinking between tool batches — NOT a finished stream.  We only give up
# tailing an active run when a status recheck confirms it left RUNNING/PENDING,
# or after a generous hard cap that guards a crashed worker which dies without
# ever writing its `end` marker.
_ACTIVE_IDLE_HARDCAP_READS = 600  # ~10 min of pure silence ⇒ assume crashed worker
_RUN_STATUS_RECHECK_READS = 5  # recheck run status every ~5s of idle
# Grace window after which a RUNNING/PENDING run whose sandbox the scheduler no
# longer knows about (``GET /runs/{sandbox_id}`` → 404) *and* whose token stream
# carries no ``end`` marker is force-FAILED.  A 404 can be momentary right at
# spawn (the container id is registered a beat after the run row is written), so
# we only give up once the run has been alive comfortably longer than any cold
# start.  Module-level so tests can monkeypatch it to a small value.
_SANDBOX_GONE_GRACE_SECONDS = 30 * 60  # 30 minutes


def _run_token_stream_key(run_id: str) -> str:
    return f"agent:run:{run_id}:tokens"


def _run_canvas_key(run_id: str) -> str:
    return f"agent:run:{run_id}:canvas"


# ── Canvas materialization guard (canvas v2, Phase B) ─────────────────────────
# Server-authoritative over-trigger guard: even though the worker's system
# prompt tells the model to reserve ``create_canvas`` for substantial
# deliverables, the model can still over-fire.  Before materializing a payload
# from run-state we require it to look like a real long-form document; anything
# below threshold is skipped (logged at INFO, fail-open — the content is already
# in the chat message, so skipping loses nothing).  Module-level so they are
# easy to tune.
_CANVAS_MIN_CHARS = 200  # min whitespace-stripped content_markdown length
_CANVAS_MIN_BLOCKS = 2    # min paragraphs (split on blank lines)
_CANVAS_MIN_HTML_CHARS = 40  # min length for an executable HTML canvas


def _canvas_payload_passes_guard(payload: dict[str, Any]) -> bool:
    """True when a run-state canvas payload is substantial enough to materialize.

    Prose (content_type="markdown") must clear ``_CANVAS_MIN_CHARS`` and split
    into at least ``_CANVAS_MIN_BLOCKS`` blank-line-separated segments.  An
    executable HTML canvas (content_type="html") is stored as one verbatim block,
    so the blank-line-segment rule does not apply — it only needs a non-trivial
    document (``_CANVAS_MIN_HTML_CHARS``).  Shared by the streaming and reconcile
    materialization paths so both apply the identical rule.
    """
    content = str(payload.get("content_markdown") or "")
    stripped = content.strip()
    if str(payload.get("content_type") or "markdown") == "html":
        return len(stripped) >= _CANVAS_MIN_HTML_CHARS
    if len(stripped) < _CANVAS_MIN_CHARS:
        return False
    segments = [seg for seg in re.split(r"\n{2,}", stripped) if seg.strip()]
    return len(segments) >= _CANVAS_MIN_BLOCKS


async def materialize_one_canvas(
    *,
    canvas_svc: Any,
    run: Run,
    user_id: str,
    kind: str,
    title: str | None,
    content_markdown: str,
    content_type: str = "markdown",
) -> tuple[str, str | None] | None:
    """Create one canvas document + backfill its assistant message (canvas v2).

    The shared per-payload core of canvas materialization: apply the over-trigger
    guard, then create the document via ``CanvasService.create_canvas`` (content +
    ``source_run_id``) and point the run's assistant message ``canvas_id`` at it.
    Returns ``(canvas_id, message_id)`` where ``message_id`` is ``None`` if no
    eligible assistant row exists yet (the canvas is still created), or ``None``
    entirely when the payload fails the guard (skipped, logged at INFO).

    Extracted so both the sandbox run-state loop (``_materialize_pending_canvases``)
    and the inline in-process tool-call path apply the identical guard + create +
    backfill semantics against the same over-trigger rule.  Callers own the
    fail-open boundary and any ``canvas_created`` SSE emission.
    """
    if not _canvas_payload_passes_guard(
        {
            "content_markdown": content_markdown,
            "title": title,
            "kind": kind,
            "content_type": content_type,
        }
    ):
        logger.info(
            "canvas payload below guard threshold; skipping materialization",
            extra={
                "run_id": run.id,
                "title": str(title or ""),
                "chars": len(str(content_markdown or "").strip()),
            },
        )
        return None

    canvas = await canvas_svc.create_canvas(
        run.session_id,
        user_id=user_id,
        content=str(content_markdown or ""),
        kind=str(kind or "article"),
        title=str(title or "").strip() or None,
        source_run_id=run.id,
        content_type=str(content_type or "markdown"),
    )
    message_id = await canvas_svc.backfill_message_canvas_id(
        session_id=run.session_id,
        run_id=run.id,
        canvas_id=canvas.id,
    )
    return canvas.id, message_id


def _canvas_created_event(*, canvas_id: str, run_id: str, message_id: str | None) -> str:
    """Format the ``canvas_created`` SSE the frontend consumes (mode-agnostic).

    Shape ``{canvas_id, run_id, message_id?}`` — ``message_id`` is omitted when no
    assistant row was anchored yet (the frontend still renders the card from
    ``canvas_id``).  Shared so the sandbox reconcile path and the inline path emit
    byte-identical events.
    """
    payload: dict[str, Any] = {"canvas_id": canvas_id, "run_id": run_id}
    if message_id is not None:
        payload["message_id"] = message_id
    return _sse("canvas_created", payload)


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
    is_run_active: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncGenerator[str, None]:
    """Tail a run token stream from Redis and yield frontend SSE events.

    Failure-open: Redis is a hot-path convenience. If it is unavailable, the
    caller falls back to DB message polling and final run reconciliation.

    ``wait_for_worker`` controls both the no-token-yet early-exit and the
    after-tokens idle handling:

    * ``False`` (default, replay of an already-terminal run): the stream is
      fully written, so bail out fast — after a couple of idle reads before any
      token, and after ~5 idle reads once tokens have started.
    * ``True`` (run is still RUNNING/PENDING): the worker container may still be
      cold-starting, so keep waiting for the first token up to
      ``_WORKER_COLDSTART_GRACE_SECONDS`` (or ``deadline``).  Without this the
      live token stream is lost to the cold-start race.  AFTER tokens start, a
      short idle gap is the model thinking between tool batches, NOT a finished
      stream — the worker writes an authoritative ``end`` marker as the last
      entry of a normal completion.  We therefore do NOT bail on a brief idle:
      only when ``is_run_active`` (re-checked every ~5 idle reads) reports the
      run left RUNNING/PENDING, or after ``_ACTIVE_IDLE_HARDCAP_READS`` of pure
      silence (crashed-worker safety net that never wrote ``end``).

    ``is_run_active`` is an optional async predicate returning ``True`` while the
    run is still RUNNING/PENDING.  When ``None`` (e.g. terminal replay calls) the
    active-run recheck path is simply not used.
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
    saw_pending = False
    idle_reads = 0
    stream_ended = False
    started_at = asyncio.get_event_loop().time()
    try:
        while asyncio.get_event_loop().time() < deadline:
            batches = await client.xread({stream_key: last_id}, block=1000, count=100)
            if not batches:
                idle_reads += 1
                if saw_entry:
                    if wait_for_worker:
                        # Active run: silence is the model thinking between tool
                        # batches, NOT a finished stream. The `end` marker is the
                        # authoritative normal exit (handled above). Only give up if
                        # the run actually left the active state, or after a generous
                        # hard cap guarding a crashed worker that never writes `end`.
                        if idle_reads >= _ACTIVE_IDLE_HARDCAP_READS:
                            break
                        if (
                            is_run_active is not None
                            and idle_reads % _RUN_STATUS_RECHECK_READS == 0
                        ):
                            still_active = True
                            try:
                                still_active = await is_run_active()
                            except Exception:  # noqa: BLE001 — failure-open: keep tailing
                                still_active = True
                            if not still_active:
                                break
                    elif idle_reads >= 5:
                        # Replay of an already-terminal run: short idle ⇒ stream done.
                        break
                else:
                    if wait_for_worker:
                        if (
                            asyncio.get_event_loop().time() - started_at
                            >= _WORKER_COLDSTART_GRACE_SECONDS
                        ):
                            break
                    elif idle_reads >= 2:
                        break
                # Reached only when this empty read did NOT break out of the
                # tail (i.e. we're staying to wait for more entries).  Emit a
                # ``ping`` heartbeat so the client's stall watchdog sees bytes
                # during long silent gaps — the model thinking between tool
                # batches has been observed at 36.7s of silence, well past any
                # reasonable client-side stall timeout.  Placed after all break
                # decisions so we never ping on a read that is about to end the
                # tail (an extra ping there would be harmless but pointless).
                yield _sse("ping", {"run_id": run_id})
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

                    # The Redis stream entry id (e.g. "1718-0") is a globally
                    # monotonic offset within this run's stream.  We attach it to
                    # every emitted event so the frontend can apply tokens
                    # idempotently: any new SSE connection replays from "0-0", so
                    # without an offset the client re-appends already-seen tokens
                    # (StrictMode remount / reconnect → scrambled, partly-lost
                    # streamed text).  With ``offset`` the client drops any event
                    # whose offset it has already applied for this run.
                    if entry_type == "end":
                        stream_ended = True
                        break
                    if entry_type == "delta":
                        text = entry.get("text") or ""
                        if text:
                            yield _sse(
                                "token",
                                {"run_id": run_id, "delta": text, "offset": last_id},
                            )
                    elif entry_type == "tool_call":
                        yield _sse(
                            "tool",
                            {
                                "run_id": run_id,
                                "kind": "call",
                                "tool_call_id": entry.get("tool_call_id") or "",
                                "name": entry.get("name") or "tool",
                                "args": entry.get("args") or "",
                                "offset": last_id,
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
                                "offset": last_id,
                            },
                        )
                    elif entry_type == "thinking":
                        text = entry.get("text") or ""
                        if text:
                            yield _sse(
                                "thinking",
                                {"run_id": run_id, "text": text, "offset": last_id},
                            )
                    elif entry_type == "canvas":
                        # Inline path: the executor materialized a canvas in-process
                        # from a model ``create_canvas`` tool call and wrote a
                        # ``canvas`` entry here so the live tail relays it.  Emit the
                        # exact ``canvas_created`` event the sandbox reconcile path
                        # synthesizes ({canvas_id, run_id, message_id?}) so the
                        # frontend handler is mode-agnostic.  (The sandbox path never
                        # writes this entry type — it materializes off the separate
                        # ``:canvas`` key and yields the event from a buffered list.)
                        canvas_id = entry.get("canvas_id") or ""
                        if canvas_id:
                            yield _canvas_created_event(
                                canvas_id=str(canvas_id),
                                run_id=run_id,
                                message_id=entry.get("message_id") or None,
                            )
                    elif entry_type == "pending_confirmation":
                        # HITL: worker paused waiting for user to confirm a tool call.
                        # Relay verbatim so the frontend can render the confirmation card.
                        saw_pending = True
                        yield _sse(
                            "pending_confirmation",
                            {
                                "run_id": run_id,
                                "tool_call_id": entry.get("tool_call_id") or "",
                                "name": entry.get("name") or "",
                                "args": entry.get("args") or "",
                            },
                        )
                    elif entry_type == "pending_question":
                        # HITL: worker paused waiting for a free-text answer.
                        saw_pending = True
                        yield _sse(
                            "pending_question",
                            {
                                "run_id": run_id,
                                "tool_call_id": entry.get("tool_call_id") or "",
                                "question": entry.get("question") or "",
                            },
                        )
                if stream_ended:
                    # `end` entry consumed in the inner loop — stop the outer
                    # batch loop too so we reach the run_stream_end emit below.
                    break
            if stream_ended:
                break
        # Normal end-of-stream (end marker, stall, cold-start grace, or
        # replay-idle).  Emit an authoritative terminal signal carrying whether
        # the run paused for HITL input — this lets the caller decide
        # paused-vs-complete without waiting on scheduler lag, and gives
        # replay-only connections a clean stream terminator.
        yield _sse("run_stream_end", {"run_id": run_id, "paused": saw_pending})
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
                # Thinking is a side-channel, not a hard boundary in assistant
                # text. Keep accumulating surrounding delta entries so
                # "你好" + thinking + "！我是…" persists as one assistant row
                # below the thinking block instead of two split text rows.
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
    user_id: str,
    task_description: str,
    summary: str,
    session_id: str = "",
) -> None:
    """Write a memory document to knowledge-ingest after run completion.

    The document is a brief Q&A pair: user task + assistant summary.  It is
    uploaded as ``text/plain``, then immediately ingested (embedded) so it
    becomes searchable.  Failure-open: any error is logged at DEBUG level
    and the caller is not affected.

    ``user_id`` must be the real originating user (``usr_...``) — knowledge-
    ingest's authz guard is fail-closed and rejects the ``SYSTEM`` sentinel,
    which used to make this write silently 403.  Callers resolve the session
    owner when they only hold a system identity.

    ``session_id`` is stamped onto the document as ``origin_session_id`` so the
    memory pre-fetch (``_fetch_memory``) can exclude this session's own prior-turn
    Q&A — that context is already restored into the agent via conversation
    history, and re-injecting it as RAG double-feeds the same turns.

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

    upload_data = {"title": title, "mime": "text/plain"}
    if session_id:
        upload_data["origin_session_id"] = session_id

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            headers = {"X-Tenant-Id": tenant_id, "X-User-Id": user_id}
            upload_resp = await client.post(
                f"{knowledge_ingest_url}/documents",
                headers=headers,
                data=upload_data,
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


async def _replay_run_blocks_into_messages(
    *,
    run: Run,
    svc: SessionService,
    tenant_id: str,
    redis_url: str,
    dlp_url: str = "",
) -> tuple[bool, bool]:
    """Replay a run's Redis token stream into ordered persisted message rows.

    Shared by the two terminal-completion paths that both need to turn the
    worker's ordered token/tool/thinking blocks into durable message rows:

    * the normal 200-``COMPLETED`` scheduler path, and
    * the 404 + ``end`` marker path (sandbox pruned after the worker finished
      writing its stream — see ``_reconcile_run_completion``).

    Keeping both callers on one helper guarantees identical side-effects:
    the ``already_persisted_for_run`` idempotency guard, the role='thinking'
    /'assistant'/'tool' row mapping, and the fail-open output-side DLP scan.

    Returns ``(persisted_from_stream, already_persisted_for_run)``:
    * ``persisted_from_stream`` — at least one message row was written from the
      replayed blocks this call.
    * ``already_persisted_for_run`` — assistant/tool/thinking rows for this run
      already existed, so the replay was skipped (idempotent no-op).

    ``redis_url`` must be truthy; callers gate on it before invoking.
    """
    existing_messages, _ = await svc.list_messages(run.session_id, limit=200)
    already_persisted_for_run = any(
        m.run_id == run.id and (m.role or "").lower() in {"assistant", "tool", "thinking"}
        for m in existing_messages
    )
    persisted_from_stream = False
    if already_persisted_for_run:
        return persisted_from_stream, already_persisted_for_run

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
                msg = await svc.create_message(
                    session_id=run.session_id,
                    role="assistant",
                    content=text,
                    tool_calls=None,
                    run_id=run.id,
                )
                persisted_from_stream = True
                # ── DLP scan (output side): fail-open ───────────────
                # Scan the assistant's natural-language reply as it is
                # persisted on its way out to the user, mirroring the
                # attachment upload path. A hit tags the message, it is
                # never blocked; any DLP error is swallowed inside
                # scan_and_tag. Off the hot path: this runs once per
                # completed text block at run finalization, not per
                # streamed token.
                if dlp_url:
                    await _dlp_scan_transcript(
                        dlp_url=dlp_url,
                        tenant_id=tenant_id,
                        content=text,
                        resource_id=msg.id,
                    )
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

    return persisted_from_stream, already_persisted_for_run


async def _redis_stream_has_end_marker(
    *,
    redis_url: str,
    run_id: str,
) -> bool:
    """Return True iff the run's token stream contains a ``type=end`` entry.

    The worker pushes exactly one authoritative ``type=end`` marker as the last
    stream entry of a *normal* completion (``push_token_end`` in the runtime
    worker's ``redis_state``).  On an HITL pause it deliberately pushes NO
    ``end`` marker — only ``pending_confirmation`` / ``pending_question``
    entries.  So the presence of ``end`` is a reliable "the worker finished
    normally" signal, which lets the 404 branch of ``_reconcile_run_completion``
    tell a completed-but-sandbox-pruned run apart from a paused one.

    Failure-open: any connect/read error returns ``False`` (caller then falls
    through to the grace-window logic, never mis-marking COMPLETED).
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
        logger.warning("Redis end-marker connect failed: %s", exc)
        return False

    try:
        entries = await client.xrange(_run_token_stream_key(run_id), min="-", max="+")
        for _entry_id, raw_entry in entries:
            if _coerce_redis_entry(raw_entry).get("type") == "end":
                return True
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis end-marker read failed: %s", exc)
        return False
    finally:
        with suppress(Exception):
            await client.aclose()


async def _read_canvas_payloads_from_redis(
    *,
    redis_url: str,
    run_id: str,
) -> list[dict[str, Any]]:
    """Read the run's accumulated canvas payloads from ``agent:run:{id}:canvas``.

    The worker's ``create_canvas`` tool appends each payload (``{kind, title,
    content_markdown, seq}``) to a JSON list under this key (see
    ``RunStateClient.push_canvas``).  Returns the list ordered by the 1-based
    ``seq`` the worker assigned; ``[]`` on any connect/read/parse error
    (failure-open — a missing canvas channel simply means no canvas to
    materialize, and the reply text is always in the chat message).
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
        logger.warning("Redis canvas connect failed: %s", exc)
        return []

    try:
        raw = await client.get(_run_canvas_key(run_id))
        if not raw:
            return []
        items = json.loads(raw)
        if not isinstance(items, list):
            return []
        payloads = [it for it in items if isinstance(it, dict)]
        payloads.sort(key=lambda it: int(it.get("seq") or 0))
        return payloads
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis canvas read failed: %s", exc)
        return []
    finally:
        with suppress(Exception):
            await client.aclose()


async def _materialize_pending_canvases(
    *,
    run: Run,
    user_id: str,
    svc: SessionService,
    tenant_id: str,
    redis_url: str | None,
) -> list[str]:
    """Materialize not-yet-created canvas payloads for ``run`` (canvas v2, Phase B).

    Shared by the streaming loop and the run-completion reconcile path so both
    check run-state for canvas payloads the worker's ``create_canvas`` tool
    wrote.  Steps, per payload ordered by ``seq``:

    1. **Idempotency** — payloads carry a 1-based ``seq``; we materialize only
       those whose ``seq`` exceeds the count of canvases already created for this
       run (``count_canvases_for_run``).  A replay (SSE reconnect / reconcile
       re-poll) therefore never double-creates.
    2. **Over-trigger guard** — ``_canvas_payload_passes_guard`` skips payloads
       that are too short / too few blocks (logged at INFO, fail-open).  A
       skipped payload still counts against the seq cursor so a later, larger
       payload keeps its own seq alignment.
    3. **Materialize** — ``CanvasService.create_canvas`` (content + source_run_id)
       creates the document + v1 version; then backfill the run's assistant
       message ``canvas_id`` when one is already persisted.

    Returns a list of ``canvas_created`` SSE strings (one per newly materialized
    canvas).  Fail-open (design invariant 5): any error is logged as a WARNING
    and the reply body still flows through the normal message path — a failed
    materialization never blocks run completion.
    """
    if not redis_url:
        return []
    try:
        payloads = await _read_canvas_payloads_from_redis(
            redis_url=redis_url, run_id=run.id
        )
        if not payloads:
            return []

        # Local import: keep the canvas service off the hot import path for
        # runs that never touch canvases.
        from agent_session_orchestrator.services.canvas_service import CanvasService

        canvas_svc = CanvasService(svc._db, tenant_id=tenant_id)  # noqa: SLF001
        existing_canvases = await canvas_svc.list_canvases_for_run(run.id)
        already = len(existing_canvases)

        # Creating the canvas and persisting the assistant message happen on
        # independent timelines.  A previous poll may have created the canvas
        # before any eligible assistant row existed, so idempotency must skip
        # document creation but MUST NOT skip the message backfill.  Retrying is
        # safe: already-linked messages are excluded by the service query.
        for existing_canvas in existing_canvases:
            await canvas_svc.backfill_message_canvas_id(
                session_id=run.session_id,
                run_id=run.id,
                canvas_id=existing_canvas.id,
            )

        events: list[str] = []
        for payload in payloads:
            seq = int(payload.get("seq") or 0)
            # Skip any payload at or below the count already materialized —
            # idempotency across replays/paths.
            if seq <= already:
                continue

            # Count it as consumed so the seq cursor stays aligned regardless of
            # outcome: a guard-skipped payload keeps re-reads on the next poll
            # from re-logging the same skip, and a materialized one advances past
            # its own seq.
            result = await materialize_one_canvas(
                canvas_svc=canvas_svc,
                run=run,
                user_id=user_id,
                kind=str(payload.get("kind") or "article"),
                title=str(payload.get("title") or "").strip() or None,
                content_markdown=str(payload.get("content_markdown") or ""),
                content_type=str(payload.get("content_type") or "markdown"),
            )
            already = seq
            if result is None:
                # Below guard threshold (already logged inside the helper).
                continue

            canvas_id, message_id = result
            events.append(
                _canvas_created_event(
                    canvas_id=canvas_id, run_id=run.id, message_id=message_id
                )
            )
            logger.info(
                "materialized canvas from run-state",
                extra={
                    "run_id": run.id,
                    "canvas_id": canvas_id,
                    "seq": seq,
                },
            )
        return events
    except Exception as exc:  # noqa: BLE001 — fail-open (invariant 5)
        logger.warning(
            "canvas materialization failed (fail-open): %s",
            exc,
            extra={"run_id": run.id},
        )
        return []


async def _dlp_scan_transcript(
    *,
    dlp_url: str,
    tenant_id: str,
    content: str,
    resource_id: str,
) -> None:
    """Route a persisted assistant message through the fail-open DLP scanner.

    Output-side counterpart to the attachment-upload scan
    (``api/v1/attachments.py``): the agent's reply text is scanned and, on a
    hit, classification-tagged via the resource id; it is **never blocked**.

    Fully fail-open and best-effort:

    * No ``dlp_url`` configured → skip silently (mirrors how the memory-write
      and other side-effects gate on their target URL being set).
    * Any DLP error is already logged and swallowed inside ``scan_and_tag``;
      we additionally guard here so a bug in the call site can never break run
      finalization or the streamed reply.

    The DLP contract has no dedicated transcript resource type (its
    ``resource_type`` is a free string used for tagging/grouping), so we pass
    ``resource_type="transcript"`` with the persisted message id as the
    ``resource_id``. This is the closest natural fit — the scanned text is the
    transcript message — and requires no change to the DLP service.
    """
    if not dlp_url or not content:
        return
    try:
        await scan_and_tag(
            dlp_url=dlp_url,
            tenant_id=tenant_id,
            content=content,
            resource_id=resource_id,
            resource_type="transcript",
        )
    except Exception as exc:  # noqa: BLE001 — DLP must never break finalization
        logger.warning(
            "DLP transcript scan failed (fail-open, ignoring)",
            extra={"resource_id": resource_id, "err": str(exc)},
        )


async def _reconcile_run_completion(
    *,
    scheduler_url: str,
    tenant_id: str,
    user_id: str,
    run: Run,
    svc: SessionService,
    knowledge_ingest_url: str = "",
    redis_url: str | None = None,
    dlp_url: str = "",
) -> tuple[bool, list[Any]]:
    """Poll the scheduler for a RUNNING run; persist result on terminal exit.

    Returns ``(terminal_reached, plan_steps)`` where:
    * ``terminal_reached`` is True when the run reached a terminal state
      (COMPLETED / FAILED / CANCELLED) *and* the DB has been updated.
      PAUSED is NOT terminal — returns False so the SSE loop does not emit
      run_complete; the stream_session path handles run_paused separately.
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

    # ── Sandbox-gone (404) reconciliation ──────────────────────────────────
    # A 404 means the scheduler no longer has a container for this sandbox_id.
    # The common cause in practice is the Docker container having been pruned
    # out from under a RUNNING run row — the scheduler then returns 404 forever
    # and, without this branch, the run stays RUNNING permanently (the sweeper
    # re-hits the same 404 and gives up too).  We resolve the run definitively
    # instead of treating 404 as "still running":
    #
    #   1. Token stream carries an ``end`` marker  → the worker DID finish
    #      normally before the sandbox vanished. Replay the ordered blocks into
    #      durable message rows (same path as a 200-COMPLETED) and mark
    #      COMPLETED. The scheduler-only fields (result_summary/usage/artifacts)
    #      are simply unavailable here, so they stay None/absent.
    #   2. No ``end`` marker, run older than the grace window → the sandbox is
    #      genuinely gone with no completion recorded. Mark FAILED so the run
    #      stops hanging.
    #   3. No ``end`` marker, run still within the grace window → a 404 can be
    #      momentary right after spawn (container id registered a beat late), so
    #      keep treating it as still-running and retry on the next poll.
    #
    # Only an explicit ``end`` marker takes the COMPLETED path: an HITL pause
    # writes NO ``end`` marker (only pending_* entries), so a paused run whose
    # sandbox was pruned correctly falls through to the grace/FAILED logic and
    # is never mis-marked COMPLETED.
    if resp.status_code == 404:
        has_end = bool(redis_url) and await _redis_stream_has_end_marker(
            redis_url=redis_url,
            run_id=run.id,
        )
        if has_end:
            logger.info(
                "sandbox gone (404) but token stream has end marker; "
                "finalizing run as COMPLETED from Redis replay",
                extra={"run_id": run.id},
            )
            # Replay the ordered blocks into durable rows (idempotent via the
            # helper's already_persisted_for_run guard).  There is no scheduler
            # ``summary`` fallback on this path, so the helper's return values
            # are not needed — the replayed rows ARE the reply.
            await _replay_run_blocks_into_messages(
                run=run,
                svc=svc,
                tenant_id=tenant_id,
                redis_url=redis_url,  # non-empty (gated by has_end above)
                dlp_url=dlp_url,
            )
            run.status = "COMPLETED"
            run.finished_at = dt.datetime.now(tz=dt.UTC)
            # Scheduler-only fields are unavailable once the sandbox is gone.
            run.result_summary = None
            run.error_message = None
            return True, []

        # No end marker: decide FAILED vs. keep-waiting on the run's age.  Both
        # started_at and created_at are tz-aware (DateTime(timezone=True)); guard
        # a naive value defensively so the comparison never raises.
        reference = run.started_at or run.created_at
        if reference is not None and reference.tzinfo is None:
            reference = reference.replace(tzinfo=dt.UTC)
        now = dt.datetime.now(tz=dt.UTC)
        age_seconds = (
            (now - reference).total_seconds() if reference is not None else None
        )
        if age_seconds is not None and age_seconds >= _SANDBOX_GONE_GRACE_SECONDS:
            logger.warning(
                "sandbox gone (404), no end marker, past grace window; "
                "marking run FAILED",
                extra={"run_id": run.id, "age_seconds": int(age_seconds)},
            )
            run.status = "FAILED"
            run.finished_at = now
            run.result_summary = (
                "sandbox no longer exists (scheduler returned 404); "
                "marked FAILED by reconcile"
            )
            run.error_message = (
                "sandbox no longer exists (scheduler returned 404); "
                "marked FAILED by reconcile"
            )
            return True, []

        # Within the grace window — a transient 404 is plausible; retry later.
        logger.info(
            "sandbox 404, no end marker, within grace window; treating as "
            "still-running and retrying next poll",
            extra={"run_id": run.id},
        )
        return False, []

    if resp.status_code != 200:
        return False, []

    payload = resp.json()
    # Always extract plan_steps — available during RUNNING and after terminal.
    plan_steps: list[Any] = payload.get("plan_steps") or []

    sched_status = payload.get("status")

    # ── HITL PAUSED detection ──────────────────────────────────────────────
    # The scheduler reports COMPLETED when the container exits 0, but the
    # worker writes result.json with ``"status": "paused"`` when it paused for
    # human-in-the-loop input.  We must inspect the worker result payload to
    # distinguish a real completion from a pause.
    #
    # Scheduler shape on paused exit (container exited 0, scheduler sees
    # COMPLETED but result.json carries status=paused):
    #   payload.get("result_status") == "paused"
    # OR the scheduler may surface it under a top-level "worker_status" key.
    # We check both paths; if either indicates "paused" we treat as PAUSED.
    worker_status = (
        payload.get("result_status")
        or payload.get("worker_status")
        or ""
    )
    if sched_status in ("COMPLETED", "FAILED") and (worker_status == "paused"):
        # Worker paused — set PAUSED, do NOT write assistant message, NOT terminal.
        run.status = "PAUSED"
        logger.info(
            "run paused (HITL): set status=PAUSED",
            extra={"run_id": run.id},
        )
        return False, plan_steps

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
        persisted_from_stream, already_persisted_for_run = (
            await _replay_run_blocks_into_messages(
                run=run,
                svc=svc,
                tenant_id=tenant_id,
                redis_url=redis_url,
                dlp_url=dlp_url,
            )
        )

    # Write an assistant message so the UI sees the reply. If Redis replay
    # already produced ordered text/tool blocks, avoid appending a duplicate
    # summary row for the same run.
    if summary and not persisted_from_stream and not already_persisted_for_run:
        msg = await svc.create_message(
            session_id=run.session_id,
            role="assistant",
            content=summary,
            tool_calls=None,
            run_id=run.id,
        )
        # ── DLP scan (output side): fail-open ───────────────────────────────
        # Same posture as the streamed-text branch above: scan the assistant
        # summary on persist. Tag-only, never block; errors swallowed.
        if dlp_url:
            await _dlp_scan_transcript(
                dlp_url=dlp_url,
                tenant_id=tenant_id,
                content=summary,
                resource_id=msg.id,
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
        # knowledge-ingest's authz guard rejects the "SYSTEM" sentinel
        # (fail-closed).  Every interactive caller already passes the session
        # owner, but the reconcile sweeper can fall back to "SYSTEM" when its
        # cross-tenant scan yields no user — resolve the owner here so the
        # memory write carries a real identity.  Failure-open: if the session
        # row is gone the sentinel goes through and knowledge-ingest rejects
        # it, which _write_memory swallows.
        memory_user_id = user_id
        if not memory_user_id or memory_user_id == "SYSTEM":
            try:
                memory_user_id = (await svc.get_session(run.session_id)).user_id
            except Exception:  # noqa: BLE001
                logger.debug(
                    "memory write: could not resolve session owner for %s",
                    run.session_id,
                )
        await _write_memory(
            knowledge_ingest_url=knowledge_ingest_url,
            tenant_id=tenant_id,
            user_id=memory_user_id,
            task_description=run.task_description or "",
            summary=summary,
            session_id=run.session_id,
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
            # ── DLP scan (mount point 3): intentionally degraded ────────────
            # The scheduler hands us only the artifact's s3_key here, not its
            # bytes. Scanning would require an extra S3 GET per artifact on the
            # reconcile/completion path — heavy IO we deliberately avoid so as
            # not to slow down or destabilise run reconciliation. We therefore
            # only record the artifact metadata and skip content scan/tagging.
            #
            # TODO(dlp-artifacts): scan output artifacts for sensitivity. Best
            # done out-of-band (async worker / event consumer keyed off the
            # attachment id + s3_key) so the reconcile path stays lightweight,
            # then call dlp_guard.scan_and_tag on the fetched text. fail-open.
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
        dlp_url = request.app.state.settings.foundation_dlp_url
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
                dlp_url=dlp_url,
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


@router.get(
    "/sessions/{session_id}/stream",
    dependencies=[Depends(require_permission("session.message.read", "session"))],
)
async def stream_session(
    session_id: str,
    request: Request,
    tenant_id: str = TTenantId,
) -> StreamingResponse:
    """Session-scoped multiplexing SSE stream (Phase 1.2).

    Unlike ``stream_messages`` which binds to one run and ends when that run
    completes, this endpoint holds the HTTP connection open for the lifetime of
    the session view and surfaces events from *whatever* run is currently active
    — including runs that start later (resumed runs, scheduled runs).

    Event-name contract
    -------------------
    Emitted now:
      ``run_started``   — a new run has become active on this session.
      ``token``         — incremental assistant text delta (from Redis stream).
      ``tool``          — tool call / result (from Redis stream).
      ``thinking``      — reasoning text (from Redis stream).
      ``plan``          — structured plan steps update.
      ``message``       — newly persisted message row.
      ``run_complete``  — active run reached a terminal state.
      ``ping``          — liveness heartbeat carrying no payload of interest.
        Emitted on every otherwise-silent poll (no active run, nothing new to
        send) and on every idle read while tailing an active run's token stream
        (the model can go quiet for tens of seconds between tool batches).  It
        exists purely so the client can run a stall watchdog: "no bytes at all"
        is indistinguishable from a wedged connection, whereas a missing ping
        past the heartbeat interval means the stream has silently stopped and
        the client should reconnect (reconnect replays from ``0-0`` and the
        first ``/messages`` poll synthesises ``run_complete``, so recovery is
        self-converging).  Clients MUST treat ``ping`` as a no-op otherwise.
      ``done``          — stream deadline exhausted; client should close.
      ``error``         — unexpected server error; detail is opaque.

    Phase 2 additions (not yet implemented — reserved):
      ``pending_confirmation`` / ``pending_question`` — run paused waiting for
        human input; would be emitted when the Redis stream surfaces the
        corresponding entry type from the worker.
      ``run_paused`` / ``run_resumed`` — lifecycle signals around confirmation.

    Behaviour
    ---------
    * Outer loop is bounded by ``_MAX_POLLS × _POLL_INTERVAL_SECONDS`` (same
      5-minute wall-clock as ``stream_messages``).
    * Each iteration: look for an active (RUNNING/PENDING) run not yet streamed
      in this connection.  If found: emit ``run_started``, drive
      ``_stream_tokens`` to exhaustion, reconcile the run to persist its
      terminal state + assistant message, emit ``run_complete``.
    * Newly persisted messages and plan updates are emitted every poll, same
      as the existing endpoint.
    * If no active run is found, sleep and loop again — the connection stays
      open waiting for a future run.
    * Buffer-then-yield rule: all events collected inside the
      ``async for db in acquire_db_session(...)`` block are buffered in a
      local list and yielded only *after* the block closes so that a client
      disconnect (GeneratorExit / CancelledError) cannot roll back a
      just-committed assistant message row.
    """
    scheduler_url: str = request.app.state.settings.agent_scheduler_url
    knowledge_ingest_url: str = request.app.state.settings.knowledge_ingest_url
    dlp_url: str = request.app.state.settings.foundation_dlp_url

    async def event_generator() -> AsyncGenerator[str, None]:
        redis_url: str | None = request.app.state.settings.redis_url
        deadline = asyncio.get_event_loop().time() + (
            _MAX_POLLS * _POLL_INTERVAL_SECONDS
        )
        # Track run ids already streamed in this connection so we never
        # re-stream the same run (e.g. if a reconcile poll sees it as still
        # RUNNING before the final terminal write completes).
        streamed_run_ids: set[str] = set()
        last_seq = 0
        # First-poll completion reconciliation.  A client that (re)connects
        # AFTER its run already reached a terminal state would otherwise never
        # receive a run_complete on this connection — the active-run selector
        # finds nothing, the idle branch just polls, and the frontend's
        # isStreaming stays stuck "true" (Working on it… pill never clears,
        # though the run + sandbox are long gone).  On the first poll we emit a
        # synthetic run_complete for the session's most-recent terminal run so
        # the client's state machine always converges.  Idempotent: the client
        # treats a duplicate run_complete as a no-op.
        completion_primed = False

        # ── HITL PAUSED-run replay state machine ────────────────────────────
        #
        # Problem: when a client reconnects (page refresh) while a run is
        # already status=PAUSED, the active-run selector skips it (PAUSED ∉
        # {RUNNING, PENDING}), so pending_confirmation / pending_question
        # entries sitting in the Redis token stream never reach the new
        # connection and the frontend cannot render the confirm card.
        #
        # Solution: a separate ``replayed_paused_run_ids`` set tracks runs
        # that have been replayed through the PAUSED path in this connection.
        #
        # Per-connection state transitions per run_id R:
        #
        #  ┌──────────────────────────────────────────────────────────────┐
        #  │ streamed_run_ids   replayed_paused_run_ids   meaning          │
        #  ├──────────────────────────────────────────────────────────────┤
        #  │ ∉  ∉                                          unseen          │
        #  │ ∈  ∉                                          actively        │
        #  │                                               streaming       │
        #  │ ∉  ∈                                          replay done,    │
        #  │                                               waiting for     │
        #  │                                               resume          │
        #  │ ∈  ∈  (transient, only during active replay)                 │
        #  └──────────────────────────────────────────────────────────────┘
        #
        # Flow:
        #  A. PAUSED run found, id ∉ streamed_run_ids, id ∉ replayed set
        #     → add to streamed_run_ids (suppress looping), replay via
        #       _stream_tokens(wait_for_worker=False), emit run_paused,
        #       discard from streamed_run_ids, add to replayed set.
        #     Net: id ∉ streamed_run_ids, id ∈ replayed_paused_run_ids.
        #     A reconnect creates a fresh generator → fresh sets → replays again.
        #
        #  B. User responds → run transitions to RUNNING
        #     → active path picks it up (id ∉ streamed_run_ids now),
        #       removes it from replayed_paused_run_ids so a second pause
        #       of the same run can replay again.
        #
        #  C. Resumed run pauses AGAIN
        #     → active path streams to the new pause marker, emits run_paused,
        #       discards from streamed_run_ids (standard pause path).
        #       id is no longer in replayed set (cleared in step B), so a
        #       subsequent reconnect will replay via path A again correctly.
        #
        # Guard: a PAUSED run with id already in replayed_paused_run_ids is
        # skipped — preventing an infinite replay loop within one connection
        # if the run somehow stays PAUSED across many polls.
        replayed_paused_run_ids: set[str] = set()

        try:
            for _ in range(_MAX_POLLS):
                if asyncio.get_event_loop().time() >= deadline:
                    break

                events: list[str] = []
                active_run_to_stream: Run | None = None
                # Set True when the run we're about to stream reached a terminal
                # state during this poll's reconcile AND its durable message rows
                # were already emitted below.  In that case we MUST NOT replay the
                # Redis token stream (which always reads from "0-0" and would
                # rebuild a duplicate set of streaming blocks alongside the durable
                # rows) — we only need the post-stream reconcile to emit
                # run_complete.
                active_run_durably_completed: bool = False
                paused_run_to_replay: Run | None = None
                session_user_id: str = "SYSTEM"
                latest_plan_steps: list[Any] = []
                latest_plan_run_id: str = ""

                # ── DB poll ──────────────────────────────────────────────────
                # Collect all events into ``events``; yield ONLY after the
                # block exits so GeneratorExit cannot roll back committed rows.
                async for db in acquire_db_session(tenant_id=tenant_id):
                    svc = SessionService(db, tenant_id=tenant_id)
                    runs = await svc.list_runs(session_id)

                    # First poll only: if there is no active/paused run but the
                    # session has a terminal run, synthesize a run_complete so a
                    # client that connected after completion clears isStreaming.
                    if not completion_primed:
                        completion_primed = True
                        has_live = any(
                            r.status in ("RUNNING", "PENDING", "PAUSED")
                            for r in runs
                        )
                        if not has_live:
                            terminal = next(
                                (
                                    r
                                    for r in runs
                                    if r.status in ("COMPLETED", "FAILED", "CANCELLED")
                                ),
                                None,
                            )
                            if terminal is not None:
                                events.append(
                                    _sse(
                                        "run_complete",
                                        {
                                            "run_id": terminal.id,
                                            "status": terminal.status,
                                            "result_summary": terminal.result_summary,
                                        },
                                    )
                                )

                    # Identify the current active run (if any).
                    active = next(
                        (r for r in runs if r.status in ("RUNNING", "PENDING")),
                        None,
                    )

                    if active is not None and active.id not in streamed_run_ids:
                        # New active run we haven't streamed yet — snapshot the
                        # session user_id now (inside the DB session) for use in
                        # the scheduler reconcile call below.
                        session_obj = await svc.get_session(session_id)
                        session_user_id = session_obj.user_id
                        active_run_to_stream = active
                        # Emit run_started envelope before leaving the DB block
                        # so the client knows which run is about to stream.
                        events.append(
                            _sse("run_started", {"run_id": active.id})
                        )
                        streamed_run_ids.add(active.id)
                        # This run entered via the active path (resumed) — clear
                        # it from the replayed-paused set so a second pause can
                        # replay again on a future reconnect (state-machine step B).
                        replayed_paused_run_ids.discard(active.id)

                    elif active is None:
                        # No active run: check for a PAUSED run to replay.
                        # Only replay once per connection (guard against loops).
                        paused = next(
                            (
                                r
                                for r in runs
                                if r.status == "PAUSED"
                                and r.id not in streamed_run_ids
                                and r.id not in replayed_paused_run_ids
                            ),
                            None,
                        )
                        if paused is not None:
                            paused_run_to_replay = paused
                            # Temporarily mark as "being streamed" to prevent
                            # a concurrent poll iteration from also picking it up.
                            streamed_run_ids.add(paused.id)
                            events.append(
                                _sse("run_started", {"run_id": paused.id})
                            )

                    # Reconcile any RUNNING/PENDING runs (even the one we're
                    # about to stream — reconcile is idempotent and brings the
                    # plan panel up to date).
                    running_runs = [
                        r for r in runs if r.status in ("RUNNING", "PENDING")
                    ]
                    if running_runs and session_user_id == "SYSTEM":
                        # We didn't fetch session_obj above (no new active run);
                        # fetch it now for reconcile.
                        session_obj = await svc.get_session(session_id)
                        session_user_id = session_obj.user_id

                    for r in running_runs:
                        _terminal, _plan = await _reconcile_run_completion(
                            scheduler_url=scheduler_url,
                            tenant_id=tenant_id,
                            user_id=session_user_id,
                            run=r,
                            svc=svc,
                            knowledge_ingest_url=knowledge_ingest_url,
                            redis_url=redis_url,
                            dlp_url=dlp_url,
                        )
                        if _plan:
                            latest_plan_steps = _plan
                            latest_plan_run_id = r.id
                        # Materialize any canvas payloads the worker wrote to
                        # run-state (Phase B).  Idempotent + fail-open — safe to
                        # run every poll while the run is active (a canvas created
                        # mid-run surfaces immediately) and again post-completion.
                        events.extend(
                            await _materialize_pending_canvases(
                                run=r,
                                user_id=session_user_id,
                                svc=svc,
                                tenant_id=tenant_id,
                                redis_url=redis_url,
                            )
                        )
                        # If the run we're about to stream just reached a terminal
                        # state, remember it: once its durable message rows are
                        # emitted below we can skip the (duplicate) token replay.
                        if (
                            _terminal
                            and active_run_to_stream is not None
                            and r.id == active_run_to_stream.id
                        ):
                            active_run_durably_completed = True

                    if latest_plan_steps:
                        events.append(
                            _sse(
                                "plan",
                                {
                                    "run_id": latest_plan_run_id,
                                    "plan_steps": latest_plan_steps,
                                },
                            )
                        )

                    # Emit newly persisted messages since last_seq.
                    items, _ = await svc.list_messages(
                        session_id,
                        cursor=str(last_seq) if last_seq else None,
                        limit=50,
                    )
                    # Track durable rows for *this* run specifically, not any
                    # row in the poll — an unrelated run persisting a message in
                    # the same poll must not satisfy the skip-replay guard below.
                    emitted_durable_for_active_run = False
                    active_run_id = (
                        active_run_to_stream.id
                        if active_run_to_stream is not None
                        else None
                    )
                    for msg in items:
                        msg_json = MessageOut.model_validate(msg).model_dump_json()
                        events.append(f"event: message\ndata: {msg_json}\n\n")
                        last_seq = msg.sequence
                        if active_run_id is not None and msg.run_id == active_run_id:
                            emitted_durable_for_active_run = True

                    # Only skip the token replay when durable rows for the
                    # just-completed active run were actually delivered this poll.
                    # If reconcile flagged terminal but no message rows landed
                    # for this run (e.g. an empty/edge result), fall through to
                    # the normal stream path so the client still gets its content.
                    if active_run_durably_completed and not emitted_durable_for_active_run:
                        active_run_durably_completed = False

                # ── DB block closed / committed ──────────────────────────────
                for ev in events:
                    yield ev

                # ── Token stream for a newly discovered active run ───────────
                # Driven OUTSIDE the DB session so that streaming (which can
                # last many seconds) does not hold an open DB connection.
                if active_run_to_stream is not None:
                    # Authoritative pause signal from the token stream itself.
                    # The post-stream reconcile below often still sees the run as
                    # RUNNING (scheduler lag) right after the worker writes a
                    # pending_confirmation and pauses; trusting only run.status
                    # would emit run_complete and leave the run stuck in
                    # streamed_run_ids, so the PAUSED-replay path never fires on
                    # the live connection.  run_stream_end{paused:true} closes
                    # that gap.
                    stream_signaled_pause = False

                    # Idle-recheck predicate for the active tail: a quiet gap
                    # between tool batches must NOT be mistaken for a finished
                    # stream.  ``_stream_tokens`` calls this every few idle reads
                    # to confirm the run is still RUNNING/PENDING before giving
                    # up.  It opens its OWN short-lived DB session (the buffer-
                    # then-yield discipline forbids holding a DB session across
                    # the stream) and never yields, so it is safe to run here.
                    # Bind the loop variable's value via a default arg so the
                    # closure captures THIS iteration's run id (not a later
                    # reassignment) — the predicate is only ever invoked during
                    # this iteration's _stream_tokens call, but the explicit bind
                    # keeps it correct and lint-clean.
                    async def _is_run_active(
                        _run_id: str = active_run_to_stream.id,
                    ) -> bool:
                        async for db in acquire_db_session(tenant_id=tenant_id):
                            svc = SessionService(db, tenant_id=tenant_id)
                            runs = await svc.list_runs(session_id)
                            run = next(
                                (r for r in runs if r.id == _run_id), None
                            )
                            return run is not None and run.status in (
                                "RUNNING",
                                "PENDING",
                            )
                        return False

                    if redis_url and not active_run_durably_completed:
                        async for ev in _stream_tokens(
                            redis_url=redis_url,
                            run_id=active_run_to_stream.id,
                            deadline=deadline,
                            wait_for_worker=True,
                            is_run_active=_is_run_active,
                        ):
                            if ev.startswith("event: run_stream_end") and (
                                '"paused": true' in ev or '"paused":true' in ev
                            ):
                                stream_signaled_pause = True
                            yield ev

                    # After _stream_tokens returns (end marker, pause, or stall),
                    # reconcile the run one final time to persist its terminal
                    # state + assistant message (or PAUSED state), then emit the
                    # appropriate lifecycle event.
                    #
                    # HITL PAUSED: if reconcile sets run.status="PAUSED" the run
                    # is non-terminal.  Emit ``run_paused`` instead of
                    # ``run_complete``, and REMOVE the run_id from
                    # ``streamed_run_ids`` so when the run is resumed (same
                    # run_id, status back to RUNNING) the outer loop re-enters
                    # _stream_tokens for the continuation tokens.
                    post_stream_events: list[str] = []
                    post_stream_run_paused = False
                    # Set when the tail returned early (hard cap / transient)
                    # but reconcile shows the run is STILL RUNNING/PENDING.  In
                    # that case we emit NO lifecycle event and re-tail next poll.
                    post_stream_run_still_active = False
                    post_stream_run_id = active_run_to_stream.id
                    async for db in acquire_db_session(tenant_id=tenant_id):
                        svc = SessionService(db, tenant_id=tenant_id)
                        runs = await svc.list_runs(session_id)
                        run_obj = next(
                            (r for r in runs if r.id == active_run_to_stream.id),
                            None,
                        )
                        if run_obj is not None:
                            _terminal, _plan = await _reconcile_run_completion(
                                scheduler_url=scheduler_url,
                                tenant_id=tenant_id,
                                user_id=session_user_id,
                                run=run_obj,
                                svc=svc,
                                knowledge_ingest_url=knowledge_ingest_url,
                                redis_url=redis_url,
                                dlp_url=dlp_url,
                            )
                            if _plan:
                                post_stream_events.append(
                                    _sse(
                                        "plan",
                                        {
                                            "run_id": run_obj.id,
                                            "plan_steps": _plan,
                                        },
                                    )
                                )
                            # Materialize canvas payloads written by this run's
                            # create_canvas tool (Phase B).  Placed BEFORE the
                            # message emit below so the backfilled canvas_id lands
                            # on the assistant message row we then stream out.
                            # Idempotent with the in-poll materialization above.
                            post_stream_events.extend(
                                await _materialize_pending_canvases(
                                    run=run_obj,
                                    user_id=session_user_id,
                                    svc=svc,
                                    tenant_id=tenant_id,
                                    redis_url=redis_url,
                                )
                            )
                            # Emit newly persisted messages produced by reconcile.
                            new_items, _ = await svc.list_messages(
                                session_id,
                                cursor=str(last_seq) if last_seq else None,
                                limit=50,
                            )
                            for msg in new_items:
                                msg_json = (
                                    MessageOut.model_validate(msg).model_dump_json()
                                )
                                post_stream_events.append(
                                    f"event: message\ndata: {msg_json}\n\n"
                                )
                                last_seq = msg.sequence

                            if run_obj.status == "PAUSED" or stream_signaled_pause:
                                # HITL pause: do NOT emit run_complete; instead
                                # signal the frontend that the run needs input.
                                # ``stream_signaled_pause`` makes the pause
                                # authoritative even when the scheduler has not
                                # yet surfaced status=PAUSED (lag), so the run is
                                # correctly discarded from streamed_run_ids and
                                # the PAUSED-replay path can fire on resume.
                                post_stream_run_paused = True
                                post_stream_events.append(
                                    _sse("run_paused", {"run_id": run_obj.id})
                                )
                            elif run_obj.status in (
                                "COMPLETED",
                                "FAILED",
                                "CANCELLED",
                            ):
                                post_stream_events.append(
                                    _sse(
                                        "run_complete",
                                        {
                                            "run_id": run_obj.id,
                                            "status": run_obj.status,
                                            "result_summary": run_obj.result_summary,
                                        },
                                    )
                                )
                            else:
                                # Still RUNNING/PENDING: the tail returned early
                                # (crashed-worker hard cap or a transient blip),
                                # NOT because the run finished.  Emit nothing
                                # lifecycle-wise — falsely completing here would
                                # strand the worker's remaining output.  Discard
                                # the run_id below so the next poll re-enters the
                                # active path and re-tails the stream.
                                post_stream_run_still_active = True

                    # DB session closed; safe to yield.
                    for ev in post_stream_events:
                        yield ev

                    # Remove from streamed_run_ids so the same run_id can be
                    # re-entered on the next poll's active path:
                    #   • PAUSED  → re-entered when resumed (status back to RUNNING)
                    #   • still RUNNING/PENDING (early tail return) → re-tailed so
                    #     the worker's remaining output is not stranded.
                    if post_stream_run_paused or post_stream_run_still_active:
                        streamed_run_ids.discard(post_stream_run_id)

                    # Continue the outer loop — do NOT end the stream; a future
                    # run may start on this session (e.g. the user sends another
                    # message).
                    await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                    continue

                # ── PAUSED-run replay (reconnect after HITL pause) ───────────
                # Driven OUTSIDE the DB session (same discipline as the active
                # path above).  The run is already fully written to Redis — no
                # reconcile needed, no run_complete emitted.
                if paused_run_to_replay is not None:
                    if redis_url:
                        async for ev in _stream_tokens(
                            redis_url=redis_url,
                            run_id=paused_run_to_replay.id,
                            deadline=deadline,
                            wait_for_worker=False,  # stream is fully written
                        ):
                            yield ev

                    # Emit run_paused so the frontend renders the confirm card.
                    yield _sse("run_paused", {"run_id": paused_run_to_replay.id})

                    # State-machine cleanup (see comment block near top of
                    # event_generator):
                    #   • discard from streamed_run_ids  → resume (RUNNING) can
                    #     re-enter the active path normally
                    #   • add to replayed_paused_run_ids → guard against re-
                    #     replaying on every poll while the run stays PAUSED
                    streamed_run_ids.discard(paused_run_to_replay.id)
                    replayed_paused_run_ids.add(paused_run_to_replay.id)

                    await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                    continue

                # No active run this poll — reaching here means no active tail
                # and no paused replay ran (both `continue` above), so the only
                # thing that could have been yielded is the buffered `events`
                # list.  If that too was empty, emit a ``ping`` heartbeat so the
                # client's stall watchdog can tell a quiet-but-healthy stream
                # from a silently wedged one.  A poll that DID yield events skips
                # the ping — it already proved the stream is live.
                if not events:
                    yield _sse("ping", {})
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)

            yield _sse("done", {})

        except asyncio.CancelledError:
            # Client disconnected; re-raise to let the framework clean up.
            raise
        except Exception:
            logger.exception(
                "session SSE stream terminated unexpectedly",
                extra={"session_id": session_id, "tenant_id": tenant_id},
            )
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
