"""Inline run executor — chat-mode runs that never touch the scheduler/sandbox.

Background
----------
The platform distinguishes two ``execution_mode`` values on a run:

* ``sandbox`` (default, unchanged) — the orchestrator relays the run to the
  agent-scheduler, which spawns an agent worker container that streams tokens
  into the Redis Stream ``agent:run:{run_id}:tokens`` and writes a
  ``result.json`` the orchestrator later reconciles.
* ``inline`` — a no-sandbox conversation turn.  There is no scheduler, but the
  trusted orchestrator can still discover and call tenant HTTP MCP tools.  It
  drives the OpenAI completion/tool loop *in-process* and writes the same Redis
  Stream entries the sandbox worker would have.  The SSE consumer
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
from agent_session_orchestrator.services import (
    attachment_text_extractor,
    doc_to_markdown_client,
    inline_knowledge_client,
    inline_mcp_client,
    inline_skill_loader,
)
from agent_session_orchestrator.services.canvas_tool_spec import (
    CANVAS_FORCING_PROMPT,
    CREATE_CANVAS_TOOL,
    CREATE_CANVAS_TOOL_NAME,
    canvas_confirmation_reply,
    extract_creation_title,
    is_written_creation_prompt,
)
from agent_session_orchestrator.services.knowledge_tool_spec import (
    KNOWLEDGE_SYSTEM_PROMPT,
    LIST_KNOWLEDGE_TOOL,
    LIST_KNOWLEDGE_TOOL_NAME,
    SEARCH_KNOWLEDGE_TOOL,
    SEARCH_KNOWLEDGE_TOOL_NAME,
)
from agent_session_orchestrator.services.session_service import SessionService
from agent_session_orchestrator.services.skill_tool_spec import (
    READ_SKILL_TOOL,
    READ_SKILL_TOOL_NAME,
    build_skills_catalog_prompt,
)

logger = logging.getLogger(__name__)

# Maximum tool-calling rounds for an inline run.  Round 1 lets the model call a
# tool (``create_canvas`` and/or ``read_skill``); subsequent rounds let it read
# more skill files, then emit its final reply after the tool results are fed back.
# Raised from 2 to 6 to accommodate multi-file skill reading (each read_skill is a
# round) while still bounding a misbehaving model.  The canvas semantics are
# unchanged by the higher cap: ``tool_choice`` is only ever forced on round 1 and
# the single-canvas-per-run guard no-ops any repeat create_canvas call.
_INLINE_MAX_TOOL_ROUNDS = 6

# Rescue threshold (chars) for the deterministic canvas fallback: when a forced
# written-creation run finishes WITHOUT a canvas, ≥ this much accumulated body
# text means the model wrote the whole piece into the chat reply (hijack form) and
# that body is materialized as-is; below it the reply is a lie/stub ("已为你创作…"
# with no real content) and a silent extra completion is issued to fetch the full
# text.  Deliberately looser than the materialization guard's own 200-char/2-block
# rule (``_CANVAS_MIN_CHARS``): this only PICKS the branch — ``materialize_one_canvas``
# still applies the stricter guard and fail-opens if the text is too thin.
_RESCUE_BODY_MIN_CHARS = 120

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
    extra_system: str | None = None,
    knowledge: str = "",
) -> list[dict[str, Any]]:
    """Assemble the OpenAI ``messages`` array for an inline completion.

    Unlike the sandbox worker (which folds history into a single system
    prompt), inline runs map the structured history directly onto native
    OpenAI roles so multi-turn context is first-class:

    * an optional ``system`` message combining ``sys_prompt`` + ``memory`` +
      ``knowledge`` + ``extra_system`` (the canvas forcing block, when the tool
      is enabled);
    * one message per history item — ``user`` / ``assistant`` text turns and
      ``tool`` turns flattened to a readable ``assistant`` note (prior tool
      activity is surfaced as context rather than as OpenAI ``tool`` role frames,
      which would require matching ``tool_calls`` on a preceding assistant
      message; the live create_canvas tool loop appends its own native frames);
    * the current ``user`` task as the final message.

    The prefetch splits into two distinct blocks: ``memory`` ("相关记忆（供参考）"
    — this user's own recalled facts, wording unchanged) and ``knowledge``
    ("知识库资料" — tenant knowledge-base hits, each already carrying its
    【文档名 | 文件夹路径】 provenance from ``inline_knowledge_client``).
    """
    messages: list[dict[str, Any]] = []

    system_parts: list[str] = []
    if sys_prompt and sys_prompt.strip():
        system_parts.append(sys_prompt.strip())
    if memory and memory.strip():
        system_parts.append("相关记忆（供参考）：\n" + memory.strip())
    if knowledge and knowledge.strip():
        system_parts.append("知识库资料：\n" + knowledge.strip())
    if extra_system and extra_system.strip():
        system_parts.append(extra_system.strip())
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

    ``end`` is a promise that run.status is already terminal in the DB (the
    post-stream reconcile relies on it to emit run_complete).  Used by the
    normal executor path (after finalize commits), cancel and orphan paths —
    all of which don't hold a Redis handle anymore.  Fail-open.
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


def _assemble_tool_calls(fragments: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse accumulated ``delta.tool_calls`` fragments into OpenAI tool_calls.

    OpenAI/DeepSeek stream a tool call across many chunks: the first fragment at a
    given ``index`` carries ``id`` + ``function.name``, later fragments append to
    ``function.arguments``.  ``fragments`` is the per-index accumulator; this
    returns the completed ``[{id, type, function: {name, arguments}}]`` list
    ordered by index (empty when the model made no tool call).
    """
    calls: list[dict[str, Any]] = []
    for index in sorted(fragments):
        frag = fragments[index]
        name = str(frag.get("name") or "")
        if not name:
            continue
        calls.append(
            {
                "id": str(frag.get("id") or f"call_{index}"),
                "type": "function",
                "function": {"name": name, "arguments": frag.get("arguments") or ""},
            }
        )
    return calls


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
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any | None = None,
) -> tuple[dict[str, int], str, list[dict[str, Any]], str | None]:
    """POST a streaming chat completion and fan its SSE chunks into Redis.

    Returns ``(usage, body_text, tool_calls, finish_reason)`` where ``usage`` is
    ``{input_tokens, output_tokens, total_tokens}`` (zeros when the gateway did
    not surface usage), ``body_text`` is the concatenated assistant content (used
    for result_summary + memory write-back, so the finalizer never has to re-read
    Redis), ``tool_calls`` is the assembled OpenAI tool-call list for this round
    (empty when the model made none), and ``finish_reason`` is the terminal reason
    from the last non-empty choice (``"tool_calls"`` when the model wants a tool).

    Wire handling mirrors the gateway's own streaming passthrough:

    * each ``data: {json}`` line is a chunk; ``choices[0].delta.content`` is a
      body token → XADD a ``delta`` entry; ``delta.reasoning_content`` is a
      DeepSeek-style reasoning token → **accumulate** (never push per-token —
      historical bug fragmented thinking into N blocks) and flush a single
      ``thinking`` entry when reasoning ends (first body token or stream end);
    * ``delta.tool_calls`` fragments are accumulated by index (never streamed to
      Redis — the canvas result is surfaced as a synthesized ``canvas_created``
      event by the caller, not as raw tool tokens);
    * a trailing frame with empty ``choices`` and populated ``usage`` carries
      the token totals;
    * ``data: [DONE]`` ends the stream; a ``{"error": {...}}`` frame raises so
      the caller marks the run FAILED (and never writes the error as a reply).

    ``tools`` / ``tool_choice`` are sent verbatim when provided (OpenAI schema);
    when ``None`` the request is a plain completion (backward-compatible).
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

    # Tools (OpenAI function schema) + optional tool_choice.  Only sent when the
    # caller advertises tools (canvas enabled); a plain turn omits both fields so
    # the request is byte-identical to the pre-canvas behaviour.
    if tools:
        payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

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
    tool_fragments: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    # Count of delta/thinking entries already XADDed to the token stream.  Carried
    # on InlineGatewayError so the caller knows whether a retry is safe (0 ⇒ the
    # frontend saw nothing from this round yet).
    streamed_entries = 0

    async def _flush_thinking() -> None:
        nonlocal streamed_entries, thinking_flushed
        if reasoning_buf and not thinking_flushed:
            text = "".join(reasoning_buf)
            if text:
                await _xadd(redis_client, run_id, {"type": "thinking", "text": text})
                streamed_entries += 1
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
                raise InlineGatewayError(
                    f"gateway stream error: {msg}",
                    streamed_entries=streamed_entries,
                )

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
            choice0 = choices[0]
            if choice0.get("finish_reason"):
                finish_reason = str(choice0["finish_reason"])
            delta = choice0.get("delta") or {}

            reasoning = delta.get("reasoning_content")
            if reasoning:
                reasoning_buf.append(str(reasoning))

            # Accumulate tool-call fragments by index (id/name arrive first, then
            # arguments stream in over subsequent chunks).
            for frag in delta.get("tool_calls") or []:
                index = int(frag.get("index") or 0)
                slot = tool_fragments.setdefault(
                    index, {"id": None, "name": None, "arguments": ""}
                )
                if frag.get("id"):
                    slot["id"] = str(frag["id"])
                fn = frag.get("function") or {}
                if fn.get("name"):
                    slot["name"] = str(fn["name"])
                if fn.get("arguments"):
                    slot["arguments"] += str(fn["arguments"])

            content = delta.get("content")
            if content:
                # First body token marks the end of the reasoning phase.
                await _flush_thinking()
                text = str(content)
                body_buf.append(text)
                await _xadd(redis_client, run_id, {"type": "delta", "text": text})
                streamed_entries += 1

    # Stream ended without any body token after reasoning (rare) — flush now so
    # the reasoning block is not lost.
    await _flush_thinking()

    if usage["total_tokens"] == 0 and (usage["input_tokens"] or usage["output_tokens"]):
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage, "".join(body_buf), _assemble_tool_calls(tool_fragments), finish_reason


class InlineGatewayError(Exception):
    """Raised when the gateway stream reports an error (HTTP >=400 or error frame).

    ``streamed_entries`` counts the delta/thinking entries already written to the
    run token stream before the error surfaced.  ``0`` means the failure happened
    before anything reached the frontend, so a caller may safely retry the round
    (e.g. the named-tool_choice downgrade in ``_run_completion_with_tools``);
    anything greater means a retry would duplicate content the user already saw.
    """

    def __init__(self, message: str, *, streamed_entries: int = 0) -> None:
        super().__init__(message)
        self.streamed_entries = streamed_entries


# ─────────────────────────────────────────────────────────────────────────────
# In-process canvas materialization (model tool call → DB + SSE)
# ─────────────────────────────────────────────────────────────────────────────


def _parse_tool_arguments(raw: str) -> dict[str, Any]:
    """Parse an OpenAI tool-call ``arguments`` JSON string, tolerant of junk."""
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


async def _inline_tool_call_dlp_decision(
    *,
    dlp_url: str,
    tenant_id: str,
    user_id: str,
    run_id: str,
    tool_call_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[bool, str]:
    """Apply the same fail-open pre-tool DLP posture as sandbox workers.

    Only an explicit, successfully parsed non-ALLOW verdict blocks the call;
    configuration, network and response errors all log and allow so a DLP
    outage cannot disable chat.  The result text is returned to the model when
    denied, keeping the OpenAI tool-call frame valid without executing MCP.
    """
    if not dlp_url:
        return True, ""
    resource_id = f"{tool_name}:{tool_call_id or run_id}"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.post(
                f"{dlp_url.rstrip('/')}/core/scan",
                json={
                    "content": json.dumps(arguments, ensure_ascii=False, default=str),
                    "resource_type": "tool_call",
                    "resource_id": resource_id,
                },
                headers=_mesh_headers(tenant_id, user_id),
            )
    except httpx.HTTPError as exc:
        logger.warning(
            "inline tool DLP unreachable (fail-open): %s",
            exc,
            extra={"run_id": run_id, "tool": tool_name},
        )
        return True, ""
    if response.status_code != 200:
        logger.warning(
            "inline tool DLP returned HTTP %s (fail-open)",
            response.status_code,
            extra={"run_id": run_id, "tool": tool_name},
        )
        return True, ""
    try:
        body = response.json()
    except ValueError:
        logger.warning(
            "inline tool DLP response unparseable (fail-open)",
            extra={"run_id": run_id, "tool": tool_name},
        )
        return True, ""
    action = str(body.get("action") or "") if isinstance(body, dict) else ""
    if action == "ALLOW":
        return True, ""
    sensitivity = (
        str(body.get("max_sensitivity") or "unknown")
        if isinstance(body, dict)
        else "unknown"
    )
    reason = f"DLP 已阻止该工具调用（action={action or '?'}, sensitivity={sensitivity}）"
    logger.warning(
        "inline tool DLP denied call",
        extra={"run_id": run_id, "tool": tool_name, "action": action},
    )
    return False, reason


async def _materialize_inline_canvas(
    *,
    settings: Settings,
    tenant_id: str,
    user_id: str,
    session_id: str,
    run_id: str,
    redis_client: Any,
    args: dict[str, Any],
) -> bool:
    """Materialize a canvas from an inline ``create_canvas`` tool call, in-process.

    Unlike the sandbox path (which parks payloads in Redis hot-state for the SSE
    poll to materialize), the inline path materializes directly: it acquires a
    short tenant-bound DB session, creates the canvas via the shared
    ``materialize_one_canvas`` helper (identical guard + create + message-backfill
    semantics as the sandbox reconcile path) and — same shape as the sandbox path
    — writes a ``canvas_created`` entry into the run token stream so ``stream_session``
    relays it to the frontend unchanged.

    The assistant message row does not exist yet at this point (it is persisted at
    finalize from the replayed stream), so ``backfill_message_canvas_id`` finds no
    target and the event carries only ``canvas_id`` + ``run_id`` — the frontend
    renders the card from ``canvas_id`` and the finalizer re-runs the backfill once
    the row exists.

    Returns ``True`` when a canvas was created, ``False`` when the guard rejected
    the payload or anything failed (fail-open: design invariant — a canvas failure
    never fails the run; the reply text still flows through the normal path).
    """
    content_markdown = str(args.get("content_markdown") or "")
    title = args.get("title")
    kind = str(args.get("kind") or "article")

    # ``get_session`` commits in its post-``yield`` teardown, so the loop body
    # must run to completion — an early ``return``/``break`` abandons the
    # generator and the ``session.begin()`` context rolls the transaction back
    # instead of committing.  Buffer outcomes in locals and emit the SSE entry
    # only after the loop (i.e. after the commit), same convention as the
    # ``stream_session`` buffering in ``api/v1/messages.py``.
    created = False
    event_entry: dict[str, Any] | None = None
    try:
        # Local imports keep the canvas service + messages helpers off the hot
        # import path for runs that never touch canvases.
        from agent_session_orchestrator.api.v1.messages import (
            materialize_one_canvas,  # noqa: PLC0415
        )
        from agent_session_orchestrator.services.canvas_service import (
            CanvasService,  # noqa: PLC0415
        )

        async for db in acquire_db_session(
            tenant_id=tenant_id, user_id=user_id or "SYSTEM", actor="USER"
        ):
            svc = SessionService(db, tenant_id=tenant_id)
            runs = await svc.list_runs(session_id)
            run = next((r for r in runs if r.id == run_id), None)
            canvas_svc = CanvasService(db, tenant_id=tenant_id)
            if run is None:
                # The RUNNING row may not be visible yet in this fresh txn; a
                # missed canvas is fail-open (the finalizer does not retry canvas
                # creation, but the reply body is unaffected).
                logger.info(
                    "inline canvas: run row not visible yet, skipping",
                    extra={"run_id": run_id},
                )
            elif await canvas_svc.count_canvases_for_run(run_id) > 0:
                # Single-canvas-per-run cap (matches the sandbox over-trigger
                # semantics for the inline path): if a canvas already exists for
                # this run, a repeat tool call is a no-op — the model gets the
                # same confirmation without a second document.
                logger.info(
                    "inline canvas: run already has a canvas, skipping duplicate",
                    extra={"run_id": run_id},
                )
            else:
                result = await materialize_one_canvas(
                    canvas_svc=canvas_svc,
                    run=run,
                    user_id=user_id,
                    kind=kind,
                    title=str(title or "").strip() or None,
                    content_markdown=content_markdown,
                )
                if result is not None:
                    canvas_id, message_id = result
                    # Buffered ``canvas`` token-stream entry so ``stream_session``
                    # relays a ``canvas_created`` SSE — the exact event the sandbox
                    # path yields, so the frontend is unchanged.  ``_stream_tokens``
                    # translates this entry type (Redis coerces all values to
                    # strings; ``message_id`` is omitted when None so the SSE
                    # payload matches the sandbox shape).
                    event_entry = {
                        "type": "canvas",
                        "canvas_id": canvas_id,
                        "run_id": run_id,
                    }
                    if message_id is not None:
                        event_entry["message_id"] = message_id
                    created = True
                # ``result is None`` = below the guard threshold — skipped
                # (logged inside the helper).

        if event_entry is not None:
            # Past the loop the transaction is committed — the frontend never
            # sees an event for a canvas that failed to persist.
            await _xadd(redis_client, run_id, event_entry)
            logger.info(
                "inline canvas materialized from tool call",
                extra={"run_id": run_id, "canvas_id": event_entry["canvas_id"]},
            )
    except Exception:  # noqa: BLE001 — fail-open: never fail the run on canvas error
        logger.warning(
            "inline canvas materialization failed (fail-open)",
            extra={"run_id": run_id},
            exc_info=True,
        )
        # ``created`` stays truthful: a post-commit relay failure still created
        # the document (finalize re-anchors it; hydration renders it on reload).
    return created


async def _backfill_inline_run_canvases(
    *, svc: SessionService, run: Any, tenant_id: str
) -> None:
    """Re-anchor the run's canvas(es) onto the now-persisted assistant message.

    A canvas created mid-stream (``_materialize_inline_canvas``) could not anchor
    its ``message.canvas_id`` because the assistant row is only persisted at
    finalize.  Called right after the replay creates that row, this points the
    assistant message at the canvas — the same up-front backfill the sandbox
    ``_materialize_pending_canvases`` performs.  Idempotent (already-linked
    messages are excluded by the service query) and fail-open.
    """
    try:
        from agent_session_orchestrator.services.canvas_service import (
            CanvasService,  # noqa: PLC0415
        )

        canvas_svc = CanvasService(svc._db, tenant_id=tenant_id)  # noqa: SLF001
        for canvas in await canvas_svc.list_canvases_for_run(run.id):
            await canvas_svc.backfill_message_canvas_id(
                session_id=run.session_id, run_id=run.id, canvas_id=canvas.id
            )
    except Exception:  # noqa: BLE001 — fail-open: never fail finalize on backfill
        logger.warning(
            "inline canvas backfill failed (fail-open)",
            extra={"run_id": run.id},
            exc_info=True,
        )


async def _canvas_enabled_for_tenant(tenant_id: str) -> bool:
    """Whether the ``enable_canvas_tool`` flag is on for ``tenant_id`` (inline).

    Reads the same foundation feature-flag source of truth the sandbox path uses,
    via the already-wired shared client.  Fail-OPEN to enabled: unlike the shared
    client's default fail-closed ``is_enabled`` (and the scheduler's fail-open-to-
    *no-canvas*), the inline path defaults canvas ON when the flag service is
    unreachable or uninitialised — the tool is advertised opportunistically and a
    model that never calls it costs nothing, so a flag outage should not silently
    strip canvas from chat mode.  A tenant that has explicitly disabled the flag is
    still respected (the client returns ``False`` for a present-but-off flag).
    """
    try:
        from feature_flag_client import get_ff_client  # noqa: PLC0415

        return await get_ff_client().is_enabled(
            "enable_canvas_tool", tenant_id, default=True
        )
    except RuntimeError:
        # Client not initialised (e.g. a unit context) — default ON.
        return True
    except Exception:  # noqa: BLE001
        logger.debug(
            "inline canvas flag check failed; defaulting canvas ON",
            extra={"tenant_id": tenant_id},
        )
        return True


async def _inline_attachments_enabled_for_tenant(tenant_id: str) -> bool:
    """Whether the ``inline_attachment_reading`` flag is on for ``tenant_id``.

    Mirrors :func:`_canvas_enabled_for_tenant`: reads the foundation feature-flag
    source of truth via the shared client and fail-OPENs to enabled.  Reading
    uploaded files is opportunistic — a model that ignores the injected block
    costs nothing — so a flag-service outage should not silently strip attachment
    context from chat mode.  A tenant that has explicitly turned the flag OFF is
    still respected (the client returns ``False`` for a present-but-off flag).
    """
    try:
        from feature_flag_client import get_ff_client  # noqa: PLC0415

        return await get_ff_client().is_enabled(
            "inline_attachment_reading", tenant_id, default=True
        )
    except RuntimeError:
        # Client not initialised (e.g. a unit context) — default ON.
        return True
    except Exception:  # noqa: BLE001
        logger.debug(
            "inline attachment flag check failed; defaulting attachment reading ON",
            extra={"tenant_id": tenant_id},
        )
        return True


# Maximum number of input attachments read into the prompt per run (oldest
# first).  Bounds the download + extraction cost for a session that has
# accumulated many uploads over many turns.
_MAX_INLINE_ATTACHMENTS = 10


def _make_blob_store(settings: Settings):  # type: ignore[no-untyped-def]
    """Build an S3BlobStore from settings; return None if unavailable.

    A background inline task has no ``request.app.state.blob_store`` (that lives
    on the FastAPI app the request built), so it constructs its own store from
    settings — mirroring :func:`agent_session_orchestrator.main._make_blob_store`
    but kept local to avoid importing the FastAPI app module from a service.
    """
    try:
        from agent_session_orchestrator.blob.s3 import S3BlobStore  # noqa: PLC0415

        return S3BlobStore(
            bucket=settings.s3_bucket,
            region=settings.s3_region,
            endpoint_url=settings.s3_endpoint_url,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
        )
    except Exception:  # noqa: BLE001 — no boto3 / creds → no attachment reading
        logger.debug("inline attachment reading: S3BlobStore unavailable")
        return None


async def _build_inline_attachments_prompt(
    *,
    settings: Settings,
    tenant_id: str,
    user_id: str,
    session_id: str,
) -> str | None:
    """Read the session's input attachments and build the injection block.

    Queries ALL ``kind='input'`` attachments for the session (oldest first,
    capped at :data:`_MAX_INLINE_ATTACHMENTS`) — not just this run's body files —
    so a file uploaded in an earlier turn can still be referenced later.  Each
    file's bytes are fetched from S3 and extracted to text on every run (no
    cache).  Returns the assembled Chinese block, or ``None`` when there is
    nothing to inject.  Fail-open throughout: any error yields ``None`` (or drops
    the offending file) and never raises.
    """
    # 1. List input attachments (buffer the result — the async-for body must not
    #    return/break, which would trigger GeneratorExit and roll the txn back).
    attachments: list[Any] = []
    try:
        async for db in acquire_db_session(
            tenant_id=tenant_id, user_id=user_id or "SYSTEM", actor="USER"
        ):
            svc = SessionService(db, tenant_id=tenant_id)
            rows = await svc.list_attachments(session_id, kind="input")
            attachments = list(rows)
    except Exception:  # noqa: BLE001
        logger.warning(
            "inline attachment listing failed (fail-open, no injection)",
            extra={"session_id": session_id},
        )
        return None

    if not attachments:
        return None

    # Oldest-first (list_attachments already orders by created_at asc); cap count.
    attachments = attachments[:_MAX_INLINE_ATTACHMENTS]

    blob_store = _make_blob_store(settings)
    if blob_store is None:
        return None

    # 2. Fetch bytes + extract text per file (fail-open per file).  Each entry is
    #    an ``(attachment_id, filename, text)`` triple so the block builder can
    #    surface the id for the ``[文件名](#file:<id>)`` link convention.
    extracted: list[tuple[str, str, str]] = []
    for att in attachments:
        att_id = str(getattr(att, "id", "") or "")
        filename = getattr(att, "filename", "") or "未命名文件"
        mime = getattr(att, "mime", "") or "application/octet-stream"
        blob_key = getattr(att, "blob_key", None)
        if not blob_key:
            continue
        try:
            data = await asyncio.to_thread(blob_store.get, blob_key)
        except Exception:  # noqa: BLE001 — a missing blob shouldn't fail the run
            logger.warning(
                "inline attachment blob fetch failed (fail-open, skipping file)",
                extra={"session_id": session_id, "att_filename": filename},
            )
            extracted.append((att_id, filename, "（文件读取失败，已跳过）"))
            continue
        text: str
        if (
            settings.doc_to_md_url
            and len(data) <= attachment_text_extractor.MAX_BYTES_PER_FILE
            and doc_to_markdown_client.supports(filename, mime)
        ):
            try:
                text = await doc_to_markdown_client.convert(
                    base_url=settings.doc_to_md_url,
                    filename=filename,
                    mime=mime,
                    data=data,
                    timeout=settings.doc_to_md_timeout,
                )
            except doc_to_markdown_client.ConversionError as exc:
                logger.warning(
                    "rich document conversion failed (using local fallback): %s",
                    exc,
                    extra={"session_id": session_id, "att_filename": filename},
                )
                text = attachment_text_extractor.extract_text(filename, mime, data)
        else:
            text = attachment_text_extractor.extract_text(filename, mime, data)
        extracted.append((att_id, filename, text))

    return attachment_text_extractor.build_attachments_block(extracted)


async def _build_workspace_files_prompt(
    *,
    settings: Settings,
    tenant_id: str,
    user_id: str,
    session_id: str,
    files: list[dict[str, Any]],
) -> str | None:
    """Build the workspace base-files injection block from pre-resolved files.

    ``files`` is the plain-dict form the create_run endpoint extracted from the
    workspace context inside its request transaction (``{"id", "filename",
    "mime", "key"}`` — never ORM rows, which must not cross the transaction
    boundary into this background task).  Bytes are fetched from S3 and extracted
    with the exact same rich-document-or-local pipeline as session attachments.
    Fail-open throughout: any error drops the file or the whole block, never
    the run.
    """
    if not files:
        return None

    blob_store = _make_blob_store(settings)
    if blob_store is None:
        return None

    # Each entry is an ``(workspace_file_id, filename, text)`` triple so the block
    # builder can surface the id for the ``[文件名](#file:<id>)`` link convention.
    extracted: list[tuple[str, str, str]] = []
    for index, item in enumerate(files):
        file_id = str(item.get("id") or "")
        filename = str(item.get("filename") or "") or "未命名文件"
        mime = str(item.get("mime") or "") or "application/octet-stream"
        blob_key = item.get("key") or item.get("blob_key")
        if not blob_key:
            continue
        if index >= attachment_text_extractor.MAX_WORKSPACE_FILES:
            # Beyond the injection cap: skip the download entirely but keep the
            # entry so the block builder lists the filename as omitted.
            extracted.append((file_id, filename, ""))
            continue
        try:
            data = await asyncio.to_thread(blob_store.get, blob_key)
        except Exception:  # noqa: BLE001 — a missing blob shouldn't fail the run
            logger.warning(
                "workspace file blob fetch failed (fail-open, skipping file)",
                extra={"session_id": session_id, "ws_filename": filename},
            )
            extracted.append((file_id, filename, "（文件读取失败，已跳过）"))
            continue
        text: str
        if (
            settings.doc_to_md_url
            and len(data) <= attachment_text_extractor.MAX_BYTES_PER_FILE
            and doc_to_markdown_client.supports(filename, mime)
        ):
            try:
                text = await doc_to_markdown_client.convert(
                    base_url=settings.doc_to_md_url,
                    filename=filename,
                    mime=mime,
                    data=data,
                    timeout=settings.doc_to_md_timeout,
                )
            except doc_to_markdown_client.ConversionError as exc:
                logger.warning(
                    "rich document conversion failed (using local fallback): %s",
                    exc,
                    extra={"session_id": session_id, "ws_filename": filename},
                )
                text = attachment_text_extractor.extract_text(filename, mime, data)
        else:
            text = attachment_text_extractor.extract_text(filename, mime, data)
        extracted.append((file_id, filename, text))

    return attachment_text_extractor.build_workspace_files_block(extracted)


def _last_user_task(messages: list[dict[str, Any]]) -> str:
    """The content of the last ``user`` message — the original task the executor
    appended before the tool loop mutated ``messages`` (used by the rescue for the
    canvas title)."""
    for item in reversed(messages):
        if item.get("role") == "user":
            return str(item.get("content") or "")
    return ""


async def _run_completion_with_tools(
    *,
    client: httpx.AsyncClient,
    redis_client: Any,
    settings: Settings,
    gateway_url: str,
    tenant_id: str,
    user_id: str,
    session_id: str,
    run_id: str,
    virtual_key: str,
    model: str,
    messages: list[dict[str, Any]],
    sampling_params: dict[str, Any],
    reasoning_effort: str | None,
    canvas_enabled: bool,
    force_canvas_first_round: bool,
    skills_catalog: list[dict[str, Any]] | None = None,
    skill_store_url: str = "",
    skill_service_scopes: str = "",
    mcp_catalog: inline_mcp_client.InlineMcpCatalog | None = None,
    knowledge_enabled: bool = False,
    knowledge_base_url: str = "",
    effective_clearance: str = "L1",
) -> tuple[dict[str, int], str]:
    """Drive up to ``_INLINE_MAX_TOOL_ROUNDS`` completion rounds, closing the tool loop.

    Round semantics when canvas is enabled:

    * Round 1 advertises ``tools=[create_canvas]``.  ``tool_choice`` is forced to
      ``create_canvas`` when ``force_canvas_first_round`` (written-creation prompt
      on a weak-instruction model), else ``auto``.  If the model returns a
      ``create_canvas`` tool call, the canvas is materialized in-process (DB + a
      ``canvas`` token-stream entry → ``canvas_created`` SSE), the assistant
      tool_calls turn + a tool result are appended to ``messages``, and we loop.
    * Round 2 drops ``tool_choice`` back to ``auto`` so the model writes its
      one-sentence pointer reply.  The loop also stops early whenever the model
      finishes with body text and no tool call (``finish_reason != "tool_calls"``).

    Deterministic rescue: after the rounds, if this is a forced written-creation
    prompt (``force_canvas_first_round``) that produced NO canvas, one of two
    fallbacks runs so the user never faces a phantom canvas — either the streamed
    body IS the article (materialize it verbatim) or a silent, non-streamed extra
    completion fetches the full text and materializes that (see the rescue block).

    Returns ``(usage, body_text)`` accumulated across the rounds.  ``usage`` is the
    LAST metered request's totals — the last tool-loop round, OR the silent rescue
    completion when the lie/stub branch fired (the gateway meters each request, and
    the rescue fetch is the final request, so it overrides).  ``body_text`` is the
    concatenation of every round's streamed body (the silent rescue text is NOT part
    of it — it never reached the frontend and is only used to fill the canvas).
    A single canvas per run is enforced by ``_materialize_inline_canvas`` (repeat
    calls no-op), so at most one canvas is ever created regardless of round count.

    Providers that reject a NAMED ``tool_choice`` (deepseek thinking mode) get one
    same-round retry with ``tool_choice="auto"`` — but only while nothing from the
    round has streamed to the frontend (``InlineGatewayError.streamed_entries``).
    """
    # Assemble the advertised tools list.  ``create_canvas`` is gated on the
    # canvas flag; ``read_skill`` is gated on a non-empty resolved skill catalog;
    # tenant MCP tools come from the healthy inline MCP catalog. Any combination
    # may be present — with an empty list we send no
    # ``tools`` at all (a plain completion, identical to the pre-skill behaviour).
    catalog = skills_catalog or []
    tool_list: list[dict[str, Any]] = []
    if canvas_enabled:
        tool_list.append(CREATE_CANVAS_TOOL)
    if catalog:
        tool_list.append(READ_SKILL_TOOL)
    if mcp_catalog is not None:
        tool_list.extend(mcp_catalog.openai_tools)
    if knowledge_enabled:
        # Tenant knowledge-base retrieval tools (search + list); dispatched
        # in-process against knowledge-ingest with the run's effective clearance.
        tool_list.append(SEARCH_KNOWLEDGE_TOOL)
        tool_list.append(LIST_KNOWLEDGE_TOOL)
    tools = tool_list or None

    # ``name -> sha256`` for the catalog, so a read_skill call keys the loader's
    # content-addressed cache off the resolved hash.
    skill_sha_by_name: dict[str, str] = {
        str(e.get("name") or ""): str(e.get("sha256") or "") for e in catalog
    }

    total_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    body_parts: list[str] = []
    tool_choice_downgraded = False  # at most one named→auto downgrade per run
    # Track whether ANY round actually materialized a canvas (the return value of
    # ``_materialize_inline_canvas``, not a DB re-query).  Drives the post-loop
    # rescue: a forced written-creation run that ends here with no canvas means the
    # model neither called the tool nor honoured the forcing prompt.
    canvas_created = False

    for round_index in range(_INLINE_MAX_TOOL_ROUNDS):
        tool_choice: Any | None = None
        if tools:
            # Force create_canvas only on the very first round for a written-
            # creation prompt; afterwards leave it to the model so it can write
            # the pointer reply (a forced choice on round 2 would loop the tool).
            if round_index == 0 and force_canvas_first_round:
                tool_choice = {
                    "type": "function",
                    "function": {"name": CREATE_CANVAS_TOOL_NAME},
                }
            else:
                tool_choice = "auto"

        while True:
            try:
                usage, body_text, tool_calls, finish_reason = await _stream_completion_into_redis(
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
                    tools=tools,
                    tool_choice=tool_choice,
                )
                break
            except InlineGatewayError as exc:
                # Some providers reject a NAMED tool_choice outright — deepseek
                # thinking mode 400s with "Thinking mode does not support this
                # tool_choice".  When the pin itself is what failed (named choice,
                # error text implicates tool_choice) and nothing from this round
                # has reached the frontend yet, downgrade to "auto" and retry the
                # same round once instead of failing the run: on these models the
                # forcing prompt alone triggers create_canvas reliably (the
                # sandbox path ships prompt-only forcing with the same model).
                # Any other gateway error — or one after content already streamed,
                # where a retry would duplicate frontend output — propagates.
                if (
                    isinstance(tool_choice, dict)
                    and not tool_choice_downgraded
                    and "tool_choice" in str(exc)
                    and getattr(exc, "streamed_entries", 0) == 0
                ):
                    tool_choice_downgraded = True
                    logger.info(
                        "named tool_choice rejected by provider; retrying round "
                        "with tool_choice=auto",
                        extra={"run_id": run_id, "model": model, "err": str(exc)},
                    )
                    tool_choice = "auto"
                    continue
                raise
        # Last round's usage wins (the gateway meters each request); bodies concat.
        total_usage = usage
        if body_text:
            body_parts.append(body_text)

        # Generalized tool dispatch: route each call by ``function.name`` to a
        # handler.  Known tools are ``create_canvas`` (materialize a canvas) and
        # ``read_skill`` (fetch a skill file); anything else gets a "not supported"
        # tool result.  Preserve the original "stop when no KNOWN tool call this
        # round" semantics — an empty tool_calls list, or one containing only
        # unknown tools, still breaks the loop rather than looping on junk.
        known_calls = []
        for tc in tool_calls:
            called_name = str((tc.get("function") or {}).get("name") or "")
            if (
                called_name in (CREATE_CANVAS_TOOL_NAME, READ_SKILL_TOOL_NAME)
                or (
                    knowledge_enabled
                    and called_name
                    in (SEARCH_KNOWLEDGE_TOOL_NAME, LIST_KNOWLEDGE_TOOL_NAME)
                )
                or mcp_catalog is not None
                and mcp_catalog.has(called_name)
            ):
                known_calls.append(tc)
        if not known_calls:
            break

        # Append the assistant tool-call turn so the follow-up round has valid
        # OpenAI context (a tool result must be preceded by its tool_calls turn).
        # ALL tool_calls the model emitted go on this frame (OpenAI requires one
        # tool result per tool_call id), and each gets a matching ``role: tool``
        # result below — including unknown tools, which get a not-supported note.
        messages.append(
            {"role": "assistant", "content": body_text or None, "tool_calls": tool_calls}
        )
        for tc in tool_calls:
            fn_name = (tc.get("function") or {}).get("name")
            tool_call_id = tc.get("id") or ""
            args = _parse_tool_arguments((tc.get("function") or {}).get("arguments") or "")

            if fn_name == CREATE_CANVAS_TOOL_NAME:
                # Canvas: materialize (the single-canvas cap makes all but the
                # first a no-op) and feed the pointer-reply confirmation back.
                if await _materialize_inline_canvas(
                    settings=settings,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_id=session_id,
                    run_id=run_id,
                    redis_client=redis_client,
                    args=args,
                ):
                    canvas_created = True
                result_content = canvas_confirmation_reply(args.get("title"))
            elif fn_name == READ_SKILL_TOOL_NAME:
                # Skill read: fetch the requested file's body (fail-open — the
                # loader returns a Chinese error string rather than raising, so a
                # skill-store hiccup surfaces to the model but never fails the run).
                skill_name = str(args.get("name") or "").strip()
                read_path = str(args.get("path") or "SKILL.md").strip() or "SKILL.md"
                if not skill_name:
                    result_content = "（read_skill 缺少 name 参数，无法读取技能）"
                else:
                    try:
                        result_content = await inline_skill_loader.fetch_skill_file(
                            skill_name,
                            skill_store_url=skill_store_url,
                            service_scopes=skill_service_scopes,
                            tenant_id=tenant_id,
                            user_id=user_id,
                            sha256=skill_sha_by_name.get(skill_name, ""),
                            path=read_path,
                        )
                    except Exception as exc:  # noqa: BLE001 — never fail the run
                        logger.warning(
                            "inline read_skill handler failed (failure-open): %s",
                            exc,
                            extra={"run_id": run_id, "skill_name": skill_name},
                        )
                        result_content = (
                            f"（读取技能「{skill_name}」的 {read_path} 时发生内部错误）"
                        )
            elif knowledge_enabled and fn_name in (
                SEARCH_KNOWLEDGE_TOOL_NAME,
                LIST_KNOWLEDGE_TOOL_NAME,
            ):
                # Tenant knowledge base: surface the call/result through the same
                # Redis tool events as MCP tools so the frontend shows the
                # activity, then feed the textual result back into the loop.  The
                # ki client is fail-open (returns a note, never raises), so a
                # retrieval outage never breaks the conversation.
                await _xadd(
                    redis_client,
                    run_id,
                    {
                        "type": "tool_call",
                        "tool_call_id": tool_call_id,
                        "name": str(fn_name),
                        "args": json.dumps(args, ensure_ascii=False),
                    },
                )
                if fn_name == SEARCH_KNOWLEDGE_TOOL_NAME:
                    query = str(args.get("query") or "").strip()
                    try:
                        k = int(args.get("k") or 5)
                    except (TypeError, ValueError):
                        k = 5
                    if not query:
                        result_content = "（search_knowledge 缺少 query 参数，无法检索。）"
                    else:
                        result_content = await inline_knowledge_client.search_knowledge(
                            base_url=knowledge_base_url,
                            tenant_id=tenant_id,
                            user_id=user_id,
                            clearance=effective_clearance,
                            query=query,
                            k=k,
                        )
                else:  # LIST_KNOWLEDGE_TOOL_NAME
                    result_content = await inline_knowledge_client.list_knowledge(
                        base_url=knowledge_base_url,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        clearance=effective_clearance,
                    )
                await _xadd(
                    redis_client,
                    run_id,
                    {
                        "type": "tool_result",
                        "tool_call_id": tool_call_id,
                        "name": str(fn_name),
                        "result": result_content,
                        "state": "success",
                    },
                )
            elif mcp_catalog is not None and mcp_catalog.has(str(fn_name or "")):
                # Tenant MCP: surface the call/result through the same Redis
                # tool events as sandbox workers, then feed the textual result
                # back into the OpenAI tool loop. A single broken call is
                # failure-isolated and the model can still finish the reply.
                await _xadd(
                    redis_client,
                    run_id,
                    {
                        "type": "tool_call",
                        "tool_call_id": tool_call_id,
                        "name": str(fn_name),
                        "args": json.dumps(args, ensure_ascii=False),
                    },
                )
                allowed, deny_reason = await _inline_tool_call_dlp_decision(
                    dlp_url=settings.foundation_dlp_url,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    run_id=run_id,
                    tool_call_id=tool_call_id,
                    tool_name=str(fn_name),
                    arguments=args,
                )
                tool_state = "success" if allowed else "denied"
                if not allowed:
                    result_content = deny_reason
                else:
                    try:
                        result_content = await mcp_catalog.call(str(fn_name), args)
                    except Exception as exc:  # noqa: BLE001 — isolate one MCP call
                        tool_state = "error"
                        logger.warning(
                            "inline MCP tool call failed (fail-open): %s",
                            exc,
                            extra={"run_id": run_id, "tool": fn_name},
                        )
                        result_content = f"MCP 工具调用失败：{exc}"
                await _xadd(
                    redis_client,
                    run_id,
                    {
                        "type": "tool_result",
                        "tool_call_id": tool_call_id,
                        "name": str(fn_name),
                        "result": result_content,
                        "state": tool_state,
                    },
                )
            else:
                # Unknown tool — feed a note back so the frame stays OpenAI-valid.
                result_content = f"（不支持的工具调用：{fn_name}）"

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result_content,
                }
            )

    body_text_all = "".join(body_parts)

    # ── Deterministic canvas rescue ─────────────────────────────────────────
    # Fires ONLY for an explicit written-creation prompt (``force_canvas_first_round``)
    # that finished the rounds above without materializing a canvas.  On weak-
    # instruction models (deepseek-reasoner after the named-tool_choice downgrade)
    # the prompt forcing alone is only ~50% reliable; when it misses the model
    # either wrote the whole piece into the chat body (hijack) or — worse — copied
    # the forcing prompt's example sentence and CLAIMED a canvas that never existed.
    # This closes both cases so the user never faces a phantom canvas.  At most one
    # rescue per run; the materialize call is fail-open (never fails the run).
    if force_canvas_first_round and not canvas_created:
        # Title comes from the ORIGINAL task.  ``messages`` may have been mutated by
        # the tool loop (appended assistant/tool frames), but the executor appended
        # the task as the LAST user turn before the loop ran, so the last-user-role
        # message is still that task.
        rescue_title = extract_creation_title(_last_user_task(messages))
        if len(body_text_all.strip()) >= _RESCUE_BODY_MIN_CHARS:
            # Body-is-the-article: the model wrote the full piece into the reply
            # instead of calling the tool.  Materialize the streamed body verbatim —
            # it already reached the frontend as delta tokens, so this only adds the
            # canvas card alongside it.
            logger.info(
                "inline canvas rescue (body-as-article): materializing streamed reply",
                extra={"run_id": run_id, "chars": len(body_text_all.strip())},
            )
            await _materialize_inline_canvas(
                settings=settings,
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                run_id=run_id,
                redis_client=redis_client,
                args={
                    "kind": "article",
                    "title": rescue_title,
                    "content_markdown": body_text_all,
                },
            )
        else:
            # Lie/stub reply: the body is too short to be the article (a phantom-
            # canvas claim or an empty turn).  Issue ONE extra completion — no tools,
            # no tool_choice, and crucially ``redis_client=None`` so nothing XADDs to
            # the run token stream — to fetch the full text, then materialize it.
            # The rescue prompt is appended to a COPY of ``messages`` so the fetch
            # sees the full context but the mutation never leaks back.
            rescue_messages = list(messages)
            rescue_messages.append(
                {
                    "role": "user",
                    "content": (
                        f"请直接输出《{rescue_title}》的完整正文（Markdown 格式），"
                        "只输出正文本身，不要任何说明、前言或客套话。"
                    ),
                }
            )
            rescue_usage: dict[str, int] | None = None
            rescue_text = ""
            try:
                rescue_usage, rescue_text, _tc, _fr = await _stream_completion_into_redis(
                    client=client,
                    redis_client=None,  # silent: never touches the run token stream
                    gateway_url=gateway_url,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_id=session_id,
                    run_id=run_id,
                    virtual_key=virtual_key,
                    model=model,
                    messages=rescue_messages,
                    sampling_params=sampling_params,
                    reasoning_effort=reasoning_effort,
                    tools=None,
                    tool_choice=None,
                )
            except InlineGatewayError as exc:
                # A failed rescue completion is non-fatal: the run still finishes on
                # the (short) reply it already produced.
                logger.warning(
                    "inline canvas rescue (silent-completion): fetch failed, abandoning",
                    extra={"run_id": run_id, "err": str(exc)},
                )
            if rescue_usage is not None:
                # Fold the rescue round's usage into the totals.  "Last round wins"
                # (the gateway meters each request separately); the silent completion
                # IS the last request, so it overrides — same semantics as the loop.
                total_usage = rescue_usage
            if len(rescue_text.strip()) >= _RESCUE_BODY_MIN_CHARS:
                logger.info(
                    "inline canvas rescue (silent-completion): materializing fetched text",
                    extra={"run_id": run_id, "chars": len(rescue_text.strip())},
                )
                await _materialize_inline_canvas(
                    settings=settings,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_id=session_id,
                    run_id=run_id,
                    redis_client=redis_client,
                    args={
                        "kind": "article",
                        "title": rescue_title,
                        "content_markdown": rescue_text,
                    },
                )
            else:
                logger.warning(
                    "inline canvas rescue (silent-completion): fetched text too short, "
                    "abandoning rescue",
                    extra={"run_id": run_id, "chars": len(rescue_text.strip())},
                )

    total_usage["total_tokens"] = total_usage.get("total_tokens", 0) or (
        total_usage.get("input_tokens", 0) + total_usage.get("output_tokens", 0)
    )
    return total_usage, body_text_all


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
    skills: list[str] | None = None,
    mcp_server_ids: list[str] | None = None,
    workspace_instructions: str | None = None,
    workspace_files: list[dict[str, Any]] | None = None,
    knowledge: str = "",
    effective_clearance: str = "L1",
) -> None:
    """Run one inline chat completion to terminal state.

    Owns the full inline lifecycle (mint key → resolve model → stream → persist
    → finalize → revoke).  Never raises to the event loop: every failure marks
    the run FAILED, pushes ``end`` and returns.  The gateway error body is NEVER
    written as an assistant message (a 402 quota error must not look like a
    reply).

    ``workspace_instructions`` / ``workspace_files`` carry the session's
    workspace context, pre-resolved by the create_run endpoint into plain
    values (never ORM rows — this task runs outside the request transaction).
    Both are injected into the system prompt; a workspace failure never fails
    the run.
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
    mcp_catalog: inline_mcp_client.InlineMcpCatalog | None = None

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

            # 3. Canvas: is the tool enabled for this tenant, and does the prompt
            #    warrant forcing it on the first round (weak-instruction models
            #    fire the tool only probabilistically even under strong wording)?
            canvas_enabled = await _canvas_enabled_for_tenant(tenant_id)
            force_canvas_first_round = (
                canvas_enabled and is_written_creation_prompt(task)
            )

            # Knowledge base: the search_knowledge / list_knowledge tools are
            # advertised whenever knowledge-ingest is reachable.  Gating on the
            # URL keeps unit contexts (which set knowledge_ingest_url="") on the
            # pre-knowledge behaviour — no extra tools, no knowledge section.
            knowledge_enabled = bool(knowledge_ingest_url)

            # 3b. Skills: resolve the run's skill names into a catalog (once per
            #     run, never cached — a skill upgraded between turns must surface
            #     its new sha immediately).  Failure-open: a bad skill is dropped,
            #     never fails the run.  A non-empty catalog advertises ``read_skill``
            #     and injects the catalog prompt (both appear/disappear together).
            skills_catalog = await inline_skill_loader.resolve_skills(
                skills,
                skill_store_url=settings.skill_store_url,
                service_scopes=settings.skill_store_service_scopes,
                tenant_id=tenant_id,
                user_id=user_id,
            )

            # 3c. MCP: discover and initialize the tenant's enabled HTTP MCP
            #     servers. The trusted inline executor calls registry-vetted
            #     endpoints directly; stdio remains sandbox-only. Discovery and
            #     handshake are fail-open per server, so a stale library entry
            #     never breaks an otherwise normal conversation.
            mcp_catalog = await inline_mcp_client.load_inline_mcp_catalog(
                base_url=settings.mcp_registry_url,
                service_scopes=settings.mcp_registry_service_scopes,
                tenant_id=tenant_id,
                user_id=user_id,
                timeout=settings.inline_mcp_timeout,
                # Presence-resolved per-run selection: None = the tenant's
                # enabled set (historical behaviour), [] = no MCP this run,
                # non-empty = only those server ids.
                server_ids=mcp_server_ids,
            )

            # 3d. Attachments: read the session's uploaded input files and build a
            #     Chinese context block so the model can answer questions about
            #     them.  Gated by the ``inline_attachment_reading`` flag (fail-open
            #     ON).  Fail-open throughout — a parse/fetch error drops the file or
            #     the whole block, never the run.  No attachments → None → no
            #     injection (behaviour identical to before this feature).
            attachments_block: str | None = None
            if await _inline_attachments_enabled_for_tenant(tenant_id):
                attachments_block = await _build_inline_attachments_prompt(
                    settings=settings,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_id=session_id,
                )

            # 3e. Workspace base files: resolve the pre-extracted file dicts into
            #     the injection block.  Fail-open — any error yields None and the
            #     run proceeds without the block.
            workspace_files_block: str | None = None
            # Workspace bound but zero base files (``[]`` sentinel from create_run,
            # vs ``None`` = not workspace-bound): tell the model the workspace is
            # empty so it answers truthfully instead of inventing a missing tool.
            workspace_empty_notice: str | None = None
            if workspace_files is not None and not workspace_files:
                workspace_empty_notice = (
                    "当前会话所属的工作区没有配置任何基础文件。"
                    "如果用户询问工作区文件，如实说明该工作区暂无基础文件即可，"
                    "不要声称缺少查看文件的工具。"
                )
            if workspace_files:
                try:
                    workspace_files_block = await _build_workspace_files_prompt(
                        settings=settings,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        session_id=session_id,
                        files=workspace_files,
                    )
                except Exception:  # noqa: BLE001 — never fail the run on workspace files
                    logger.warning(
                        "workspace files prompt build failed (fail-open)",
                        extra={"run_id": run_id},
                        exc_info=True,
                    )

            # 4. Assemble OpenAI messages (native roles, not folded into system);
            #    append the canvas forcing block AND/OR the skills catalog block to
            #    the system message so each instruction rides with its tool (canvas
            #    block ↔ create_canvas, catalog block ↔ read_skill).  Workspace
            #    context slots in deterministically: instructions right after the
            #    canvas block (before the skills catalog), the workspace files
            #    block before the session-attachments block.
            extra_system_parts: list[str] = []
            if canvas_enabled:
                extra_system_parts.append(CANVAS_FORCING_PROMPT)
            if workspace_instructions and workspace_instructions.strip():
                extra_system_parts.append(
                    "# 工作区指令\n" + workspace_instructions.strip()
                )
            if skills_catalog:
                extra_system_parts.append(build_skills_catalog_prompt(skills_catalog))
            # Knowledge-base section — kept EXPLICITLY separate from the workspace
            # base-files notice below so the model never conflates "工作区没有基础
            # 文件" with "没有知识库".  Appears iff the two knowledge tools do.
            if knowledge_enabled:
                extra_system_parts.append(KNOWLEDGE_SYSTEM_PROMPT)
            if workspace_files_block:
                extra_system_parts.append(workspace_files_block)
            elif workspace_empty_notice:
                extra_system_parts.append(workspace_empty_notice)
            if attachments_block:
                extra_system_parts.append(attachments_block)
            # Whenever any file block was injected, append the file-reference link
            # convention ONCE (after all file blocks) so the model references those
            # files via [文件名](#file:<id>) using the ids given in the blocks.
            if attachments_block or workspace_files_block:
                extra_system_parts.append(
                    attachment_text_extractor.FILE_LINK_CONVENTION_PROMPT
                )
            extra_system = (
                "\n\n".join(p for p in extra_system_parts if p and p.strip())
                or None
            )
            messages = build_openai_messages(
                sys_prompt=sys_prompt, memory=memory, history=history, task=task,
                extra_system=extra_system, knowledge=knowledge,
            )

            # 5. Drive the completion (+ tool loop) into Redis.  With canvas off
            #    and no skills this is a single plain completion, identical to before.
            usage, body_text = await _run_completion_with_tools(
                client=client,
                redis_client=redis_client,
                settings=settings,
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
                canvas_enabled=canvas_enabled,
                force_canvas_first_round=force_canvas_first_round,
                skills_catalog=skills_catalog,
                skill_store_url=settings.skill_store_url,
                skill_service_scopes=settings.skill_store_service_scopes,
                mcp_catalog=mcp_catalog,
                knowledge_enabled=knowledge_enabled,
                knowledge_base_url=knowledge_ingest_url,
                effective_clearance=effective_clearance,
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
        # NOTE: the ``end`` marker is deliberately NOT pushed here.  It must
        # only appear once the terminal status is durable (below): the SSE tail
        # exits on ``end`` and immediately reconciles run.status — an ``end``
        # ahead of the finalize commit makes that reconcile read RUNNING, emit
        # no lifecycle event, and the live connection never delivers
        # run_complete (frontend stuck on "Working on it…" until a refresh).
        if redis_client is not None:
            with contextlib.suppress(Exception):
                await redis_client.aclose()
        if mcp_catalog is not None:
            with contextlib.suppress(Exception):
                await mcp_catalog.close()

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

                    # Anchor any canvas created during streaming onto the assistant
                    # message row that replay just persisted.  ``_materialize_inline_canvas``
                    # created the document mid-stream when no assistant row existed
                    # yet (so its own backfill was a no-op); mirroring the sandbox
                    # reconcile path, we re-run the backfill here now the row exists,
                    # so ``message.canvas_id`` is set and the hydration path can
                    # re-anchor the card on reload.  Fail-open.
                    await _backfill_inline_run_canvases(
                        svc=svc, run=run, tenant_id=tenant_id
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

    # Terminate the SSE tail only now that the terminal status is committed:
    # ``end`` means "reconciling run.status will observe a terminal state", so
    # the post-stream reconcile emits run_complete on the live connection.
    # (Cancel pushes its own ``end`` in cancel_inline_run and never reaches
    # here; the un-finalized edge keeps the pre-existing semantics — the tail
    # re-polls and the sweeper eventually orphan-fails the run.)
    await _push_end(redis_url, run_id)

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
    skills: list[str] | None = None,
    mcp_server_ids: list[str] | None = None,
    workspace_instructions: str | None = None,
    workspace_files: list[dict[str, Any]] | None = None,
    knowledge: str = "",
    effective_clearance: str = "L1",
) -> None:
    """Launch the background inline-run task and register it for cancel.

    Called from the create_run endpoint AFTER the run row has been flipped to
    RUNNING and committed by the surrounding request transaction — the task
    finalizes in its own fresh transaction, so it must not observe the row as
    PENDING.  Fire-and-forget: the HTTP response returns immediately.

    ``reasoning_effort`` is passed through verbatim to the /chat/completions
    payload when non-None (see ``_stream_completion_into_redis``).

    ``skills`` is the resolved-presence skill name list from the create_run
    endpoint (explicit selection, or the user's default-enabled installs when the
    request omitted the key), already unioned with the workspace's skill set.
    Empty/None means no skill loop for this run — the executor then behaves
    exactly as before skills were supported.

    ``mcp_server_ids`` is the presence-resolved MCP selection from create_run:
    ``None`` (request omitted the key) → discover every enabled tenant server,
    ``[]`` (explicitly sent empty) → no MCP tools at all for this run,
    non-empty → only those server ids.  ``None`` and ``[]`` are NOT
    interchangeable.

    ``workspace_instructions`` / ``workspace_files`` are the session workspace's
    context, extracted into PLAIN values (str / list of dicts with
    filename/mime/key) inside the request transaction — never ORM rows, which
    must not cross into this background task.

    ``knowledge`` is the prefetched tenant-knowledge "知识库资料" block (top-k
    ``scope=knowledge`` hits, each with its 【文档名 | 文件夹路径】 provenance);
    ``effective_clearance`` (``min(user, workspace)``) is the level the in-process
    search_knowledge / list_knowledge tools carry on every knowledge-ingest call.
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
            skills=skills,
            mcp_server_ids=mcp_server_ids,
            workspace_instructions=workspace_instructions,
            workspace_files=workspace_files,
            knowledge=knowledge,
            effective_clearance=effective_clearance,
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
