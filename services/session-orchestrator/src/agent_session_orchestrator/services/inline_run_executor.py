"""Inline run executor — chat-mode runs that never touch the scheduler/sandbox.

Background
----------
The platform distinguishes two ``execution_mode`` values on a run:

* ``sandbox`` (default, unchanged) — the orchestrator relays the run to the
  agent-scheduler, which spawns an agent worker container that streams tokens
  into the Redis Stream ``agent:run:{run_id}:tokens`` and writes a
  ``result.json`` the orchestrator later reconciles.
* ``inline`` — a pure conversation turn.  There is no sandbox and no
  scheduler.  The orchestrator streams a plain OpenAI chat completion directly
  from the LLM gateway *in-process* and writes the very same Redis Stream
  entries the sandbox worker would have.  The SSE consumer
  (``api.v1.messages._stream_tokens``) and the frontend are therefore
  identical for both modes — inline is invisible on the wire.

Why the executor owns finalization
-----------------------------------
``_reconcile_run_completion`` short-circuits on ``run.sandbox_id is None``
(it polls the scheduler by container id, which an inline run does not have),
so it never persists the assistant/thinking rows for an inline run.  This
executor therefore does the finalization itself: it replays the Redis Stream
it just wrote into durable message rows via the shared
``_replay_run_blocks_into_messages`` helper (so the output-side DLP scan and
the ``already_persisted_for_run`` idempotency guard both apply exactly as they
do for the sandbox path).

Auth model
----------
The LLM gateway resolves tenant + actor from the ``X-Tenant-Id`` /
``X-User-Id`` / ``X-Actor`` request headers (blindly trusted inside the mesh),
so the orchestrator needs no bearer token to mint an ephemeral virtual key or
to call ``/chat/completions`` — it mirrors the scheduler's ephemeral-key logic
and the BFF canvas ``/chat/completions`` call, but with header auth only.

Lifecycle
---------
1. ``start_inline_run`` (called from the create_run endpoint, inside the
   request transaction after the row is flipped to RUNNING) launches
   ``_execute_inline_run`` as a background ``asyncio.Task`` and registers the
   handle in a module-level registry keyed by run id (for cancel).
2. The background task mints a per-run ephemeral key, resolves the model,
   assembles the OpenAI ``messages`` array, streams the completion into Redis,
   finalizes the run (COMPLETED/FAILED), persists messages, and revokes the
   key.  It always pushes an ``end`` marker so the SSE tail terminates.
3. ``cancel_inline_run`` cancels the registered task, pushes ``end`` and
   revokes the key (the endpoint flips the DB row to CANCELLED).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import time
from typing import Any

import httpx

from agent_session_orchestrator.config import Settings
from agent_session_orchestrator.domain.db import get_session as acquire_db_session
from agent_session_orchestrator.services.session_service import SessionService

logger = logging.getLogger(__name__)

# ── Module-level registry of in-flight inline run tasks ─────────────────────
# Maps run_id → asyncio.Task so cancel_inline_run can stop a running task.
# Single-process scope (one uvicorn worker per pod in this deployment); a
# process restart drops the registry, which is exactly why the sweeper has an
# inline-orphan branch (see reconcile_sweeper).
_inline_tasks: dict[str, asyncio.Task[None]] = {}

# Per-process default-model cache: {tenant_id: (model_id, expires_monotonic)}.
# Mirrors the BFF canvas TTL cache so a burst of inline turns doesn't re-query
# the gateway's tenant-defaults on every message.
_MODEL_CACHE_TTL_SECONDS = 300.0
_model_cache: dict[str, tuple[str, float]] = {}

# Ephemeral virtual-key TTL for an inline run.  An inline turn is short, but
# reasoning models can run for minutes; 15 min + grace comfortably covers a
# single completion while bounding the blast radius of a leaked key.
_INLINE_KEY_TTL_SECONDS = 15 * 60 + 60

# Result summary is a truncated echo of the assistant text (mirrors the
# scheduler/worker convention of a short human-readable summary on the row).
_RESULT_SUMMARY_MAX_CHARS = 2000


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def _token_stream_key(run_id: str) -> str:
    return f"agent:run:{run_id}:tokens"


def _mesh_headers(tenant_id: str, user_id: str) -> dict[str, str]:
    """Headers the gateway uses to resolve tenant/actor context (no bearer).

    The gateway's context middleware reads X-Tenant-Id / X-User-Id / X-Actor
    directly from the request (trusted inside the mesh).  ``USER`` is the
    correct actor for an interactive chat turn.
    """
    return {
        "X-Tenant-Id": tenant_id,
        "X-User-Id": user_id or "SYSTEM",
        "X-Actor": "USER",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Virtual key (mint ephemeral, fallback tenant key) — mirrors scheduler logic
# ─────────────────────────────────────────────────────────────────────────────


async def _mint_run_virtual_key(
    *, client: httpx.AsyncClient, gateway_url: str, tenant_id: str,
    user_id: str, run_id: str,
) -> str | None:
    """Mint a short-lived, run-scoped virtual key (POST /tenant-keys/ephemeral).

    Returns the ephemeral virtual key, or ``None`` if the gateway is unset /
    unreachable / lacks the endpoint — the caller then falls back to a
    long-lived tenant key so an older or briefly-unhealthy gateway never breaks
    the run.  Ported from the scheduler's ``_mint_run_virtual_key`` but with
    header auth (no bearer).
    """
    if not gateway_url:
        return None
    url = f"{gateway_url.rstrip('/')}/tenant-keys/ephemeral"
    try:
        resp = await client.post(
            url,
            json={"run_id": run_id, "ttl_seconds": _INLINE_KEY_TTL_SECONDS},
            headers=_mesh_headers(tenant_id, user_id),
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "llm-gateway unreachable while minting inline run key "
            "(falling back to tenant key): %s",
            exc,
        )
        return None
    if resp.status_code not in (200, 201):
        logger.warning(
            "llm-gateway returned HTTP %s for POST /tenant-keys/ephemeral "
            "(falling back to tenant key)",
            resp.status_code,
        )
        return None
    try:
        data = resp.json()
        key_val = data.get("virtual_key") or data.get("key") or data.get("value")
        if key_val:
            return str(key_val)
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to parse /tenant-keys/ephemeral response: %s", exc)
    return None


async def _fetch_tenant_virtual_key(
    *, client: httpx.AsyncClient, gateway_url: str, tenant_id: str, user_id: str,
) -> str | None:
    """Fallback: the tenant's first enabled long-lived virtual key (GET /tenant-keys).

    Ported from the scheduler / BFF ``_fetch_tenant_virtual_key`` parsing (list
    or {keys|data|items|results:[...]}, tolerant of key/virtual_key/value alias).
    """
    if not gateway_url:
        return None
    url = f"{gateway_url.rstrip('/')}/tenant-keys"
    try:
        resp = await client.get(
            url,
            params={"tenant_id": tenant_id},
            headers=_mesh_headers(tenant_id, user_id),
        )
    except httpx.HTTPError as exc:
        logger.warning("llm-gateway unreachable while fetching tenant key: %s", exc)
        return None
    if resp.status_code != 200:
        logger.warning("llm-gateway returned HTTP %s for GET /tenant-keys", resp.status_code)
        return None

    def _pick(item: dict) -> str | None:
        if not item.get("enabled", True):
            return None
        val = item.get("virtual_key") or item.get("key") or item.get("value")
        return str(val) if val else None

    try:
        keys = resp.json()
        if isinstance(keys, list):
            for item in keys:
                if isinstance(item, dict) and (val := _pick(item)):
                    return val
        elif isinstance(keys, dict):
            for field in ("keys", "data", "items", "results"):
                items = keys.get(field, [])
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict) and (val := _pick(item)):
                            return val
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to parse /tenant-keys response: %s", exc)
    return None


async def _revoke_run_virtual_key(
    *, client: httpx.AsyncClient, gateway_url: str, tenant_id: str,
    user_id: str, run_id: str,
) -> None:
    """Best-effort proactive revoke of the run's ephemeral key (fail-open).

    TTL expiry is the authoritative safety net; a flaky revoke only logs.
    """
    if not gateway_url:
        return
    url = f"{gateway_url.rstrip('/')}/tenant-keys/revoke-run"
    try:
        await client.post(
            url,
            json={"run_id": run_id},
            headers=_mesh_headers(tenant_id, user_id),
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "llm-gateway unreachable while revoking inline run key "
            "(TTL expiry still applies): %s",
            exc,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Default model resolution (TTL cache) — mirrors BFF canvas
# ─────────────────────────────────────────────────────────────────────────────


async def _resolve_default_model(
    *, client: httpx.AsyncClient, gateway_url: str, tenant_id: str, user_id: str,
) -> str | None:
    """Resolve the tenant default model (GET /tenant-defaults/resolve?purpose=llm).

    Cached per-tenant for ``_MODEL_CACHE_TTL_SECONDS``.  Returns ``None`` when
    the tenant has no default (caller then fails the run cleanly).
    """
    now = time.monotonic()
    cached = _model_cache.get(tenant_id)
    if cached is not None and cached[1] > now:
        return cached[0]

    if not gateway_url:
        return None
    url = f"{gateway_url.rstrip('/')}/tenant-defaults/resolve"
    try:
        resp = await client.get(
            url,
            params={"purpose": "llm"},
            headers=_mesh_headers(tenant_id, user_id),
        )
    except httpx.HTTPError as exc:
        logger.warning("llm-gateway unreachable while resolving default model: %s", exc)
        return None
    if resp.status_code != 200:
        logger.warning(
            "llm-gateway returned HTTP %s for GET /tenant-defaults/resolve",
            resp.status_code,
        )
        return None
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to parse /tenant-defaults/resolve response: %s", exc)
        return None
    if isinstance(data, dict):
        model_id = data.get("model_id")
        if model_id:
            _model_cache[tenant_id] = (str(model_id), now + _MODEL_CACHE_TTL_SECONDS)
            return str(model_id)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI messages assembly
# ─────────────────────────────────────────────────────────────────────────────


def build_openai_messages(
    *,
    sys_prompt: str | None,
    memory: str,
    history: list[dict[str, Any]],
    task: str,
) -> list[dict[str, Any]]:
    """Assemble the OpenAI ``messages`` array for an inline completion.

    Unlike the sandbox worker (which folds history into a single system
    prompt), inline runs map the structured history directly onto native
    OpenAI roles so multi-turn context is first-class:

    * an optional ``system`` message combining ``sys_prompt`` + ``memory``;
    * one message per history item — ``user`` / ``assistant`` text turns and
      ``tool`` turns flattened to a readable ``assistant`` note (there is no
      tool execution on the inline path, so we surface prior tool activity as
      context rather than as OpenAI ``tool`` role frames, which would require
      matching ``tool_calls`` on a preceding assistant message);
    * the current ``user`` task as the final message.
    """
    messages: list[dict[str, Any]] = []

    system_parts: list[str] = []
    if sys_prompt and sys_prompt.strip():
        system_parts.append(sys_prompt.strip())
    if memory and memory.strip():
        system_parts.append("相关记忆（供参考）：\n" + memory.strip())
    if system_parts:
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})

    for item in history:
        role = item.get("role")
        if role == "user":
            content = str(item.get("content") or "")
            if content:
                messages.append({"role": "user", "content": content})
        elif role == "assistant":
            content = str(item.get("content") or "")
            if content:
                messages.append({"role": "assistant", "content": content})
        elif role == "tool":
            tool = item.get("tool") or {}
            if isinstance(tool, dict) and tool:
                name = str(tool.get("name") or "tool")
                result = tool.get("result")
                note = (
                    f"[工具 {name} 的历史调用结果]\n{result}"
                    if result
                    else f"[工具 {name} 曾被调用]"
                )
                messages.append({"role": "assistant", "content": note})

    messages.append({"role": "user", "content": task})
    return messages


# ─────────────────────────────────────────────────────────────────────────────
# Redis stream writer
# ─────────────────────────────────────────────────────────────────────────────


async def _open_redis(redis_url: str) -> Any | None:
    try:
        import redis.asyncio as aioredis  # type: ignore[import-not-found]

        return aioredis.Redis.from_url(
            redis_url,
            socket_connect_timeout=2,
            socket_timeout=2,
            decode_responses=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("inline run: Redis connect failed: %s", exc)
        return None


async def _xadd(client: Any, run_id: str, entry: dict[str, Any]) -> None:
    """Append one entry to the run token stream.  Fail-open (Redis is hot-path)."""
    if client is None:
        return
    try:
        await client.xadd(_token_stream_key(run_id), entry)
    except Exception as exc:  # noqa: BLE001
        logger.warning("inline run: XADD failed for %s: %s", run_id, exc)


async def _push_end(redis_url: str | None, run_id: str) -> None:
    """Push the authoritative ``end`` marker so the SSE tail terminates.

    Used on the cancel / orphan paths where we don't already hold a Redis
    handle.  Fail-open.
    """
    if not redis_url:
        return
    client = await _open_redis(redis_url)
    if client is None:
        return
    try:
        await _xadd(client, run_id, {"type": "end"})
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()


# ─────────────────────────────────────────────────────────────────────────────
# Streaming completion
# ─────────────────────────────────────────────────────────────────────────────


async def _stream_completion_into_redis(
    *,
    client: httpx.AsyncClient,
    redis_client: Any,
    gateway_url: str,
    tenant_id: str,
    user_id: str,
    session_id: str,
    run_id: str,
    virtual_key: str,
    model: str,
    messages: list[dict[str, Any]],
    sampling_params: dict[str, Any],
    reasoning_effort: str | None = None,
) -> tuple[dict[str, int], str]:
    """POST a streaming chat completion and fan its SSE chunks into Redis.

    Returns ``(usage, body_text)`` where ``usage`` is
    ``{input_tokens, output_tokens, total_tokens}`` (zeros when the gateway did
    not surface usage) and ``body_text`` is the concatenated assistant content
    (used for result_summary + memory write-back, so the finalizer never has to
    re-read Redis).

    Wire handling mirrors the gateway's own streaming passthrough:

    * each ``data: {json}`` line is a chunk; ``choices[0].delta.content`` is a
      body token → XADD a ``delta`` entry; ``delta.reasoning_content`` is a
      DeepSeek-style reasoning token → **accumulate** (never push per-token —
      historical bug fragmented thinking into N blocks) and flush a single
      ``thinking`` entry when reasoning ends (first body token or stream end);
    * a trailing frame with empty ``choices`` and populated ``usage`` carries
      the token totals;
    * ``data: [DONE]`` ends the stream; a ``{"error": {...}}`` frame raises so
      the caller marks the run FAILED (and never writes the error as a reply).
    """
    headers = _mesh_headers(tenant_id, user_id)
    headers["X-LLM-Virtual-Key"] = virtual_key
    headers["Accept"] = "text/event-stream"

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        # The gateway forces include_usage itself, but sending it is harmless
        # (unknown fields are ignored) and keeps the intent explicit / robust
        # to a gateway that stops forcing it.
        "stream_options": {"include_usage": True},
        # Attribution + affinity routing: the gateway reads run_id/user_id/
        # session_id from metadata for usage records, billing and audit.
        "metadata": {"run_id": run_id, "user_id": user_id, "session_id": session_id},
    }
    # Apply per-run sampling params the gateway understands (temperature,
    # max_tokens, top_p, stop).  Unknown keys are ignored by the gateway schema.
    for key in ("temperature", "max_tokens", "top_p", "stop"):
        if key in sampling_params and sampling_params[key] is not None:
            payload[key] = sampling_params[key]

    # reasoning_effort: original passthrough, no clamp; None omits the field.
    # NB: the current dev llm-gateway parses the request into a typed
    # ChatCompletionRequest (extra="ignore", no reasoning_effort field) and
    # rebuilds the upstream payload field-by-field, so it accepts this field
    # (HTTP 200) but drops it before forwarding to the provider.  Sending it is
    # a forward-compatible no-op — it begins to take effect the instant the
    # gateway adds reasoning_effort to its request model.
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort

    usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    reasoning_buf: list[str] = []
    body_buf: list[str] = []
    thinking_flushed = False

    async def _flush_thinking() -> None:
        nonlocal thinking_flushed
        if reasoning_buf and not thinking_flushed:
            text = "".join(reasoning_buf)
            if text:
                await _xadd(redis_client, run_id, {"type": "thinking", "text": text})
            thinking_flushed = True

    url = f"{gateway_url.rstrip('/')}/chat/completions"
    async with client.stream("POST", url, json=payload, headers=headers) as resp:
        if resp.status_code >= 400:
            body = await resp.aread()
            snippet = body.decode(errors="replace")[:200] if body else ""
            raise InlineGatewayError(
                f"gateway returned HTTP {resp.status_code}: {snippet}"
            )
        async for line in resp.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            data_str = line[len("data:"):].strip()
            if not data_str:
                continue
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            # Explicit error frame from the gateway/provider passthrough.
            if isinstance(chunk, dict) and chunk.get("error"):
                err = chunk["error"]
                msg = err.get("message") if isinstance(err, dict) else str(err)
                raise InlineGatewayError(f"gateway stream error: {msg}")

            u = chunk.get("usage") or {}
            if u:
                # DeepSeek/OpenAI usage keys → our column names.
                pt = u.get("prompt_tokens")
                ct = u.get("completion_tokens")
                tt = u.get("total_tokens")
                if pt is not None:
                    usage["input_tokens"] = int(pt)
                if ct is not None:
                    usage["output_tokens"] = int(ct)
                if tt is not None:
                    usage["total_tokens"] = int(tt)

            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}

            reasoning = delta.get("reasoning_content")
            if reasoning:
                reasoning_buf.append(str(reasoning))

            content = delta.get("content")
            if content:
                # First body token marks the end of the reasoning phase.
                await _flush_thinking()
                text = str(content)
                body_buf.append(text)
                await _xadd(redis_client, run_id, {"type": "delta", "text": text})

    # Stream ended without any body token after reasoning (rare) — flush now so
    # the reasoning block is not lost.
    await _flush_thinking()

    if usage["total_tokens"] == 0 and (usage["input_tokens"] or usage["output_tokens"]):
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage, "".join(body_buf)


class InlineGatewayError(Exception):
    """Raised when the gateway stream reports an error (HTTP >=400 or error frame)."""


# ─────────────────────────────────────────────────────────────────────────────
# Background executor
# ─────────────────────────────────────────────────────────────────────────────


async def _execute_inline_run(
    *,
    settings: Settings,
    tenant_id: str,
    user_id: str,
    session_id: str,
    run_id: str,
    task: str,
    model_hint: str | None,
    sys_prompt: str | None,
    memory: str,
    history: list[dict[str, Any]],
    sampling_params: dict[str, Any],
    reasoning_effort: str | None = None,
) -> None:
    """Run one inline chat completion to terminal state.

    Owns the full inline lifecycle (mint key → resolve model → stream → persist
    → finalize → revoke).  Never raises to the event loop: every failure marks
    the run FAILED, pushes ``end`` and returns.  The gateway error body is NEVER
    written as an assistant message (a 402 quota error must not look like a
    reply).
    """
    gateway_url = settings.llm_gateway_url
    redis_url = settings.redis_url
    dlp_url = settings.foundation_dlp_url
    knowledge_ingest_url = settings.knowledge_ingest_url

    redis_client = await _open_redis(redis_url) if redis_url else None
    virtual_key: str | None = None
    error_message: str | None = None
    final_status = "FAILED"
    usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    body_text = ""

    try:
        async with httpx.AsyncClient(timeout=settings.inline_completion_timeout) as client:
            # 1. Per-run ephemeral key (fallback to tenant key).
            virtual_key = await _mint_run_virtual_key(
                client=client, gateway_url=gateway_url, tenant_id=tenant_id,
                user_id=user_id, run_id=run_id,
            )
            if not virtual_key:
                virtual_key = await _fetch_tenant_virtual_key(
                    client=client, gateway_url=gateway_url,
                    tenant_id=tenant_id, user_id=user_id,
                )
            if not virtual_key:
                raise InlineGatewayError(
                    "tenant has no usable virtual key (mint + fetch both failed)"
                )

            # 2. Model: request hint wins; else tenant default; else fail.
            model = model_hint or None
            if not model:
                model = await _resolve_default_model(
                    client=client, gateway_url=gateway_url,
                    tenant_id=tenant_id, user_id=user_id,
                )
            if not model:
                raise InlineGatewayError(
                    "no model provided and tenant has no default model"
                )

            # 3. Assemble OpenAI messages (native roles, not folded into system).
            messages = build_openai_messages(
                sys_prompt=sys_prompt, memory=memory, history=history, task=task,
            )

            # 4. Stream the completion into Redis (delta / thinking entries).
            usage, body_text = await _stream_completion_into_redis(
                client=client,
                redis_client=redis_client,
                gateway_url=gateway_url,
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                run_id=run_id,
                virtual_key=virtual_key,
                model=model,
                messages=messages,
                sampling_params=sampling_params,
                reasoning_effort=reasoning_effort,
            )
            final_status = "COMPLETED"
    except asyncio.CancelledError:
        # cancel_inline_run handles DB status + end + revoke; just propagate so
        # the task actually stops.  Do NOT push end here (the canceller does).
        logger.info("inline run cancelled", extra={"run_id": run_id})
        raise
    except InlineGatewayError as exc:
        error_message = str(exc)
        logger.warning("inline run gateway error", extra={"run_id": run_id, "err": error_message})
    except Exception as exc:  # noqa: BLE001 — any failure → FAILED, never crash the loop
        error_message = f"inline run failed: {exc}"
        logger.exception("inline run unexpected error", extra={"run_id": run_id})
    finally:
        # Always terminate the SSE tail, whatever happened above.
        await _xadd(redis_client, run_id, {"type": "end"})
        if redis_client is not None:
            with contextlib.suppress(Exception):
                await redis_client.aclose()

    # ── Finalize the run row + persist messages in a fresh tenant-bound txn ──
    # (The request transaction that created the run is long gone.)  Failure-open
    # per-step so a persistence hiccup still lands a terminal status.
    #
    # Row-visibility guard: the create_run request commits the RUNNING row only
    # in its dependency-teardown, AFTER this task was scheduled.  In practice the
    # first mint/stream await yields long enough for that commit to land, but a
    # very fast failure path (e.g. gateway unreachable) can reach finalize before
    # the commit is visible in a fresh transaction.  Retry the lookup a few times
    # with a short backoff so we never strand a just-created run as PENDING.
    finalized = False
    for _attempt in range(10):
        try:
            async for db in acquire_db_session(
                tenant_id=tenant_id, user_id=user_id or "SYSTEM", actor="USER"
            ):
                svc = SessionService(db, tenant_id=tenant_id)
                runs = await svc.list_runs(session_id)
                run = next((r for r in runs if r.id == run_id), None)
                if run is None:
                    # Row not yet committed by the request txn — retry below.
                    break

                run.status = final_status
                run.finished_at = _now()
                run.input_tokens = usage["input_tokens"] or None
                run.output_tokens = usage["output_tokens"] or None
                run.total_tokens = usage["total_tokens"] or None
                if error_message:
                    run.error_message = error_message

                if final_status == "COMPLETED" and redis_url:
                    # Replay the stream we just wrote into durable message rows.
                    # Reuses the shared helper so the output-side DLP scan and the
                    # already_persisted_for_run idempotency guard both apply, and
                    # the assistant/thinking role mapping matches the sandbox path.
                    from agent_session_orchestrator.api.v1.messages import (
                        _replay_run_blocks_into_messages,  # noqa: PLC0415
                    )

                    await _replay_run_blocks_into_messages(
                        run=run,
                        svc=svc,
                        tenant_id=tenant_id,
                        redis_url=redis_url,
                        dlp_url=dlp_url,
                    )
                if final_status == "COMPLETED" and body_text:
                    # Truncated echo of the reply as result_summary (row-level
                    # convenience; the durable rows are the authoritative reply).
                    run.result_summary = body_text[:_RESULT_SUMMARY_MAX_CHARS]
                finalized = True
        except Exception:  # noqa: BLE001
            logger.exception("inline run finalize attempt failed", extra={"run_id": run_id})
            break
        if finalized:
            break
        await asyncio.sleep(0.2)

    if not finalized:
        logger.warning(
            "inline run finalize: run row not visible after retries "
            "(sweeper will orphan-fail it)",
            extra={"run_id": run_id},
        )

    # ── Memory write-back (COMPLETED only, failure-open) ────────────────────
    if final_status == "COMPLETED" and knowledge_ingest_url and body_text:
        try:
            from agent_session_orchestrator.api.v1.messages import (
                _write_memory,  # noqa: PLC0415
            )

            await _write_memory(
                knowledge_ingest_url=knowledge_ingest_url,
                tenant_id=tenant_id,
                user_id=user_id,
                task_description=task,
                summary=body_text,
                session_id=session_id,
            )
        except Exception:  # noqa: BLE001
            logger.debug("inline run memory write-back failed (failure-open)")

    # ── Revoke the ephemeral key (best-effort) ──────────────────────────────
    if virtual_key:
        try:
            async with httpx.AsyncClient(timeout=settings.inline_meta_timeout) as client:
                await _revoke_run_virtual_key(
                    client=client, gateway_url=gateway_url,
                    tenant_id=tenant_id, user_id=user_id, run_id=run_id,
                )
        except Exception:  # noqa: BLE001
            logger.debug("inline run key revoke failed (failure-open)")


# ─────────────────────────────────────────────────────────────────────────────
# Public entry points
# ─────────────────────────────────────────────────────────────────────────────


def start_inline_run(
    *,
    settings: Settings,
    tenant_id: str,
    user_id: str,
    session_id: str,
    run_id: str,
    task: str,
    model_hint: str | None,
    sys_prompt: str | None,
    memory: str,
    history: list[dict[str, Any]],
    sampling_params: dict[str, Any],
    reasoning_effort: str | None = None,
) -> None:
    """Launch the background inline-run task and register it for cancel.

    Called from the create_run endpoint AFTER the run row has been flipped to
    RUNNING and committed by the surrounding request transaction — the task
    finalizes in its own fresh transaction, so it must not observe the row as
    PENDING.  Fire-and-forget: the HTTP response returns immediately.

    ``reasoning_effort`` is passed through verbatim to the /chat/completions
    payload when non-None (see ``_stream_completion_into_redis``).
    """
    task_obj = asyncio.create_task(
        _execute_inline_run(
            settings=settings,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            task=task,
            model_hint=model_hint,
            sys_prompt=sys_prompt,
            memory=memory,
            history=history,
            sampling_params=sampling_params,
            reasoning_effort=reasoning_effort,
        ),
        name=f"inline-run-{run_id}",
    )
    _inline_tasks[run_id] = task_obj

    def _cleanup(_t: asyncio.Task[None], _rid: str = run_id) -> None:
        # Drop the handle once the task settles so the registry never leaks.
        if _inline_tasks.get(_rid) is _t:
            _inline_tasks.pop(_rid, None)

    task_obj.add_done_callback(_cleanup)


async def cancel_inline_run(
    *, settings: Settings, tenant_id: str, user_id: str, run_id: str,
) -> None:
    """Cancel an in-flight inline run: stop the task, push ``end``, revoke key.

    The caller (endpoint) flips the DB row to CANCELLED and writes the system
    message.  This only tears down the in-process side effects.  Idempotent:
    a run with no registered task (already finished / process restarted) still
    gets an ``end`` marker and a key revoke.
    """
    task_obj = _inline_tasks.pop(run_id, None)
    if task_obj is not None and not task_obj.done():
        task_obj.cancel()
        # Await the cancelled task so it fully unwinds before we push end/revoke.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task_obj

    # Terminate the SSE tail and best-effort revoke the ephemeral key.
    await _push_end(settings.redis_url, run_id)
    if settings.llm_gateway_url:
        try:
            async with httpx.AsyncClient(timeout=settings.inline_meta_timeout) as client:
                await _revoke_run_virtual_key(
                    client=client, gateway_url=settings.llm_gateway_url,
                    tenant_id=tenant_id, user_id=user_id, run_id=run_id,
                )
        except Exception:  # noqa: BLE001
            logger.debug("inline cancel key revoke failed (failure-open)")


def has_inline_task(run_id: str) -> bool:
    """True if this process holds a live task for ``run_id`` (used by cancel)."""
    task_obj = _inline_tasks.get(run_id)
    return task_obj is not None and not task_obj.done()
