"""Privacy service — GDPR data-owner controls for session-orchestrator data.

What lives here
---------------
* **Export** (Art. 15/20) — :meth:`PrivacyService.export_user_data` returns
  every session (messages, runs, attachments, canvases + versions) and
  workspace (files + versions) a user owns, *including* soft-deleted rows that
  have not been purged yet, plus the user's privacy settings.  A user's
  sessions include those a service account (cron-runner) ran on their behalf
  — see :func:`_subject_sessions`.
* **Erasure** (Art. 17) — :meth:`PrivacyService.erase_user_data` cancels the
  user's active runs, then HARD-deletes their sessions + children and
  workspaces + files + versions, the S3 objects behind them, the Redis
  ``agent:run:{run_id}:*`` hot state and the privacy-settings row.
* **Memory opt-out** — :func:`memory_enabled_for` is the fail-closed gate the
  run paths consult before prefetching or writing long-term memory.
* **Trash purge** (Art. 5(1)(e)) — :func:`purge_trash` physically deletes rows
  whose ``deleted_at`` is older than the grace period, through the same purge
  core as erasure.
* **Blob hygiene** — :func:`delete_unreferenced_blobs` is the one place that
  deletes S3 objects.  Workspace writeback, version restore and output
  attachments can share an S3 key between rows, so a key is only deleted once
  no attachment / workspace file / file version row references it any more.

Transaction discipline
----------------------
Same as :mod:`session_service`: nothing here commits.  Row deletes run inside
the caller's tenant-bound transaction (RLS still guards every mutation; the
background purge re-binds the tenant per item via ``get_session``).  Blob
deletion happens *after* the row deletes are flushed but *before* commit, so a
failed blob delete in strict mode aborts the transaction and the erasure can
simply be retried — the alternative (delete rows first, blobs after commit)
would leave unreferenced, unfindable objects behind on failure.

Process-wide registry
---------------------
Some call sites cannot reach ``app.state`` (the reconcile path, the service
layer's rewind and delete).  :func:`configure` — called once from
``main.create_app`` — registers the app's settings and blob store so those
paths can find them; explicit arguments always win over the registry.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.config import Settings
from agent_session_orchestrator.domain.db import get_session, privileged_scalars
from agent_session_orchestrator.domain.models import (
    Attachment,
    CanvasDocument,
    CanvasSuggestion,
    CanvasVersion,
    Message,
    Run,
    Session,
    UserPrivacySettings,
    Workspace,
    WorkspaceFile,
    WorkspaceFileVersion,
)
from agent_session_orchestrator.errors import OrchestratorError

logger = logging.getLogger(__name__)

USER_ID_RE = re.compile(r"^usr_[0-9A-Z]{26}$")

# Bind-parameter budget per IN (...) list — far below asyncpg's 32767 limit.
_CHUNK = 500
# Runs that may still own a live sandbox (PAUSED keeps its run dir for resume).
_ACTIVE_RUN_STATUSES = ("PENDING", "RUNNING", "PAUSED")
# Every Redis key family written for a run (worker, tool-broker, orchestrator).
_RUN_KEY_SUFFIXES = ("state", "tokens", "paused", "canvas", "resume_lock", "dispatch")
# Parallel S3 deletes / knowledge-ingest calls per erasure or purge.
_BLOB_DELETE_CONCURRENCY = 8
_KI_CONCURRENCY = 4
_KI_TIMEOUT = 3.0
_SCHEDULER_CANCEL_TIMEOUT = 10.0


# ── errors ───────────────────────────────────────────────────────────────────


class PrivacyForbiddenError(OrchestratorError):
    def __init__(self) -> None:
        super().__init__(
            "user_id may only be given by an ADMIN actor",
            code="AGENT.PRIVACY.FORBIDDEN",
            status=403,
            user_message="You can only access your own data.",
        )


class PrivacyInvalidSubjectError(OrchestratorError):
    def __init__(self, detail: str) -> None:
        super().__init__(
            detail,
            code="AGENT.PRIVACY.INVALID_SUBJECT",
            status=422,
            user_message="A valid user id is required.",
        )


class PrivacyBlobStoreError(OrchestratorError):
    def __init__(self, detail: str) -> None:
        super().__init__(
            detail,
            code="AGENT.PRIVACY.BLOB_STORE_UNAVAILABLE",
            status=503,
            user_message="Stored files could not be deleted right now. Please try again later.",
        )


# ── process-wide registry ────────────────────────────────────────────────────

_registry: dict[str, Any] = {"settings": None, "blob_store": None}


def configure(*, settings: Settings | None, blob_store: Any | None) -> None:
    """Register the app's settings + blob store for paths without ``app.state``."""
    _registry["settings"] = settings
    _registry["blob_store"] = blob_store


def registered_settings() -> Settings | None:
    return _registry["settings"]  # type: ignore[no-any-return]


def registered_blob_store() -> Any | None:
    return _registry["blob_store"]


# ── small helpers ────────────────────────────────────────────────────────────


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _chunks(items: Sequence[Any], size: int = _CHUNK) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield list(items[start:start + size])


def _rowcount(result: Any, fallback: int = 0) -> int:
    count = getattr(result, "rowcount", None)
    return count if isinstance(count, int) and count >= 0 else fallback


# ── memory opt-out gate ──────────────────────────────────────────────────────


