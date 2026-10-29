"""Run API endpoints.

State consistency
-----------------
``create_run`` creates the run row and updates ``session.current_run_id``
inside a *single* transaction.  The earlier implementation committed
twice; if the second commit failed (e.g. session row updated_at conflict)
the run row was already persisted but the session pointed nowhere, leaving
an orphaned run hidden from the UI but still consuming sandbox resources.

If the scheduler call itself fails, the whole transaction rolls back via
the implicit ``async with session.begin()`` opened by ``db_session``, so
no half-baked run record survives.  The container is the one external
side-effect we cannot atomically roll back; if spawn succeeded but the
DB commit fails we instead emit a best-effort cancel — at worst the
sandbox times out via ``Settings.sandbox_timeout`` and stops itself.

Memory pre-fetch
----------------
Before relaying the run to the scheduler, ``create_run`` calls knowledge-ingest
``GET /search`` using the task text as the query.  The top-K hits (memories
stored by previous runs) are formatted into a ``memory`` string and included
in the task body sent to the scheduler, which in turn writes it to task.json.
The in-sandbox worker reads ``task["memory"]`` and prepends it to the system
prompt.  This is intentionally failure-open: if knowledge-ingest is unreachable
the run proceeds without memory context.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.api.authz_guard import require_permission
from agent_session_orchestrator.api.dependencies import TClearance, TDbSession, TTenantId, TUserId

# Reuse the chat path's run-completion reconciler verbatim (poll scheduler,
# map container status, persist result_summary/status/finished_at/tokens, write
# the assistant message, failure-open).  It is module-private in messages.py but
# both modules live in the same ``api.v1`` package, so importing the
# ``_``-prefixed helper here is the least-invasive way to share it without
# duplicating any of its logic.
from agent_session_orchestrator.api.v1.messages import _reconcile_run_completion
from agent_session_orchestrator.domain.models import Run
from agent_session_orchestrator.services import knowledge_client
from agent_session_orchestrator.services.clearance import clearance_rank
from agent_session_orchestrator.services.session_service import SessionService

router = APIRouter(tags=["Runs"])
logger = logging.getLogger(__name__)

_MEMORY_SEARCH_K = 5  # top-K memory hits to inject
_KNOWLEDGE_SEARCH_K = 5  # top-K tenant-knowledge hits to inject


def _effective_clearance(user_clearance: str, workspace_sensitivity: str | None) -> str:
    """The run's effective clearance = ``min(user, workspace)`` by level rank.

    No workspace (or an unreadable/soft-deleted one → ``workspace_sensitivity``
    is ``None``) means the effective clearance is just the user's own.  This is
    the file≤workspace≤user matrix landing on the retrieval side: a session bound
    to an L2 workspace caps knowledge/memory visibility at L2 even for an L5 user.
    Fail-open to ``user_clearance`` on any malformed level so retrieval is never
    hard-blocked by a bad value.
    """
    if not workspace_sensitivity:
        return user_clearance
    try:
        if clearance_rank(workspace_sensitivity) < clearance_rank(user_clearance):
            return workspace_sensitivity
        return user_clearance
    except ValueError:
        return user_clearance


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def _merge_skills(base: list[str], extra: list[str]) -> list[str]:
    """Order-preserving union of two skill-name lists.

    ``base`` (the run's explicit/default skills) keeps its order and wins on
    duplicates; ``extra`` (the workspace's skill set) is appended in its own
    order, deduped.  Pure function so the workspace-skills merge is unit-testable.
    """
    merged = list(base)
    seen = set(merged)
    for name in extra:
        if name not in seen:
            merged.append(name)
            seen.add(name)
    return merged


def _prioritize_workspace_files(
    files: list[dict[str, Any]], referenced_ids: list[str]
) -> list[dict[str, Any]]:
    """Hoist @-referenced workspace files to the front, order-preserving.

    ``files`` is the workspace's file list (dicts carrying an ``id``);
    ``referenced_ids`` are the ids the message @-referenced.  Files whose id is in
    ``referenced_ids`` come first in their original relative order, then the
    un-referenced tail in its original order.  Ids not present in ``files`` are
    ignored.  Pure function so the priority ordering is unit-testable, and a
    no-op (returns a shallow copy) when nothing was referenced.
    """
    if not referenced_ids:
        return list(files)
    ref = set(referenced_ids)
    referenced = [f for f in files if f.get("id") in ref]
    unreferenced = [f for f in files if f.get("id") not in ref]
    return referenced + unreferenced


def _serialize_run(r: Run) -> dict[str, Any]:
    """Run → dict for list responses (session-level and tenant-level share this)."""
    return {
        "id": r.id,
        "session_id": r.session_id,
        "sandbox_id": r.sandbox_id,
        "status": r.status,
        "task_description": r.task_description,
        "result_summary": r.result_summary,
        "error_message": r.error_message,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "created_at": r.created_at.isoformat(),
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "total_tokens": r.total_tokens,
        "app_snapshot": r.app_snapshot,
        "source": r.source,
        "execution_mode": r.execution_mode,
    }


async def _fetch_memory(
    *,
    knowledge_ingest_url: str,
    tenant_id: str,
    user_id: str,
    query: str,
    session_id: str = "",
    clearance: str = "L1",
) -> str:
    """Query knowledge-ingest for relevant memories and return a formatted string.

    Uses ``GET /search?q=<query>&k=<K>&scope=memory`` with ``X-Tenant-Id`` /
    ``X-User-Id`` / ``X-User-Clearance`` headers.  ``scope=memory`` restricts the
    search to this user's own ``[memory]`` documents (the knowledge base is a
    separate ``scope=knowledge`` prefetch — see ``_fetch_knowledge``).  Returns an
    empty string on any error (failure-open).

    ``user_id`` must be the real originating user (``usr_...``) — knowledge-
    ingest's authz guard is fail-closed and rejects the ``SYSTEM`` sentinel,
    which used to make this search silently 403 (memory retrieval always empty).
    Callers pass the resolved session owner, never the system identity.

    When ``session_id`` is provided it is sent as ``exclude_origin_session_id``
    so this session's own prior-turn Q&A is excluded from the results — that
    context is already restored into the agent via conversation history, and
    re-injecting it as RAG double-feeds the same turns.
    """
    if not query.strip() or not knowledge_ingest_url:
        return ""
    params: dict[str, Any] = {
        "q": query[:500],
        "k": _MEMORY_SEARCH_K,
        "scope": "memory",
    }
    if session_id:
        params["exclude_origin_session_id"] = session_id
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{knowledge_ingest_url}/search",
                params=params,
                headers={
                    "X-Tenant-Id": tenant_id,
                    "X-User-Id": user_id,
                    "X-User-Clearance": clearance or "L1",
                },
            )
        if resp.status_code != 200:
            logger.debug(
                "memory search returned %s; proceeding without memory",
                resp.status_code,
            )
            return ""
        hits = resp.json().get("hits") or []
        if not hits:
            return ""
        snippets = []
        for hit in hits:
            text = str(hit.get("text") or "").strip()
            if text:
                snippets.append(f"- {text}")
        return "\n".join(snippets)
    except Exception as exc:  # noqa: BLE001
        logger.debug("memory pre-fetch failed (failure-open): %s", exc)
        return ""



async def _fetch_default_installed_skills(
    *,
    skill_store_url: str,
    service_scopes: str,
    tenant_id: str,
    user_id: str,
) -> list[str]:
    """Return the user's installed skills that are enabled by default.

    This lookup is used only when the run request omits the ``skills`` key.
    It is intentionally failure-open: an unavailable or malformed skill-store
    response must not prevent the run from reaching the scheduler.
    """
    if not skill_store_url:
        return []

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{skill_store_url.rstrip('/')}/internal/skills/default-installed",
                params={"tenant_id": tenant_id, "user_id": user_id},
                headers={
                    "X-Tenant-Id": tenant_id,
                    "X-User-Id": user_id,
                    "X-Service-Scopes": service_scopes,
                },
            )
        if resp.status_code != 200:
            logger.warning(
                "default installed skills lookup returned %s; proceeding without skills",
                resp.status_code,
            )
            return []

        items = resp.json().get("items") or []
        names: list[str] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name and name not in seen:
                names.append(name)
                seen.add(name)
        return names
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "default installed skills lookup failed; proceeding without skills: %s",
            exc,
        )
        return []


async def _fetch_platform_default_skills(
    *,
    skill_store_url: str,
    service_scopes: str,
    tenant_id: str,
    user_id: str,
) -> list[str]:
    """Return the platform-global default skill names.

    These are the skills EVERY organization (including brand-new tenants) gets by
    default with zero per-user install rows — owned by skill-store's reserved
    platform tenant.  The endpoint is tenant-agnostic (any caller gets the same
    names); we still pass the run's tenant/user/scope headers for consistency
    with the other ``/internal/*`` calls and to satisfy the internal-auth guard.

    Intentionally failure-open, exactly like ``_fetch_default_installed_skills``:
    an unavailable or malformed skill-store response must never block a run.
    """
    if not skill_store_url:
        return []

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{skill_store_url.rstrip('/')}/internal/skills/platform-defaults",
                params={"tenant_id": tenant_id, "user_id": user_id},
                headers={
                    "X-Tenant-Id": tenant_id,
                    "X-User-Id": user_id,
                    "X-Service-Scopes": service_scopes,
                },
            )
        if resp.status_code != 200:
            logger.warning(
                "platform default skills lookup returned %s; proceeding without them",
                resp.status_code,
            )
            return []

        items = resp.json().get("items") or []
        names: list[str] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name and name not in seen:
                names.append(name)
                seen.add(name)
        return names
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "platform default skills lookup failed; proceeding without them: %s",
            exc,
        )
        return []


async def _fetch_knowledge(
    *,
    knowledge_ingest_url: str,
    tenant_id: str,
    user_id: str,
    query: str,
    clearance: str,
) -> str:
    """Prefetch top-K tenant-knowledge hits for the worker's system prompt.

    Delegates the ``GET /search?scope=knowledge`` call + ``[Document | folder/path]``
    provenance formatting to :mod:`knowledge_client`.  Carries the run's
    **effective clearance** in ``X-User-Clearance`` so ki enforces the
    visibility gate.  Failure-open: any error → ``""``.
    """
    if not query.strip() or not knowledge_ingest_url:
        return ""
    return await knowledge_client.fetch_knowledge_prefetch(
        base_url=knowledge_ingest_url,
        tenant_id=tenant_id,
        user_id=user_id,
        clearance=clearance,
        query=query,
        k=_KNOWLEDGE_SEARCH_K,
    )


async def _assemble_history(
    svc: SessionService, session_id: str, *, current_task: str
) -> list[dict[str, Any]]:
    """Build structured prior-turn history for the worker to restore.

    Returns a list of items shaped like::

        {"role": "user", "type": "text", "content": "..."}
        {"role": "assistant", "type": "text", "content": "..."}
        {"role": "tool", "type": "tool", "tool": {id,name,input,result,state}}

    The current user message — already persisted by BFF just before this run
    was triggered — is excluded (it's this turn's input, not history).  SYSTEM
    rows are dropped; empty ASSISTANT/TOOL rows are skipped.  Failure-open:
    returns an empty list on any error so a history glitch never blocks a run.
    """
    try:
        messages = await svc.list_all_messages(session_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("history assembly failed (failure-open): %s", exc)
        return []

    # Drop the most recent USER row whose content matches this run's task —
    # that's the message BFF just wrote as this turn's input.
    # NB: the message table stores role inconsistently ("USER" upper-cased,
    # but "assistant"/"tool"/"system" lower-cased), so compare case-insensitively
    # — otherwise assistant/tool history is silently dropped and the model only
    # sees a stream of unanswered user questions.
    task_norm = current_task.strip()
    for i in range(len(messages) - 1, -1, -1):
        if (messages[i].role or "").upper() == "USER":
            if (messages[i].content or "").strip() == task_norm:
                messages.pop(i)
            break

    history: list[dict[str, Any]] = []
    for msg in messages:
        role = (msg.role or "").upper()
        if role == "USER":
            history.append(
                {"role": "user", "type": "text", "content": msg.content or ""}
            )
        elif role == "ASSISTANT":
            if (msg.content or "").strip():
                history.append(
                    {"role": "assistant", "type": "text", "content": msg.content}
                )
        elif role == "TOOL":
            calls = msg.tool_calls or []
            tool = calls[0] if isinstance(calls, list) and calls else None
            if isinstance(tool, dict) and tool:
                history.append({"role": "tool", "type": "tool", "tool": tool})
        # SYSTEM rows are intentionally skipped.
    return history


async def _get_scheduler_url(request: Request) -> str:
    url: str = request.app.state.settings.agent_scheduler_url
    return url


@router.post("/sessions/{session_id}/runs", status_code=201,
             dependencies=[Depends(require_permission("run.create", "run"))])
async def create_run(
    session_id: str,
    body: dict[str, Any],
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
    clearance: str = TClearance,
) -> dict[str, Any]:
    svc = SessionService(db, tenant_id=tenant_id)
    session = await svc.get_session(session_id)

    task = body.get("task", "")

    # Workspace files the user @-referenced in this message (``wsf_…`` ids).
    # Optional and best-effort: a non-list body value, or non-string elements,
    # are silently ignored (never a 400 — an over-eager client should still get a
    # working run).  Referenced files are hoisted to the front of the injected
    # workspace-file list below so the MAX_WORKSPACE_FILES / char-budget cuts fall
    # on the un-referenced tail first.
    raw_ws_file_ids = body.get("workspace_file_ids")
    workspace_file_ids: list[str] = (
        [x for x in raw_ws_file_ids if isinstance(x, str)]
        if isinstance(raw_ws_file_ids, list)
        else []
    )

    # App-harness fields (optional; only App runs send these — chat runs omit
    # them and keep working unchanged).  ``sys_prompt`` is appended to the
    # system prompt by the worker; ``sampling_params`` is applied per-run by
    # the gateway model builder; ``app_snapshot`` is persisted on the run row.
    sys_prompt = body.get("sys_prompt")
    sampling_params = body.get("sampling_params") or {}
    app_snapshot = body.get("app_snapshot")

    # Run origin — "user" (default) for interactive runs, "schedule" for
    # cron-runner-triggered runs, "webhook" for inbound-webhook-triggered runs.
    _VALID_SOURCES = {"user", "schedule", "webhook"}
    source: str = body.get("source") or "user"
    if source not in _VALID_SOURCES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid source {source!r}; must be one of {sorted(_VALID_SOURCES)}",
        )

    # Execution mode — every run relays to the agent-scheduler, which spawns a
    # Claude Agent SDK worker sandbox.  The legacy ``inline`` (in-process chat
    # completion) mode was removed; an old client still sending it is coerced
    # to ``sandbox`` rather than rejected so it keeps working.
    execution_mode: str = body.get("execution_mode") or "sandbox"
    if execution_mode == "inline":
        logger.info(
            "execution_mode=inline is no longer supported; using sandbox",
            extra={"session_id": session_id},
        )
        execution_mode = "sandbox"
    if execution_mode != "sandbox":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid execution_mode {execution_mode!r}; must be 'sandbox'",
        )

    # Persist the snapshot, source and execution_mode atomically with the run
    # row (same transaction).
    run = await svc.create_run(
        session_id, task, app_snapshot=app_snapshot, source=source,
        execution_mode=execution_mode,
    )

    # Relay to agent-scheduler.
    scheduler_url = await _get_scheduler_url(request)
    headers = {"X-Tenant-Id": tenant_id, "X-User-Id": user_id}

    # Optional attachment files — [{"key": s3key, "filename": name, "mime": mime}, ...]
    files: list[dict[str, Any]] = body.get("files") or []

    # Sum the byte size of this run's attachments so the scheduler can pick the
    # sandbox memory tier.  The file dicts carry only the blob ``key`` (no size),
    # so resolve sizes from the session's Attachment rows.  Failure-open: any
    # error or no files → 0 bytes → scheduler falls back to the small tier.
    total_attachment_bytes = 0
    if files:
        try:
            attachments = await svc.list_attachments(session_id)
            size_by_key = {
                att.blob_key: int(att.size_bytes or 0) for att in attachments
            }
            total_attachment_bytes = sum(
                size_by_key.get(f.get("key"), 0) for f in files
            )
        except Exception:  # noqa: BLE001 — never block a run on size lookup
            logger.warning(
                "failed to resolve attachment sizes; defaulting to small tier",
                extra={"session_id": session_id, "run_id": run.id},
            )
            total_attachment_bytes = 0

    # ── Effective clearance (file≤workspace≤user, retrieval side) ───────────
    # The run's effective clearance caps knowledge/memory visibility at
    # ``min(user_clearance, workspace_sensitivity)``.  Resolved with a light PK
    # lookup — NOT gated on the workspace_context_injection flag (clearance is a
    # security control, not opportunistic context).  Fail-open to the user's own
    # clearance so a lookup hiccup never hard-blocks retrieval.
    from agent_session_orchestrator.services.workspace_context import (
        resolve_workspace_sensitivity,
    )

    try:
        workspace_sensitivity = await resolve_workspace_sensitivity(db, session)
    except Exception:  # noqa: BLE001 — fail-open: retrieval still runs at user clearance
        logger.debug(
            "workspace sensitivity lookup failed (fail-open to user clearance)",
            extra={"session_id": session_id},
        )
        workspace_sensitivity = None
    effective_clearance = _effective_clearance(clearance, workspace_sensitivity)

    # Pre-fetch relevant memory (this user's own [memory] docs) from
    # knowledge-ingest; failure-open.  The tenant knowledge base
    # (scope=knowledge) is prefetched separately below and folded into
    # sys_prompt.
    knowledge_ingest_url = request.app.state.settings.knowledge_ingest_url
    memory_snippets = await _fetch_memory(
        knowledge_ingest_url=knowledge_ingest_url,
        tenant_id=tenant_id,
        user_id=user_id,
        query=task,
        session_id=session_id,
        clearance=effective_clearance,
    )

    # Assemble prior-turn conversation history so the worker can restore it
    # into the agent's context (multi-turn memory).  BFF writes the current
    # user message *before* triggering this run, so it's already in the table
    # as the latest USER row — we drop it (it's this run's input, not history).
    history = await _assemble_history(svc, session_id, current_task=task)

    # Skill selection has presence semantics, not truthiness semantics:
    # explicitly sending ``skills`` (including ``[]`` or ``null``) suppresses
    # automatic defaults.  Only a truly omitted key loads the user's installed
    # skills whose install relation has ``enabled_by_default=true``.  The
    # resolved names are written into task.json for the worker.
    if "skills" in body:
        run_skills = body.get("skills") or []
    else:
        _settings = request.app.state.settings
        run_skills = await _fetch_default_installed_skills(
            skill_store_url=_settings.skill_store_url,
            service_scopes=_settings.skill_store_service_scopes,
            tenant_id=tenant_id,
            user_id=user_id,
        )

    # MCP server selection has the SAME presence semantics as skills, and for
    # the same reason: ``[]`` and "key absent" are different requests.
    #   * key absent      -> ``None`` -> consumers fall back to the tenant's
    #                        enabled-by-default server set (the historical
    #                        auto-inject-everything behaviour).
    #   * explicit ``[]`` -> ``[]``   -> no MCP servers at all for this run.
    #   * non-empty list  -> only those server ids.
    # Never use a truthiness test here — ``if body.get("mcp_server_ids")``
    # would silently turn "disable all" back into "inject all".
    run_mcp_server_ids: list[str] | None = None
    if "mcp_server_ids" in body:
        _raw_mcp_ids = body.get("mcp_server_ids") or []
        run_mcp_server_ids = [str(s) for s in _raw_mcp_ids if s]

    # ── Workspace context ───────────────────────────────────────────────────
    # A session created against a workspace inherits its instructions / base
    # files / skills on every run.  Values are
    # extracted into plain str/dicts immediately so nothing downstream holds
    # ORM rows.  Gated on the ``workspace_context_injection`` flag (fail-open
    # ON) and fail-open throughout — a workspace hiccup never blocks a run.
    from agent_session_orchestrator.services.workspace_context import (
        load_workspace_context,
        workspace_context_injection_enabled,
    )

    # ``None`` = session not workspace-bound (or context injection unavailable);
    # ``[]`` = workspace bound but it has no base files.
    ws_instructions: str | None = None
    ws_files: list[dict[str, Any]] | None = None
    if session.workspace_id and await workspace_context_injection_enabled(tenant_id):
        try:
            ws_ctx = await load_workspace_context(
                db, session, user_clearance=clearance
            )
        except Exception:  # noqa: BLE001 — fail-open: run proceeds without workspace context
            logger.warning(
                "workspace context load failed (fail-open)",
                extra={"session_id": session_id, "run_id": run.id},
                exc_info=True,
            )
            ws_ctx = None
        if ws_ctx is not None:
            ws_instructions = ws_ctx.instructions
            ws_files = [
                {
                    "id": f.id,
                    "key": f.blob_key,
                    "filename": f.filename,
                    "mime": f.mime,
                    "size_bytes": int(f.size_bytes or 0),
                }
                for f in ws_ctx.files
            ]
            # @-reference priority: hoist any workspace file the message referenced
            # to the front (preserving each group's original relative order) so the
            # downstream MAX_WORKSPACE_FILES + char-budget truncation naturally
            # favours the @-referenced files.  Unknown ids are silently ignored.
            ws_files = _prioritize_workspace_files(ws_files, workspace_file_ids)
            # Union the workspace's skill set into the run's (order-preserving
            # dedupe: explicit/default skills first, then workspace additions).
            run_skills = _merge_skills(list(run_skills), ws_ctx.skills)

    # ── Platform-global default skills ──────────────────────────────────────
    # Union the platform default skill names on top of whatever run_skills
    # resolved to (explicit selection, user defaults, or workspace additions),
    # so every organization gets them by default with zero per-user install
    # rows.  They are appended last (deduped, order-preserving) so
    # explicit/default/workspace skills keep priority.
    #
    # Per-session opt-out: a request may set ``include_platform_defaults: false``
    # to suppress the platform defaults for THIS run only (e.g. a focused session
    # where the extra skill catalog is noise).  Presence-tolerant — only a literal
    # ``false`` disables; absent / null / true all keep the defaults on, so the
    # "自带" behaviour stays the default.  Failure-open — a skill-store hiccup
    # never blocks the run.
    if body.get("include_platform_defaults") is not False:
        _platform_defaults = await _fetch_platform_default_skills(
            skill_store_url=request.app.state.settings.skill_store_url,
            service_scopes=request.app.state.settings.skill_store_service_scopes,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        run_skills = _merge_skills(list(run_skills), _platform_defaults)

    # ── Workspace context, sandbox shape ────────────────────────────────────
    # Base files ride the ordinary files[] channel (the worker lands each entry
    # at /workspace/inputs/<filename>); a workspace file whose name collides
    # with a session attachment already in files[] is skipped — same-name
    # writes would overwrite each other in the sandbox, and the session-scoped
    # upload wins.  Sizes count toward the memory-tier total.  Instructions
    # are folded into sys_prompt (the worker appends it to the system prompt);
    # skills were already unioned into run_skills above.
    if ws_files:
        existing_names = {
            str(f.get("filename") or "") for f in files if isinstance(f, dict)
        }
        for wf in ws_files:
            if wf["filename"] in existing_names:
                logger.info(
                    "skipping workspace file shadowed by a session attachment",
                    extra={"run_id": run.id, "ws_filename": wf["filename"]},
                )
                continue
            files.append(
                {"key": wf["key"], "filename": wf["filename"], "mime": wf["mime"]}
            )
            existing_names.add(wf["filename"])
            total_attachment_bytes += int(wf.get("size_bytes") or 0)
    if ws_instructions:
        sys_prompt = (
            (sys_prompt + "\n\n" if sys_prompt else "")
            + "# Workspace instructions\n"
            + ws_instructions
        )

    # Tenant knowledge base: top-K hits for this task, gated by the effective
    # clearance, appended to sys_prompt.  Failure-open (empty → no block).
    knowledge_snippets = await _fetch_knowledge(
        knowledge_ingest_url=knowledge_ingest_url,
        tenant_id=tenant_id,
        user_id=user_id,
        query=task,
        clearance=effective_clearance,
    )
    if knowledge_snippets.strip():
        sys_prompt = (
            (sys_prompt + "\n\n" if sys_prompt else "")
            + "Knowledge base excerpts:\n"
            + knowledge_snippets.strip()
        )

    scheduler_body: dict[str, Any] = {
        "session_id": session_id,
        "run_id": run.id,
        "task": task,
        "agent_config": {
            **(body.get("agent_config") or {}),
            "model": body.get("model"),
            "reasoning_effort": body.get("reasoning_effort"),
        },
        "files": files,
        "workspace_id": session.workspace_id or "",
        # Total attachment bytes for this run; the scheduler maps it
        # to a concrete sandbox memory limit (small vs large tier).
        "total_attachment_bytes": total_attachment_bytes,
        "memory": memory_snippets,
        # Explicit per-message selection, or (only when the request
        # omitted ``skills``) the user's default-enabled installs.
        # The scheduler writes these into task.json["skills"].
        "skills": run_skills,
        # Prior-turn conversation history for multi-turn memory.
        "history": history,
        # App-harness fields (empty/absent for chat runs).  The
        # scheduler writes these into task.json for the worker:
        # ``sys_prompt`` is appended to the system prompt;
        # ``sampling_params`` is applied per-run by the gateway model.
        "sys_prompt": sys_prompt,
        "sampling_params": sampling_params,
        # HITL gating: normal session runs always enable human-in-the-loop
        # so the worker can pause for confirmation / free-text questions.
        # Scheduled/batch runs will override to False (future).
        "hitl": True,
        # Effective clearance (min(user, workspace)) so the sandbox
        # worker's knowledge_tool can send X-User-Clearance to
        # knowledge-ingest and the scheduler can sign it into the
        # broker lease (broker mode re-injects it from the lease).
        # Empty/absent -> ki treats the caller as L1.
        "effective_clearance": effective_clearance,
    }
    # MCP selection is presence-carried all the way down: the key is only put
    # on the wire when the caller expressed a choice.  ``None`` (= caller said
    # nothing) must NOT be serialized as ``null`` — the scheduler and the
    # in-sandbox injector distinguish "absent -> tenant default" from
    # "[] -> disable all", and a null would collapse that distinction.
    if run_mcp_server_ids is not None:
        scheduler_body["mcp_server_ids"] = run_mcp_server_ids

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{scheduler_url}/runs",
                json=scheduler_body,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        # No container was spawned — let the transaction roll back.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="agent-scheduler unreachable",
        ) from exc

    if resp.status_code >= 400:
        # Don't echo upstream body verbatim — it may include stack traces or
        # other sensitive details.  Capture in audit instead.
        logger.warning(
            "scheduler rejected run",
            extra={"status": resp.status_code, "run_id": run.id},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"scheduler rejected run (HTTP {resp.status_code})",
        )

    scheduler_run = resp.json()

    # Update both rows; the surrounding ``async with session.begin()`` will
    # commit once when the request completes, or roll back atomically if
    # anything else in this handler raises.
    # Docker container ids are 64 chars; the ``sandbox_id`` column is
    # String(50).  Persist Docker's standard 12-char short form, which
    # the scheduler also accepts on subsequent lookups.
    sandbox_id_full = str(scheduler_run.get("sandbox_id") or "")
    run.sandbox_id = sandbox_id_full[:12] if sandbox_id_full else None
    run.status = "RUNNING"
    run.started_at = _now()
    session.current_run_id = run.id
    await db.flush()

    return {
        "id": run.id,
        "session_id": session_id,
        "sandbox_id": run.sandbox_id,
        "status": run.status,
        "task_description": run.task_description,
        "source": run.source,
        "execution_mode": run.execution_mode,
        "created_at": run.created_at.isoformat(),
    }


@router.get("/sessions/{session_id}/runs",
            dependencies=[Depends(require_permission("run.read", "run"))])
async def list_runs(
    session_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> dict[str, Any]:
    svc = SessionService(db, tenant_id=tenant_id)
    runs = await svc.list_runs(session_id)
    return {"items": [_serialize_run(r) for r in runs]}


@router.get("/runs",
            dependencies=[Depends(require_permission("run.read", "run"))])
async def list_tenant_runs(
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    status: str | None = None,
    app_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List runs for the current tenant across all sessions.

    Tenant is scoped from the X-Tenant-Id middleware (same as every other
    endpoint here).  ``status`` is normalised to upper-case before filtering;
    ``app_id`` narrows the result to a single App's execution history (NULL
    ``app_id`` chat runs never match - the App history view stays chat-free);
    ``limit`` is capped at 200 and ``offset`` pages past the newest rows.  Both
    ``app_id`` and ``offset`` default to off so the existing tenant-wide
    behaviour is preserved.  Response shape matches the session-level list.
    """
    svc = SessionService(db, tenant_id=tenant_id)
    status_norm = status.upper() if status else None
    runs = await svc.list_tenant_runs(
        status=status_norm, limit=limit, app_id=app_id, offset=offset,
    )
    return {"items": [_serialize_run(r) for r in runs]}


@router.get("/sessions/{session_id}/runs/{run_id}",
            dependencies=[Depends(require_permission("run.read", "run"))])
async def get_run(
    session_id: str,
    run_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> dict[str, Any]:
    svc = SessionService(db, tenant_id=tenant_id)
    run = await svc.get_run(session_id, run_id)

    # Opportunistically reconcile a still-running run before returning.
    #
    # App runs are triggered via POST /sessions/{id}/runs directly (not the
    # chat path), and the App result page polls THIS endpoint for terminal
    # status + result_summary.  It never calls list_messages or opens the chat
    # SSE stream — the only two places that used to invoke reconciliation.  So
    # without this, the sandbox container exits but the run row stays RUNNING
    # forever and the page polls indefinitely.  We reuse the exact same helper
    # (and the same failure-open + flush discipline) as list_messages so the
    # chat path behaviour is unchanged.
    if run.status in ("RUNNING", "PENDING"):
        try:
            scheduler_url = request.app.state.settings.agent_scheduler_url
            knowledge_ingest_url = request.app.state.settings.knowledge_ingest_url
            redis_url = request.app.state.settings.redis_url
            # The scheduler's authz guard rejects the "SYSTEM" sentinel, so use
            # the session's originating user for the poll header (as list_messages does).
            session_obj = await svc.get_session(session_id)
            await _reconcile_run_completion(
                scheduler_url=scheduler_url,
                tenant_id=tenant_id,
                user_id=session_obj.user_id,
                run=run,
                svc=svc,
                knowledge_ingest_url=knowledge_ingest_url,
                redis_url=redis_url,
            )
            # Flush (not commit) so the reconciled status/result_summary/tokens
            # land in this transaction; the db_session dependency commits on
            # return.  ``run`` is the same ORM instance the helper mutated, so
            # the response below already reflects the new values.
            await db.flush()
        except Exception as exc:  # best-effort — never block the run read
            logger.warning(
                "reconcile in get_run failed", extra={"run_id": run_id, "err": str(exc)}
            )

    return {
        "id": run.id,
        "session_id": run.session_id,
        "sandbox_id": run.sandbox_id,
        "status": run.status,
        "task_description": run.task_description,
        "result_summary": run.result_summary,
        "error_message": run.error_message,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_at": run.created_at.isoformat(),
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "total_tokens": run.total_tokens,
        "app_snapshot": run.app_snapshot,
        "source": run.source,
        "execution_mode": run.execution_mode,
    }


@router.get("/sessions/{session_id}/runs/{run_id}/state",
            dependencies=[Depends(require_permission("run.read", "run"))])
async def get_run_state(
    session_id: str,
    run_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> dict:
    """Return the real-time hot-state for a run from Redis.

    The runtime_worker inside the sandbox writes a JSON blob to
    ``agent:run:{run_id}:state`` after every ThinkEvent / step boundary.
    This endpoint reads that blob so the frontend can show live plan/progress.

    Failure-open: if Redis is unavailable or the key has expired, returns
    200 with ``plan=""``, ``step=0``, ``cursor=""`` and status from the DB row.
    """
    import json

    # Validate ownership: raises 404 if run does not belong to this session/tenant.
    svc = SessionService(db, tenant_id=tenant_id)
    run = await svc.get_run(session_id, run_id)

    redis_state: dict | None = None
    redis_url: str | None = request.app.state.settings.redis_url
    if redis_url:
        try:
            import redis.asyncio as aioredis  # type: ignore[import-not-found]

            client = aioredis.Redis.from_url(
                redis_url,
                socket_connect_timeout=2,
                socket_timeout=2,
                decode_responses=True,
            )
            try:
                raw = await client.get(f"agent:run:{run_id}:state")
                if raw:
                    redis_state = json.loads(raw)
            finally:
                await client.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis read failed for run_state %s: %s", run_id, exc)

    if redis_state:
        return {
            "run_id": run_id,
            "plan": redis_state.get("plan") or "",
            "step": redis_state.get("step") or 0,
            "status": redis_state.get("status") or run.status,
            "cursor": redis_state.get("cursor") or "",
        }

    # Redis key absent / Redis unavailable — return DB status, empty plan.
    return {
        "run_id": run_id,
        "plan": "",
        "step": 0,
        "status": run.status,
        "cursor": "",
    }


@router.get(
    "/sessions/{session_id}/pending",
    dependencies=[Depends(require_permission("run.read", "run"))],
)
async def get_session_pending(
    session_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> dict[str, Any]:
    """Return all currently-paused HITL interactions across the session's runs.

    The frontend calls this on mount / reconnect so it can rehydrate the
    ConfirmCard / QuestionCard for a run that paused while the page was closed
    (or paused on an earlier run in the session).  Without it, the live SSE
    ``pending_confirmation`` / ``pending_question`` event is the only signal and
    a page refresh leaves the run PAUSED forever with no way to approve/reject.

    Authority is Redis, not the DB row: the worker writes
    ``agent:run:{run_id}:paused`` before scheduler reconciliation flips the DB
    row to PAUSED, so a RUNNING/PENDING run may still have a live pause blob
    (same reasoning as ``respond_run``).  We therefore list every run in the
    session and GET its paused key directly rather than filtering on DB status.

    Best-effort / failure-open: never 404 or 500 from this mount-time pull.
    If Redis is unavailable or unconfigured we return ``{"items": []}``.  The
    ``agent_state`` field of the paused blob is deliberately NOT leaked (huge).
    """
    import json

    # Validate ownership/tenant scope (raises 404 only if the session itself is
    # not visible to this tenant — list_runs is tenant-scoped regardless).
    svc = SessionService(db, tenant_id=tenant_id)
    runs = await svc.list_runs(session_id)

    items: list[dict[str, Any]] = []
    redis_url: str | None = request.app.state.settings.redis_url
    if not redis_url or not runs:
        return {"items": items}

    try:
        import redis.asyncio as aioredis  # type: ignore[import-not-found]

        client = aioredis.Redis.from_url(
            redis_url,
            socket_connect_timeout=2,
            socket_timeout=2,
            decode_responses=True,
        )
        try:
            for run in runs:
                raw = await client.get(f"agent:run:{run.id}:paused")
                if not raw:
                    continue
                blob = json.loads(raw)
                items.append(
                    {
                        "run_id": run.id,
                        "kind": blob.get("kind"),
                        "tool_calls": [
                            {
                                "id": tc.get("id"),
                                "name": tc.get("name"),
                                "args": tc.get("args"),
                            }
                            for tc in (blob.get("tool_calls") or [])
                        ],
                        "question": blob.get("question"),
                        "paused_at": blob.get("paused_at"),
                    }
                )
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001 — never break the mount path
        logger.warning(
            "pending read failed for session %s: %s", session_id, exc
        )
        return {"items": []}

    return {"items": items}


@router.post(
    "/sessions/{session_id}/runs/{run_id}/respond",
    dependencies=[Depends(require_permission("run.create", "run"))],
)
async def respond_run(
    session_id: str,
    run_id: str,
    body: dict[str, Any],
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> dict[str, Any]:
    """Respond to a HITL pause: resume a paused run with a confirmation or answer.

    Body:
      ``kind``        — "confirm" | "answer"
      ``tool_call_id``— the tool call id from the pending_confirmation / pending_question event
      ``confirmed``   — bool | null  (used when kind="confirm")
      ``answer``      — str | null   (used when kind="answer")

    Flow:
    1. Load run and validate the submitted response body.
    2. Read + validate ``agent:run:{run_id}:paused`` from Redis (404/410 if missing — pause expired).
       The Redis paused blob is the authoritative pause signal: the live SSE
       stream can deliver ``pending_confirmation`` before scheduler reconciliation
       has flipped the DB row to PAUSED, so RUNNING/PENDING with a paused blob is
       accepted as an in-flight pause.
    3. Re-dispatch to the scheduler with a ``resume`` payload containing the agent_state
       from the paused Redis key plus the user decision.
    4. Update the same run row: status="RUNNING", sandbox_id=<new container>, clear errors.
    5. DELETE the paused Redis key (consumed).
    6. Return {run_id, status: "RUNNING"}.

    Uses the same ``run.create`` permission as create_run (write-level action on runs).
    """
    import json as _json

    svc = SessionService(db, tenant_id=tenant_id)
    run = await svc.get_run(session_id, run_id)

    # Step 1: Validate the body fields before touching Redis state.
    kind = body.get("kind")
    if kind not in ("confirm", "answer"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="body.kind must be 'confirm' or 'answer'",
        )
    tool_call_id = body.get("tool_call_id")
    if not tool_call_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="body.tool_call_id is required",
        )

    confirmed: bool | None = body.get("confirmed")
    answer: str | None = body.get("answer")

    # Step 2: Read the paused state from Redis.
    redis_url: str | None = request.app.state.settings.redis_url
    if not redis_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis is not configured; cannot resume paused run",
        )

    paused_key = f"agent:run:{run_id}:paused"
    resume_lock_key = f"agent:run:{run_id}:resume_lock"
    paused_data: dict[str, Any] | None = None
    redis_client: Any | None = None
    resume_lock_acquired = False
    try:
        import redis.asyncio as aioredis  # type: ignore[import-not-found]

        redis_client = aioredis.Redis.from_url(
            redis_url,
            socket_connect_timeout=2,
            socket_timeout=2,
            decode_responses=True,
        )
        raw = await redis_client.get(paused_key)
        if raw:
            paused_data = _json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("respond: Redis read failed for run %s: %s", run_id, exc)
        if redis_client is not None:
            try:
                await redis_client.aclose()
            except Exception:  # noqa: BLE001
                pass
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis read failed; cannot resume paused run",
        ) from exc

    if paused_data is None:
        if redis_client is not None:
            try:
                await redis_client.aclose()
            except Exception:  # noqa: BLE001
                pass
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Paused state not found for run (may have expired — TTL 7d); "
                "cannot resume"
            ),
        )

    # Validate the response targets the actual paused interaction.  The frontend
    # should never send a mismatch, but this keeps stale/replayed cards from
    # resuming the wrong tool call.
    expected_pause_kind = "question" if kind == "answer" else "confirm"
    paused_kind = paused_data.get("kind")
    if paused_kind and paused_kind != expected_pause_kind:
        if redis_client is not None:
            await redis_client.aclose()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"response kind {kind!r} does not match paused interaction "
                f"{paused_kind!r}"
            ),
        )
    paused_tool_calls = paused_data.get("tool_calls") or []
    if isinstance(paused_tool_calls, list) and paused_tool_calls:
        paused_ids = {
            str(tc.get("id"))
            for tc in paused_tool_calls
            if isinstance(tc, dict) and tc.get("id") is not None
        }
        if paused_ids and str(tool_call_id) not in paused_ids:
            if redis_client is not None:
                await redis_client.aclose()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="body.tool_call_id does not match paused interaction",
            )

    # Guard terminal states, but accept RUNNING/PENDING when Redis already has
    # the paused blob.  That is the normal HITL race: the worker wrote the pause
    # state and SSE rendered the card before scheduler reconciliation updated DB.
    if run.status not in ("PAUSED", "RUNNING", "PENDING"):
        if redis_client is not None:
            await redis_client.aclose()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"run is not PAUSED (current status: {run.status}); cannot respond",
        )
    if run.status != "PAUSED":
        logger.info(
            "respond: accepting Redis-authoritative pause before DB status PAUSED",
            extra={"run_id": run_id, "db_status": run.status},
        )

    # Only one resume dispatch may consume a paused blob.  Without this lock,
    # two fast POSTs can both read the same Redis key and spawn duplicate
    # resumed sandboxes before the success path deletes the key.
    try:
        resume_lock_acquired = bool(
            await redis_client.set(resume_lock_key, "1", ex=120, nx=True)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("respond: Redis resume lock failed for run %s: %s", run_id, exc)
        if redis_client is not None:
            await redis_client.aclose()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis lock failed; cannot resume paused run",
        ) from exc

    if not resume_lock_acquired:
        if redis_client is not None:
            await redis_client.aclose()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="run resume is already in progress",
        )

    # agent_state is the worker's full in-memory state snapshot; replaying it
    # replaces the need to re-send conversation history.
    agent_state: dict[str, Any] = paused_data.get("agent_state") or {}

    # Step 3: Re-dispatch to the scheduler.
    # Reuse the same env/skill assembly as create_run (scheduler builds the
    # sandbox from the task payload, not from the original run_id).  For a
    # resume the history is replaced by agent_state; we pass history=[] to
    # signal the worker to restore from agent_state instead.
    scheduler_url = await _get_scheduler_url(request)
    headers = {"X-Tenant-Id": tenant_id, "X-User-Id": user_id}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{scheduler_url}/runs",
                json={
                    "session_id": session_id,
                    "run_id": run_id,
                    "task": run.task_description,
                    "agent_config": {},
                    "files": [],
                    "total_attachment_bytes": 0,
                    "memory": "",
                    # agent_state from the paused key replaces history for resume.
                    "history": [],
                    "sys_prompt": None,
                    "sampling_params": {},
                    # HITL gating: resumed runs retain HITL so the worker can
                    # pause again if needed.
                    "hitl": True,
                    # Resume payload: tells the worker to restore agent_state
                    # and deliver the user decision as the tool response.
                    "resume": {
                        "kind": kind,
                        "tool_call_id": tool_call_id,
                        "confirmed": confirmed,
                        "answer": answer,
                        "agent_state": agent_state,
                    },
                },
                headers=headers,
            )
    except httpx.HTTPError as exc:
        if resume_lock_acquired and redis_client is not None:
            try:
                await redis_client.delete(resume_lock_key)
            except Exception:  # noqa: BLE001
                pass
        if redis_client is not None:
            await redis_client.aclose()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="agent-scheduler unreachable",
        ) from exc

    if resp.status_code >= 400:
        logger.warning(
            "scheduler rejected resume",
            extra={"status": resp.status_code, "run_id": run_id},
        )
        if resume_lock_acquired and redis_client is not None:
            try:
                await redis_client.delete(resume_lock_key)
            except Exception:  # noqa: BLE001
                pass
        if redis_client is not None:
            await redis_client.aclose()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"scheduler rejected resume (HTTP {resp.status_code})",
        )

    scheduler_run = resp.json()

    # Step 4: Update the same run row — reuse, do NOT create a new Run.
    sandbox_id_full = str(scheduler_run.get("sandbox_id") or "")
    run.sandbox_id = sandbox_id_full[:12] if sandbox_id_full else None
    run.status = "RUNNING"
    run.error_message = None
    run.started_at = _now()
    await db.flush()

    # Step 5: Delete the paused Redis key (it has been consumed) and release the
    # short resume lock.
    try:
        if redis_client is not None:
            await redis_client.delete(paused_key)
            if resume_lock_acquired:
                await redis_client.delete(resume_lock_key)
            await redis_client.aclose()
    except Exception as exc:  # noqa: BLE001 — failure-open; run is already RUNNING
        logger.warning(
            "respond: failed to delete paused Redis key / resume lock for run %s: %s",
            run_id,
            exc,
        )

    # Step 6: Return.
    return {"run_id": run_id, "status": "RUNNING"}


async def _do_cancel_run(
    *,
    session_id: str,
    run_id: str,
    request: Request,
    db: AsyncSession,
    tenant_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Core cancel logic — shared by the run-level and session-level endpoints."""
    svc = SessionService(db, tenant_id=tenant_id)
    run = await svc.get_run(session_id, run_id)

    # Legacy inline (chat-mode) runs have no sandbox / scheduler container, so
    # there is nothing to stop — just mark the row cancelled.
    if run.execution_mode == "inline":
        run.status = "CANCELLED"
        run.finished_at = _now()
        await db.flush()
        await svc.create_message(
            session_id,
            role="system",
            content="Task cancelled",
            run_id=run_id,
        )
        return {"id": run.id, "status": "CANCELLED"}

    # Use run.sandbox_id (short Docker container id) as the scheduler path
    # segment — the same convention used by _reconcile_run_completion in
    # messages.py.  Passing run_id here would fail because the scheduler
    # looks up the container by its Docker id, not our internal run id.
    scheduler_run_key = run.sandbox_id or run_id

    scheduler_url = await _get_scheduler_url(request)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{scheduler_url}/runs/{scheduler_run_key}/cancel",
                headers={"X-Tenant-Id": tenant_id, "X-User-Id": user_id},
            )
    except httpx.HTTPError:
        # The container may already have stopped; we still mark it cancelled
        # in our DB so the UI stops showing it as RUNNING.  The next sandbox
        # timeout will reap any leftover container.
        logger.warning("cancel: scheduler unreachable", extra={"run_id": run_id})

    run.status = "CANCELLED"
    run.finished_at = _now()
    await db.flush()

    # Write a system message so the transcript records the cancellation.
    await svc.create_message(
        session_id,
        role="system",
        content="Task cancelled",
        run_id=run_id,
    )

    return {"id": run.id, "status": "CANCELLED"}


@router.get("/runs/token-usage/daily")
async def daily_token_usage(
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    days: int = Query(default=7, ge=1, le=90),
) -> dict[str, Any]:
    """Return per-day token totals for the last N days (default 7).

    SQL: SELECT date(created_at) AS day, SUM(COALESCE(total_tokens, 0)) AS tokens
         FROM run
         WHERE tenant_id = :tenant_id AND created_at >= now() - interval 'N days'
         GROUP BY date(created_at)

    Fills in missing days with 0 so the response always contains exactly
    ``days`` entries in ascending order.
    """
    cutoff = dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=days)

    stmt = (
        select(
            func.date(Run.created_at).label("day"),
            func.sum(func.coalesce(Run.total_tokens, 0)).label("tokens"),
        )
        .where(Run.tenant_id == tenant_id, Run.created_at >= cutoff)
        .group_by(func.date(Run.created_at))
        .order_by(func.date(Run.created_at))
    )
    result = await db.execute(stmt)
    rows = result.all()

    # Build a dict keyed by date string for fast lookup.
    by_day: dict[str, int] = {}
    for row in rows:
        # row.day may be a date object or a string depending on the DB driver.
        day_key = str(row.day) if not isinstance(row.day, str) else row.day
        by_day[day_key] = int(row.tokens or 0)

    # Generate the full range of the last N days (today is index days-1).
    today = dt.date.today()
    out: list[dict[str, Any]] = []
    for i in range(days):
        d = today - dt.timedelta(days=days - 1 - i)
        key = d.isoformat()
        out.append({"day": key, "tokens": by_day.get(key, 0)})

    return {"days": out}


@router.get("/runs/token-usage/breakdown")
async def token_usage_breakdown(
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    days: int = Query(default=7, ge=1, le=90),
) -> dict[str, Any]:
    """Return a token-usage breakdown for the last N days (default 7).

    Combines the per-day series (like ``daily_token_usage`` but split into
    input/output/total) with a per-source rollup over the same window.

    The ``daily`` list always contains exactly ``days`` entries in ascending
    order (missing days zero-filled, today is the last entry).  ``by_source``
    holds one row per distinct ``Run.source`` that actually appears in the
    window, sorted by ``total_tokens`` descending; it is not zero-filled.
    """
    cutoff = dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=days)

    # Per-day series, split into input/output/total token sums.
    daily_stmt = (
        select(
            func.date(Run.created_at).label("day"),
            func.sum(func.coalesce(Run.input_tokens, 0)).label("input_tokens"),
            func.sum(func.coalesce(Run.output_tokens, 0)).label("output_tokens"),
            func.sum(func.coalesce(Run.total_tokens, 0)).label("total_tokens"),
        )
        .where(Run.tenant_id == tenant_id, Run.created_at >= cutoff)
        .group_by(func.date(Run.created_at))
        .order_by(func.date(Run.created_at))
    )
    daily_result = await db.execute(daily_stmt)
    daily_rows = daily_result.all()

    # Build a dict keyed by date string for fast lookup.
    by_day: dict[str, dict[str, int]] = {}
    for row in daily_rows:
        # row.day may be a date object or a string depending on the DB driver.
        day_key = str(row.day) if not isinstance(row.day, str) else row.day
        by_day[day_key] = {
            "input_tokens": int(row.input_tokens or 0),
            "output_tokens": int(row.output_tokens or 0),
            "total_tokens": int(row.total_tokens or 0),
        }

    # Generate the full range of the last N days (today is index days-1).
    today = dt.date.today()
    daily: list[dict[str, Any]] = []
    for i in range(days):
        d = today - dt.timedelta(days=days - 1 - i)
        key = d.isoformat()
        totals = by_day.get(key)
        daily.append(
            {
                "day": key,
                "input_tokens": totals["input_tokens"] if totals else 0,
                "output_tokens": totals["output_tokens"] if totals else 0,
                "total_tokens": totals["total_tokens"] if totals else 0,
            }
        )

    # Per-source rollup over the same window (no zero-fill).
    source_stmt = (
        select(
            Run.source.label("source"),
            func.sum(func.coalesce(Run.total_tokens, 0)).label("total_tokens"),
            func.count().label("run_count"),
        )
        .where(Run.tenant_id == tenant_id, Run.created_at >= cutoff)
        .group_by(Run.source)
        .order_by(func.sum(func.coalesce(Run.total_tokens, 0)).desc())
    )
    source_result = await db.execute(source_stmt)
    source_rows = source_result.all()

    by_source: list[dict[str, Any]] = []
    for row in source_rows:
        # Coalesce a NULL/empty source to "user" so the output never crashes.
        by_source.append(
            {
                "source": row.source or "user",
                "total_tokens": int(row.total_tokens or 0),
                "run_count": int(row.run_count or 0),
            }
        )

    return {"days_window": days, "daily": daily, "by_source": by_source}


@router.post("/sessions/{session_id}/runs/{run_id}/cancel",
             dependencies=[Depends(require_permission("run.cancel", "run"))])
async def cancel_run(
    session_id: str,
    run_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> dict[str, Any]:
    return await _do_cancel_run(
        session_id=session_id,
        run_id=run_id,
        request=request,
        db=db,
        tenant_id=tenant_id,
        user_id=user_id,
    )


@router.post("/sessions/{session_id}/cancel",
             dependencies=[Depends(require_permission("run.cancel", "run"))])
async def cancel_session_run(
    session_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> dict[str, Any]:
    """Convenience endpoint: cancel the current active run for a session.

    The frontend only knows the session_id at cancel time; this endpoint
    resolves the current run internally so the client doesn't need to track
    run_id.
    """
    from sqlalchemy import select as _select

    from agent_session_orchestrator.domain.models import Run as _Run

    svc = SessionService(db, tenant_id=tenant_id)
    await svc.get_session(session_id)

    # Find the most recent RUNNING or PENDING run for this session.
    stmt = (
        _select(_Run)
        .where(
            _Run.session_id == session_id,
            _Run.tenant_id == tenant_id,
            _Run.status.in_(("RUNNING", "PENDING")),
        )
        .order_by(_Run.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    run = result.scalar_one_or_none()

    if run is None:
        # No active run — idempotently return success.
        return {"id": None, "status": "CANCELLED", "detail": "no active run"}

    return await _do_cancel_run(
        session_id=session_id,
        run_id=run.id,
        request=request,
        db=db,
        tenant_id=tenant_id,
        user_id=user_id,
    )