async def memory_enabled_for(db: AsyncSession, *, tenant_id: str, user_id: str | None) -> bool:
    """Whether long-term memory may be read/written for ``user_id``.

    Fail-closed: the opt-out must never be bypassed by an outage, so a lookup
    error (or the SYSTEM sentinel / no user) answers ``False``.  A missing row
    means "defaults" → ``True``.  The lookup runs in a SAVEPOINT so a failure
    cannot poison the caller's transaction (run creation / completion carry on,
    just without memory).
    """
    if not user_id or user_id == "SYSTEM":
        return False
    try:
        async with db.begin_nested():
            value = (
                await db.execute(
                    select(UserPrivacySettings.memory_enabled).where(
                        UserPrivacySettings.tenant_id == tenant_id,
                        UserPrivacySettings.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 — fail closed: no memory when unsure
        logger.warning(
            "privacy settings lookup failed; treating memory as disabled",
            extra={"tenant_id": tenant_id, "user_id": user_id},
            exc_info=True,
        )
        return False
    return value is None or value is True


# ── blob hygiene ─────────────────────────────────────────────────────────────


async def _referenced_blob_keys(
    db: AsyncSession, *, tenant_id: str, keys: Sequence[str],
) -> set[str]:
    """Subset of ``keys`` still referenced by any attachment / file / version row."""
    referenced: set[str] = set()
    for chunk in _chunks(keys):
        for column, tenant_col in (
            (Attachment.blob_key, Attachment.tenant_id),
            (WorkspaceFile.blob_key, WorkspaceFile.tenant_id),
            (WorkspaceFileVersion.blob_key, WorkspaceFileVersion.tenant_id),
        ):
            rows = await db.execute(
                select(column).where(tenant_col == tenant_id, column.in_(chunk)).distinct()
            )
            referenced.update(str(k) for k in rows.scalars().all() if k)
    return referenced


async def _delete_blob_keys(blob_store: Any, keys: Sequence[str]) -> tuple[int, list[str]]:
    """Delete ``keys`` off the event loop with bounded concurrency.

    Returns ``(deleted, failed_keys)``.  S3 ``DeleteObject`` is idempotent, so
    an already-missing key counts as deleted.
    """
    semaphore = asyncio.Semaphore(_BLOB_DELETE_CONCURRENCY)
    failed: list[str] = []

    async def _one(key: str) -> None:
        async with semaphore:
            try:
                await asyncio.to_thread(blob_store.delete, key)
            except Exception as exc:  # noqa: BLE001 — collected, reported by caller
                failed.append(key)
                logger.warning("blob delete failed: %s", exc, extra={"blob_key": key})

    await asyncio.gather(*(_one(k) for k in keys))
    return len(keys) - len(failed), failed


async def delete_unreferenced_blobs(
    db: AsyncSession,
    *,
    tenant_id: str,
    keys: Iterable[str],
    blob_store: Any | None = None,
    strict: bool = False,
) -> int:
    """Delete the S3 objects in ``keys`` that no row references any more.

    Call it *after* the rows that referenced ``keys`` were deleted and flushed
    in the same transaction, so the reference check sees the post-delete
    state.  ``blob_store`` defaults to the registered app blob store.

    * ``strict=False`` (eviction, rewind) — best-effort: errors are logged,
      nothing raises, and a failed reference check deletes nothing.
    * ``strict=True`` (erasure, trash purge) — a missing blob store, a failed
      reference check or any failed delete raises, so the caller's transaction
      rolls back and the purge can be retried.

    Returns the number of objects deleted.
    """
    candidates = sorted({str(k) for k in keys if k})
    if not candidates:
        return 0
    store = blob_store if blob_store is not None else registered_blob_store()
    if store is None:
        if strict:
            raise PrivacyBlobStoreError("blob store not configured; cannot delete stored files")
        logger.warning("blob store not configured; %d blob(s) left in place", len(candidates))
        return 0

    try:
        async with db.begin_nested():
            referenced = await _referenced_blob_keys(db, tenant_id=tenant_id, keys=candidates)
    except Exception:
        if strict:
            raise
        logger.warning(
            "blob reference check failed; keeping %d blob(s)", len(candidates), exc_info=True,
        )
        return 0

    to_delete = [k for k in candidates if k not in referenced]
    deleted, failed = await _delete_blob_keys(store, to_delete)
    if failed and strict:
        raise PrivacyBlobStoreError(f"{len(failed)} stored file(s) could not be deleted")
    return deleted


# ── knowledge-ingest memory + Redis + scheduler side effects ─────────────────


def _knowledge_ingest_url(explicit: str | None) -> str:
    if explicit is not None:
        return explicit
    settings = registered_settings()
    return settings.knowledge_ingest_url if settings is not None else ""


async def delete_session_memories(
    *,
    tenant_id: str,
    session_id: str,
    owner_user_id: str | None,
    as_admin: bool = False,
    knowledge_ingest_url: str | None = None,
) -> bool:
    """Best-effort ``DELETE {ki}/memories?origin_session_id=<sid>``.

    The memory docs derived from a session go with it.  ``X-User-Id`` is the
    session owner, so knowledge-ingest's owner-scoped delete removes the
    owner's docs; ``as_admin`` (system- or admin-initiated deletes) adds
    ``X-Actor: ADMIN``, which widens the purge to every memory doc of that
    session regardless of owner.  Never raises; returns whether ki answered 2xx.
    """
    base_url = _knowledge_ingest_url(knowledge_ingest_url)
    if not base_url or not session_id:
        return False
    headers = {"X-Tenant-Id": tenant_id, "X-User-Id": owner_user_id or "SYSTEM"}
    if as_admin:
        headers["X-Actor"] = "ADMIN"
    try:
        async with httpx.AsyncClient(timeout=_KI_TIMEOUT) as client:
            resp = await client.delete(
                f"{base_url}/memories",
                params={"origin_session_id": session_id},
                headers=headers,
            )
    except Exception as exc:  # noqa: BLE001 — never fail the delete on it
        logger.warning(
            "memory purge failed (best-effort): %s", type(exc).__name__,
            extra={"tenant_id": tenant_id, "session_id": session_id},
        )
        return False
    if not 200 <= resp.status_code < 300:
        logger.warning(
            "memory purge returned HTTP %s (best-effort)", resp.status_code,
            extra={"tenant_id": tenant_id, "session_id": session_id},
        )
        return False
    return True


async def _delete_memories_for_sessions(
    *,
    tenant_id: str,
    session_owners: dict[str, str],
    as_admin: bool,
    knowledge_ingest_url: str | None,
) -> None:
    semaphore = asyncio.Semaphore(_KI_CONCURRENCY)

    async def _one(session_id: str, owner: str) -> None:
        async with semaphore:
            await delete_session_memories(
                tenant_id=tenant_id,
                session_id=session_id,
                owner_user_id=owner,
                as_admin=as_admin,
                knowledge_ingest_url=knowledge_ingest_url,
            )

    await asyncio.gather(*(_one(sid, uid) for sid, uid in session_owners.items()))


async def delete_run_redis_keys(
    redis_url: str | None, run_ids: Sequence[str], *, scan: bool = False,
) -> int:
    """Best-effort delete of ``agent:run:{run_id}:*`` for every run in ``run_ids``.

    Known key families are deleted directly; ``scan=True`` additionally walks
    ``agent:run:*`` once to catch any other suffix.  Returns keys deleted.
    """
    if not redis_url or not run_ids:
        return 0
    try:
        import redis.asyncio as aioredis

        client = aioredis.Redis.from_url(
            redis_url, socket_connect_timeout=2, socket_timeout=5, decode_responses=True,
        )
        try:
            deleted = 0
            keys = [f"agent:run:{rid}:{suffix}" for rid in run_ids for suffix in _RUN_KEY_SUFFIXES]
            for chunk in _chunks(keys):
                deleted += int(await client.delete(*chunk) or 0)
            if scan:
                wanted = set(run_ids)
                extra: list[str] = []
                async for key in client.scan_iter(match="agent:run:*", count=1000):
                    parts = str(key).split(":", 3)
                    if len(parts) == 4 and parts[2] in wanted and parts[3] not in _RUN_KEY_SUFFIXES:
                        extra.append(str(key))
                for chunk in _chunks(extra):
                    deleted += int(await client.delete(*chunk) or 0)
            return deleted
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001 — hot state expires on its own TTL
        logger.warning("redis run-state cleanup failed (best-effort): %s", type(exc).__name__)
        return 0


async def _cancel_scheduler_runs(
    *,
    scheduler_url: str,
    tenant_id: str,
    user_id: str,
    runs: Sequence[tuple[str, str | None, str]],
    as_admin: bool,
) -> int:
    """POST ``{scheduler}/runs/{sandbox_id or run_id}/cancel`` per run (best-effort).

    Same path and headers as ``runs._do_cancel_run``; legacy inline runs have
    no sandbox and are skipped.  Returns the number of 2xx answers.
    """
    targets = [(rid, sid) for rid, sid, mode in runs if mode != "inline"]
    if not scheduler_url or not targets:
        return 0
    headers = {"X-Tenant-Id": tenant_id, "X-User-Id": user_id}
    if as_admin:
        headers["X-Actor"] = "ADMIN"
    cancelled = 0
    async with httpx.AsyncClient(timeout=_SCHEDULER_CANCEL_TIMEOUT) as client:
        for run_id, sandbox_id in targets:
            try:
                resp = await client.post(
                    f"{scheduler_url}/runs/{sandbox_id or run_id}/cancel", headers=headers,
                )
            except httpx.HTTPError:
                logger.warning("erase: scheduler cancel failed", extra={"run_id": run_id})
                continue
            if 200 <= resp.status_code < 300:
                cancelled += 1
            else:
                logger.warning(
                    "erase: scheduler cancel returned HTTP %s", resp.status_code,
                    extra={"run_id": run_id},
                )
    return cancelled


def _subject_sessions(tenant_id: str, user_id: str, settings: Settings | None) -> Any:
    """Sessions that are ``user_id``'s data: their own, and runs started for them.

    A scheduled run executes as a service account, so its session is owned by
    that account and tagged ``metadata.on_behalf_of_user_id``.  The tag is
    trusted only on sessions a configured service account owns — anyone can
    write metadata on their own sessions, so a tag elsewhere proves nothing.
    """
    own = Session.user_id == user_id
    principals = [p for p in (settings.on_behalf_principal_user_ids if settings else []) if p]
    if not principals:
        return and_(Session.tenant_id == tenant_id, own)
    on_behalf = and_(
        Session.user_id.in_(principals),
        Session.agent_metadata["on_behalf_of_user_id"].as_string() == user_id,
    )
    return and_(Session.tenant_id == tenant_id, or_(own, on_behalf))


# ── purge core ───────────────────────────────────────────────────────────────

_COUNT_KEYS = (
    "sessions", "messages", "runs", "attachments", "canvases", "canvas_versions",
    "canvas_suggestions", "workspaces", "workspace_files", "workspace_file_versions",
)


@dataclass
class PurgeResult:
    """Outcome of :meth:`PrivacyService.purge` (dry-run or real)."""

    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_COUNT_KEYS, 0))
    # Candidate S3 keys of every purged attachment / workspace file / version.
    blob_keys: set[str] = field(default_factory=set)
    run_ids: list[str] = field(default_factory=list)
    # session_id → owner user_id (for the knowledge-ingest memory purge).
    session_owners: dict[str, str] = field(default_factory=dict)

    def add(self, key: str, n: int) -> None:
        self.counts[key] = self.counts.get(key, 0) + n


class PrivacyService:
    def __init__(self, db: AsyncSession, *, tenant_id: str) -> None:
        self._db = db
        self._tenant_id = tenant_id

    async def _scalars(self, stmt: Any) -> list[Any]:
        return list((await self._db.execute(stmt)).scalars().all())

    async def _rows(self, stmt: Any) -> list[Any]:
        return list((await self._db.execute(stmt)).all())

    # ------------------------------------------------------------- settings

    async def get_privacy_settings(self, user_id: str) -> dict[str, Any]:
        row = (
            await self._db.execute(
                select(UserPrivacySettings).where(
                    UserPrivacySettings.tenant_id == self._tenant_id,
                    UserPrivacySettings.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return {"memory_enabled": True, "updated_at": None}
        return {"memory_enabled": bool(row.memory_enabled), "updated_at": _iso(row.updated_at)}

    async def set_privacy_settings(self, user_id: str, *, memory_enabled: bool) -> dict[str, Any]:
        now = _now()
        stmt = (
            pg_insert(UserPrivacySettings)
            .values(
                tenant_id=self._tenant_id,
                user_id=user_id,
                memory_enabled=memory_enabled,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[UserPrivacySettings.tenant_id, UserPrivacySettings.user_id],
                set_={"memory_enabled": memory_enabled, "updated_at": now},
            )
            .returning(UserPrivacySettings.memory_enabled, UserPrivacySettings.updated_at)
        )
        enabled, updated_at = (await self._db.execute(stmt)).one()
        return {"memory_enabled": bool(enabled), "updated_at": _iso(updated_at)}

    # --------------------------------------------------------------- export

    async def export_user_data(
        self, user_id: str, *, settings: Settings | None = None,
    ) -> dict[str, Any]:
        """Everything this service holds about ``user_id``, soft-deleted rows included.

        Sessions include those a service account ran on the user's behalf.
        """
        tid = self._tenant_id
        sessions = await self._scalars(
            select(Session)
            .where(_subject_sessions(tid, user_id, settings or registered_settings()))
            .order_by(Session.created_at, Session.id)
        )
        session_ids = [s.id for s in sessions]

        messages: dict[str, list[dict[str, Any]]] = {sid: [] for sid in session_ids}
        runs: dict[str, list[dict[str, Any]]] = {sid: [] for sid in session_ids}
        attachments: dict[str, list[dict[str, Any]]] = {sid: [] for sid in session_ids}
        canvases: dict[str, list[dict[str, Any]]] = {sid: [] for sid in session_ids}
        canvas_by_id: dict[str, dict[str, Any]] = {}
        for chunk in _chunks(session_ids):
            for m in await self._scalars(
                select(Message)
                .where(Message.tenant_id == tid, Message.session_id.in_(chunk))
                .order_by(Message.session_id, Message.sequence)
            ):
                messages[m.session_id].append(_message_out(m))
            for r in await self._scalars(
                select(Run)
                .where(Run.tenant_id == tid, Run.session_id.in_(chunk))
                .order_by(Run.session_id, Run.created_at)
            ):
                runs[r.session_id].append(_run_out(r))
            for a in await self._scalars(
                select(Attachment)
                .where(Attachment.tenant_id == tid, Attachment.session_id.in_(chunk))
                .order_by(Attachment.session_id, Attachment.created_at)
            ):
                attachments[a.session_id].append(_attachment_out(a))
            for c in await self._scalars(
                select(CanvasDocument)
                .where(CanvasDocument.tenant_id == tid, CanvasDocument.session_id.in_(chunk))
                .order_by(CanvasDocument.session_id, CanvasDocument.created_at)
            ):
                out = _canvas_out(c)
                canvases[c.session_id].append(out)
                canvas_by_id[c.id] = out

        for chunk in _chunks(list(canvas_by_id)):
            for v in await self._scalars(
                select(CanvasVersion)
                .where(CanvasVersion.tenant_id == tid, CanvasVersion.canvas_id.in_(chunk))
                .order_by(CanvasVersion.canvas_id, CanvasVersion.version)
            ):
                canvas_by_id[v.canvas_id]["versions"].append(_canvas_version_out(v))
            for sg in await self._scalars(
                select(CanvasSuggestion)
                .where(CanvasSuggestion.tenant_id == tid, CanvasSuggestion.canvas_id.in_(chunk))
                .order_by(CanvasSuggestion.canvas_id, CanvasSuggestion.created_at)
            ):
                canvas_by_id[sg.canvas_id]["suggestions"].append(_canvas_suggestion_out(sg))

        workspaces = await self._scalars(
            select(Workspace)
            .where(Workspace.tenant_id == tid, Workspace.user_id == user_id)
            .order_by(Workspace.created_at, Workspace.id)
        )
        files: dict[str, list[dict[str, Any]]] = {w.id: [] for w in workspaces}
        file_by_id: dict[str, dict[str, Any]] = {}
        for chunk in _chunks([w.id for w in workspaces]):
            for f in await self._scalars(
                select(WorkspaceFile)
                .where(WorkspaceFile.tenant_id == tid, WorkspaceFile.workspace_id.in_(chunk))
                .order_by(WorkspaceFile.workspace_id, WorkspaceFile.created_at)
            ):
                out = _workspace_file_out(f)
                files[f.workspace_id].append(out)
                file_by_id[f.id] = out
        for chunk in _chunks(list(file_by_id)):
            for fv in await self._scalars(
                select(WorkspaceFileVersion)
                .where(
                    WorkspaceFileVersion.tenant_id == tid,
                    WorkspaceFileVersion.workspace_file_id.in_(chunk),
                )
                .order_by(WorkspaceFileVersion.workspace_file_id, WorkspaceFileVersion.version)
            ):
                file_by_id[fv.workspace_file_id]["versions"].append(_file_version_out(fv))

        privacy = await self.get_privacy_settings(user_id)
        return {
            "subject_user_id": user_id,
            "generated_at": _now().isoformat(),
            "sessions": [
                {
                    **_session_out(s),
                    "messages": messages[s.id],
                    "runs": runs[s.id],
                    "attachments": attachments[s.id],
                    "canvases": canvases[s.id],
                }
                for s in sessions
            ],
            "workspaces": [{**_workspace_out(w), "files": files[w.id]} for w in workspaces],
            "privacy_settings": {"memory_enabled": privacy["memory_enabled"]},
        }

    # ---------------------------------------------------------------- purge

    async def purge(
        self,
        *,
        session_ids: Sequence[str] = (),
        workspace_ids: Sequence[str] = (),
        canvas_ids: Sequence[str] = (),
        dry_run: bool = False,
    ) -> PurgeResult:
        """HARD-delete sessions (+ children), workspaces (+ files, versions) and
        individually soft-deleted canvases (+ versions, suggestions).

        Deletes run in FK order (suggestion → version → canvas → message /
        attachment / run → session; version → file → workspace) and key every
        child table on its parent id, so rows created concurrently under a
        purged parent go too.  ``RETURNING`` collects the S3 keys of the rows
        actually deleted; the caller removes those blobs with
        :func:`delete_unreferenced_blobs`.  ``dry_run`` only counts (its
        ``blob_keys`` is the candidate set, not reference-checked).

        Never commits; flushes before returning.
        """
        result = PurgeResult()
        tid = self._tenant_id
        session_ids = sorted(set(session_ids))
        workspace_ids = sorted(set(workspace_ids))
        canvas_ids = sorted(set(canvas_ids))

        for chunk in _chunks(session_ids):
            for sid, owner in await self._rows(
                select(Session.id, Session.user_id).where(
                    Session.tenant_id == tid, Session.id.in_(chunk),
                )
            ):
                result.session_owners[sid] = owner
            result.run_ids.extend(
                await self._scalars(
                    select(Run.id).where(Run.tenant_id == tid, Run.session_id.in_(chunk))
                )
            )

        if dry_run:
            await self._count_purge(result, session_ids, workspace_ids, canvas_ids)
            return result

        for chunk in _chunks(canvas_ids):
            await self._delete_canvases(result, CanvasDocument.id.in_(chunk))
        for chunk in _chunks(session_ids):
            await self._delete_canvases(result, CanvasDocument.session_id.in_(chunk))
            res = await self._db.execute(
                delete(Message)
                .where(Message.tenant_id == tid, Message.session_id.in_(chunk))
                .execution_options(synchronize_session=False)
            )
            result.add("messages", _rowcount(res))
            keys = await self._scalars(
                delete(Attachment)
                .where(Attachment.tenant_id == tid, Attachment.session_id.in_(chunk))
                .returning(Attachment.blob_key)
                .execution_options(synchronize_session=False)
            )
            result.add("attachments", len(keys))
            result.blob_keys.update(k for k in keys if k)
            res = await self._db.execute(
                delete(Run)
                .where(Run.tenant_id == tid, Run.session_id.in_(chunk))
                .execution_options(synchronize_session=False)
            )
            result.add("runs", _rowcount(res))
            res = await self._db.execute(
                delete(Session)
                .where(Session.tenant_id == tid, Session.id.in_(chunk))
                .execution_options(synchronize_session=False)
            )
            result.add("sessions", _rowcount(res))

        for chunk in _chunks(workspace_ids):
            file_ids = select(WorkspaceFile.id).where(
                WorkspaceFile.tenant_id == tid, WorkspaceFile.workspace_id.in_(chunk),
            )
            keys = await self._scalars(
                delete(WorkspaceFileVersion)
                .where(
                    WorkspaceFileVersion.tenant_id == tid,
                    WorkspaceFileVersion.workspace_file_id.in_(file_ids),
                )
                .returning(WorkspaceFileVersion.blob_key)
                .execution_options(synchronize_session=False)
            )
            result.add("workspace_file_versions", len(keys))
            result.blob_keys.update(k for k in keys if k)
            keys = await self._scalars(
                delete(WorkspaceFile)
                .where(WorkspaceFile.tenant_id == tid, WorkspaceFile.workspace_id.in_(chunk))
                .returning(WorkspaceFile.blob_key)
                .execution_options(synchronize_session=False)
            )
            result.add("workspace_files", len(keys))
            result.blob_keys.update(k for k in keys if k)
            res = await self._db.execute(
                delete(Workspace)
                .where(Workspace.tenant_id == tid, Workspace.id.in_(chunk))
                .execution_options(synchronize_session=False)
            )
            result.add("workspaces", _rowcount(res))

        await self._db.flush()
        return result

    async def _delete_canvases(self, result: PurgeResult, criterion: Any) -> None:
        tid = self._tenant_id
        canvas_ids = select(CanvasDocument.id).where(CanvasDocument.tenant_id == tid, criterion)
        res = await self._db.execute(
            delete(CanvasSuggestion)
            .where(CanvasSuggestion.tenant_id == tid, CanvasSuggestion.canvas_id.in_(canvas_ids))
            .execution_options(synchronize_session=False)
        )
        result.add("canvas_suggestions", _rowcount(res))
        res = await self._db.execute(
            delete(CanvasVersion)
            .where(CanvasVersion.tenant_id == tid, CanvasVersion.canvas_id.in_(canvas_ids))
            .execution_options(synchronize_session=False)
        )
        result.add("canvas_versions", _rowcount(res))
        res = await self._db.execute(
            delete(CanvasDocument)
            .where(CanvasDocument.tenant_id == tid, criterion)
            .execution_options(synchronize_session=False)
        )
        result.add("canvases", _rowcount(res))

    async def _count(self, model: Any, *criteria: Any) -> int:
        stmt = select(func.count()).select_from(model).where(model.tenant_id == self._tenant_id)
        return int((await self._db.execute(stmt.where(*criteria))).scalar_one() or 0)

    async def _count_purge(
        self,
        result: PurgeResult,
        session_ids: Sequence[str],
        workspace_ids: Sequence[str],
        canvas_ids: Sequence[str],
    ) -> None:
        tid = self._tenant_id
        canvas_set: set[str] = set()
        for chunk in _chunks(canvas_ids):
            canvas_set.update(
                await self._scalars(
                    select(CanvasDocument.id).where(
                        CanvasDocument.tenant_id == tid, CanvasDocument.id.in_(chunk),
                    )
                )
            )
        for chunk in _chunks(session_ids):
            canvas_set.update(
                await self._scalars(
                    select(CanvasDocument.id).where(
                        CanvasDocument.tenant_id == tid, CanvasDocument.session_id.in_(chunk),
                    )
                )
            )
            result.add("messages", await self._count(Message, Message.session_id.in_(chunk)))
            keys = await self._scalars(
                select(Attachment.blob_key).where(
                    Attachment.tenant_id == tid, Attachment.session_id.in_(chunk),
                )
            )
            result.add("attachments", len(keys))
            result.blob_keys.update(k for k in keys if k)
        result.add("sessions", len(result.session_owners))
        result.add("runs", len(result.run_ids))
        result.add("canvases", len(canvas_set))
        for chunk in _chunks(sorted(canvas_set)):
            result.add(
                "canvas_versions",
                await self._count(CanvasVersion, CanvasVersion.canvas_id.in_(chunk)),
            )
            result.add(
                "canvas_suggestions",
                await self._count(CanvasSuggestion, CanvasSuggestion.canvas_id.in_(chunk)),
            )
        for chunk in _chunks(workspace_ids):
            result.add("workspaces", await self._count(Workspace, Workspace.id.in_(chunk)))
            files = await self._rows(
                select(WorkspaceFile.id, WorkspaceFile.blob_key).where(
                    WorkspaceFile.tenant_id == tid, WorkspaceFile.workspace_id.in_(chunk),
                )
            )
            result.add("workspace_files", len(files))
            result.blob_keys.update(key for _, key in files if key)
            for file_chunk in _chunks([fid for fid, _ in files]):
                keys = await self._scalars(
                    select(WorkspaceFileVersion.blob_key).where(
                        WorkspaceFileVersion.tenant_id == tid,
                        WorkspaceFileVersion.workspace_file_id.in_(file_chunk),
                    )
                )
                result.add("workspace_file_versions", len(keys))
                result.blob_keys.update(k for k in keys if k)

    # ---------------------------------------------------------------- erase

    async def erase_user_data(
        self,
        user_id: str,
        *,
        dry_run: bool = False,
        blob_store: Any | None = None,
        settings: Settings | None = None,
        actor_user_id: str | None = None,
        as_admin: bool = False,
    ) -> dict[str, Any]:
        """Right to erasure: hard-delete everything this service holds about ``user_id``.

        Order: cancel active runs (best-effort) → purge rows (FK order) →
        delete the now-unreferenced S3 objects (strict: a failure raises and
        the caller's transaction rolls back, so a retry finishes the job) →
        drop the privacy-settings row → best-effort knowledge-ingest memory
        purge per session and Redis hot-state cleanup.  Idempotent: a second
        call finds nothing and reports zeros.

        Sessions a service account ran on the user's behalf go with theirs
        (:func:`_subject_sessions`).  ``dry_run`` deletes and cancels nothing;
        ``runs_cancelled`` then counts the runs that *would* be cancelled and
        ``blobs`` the candidate objects.
        """
        settings = settings or registered_settings()
        tid = self._tenant_id
        session_ids = await self._scalars(
            select(Session.id).where(_subject_sessions(tid, user_id, settings))
        )
        workspace_ids = await self._scalars(
            select(Workspace.id).where(Workspace.tenant_id == tid, Workspace.user_id == user_id)
        )
        active_runs: list[tuple[str, str | None, str]] = []
        for chunk in _chunks(session_ids):
            active_runs.extend(
                (rid, sid, mode)
                for rid, sid, mode in await self._rows(
                    select(Run.id, Run.sandbox_id, Run.execution_mode).where(
                        Run.tenant_id == tid,
                        Run.session_id.in_(chunk),
                        Run.status.in_(_ACTIVE_RUN_STATUSES),
                    )
                )
            )

        if dry_run:
            purge = await self.purge(
                session_ids=session_ids, workspace_ids=workspace_ids, dry_run=True,
            )
            return _erase_response(
                user_id,
                dry_run=True,
                counts=purge.counts,
                blobs=len(purge.blob_keys),
                runs_cancelled=sum(1 for _, _, mode in active_runs if mode != "inline"),
            )

        runs_cancelled = await _cancel_scheduler_runs(
            scheduler_url=settings.agent_scheduler_url if settings else "",
            tenant_id=tid,
            user_id=actor_user_id or user_id,
            runs=active_runs,
            as_admin=as_admin,
        )
        purge = await self.purge(session_ids=session_ids, workspace_ids=workspace_ids)
        blobs = await delete_unreferenced_blobs(
            self._db, tenant_id=tid, keys=purge.blob_keys, blob_store=blob_store, strict=True,
        )
        # An opt-out survives: it is the record that keeps memory off for a user
        # who goes on using the account (dropping it would silently re-enable
        # memory).  A row that only says "enabled" equals the default and goes.
        await self._db.execute(
            delete(UserPrivacySettings)
            .where(
                UserPrivacySettings.tenant_id == tid,
                UserPrivacySettings.user_id == user_id,
                UserPrivacySettings.memory_enabled.is_(True),
            )
            .execution_options(synchronize_session=False)
        )
        await self._db.flush()

        await _delete_memories_for_sessions(
            tenant_id=tid,
            session_owners=purge.session_owners,
            as_admin=as_admin,
            knowledge_ingest_url=settings.knowledge_ingest_url if settings else None,
        )
        await delete_run_redis_keys(
            settings.redis_url if settings else None, purge.run_ids, scan=True,
        )
        logger.info(
            "privacy erase completed",
            extra={
                "tenant_id": tid,
                "subject_user_id": user_id,
                "deleted": purge.counts,
                "blobs": blobs,
                "runs_cancelled": runs_cancelled,
            },
        )
        return _erase_response(
            user_id, dry_run=False, counts=purge.counts, blobs=blobs,
            runs_cancelled=runs_cancelled,
        )


def _erase_response(
    user_id: str, *, dry_run: bool, counts: dict[str, int], blobs: int, runs_cancelled: int,
) -> dict[str, Any]:
    return {
        "subject_user_id": user_id,
        "dry_run": dry_run,
        "deleted": {**counts, "blobs": blobs},
        "runs_cancelled": runs_cancelled,
    }


# ── trash purge (background) ─────────────────────────────────────────────────


def _split_scan_row(row: Any) -> tuple[str, str] | None:
    tenant_id, _, item_id = str(row).partition("\x1f")
    if not tenant_id or not item_id:
        logger.warning("trash purge: malformed scan row %r — skipping", row)
        return None
    return tenant_id, item_id


async def purge_trash(
    settings: Settings, *, grace_days: int, blob_store: Any | None = None,
) -> dict[str, int]:
    """Physically delete soft-deleted data older than ``grace_days`` (all tenants).

    Covers soft-deleted sessions (+ messages, runs, attachments, canvases),
    soft-deleted workspaces (+ files, versions) and canvases soft-deleted on
    their own inside a live session — each through :meth:`PrivacyService.purge`
    plus strict blob deletion, i.e. the erasure path.  Cross-tenant *scans* run
    on the migrator connection (:func:`privileged_scalars`); every delete runs
    in its own tenant-bound transaction, so one failing item is logged, rolled
    back and retried on the next sweep.  A negative ``grace_days`` disables it.

    Returns per-kind counts of purged items.
    """
    stats = {"sessions": 0, "workspaces": 0, "canvases": 0, "failed": 0}
    if grace_days < 0:
        return stats
    cutoff = _now() - dt.timedelta(days=grace_days)
    scans = (
        (
            "sessions",
            "SELECT tenant_id || '\x1f' || id FROM session_orchestrator.session"
            " WHERE deleted_at IS NOT NULL AND deleted_at < :cutoff ORDER BY tenant_id",
        ),
        (
            "workspaces",
            "SELECT tenant_id || '\x1f' || id FROM session_orchestrator.workspace"
            " WHERE deleted_at IS NOT NULL AND deleted_at < :cutoff ORDER BY tenant_id",
        ),
        (
            "canvases",
            "SELECT c.tenant_id || '\x1f' || c.id FROM session_orchestrator.canvas_document c"
            " JOIN session_orchestrator.session s ON s.id = c.session_id"
            " WHERE c.deleted_at IS NOT NULL AND c.deleted_at < :cutoff"
            " AND s.deleted_at IS NULL ORDER BY c.tenant_id",
        ),
    )
    for kind, sql in scans:
        for row in await privileged_scalars(settings, sql, {"cutoff": cutoff}):
            parsed = _split_scan_row(row)
            if parsed is None:
                continue
            tenant_id, item_id = parsed
            try:
                purge = await _purge_one(kind, tenant_id, item_id, blob_store=blob_store)
            except Exception:  # noqa: BLE001 — one bad item must not abort the sweep
                stats["failed"] += 1
                logger.exception(
                    "trash purge: failed to purge %s", kind[:-1],
                    extra={"tenant_id": tenant_id, "item_id": item_id},
                )
                continue
            stats[kind] += 1
            if purge.session_owners:
                await _delete_memories_for_sessions(
                    tenant_id=tenant_id,
                    session_owners=purge.session_owners,
                    as_admin=True,
                    knowledge_ingest_url=settings.knowledge_ingest_url,
                )
            await delete_run_redis_keys(settings.redis_url, purge.run_ids)

    logger.info(
        "trash purge: %d session(s), %d workspace(s), %d canvas(es) purged, %d failed",
        stats["sessions"], stats["workspaces"], stats["canvases"], stats["failed"],
    )
    return stats


async def _purge_one(
    kind: str, tenant_id: str, item_id: str, *, blob_store: Any | None,
) -> PurgeResult:
    purge = PurgeResult()
    async for db in get_session(tenant_id=tenant_id, user_id="SYSTEM", actor="SYSTEM"):
        svc = PrivacyService(db, tenant_id=tenant_id)
        purge = await svc.purge(
            session_ids=[item_id] if kind == "sessions" else (),
            workspace_ids=[item_id] if kind == "workspaces" else (),
            canvas_ids=[item_id] if kind == "canvases" else (),
        )
        await delete_unreferenced_blobs(
            db, tenant_id=tenant_id, keys=purge.blob_keys, blob_store=blob_store, strict=True,
        )
    return purge


# ── export serializers ───────────────────────────────────────────────────────


def _session_out(s: Session) -> dict[str, Any]:
    return {
        "id": s.id,
        "tenant_id": s.tenant_id,
        "user_id": s.user_id,
        "title": s.title,
        "status": s.status,
        "current_run_id": s.current_run_id,
        "workspace_id": s.workspace_id,
        "metadata": s.agent_metadata,
        "created_at": _iso(s.created_at),
        "updated_at": _iso(s.updated_at),
        "deleted_at": _iso(s.deleted_at),
    }


def _message_out(m: Message) -> dict[str, Any]:
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "tool_calls": m.tool_calls,
        "attachments": m.attachments,
        "run_id": m.run_id,
        "canvas_id": m.canvas_id,
        "sequence": m.sequence,
        "created_at": _iso(m.created_at),
    }


def _run_out(r: Run) -> dict[str, Any]:
    return {
        "id": r.id,
        "status": r.status,
        "task_description": r.task_description,
        "result_summary": r.result_summary,
        "error_message": r.error_message,
        "source": r.source,
        "execution_mode": r.execution_mode,
        "app_id": r.app_id,
        "app_snapshot": r.app_snapshot,
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "total_tokens": r.total_tokens,
        "started_at": _iso(r.started_at),
        "finished_at": _iso(r.finished_at),
        "created_at": _iso(r.created_at),
    }


def _attachment_out(a: Attachment) -> dict[str, Any]:
    return {
        "id": a.id,
        "filename": a.filename,
        "mime": a.mime,
        "size_bytes": a.size_bytes,
        "kind": a.kind,
        "created_at": _iso(a.created_at),
        "download_path": f"/sessions/{a.session_id}/files/{a.id}/content",
    }


def _canvas_out(c: CanvasDocument) -> dict[str, Any]:
    return {
        "id": c.id,
        "kind": c.kind,
        "title": c.title,
        "current_version": c.current_version,
        "source_message_id": c.source_message_id,
        "source_run_id": c.source_run_id,
        "created_at": _iso(c.created_at),
        "updated_at": _iso(c.updated_at),
        "deleted_at": _iso(c.deleted_at),
        "versions": [],
        "suggestions": [],
    }


def _canvas_version_out(v: CanvasVersion) -> dict[str, Any]:
    return {
        "id": v.id,
        "version": v.version,
        "blocks": v.blocks,
        "change_summary": v.change_summary,
        "created_by": v.created_by,
        "base_version": v.base_version,
        "created_at": _iso(v.created_at),
    }


def _canvas_suggestion_out(sg: CanvasSuggestion) -> dict[str, Any]:
    return {
        "id": sg.id,
        "base_version": sg.base_version,
        "status": sg.status,
        "instruction": sg.instruction,
        "diffs": sg.diffs,
        "change_summary": sg.change_summary,
        "created_at": _iso(sg.created_at),
        "resolved_at": _iso(sg.resolved_at),
    }


def _workspace_out(w: Workspace) -> dict[str, Any]:
    return {
        "id": w.id,
        "name": w.name,
        "description": w.description,
        "instructions": w.instructions,
        "skills": w.skills,
        "is_default": w.is_default,
        "meta": w.meta,
        "sensitivity_level": w.sensitivity_level,
        "created_at": _iso(w.created_at),
        "updated_at": _iso(w.updated_at),
        "deleted_at": _iso(w.deleted_at),
    }


def _workspace_file_out(f: WorkspaceFile) -> dict[str, Any]:
    return {
        "id": f.id,
        "filename": f.filename,
        "mime": f.mime,
        "size_bytes": f.size_bytes,
        "description": f.description,
        "sensitivity_level": f.sensitivity_level,
        "current_version": f.current_version,
        "created_at": _iso(f.created_at),
        "download_path": f"/workspaces/{f.workspace_id}/files/{f.id}/content",
        "versions": [],
    }


def _file_version_out(v: WorkspaceFileVersion) -> dict[str, Any]:
    return {
        "id": v.id,
        "version": v.version,
        "size_bytes": v.size_bytes,
        "change_summary": v.change_summary,
        "created_by": v.created_by,
        "created_at": _iso(v.created_at),
    }
