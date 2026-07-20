"""Canvas service — business logic for the canvas v2 entity layer.

System-of-record for canvas documents, versions, and suggestions (design §5.1).

Transaction discipline (identical to :class:`SessionService`): this service
NEVER calls ``commit()``.  Every write happens inside the request transaction
opened by ``domain.db.get_session`` (which installed ``SET LOCAL
app.tenant_id`` so RLS works).  It uses ``flush()`` to get server-generated
columns before the transaction ends.

Ownership: canvas rows are session-scoped, so every canvas lookup validates the
owning session via ``SessionService.get_session`` (which raises
``SessionNotFoundError`` for a missing / soft-deleted / cross-tenant session).
Tenant RLS is the backstop; the explicit ``tenant_id`` filters here are the
tighter application-level guard.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.domain.models import (
    CanvasDocument,
    CanvasSuggestion,
    CanvasVersion,
    Message,
)
from agent_session_orchestrator.errors import (
    CanvasBadRequestError,
    CanvasNotFoundError,
    CanvasSuggestionResolvedError,
    CanvasVersionConflictError,
    CanvasVersionNotFoundError,
)
from agent_session_orchestrator.services.canvas_blocks import (
    materialize_blocks,
    split_blocks,
)
from agent_session_orchestrator.services.session_service import SessionService


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def _default_title(blocks: list[dict]) -> str:
    """Derive a title from the first heading block, else the first paragraph.

    Falls back to a generic label so ``title`` (NOT NULL) is always set.
    Bounded to the column width (200).
    """
    for block in blocks:
        if block.get("type") == "heading":
            text = str(block.get("text") or "").lstrip("#").strip()
            if text:
                return text[:200]
    for block in blocks:
        text = str(block.get("text") or "").strip()
        if text:
            return text.split("\n", 1)[0][:200]
    return "未命名画布"


class CanvasService:
    def __init__(self, db: AsyncSession, *, tenant_id: str) -> None:
        self._db = db
        self._tenant_id = tenant_id
        self._sessions = SessionService(db, tenant_id=tenant_id)

    # ── lookups ──────────────────────────────────────────────────────────────

    async def _get_canvas(self, canvas_id: str) -> CanvasDocument:
        """Fetch a live (non-soft-deleted) canvas for this tenant, or raise."""
        stmt = select(CanvasDocument).where(
            CanvasDocument.id == canvas_id,
            CanvasDocument.tenant_id == self._tenant_id,
            CanvasDocument.deleted_at.is_(None),
        )
        result = await self._db.execute(stmt)
        canvas = result.scalar_one_or_none()
        if canvas is None:
            raise CanvasNotFoundError(canvas_id)
        return canvas

    async def _get_version(self, canvas_id: str, version: int) -> CanvasVersion:
        stmt = select(CanvasVersion).where(
            CanvasVersion.canvas_id == canvas_id,
            CanvasVersion.tenant_id == self._tenant_id,
            CanvasVersion.version == version,
        )
        result = await self._db.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            raise CanvasVersionNotFoundError(canvas_id, version)
        return row

    async def get_canvas(self, canvas_id: str) -> CanvasDocument:
        return await self._get_canvas(canvas_id)

    async def get_current_blocks(self, canvas: CanvasDocument) -> list[dict]:
        version = await self._get_version(canvas.id, canvas.current_version)
        return list(version.blocks or [])

    async def get_version(self, canvas_id: str, version: int) -> CanvasVersion:
        await self._get_canvas(canvas_id)
        return await self._get_version(canvas_id, version)

    async def list_canvases(self, session_id: str) -> list[CanvasDocument]:
        # Validate session ownership (raises for missing / cross-tenant).
        await self._sessions.get_session(session_id)
        stmt = (
            select(CanvasDocument)
            .where(
                CanvasDocument.session_id == session_id,
                CanvasDocument.tenant_id == self._tenant_id,
                CanvasDocument.deleted_at.is_(None),
            )
            .order_by(CanvasDocument.created_at.asc())
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def list_canvases_for_user(self, user_id: str) -> list[CanvasDocument]:
        """Return every live canvas owned by ``user_id`` in this tenant.

        Cross-session listing for the Artifacts panel (design §artifacts): no
        session ownership to validate, just a tenant + owner + not-soft-deleted
        predicate.  Tenant RLS already scopes to ``tenant_id``; the ``user_id``
        predicate is a strictly tighter filter on top (never crosses tenants).
        Ordered newest-first so the UI shows recent artifacts first.
        """
        stmt = (
            select(CanvasDocument)
            .where(
                CanvasDocument.tenant_id == self._tenant_id,
                CanvasDocument.user_id == user_id,
                CanvasDocument.deleted_at.is_(None),
            )
            .order_by(CanvasDocument.created_at.desc())
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def soft_delete(self, canvas_id: str) -> CanvasDocument:
        """Soft-delete a canvas (set ``deleted_at``); leaves versions in place.

        Used by the Artifacts panel's per-canvas delete.  Versions and
        suggestions are pruned on physical purge (retention sweep), not here —
        the soft-delete only flips the parent's ``deleted_at`` so subsequent
        reads (list / get / materialize) skip it.
        """
        canvas = await self._get_canvas(canvas_id)
        canvas.deleted_at = _now()
        await self._db.flush()
        return canvas

    async def list_canvases_for_run(self, run_id: str) -> list[CanvasDocument]:
        """Return live canvases materialized from ``run_id`` in creation order."""
        stmt = (
            select(CanvasDocument)
            .where(
                CanvasDocument.source_run_id == run_id,
                CanvasDocument.tenant_id == self._tenant_id,
                CanvasDocument.deleted_at.is_(None),
            )
            .order_by(CanvasDocument.created_at.asc())
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def count_canvases_for_run(self, run_id: str) -> int:
        """Count live canvases already materialized from ``run_id`` (Phase B).

        Used for idempotent materialization of the run's Redis canvas payloads:
        payloads carry a 1-based ``seq``; a payload is materialized only when
        ``seq`` exceeds the count of canvases already created for this run, so a
        replay (SSE reconnect, reconcile re-poll) never double-creates.  Includes
        soft-deleted rows would over-count and drop a re-create, so we count only
        live rows — matching what a fresh materialization would append after.
        """
        return len(await self.list_canvases_for_run(run_id))

    async def backfill_message_canvas_id(
        self, *, session_id: str, run_id: str, canvas_id: str
    ) -> str | None:
        """Point the run's assistant message at ``canvas_id`` (best-effort).

        Finds the latest assistant message for ``run_id`` in ``session_id`` that
        does not already carry a ``canvas_id`` and sets it, so the frontend can
        anchor the canvas card to that message.  Returns the message id it
        updated, or ``None`` when no eligible message exists yet (the assistant
        row may not be persisted at materialization time — the ``canvas_created``
        event still carries ``canvas_id`` so the card renders regardless).
        """
        # Idempotency guard: reconciliation calls this repeatedly.  If this
        # canvas is already anchored, return that message instead of assigning
        # the same canvas to another unlinked assistant row from the run.
        linked_stmt = (
            select(Message)
            .where(
                Message.session_id == session_id,
                Message.tenant_id == self._tenant_id,
                Message.run_id == run_id,
                Message.role == "assistant",
                Message.canvas_id == canvas_id,
            )
            .order_by(Message.sequence.desc())
        )
        linked_result = await self._db.execute(linked_stmt)
        linked = linked_result.scalars().first()
        if linked is not None:
            return linked.id

        stmt = (
            select(Message)
            .where(
                Message.session_id == session_id,
                Message.tenant_id == self._tenant_id,
                Message.run_id == run_id,
                Message.role == "assistant",
                Message.canvas_id.is_(None),
            )
            .order_by(Message.sequence.desc())
        )
        result = await self._db.execute(stmt)
        msg = result.scalars().first()
        if msg is None:
            return None
        msg.canvas_id = canvas_id
        await self._db.flush()
        return msg.id

    async def list_versions(self, canvas_id: str) -> list[CanvasVersion]:
        await self._get_canvas(canvas_id)
        stmt = (
            select(CanvasVersion)
            .where(
                CanvasVersion.canvas_id == canvas_id,
                CanvasVersion.tenant_id == self._tenant_id,
            )
            .order_by(CanvasVersion.version.asc())
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def get_suggestion(
        self, canvas_id: str, suggestion_id: str
    ) -> CanvasSuggestion:
        stmt = select(CanvasSuggestion).where(
            CanvasSuggestion.id == suggestion_id,
            CanvasSuggestion.canvas_id == canvas_id,
            CanvasSuggestion.tenant_id == self._tenant_id,
        )
        result = await self._db.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            # A missing suggestion under a valid canvas is a NOT_FOUND on the
            # canvas namespace; reuse the canvas code so the frontend routes it
            # to the same error surface.
            raise CanvasNotFoundError(suggestion_id)
        return row

    # ── create ───────────────────────────────────────────────────────────────

    async def create_canvas(
        self,
        session_id: str,
        *,
        user_id: str,
        source_message_id: str | None = None,
        content: str | None = None,
        kind: str = "article",
        title: str | None = None,
        source_run_id: str | None = None,
    ) -> CanvasDocument:
        """Materialize a canvas from a source message or supplied content.

        Validates session ownership first (raises ``SessionNotFoundError`` for a
        soft-deleted / cross-tenant / missing session — this is the "soft-del
        session ⇒ create rejected" guard).

        ``source_message_id`` and ``content`` are mutually exclusive; exactly
        one must be provided.  When ``source_message_id`` is given the server
        reads that message's content and backfills ``message.canvas_id``.

        ``source_run_id`` records the run whose ``create_canvas`` tool call
        produced this canvas (Phase B).  It is orthogonal to the
        source_message_id/content choice — both a run-triggered create (content
        + source_run_id) and a Phase A create (source_message_id) set it when
        available.
        """
        # Ownership + soft-delete guard.
        await self._sessions.get_session(session_id)

        if bool(source_message_id) == bool(content):
            raise CanvasBadRequestError(
                "exactly one of source_message_id / content is required",
                user_message="创建画布需要提供来源消息或内容其一。",
            )

        source_message: Message | None = None
        if source_message_id:
            source_message = await self._get_message(session_id, source_message_id)
            raw = source_message.content or ""
            blocks = materialize_blocks(raw)
        else:
            blocks = split_blocks(content or "")

        if not blocks:
            raise CanvasBadRequestError(
                "no content to materialize into a canvas",
                user_message="内容为空，无法创建画布。",
            )

        resolved_title = (title or "").strip() or _default_title(blocks)

        canvas = CanvasDocument(
            tenant_id=self._tenant_id,
            session_id=session_id,
            user_id=user_id,
            kind=kind,
            title=resolved_title,
            current_version=1,
            source_message_id=source_message_id,
            source_run_id=source_run_id,
        )
        self._db.add(canvas)
        await self._db.flush()  # populate canvas.id via PK default

        version = CanvasVersion(
            tenant_id=self._tenant_id,
            canvas_id=canvas.id,
            version=1,
            blocks=blocks,
            change_summary="初始版本",
            created_by="model" if source_message_id else "user",
            base_version=None,
        )
        self._db.add(version)

        # Backfill the source message's canvas_id so the frontend anchors the
        # card to that message (design §3.4).
        if source_message is not None:
            source_message.canvas_id = canvas.id

        await self._db.flush()
        await self._db.refresh(canvas)
        return canvas

    async def _get_message(self, session_id: str, message_id: str) -> Message:
        stmt = select(Message).where(
            Message.id == message_id,
            Message.session_id == session_id,
            Message.tenant_id == self._tenant_id,
        )
        result = await self._db.execute(stmt)
        msg = result.scalar_one_or_none()
        if msg is None:
            raise CanvasBadRequestError(
                f"source message {message_id} not found in session {session_id}",
                user_message="来源消息不存在。",
            )
        return msg

    # ── versions ─────────────────────────────────────────────────────────────

    async def create_manual_version(
        self,
        canvas_id: str,
        *,
        blocks: list[dict],
        base_version: int,
    ) -> CanvasDocument:
        """Persist a manually-edited full-blocks snapshot as a new version.

        Optimistic lock: ``base_version`` must equal the canvas'
        ``current_version`` or we raise 409 (design §5.1).
        """
        canvas = await self._get_canvas(canvas_id)
        if base_version != canvas.current_version:
            raise CanvasVersionConflictError(base_version, canvas.current_version)

        await self._append_version(
            canvas,
            blocks=blocks,
            change_summary="手动编辑",
            created_by="user",
            base_version=base_version,
        )
        await self._db.flush()
        await self._db.refresh(canvas)
        return canvas

    async def revert(self, canvas_id: str, *, to_version: int) -> CanvasDocument:
        """Append a new version copying the blocks of ``to_version`` (design §5.1)."""
        canvas = await self._get_canvas(canvas_id)
        source = await self._get_version(canvas_id, to_version)

        await self._append_version(
            canvas,
            blocks=list(source.blocks or []),
            change_summary=f"回退到版本 {to_version}",
            created_by="user",
            base_version=to_version,
        )
        await self._db.flush()
        await self._db.refresh(canvas)
        return canvas

    async def _append_version(
        self,
        canvas: CanvasDocument,
        *,
        blocks: list[dict],
        change_summary: str | None,
        created_by: str,
        base_version: int | None,
    ) -> CanvasVersion:
        """Append a new version and advance ``current_version`` (no commit)."""
        new_version = canvas.current_version + 1
        version = CanvasVersion(
            tenant_id=self._tenant_id,
            canvas_id=canvas.id,
            version=new_version,
            blocks=blocks,
            change_summary=change_summary,
            created_by=created_by,
            base_version=base_version,
        )
        self._db.add(version)
        canvas.current_version = new_version
        canvas.updated_at = _now()
        await self._db.flush()
        return version

    # ── suggestions ──────────────────────────────────────────────────────────

    async def create_suggestion(
        self,
        canvas_id: str,
        *,
        base_version: int,
        instruction: str,
        diffs: list[dict],
        change_summary: str | None = None,
    ) -> CanvasSuggestion:
        canvas = await self._get_canvas(canvas_id)
        suggestion = CanvasSuggestion(
            tenant_id=self._tenant_id,
            canvas_id=canvas.id,
            base_version=base_version,
            status="proposed",
            instruction=instruction,
            diffs=diffs,
            change_summary=change_summary,
        )
        self._db.add(suggestion)
        await self._db.flush()
        await self._db.refresh(suggestion)
        return suggestion

    async def accept_suggestion(
        self, canvas_id: str, suggestion_id: str
    ) -> CanvasDocument:
        """Apply a suggestion's diffs and append the resulting version.

        Single transaction (design §5.1):

        1. suggestion must be ``proposed`` (else SUGGESTION_RESOLVED 409);
        2. ``suggestion.base_version`` must equal ``canvas.current_version``
           (else VERSION_CONFLICT 409);
        3. apply diffs to the current blocks per the §4 id-stability rules;
        4. append the new version, advance ``current_version``;
        5. mark the suggestion ``accepted`` + ``resolved_at``.
        """
        canvas = await self._get_canvas(canvas_id)
        suggestion = await self.get_suggestion(canvas_id, suggestion_id)

        if suggestion.status != "proposed":
            raise CanvasSuggestionResolvedError(suggestion_id, suggestion.status)
        if suggestion.base_version != canvas.current_version:
            raise CanvasVersionConflictError(
                suggestion.base_version, canvas.current_version
            )

        current = await self._get_version(canvas_id, canvas.current_version)
        new_blocks = apply_diffs(list(current.blocks or []), list(suggestion.diffs or []))

        await self._append_version(
            canvas,
            blocks=new_blocks,
            change_summary=suggestion.change_summary,
            created_by="model",
            base_version=suggestion.base_version,
        )
        suggestion.status = "accepted"
        suggestion.resolved_at = _now()
        await self._db.flush()
        await self._db.refresh(canvas)
        return canvas

    async def reject_suggestion(
        self, canvas_id: str, suggestion_id: str
    ) -> CanvasSuggestion:
        await self._get_canvas(canvas_id)
        suggestion = await self.get_suggestion(canvas_id, suggestion_id)
        if suggestion.status != "proposed":
            raise CanvasSuggestionResolvedError(suggestion_id, suggestion.status)
        suggestion.status = "rejected"
        suggestion.resolved_at = _now()
        await self._db.flush()
        await self._db.refresh(suggestion)
        return suggestion


def apply_diffs(blocks: list[dict], diffs: list[dict]) -> list[dict]:
    """Apply suggestion diffs to a block list, honouring the §4 id rules.

    * ``replace`` — keep the block id, swap ``text`` to ``sugg``.
    * ``delete`` — remove the block.
    * ``insert_after`` — insert a new ``paragraph`` block (fresh ``blk_`` id)
      right after the target block.

    A diff whose ``block_id`` is not present in ``blocks`` is silently ignored
    (the target may have been removed by an earlier diff in the same batch);
    untouched block ids are preserved unchanged — the geometry the "model only
    generates, never locates" contract depends on.
    """
    result: list[dict] = []
    # Index diffs by block for O(1) lookup as we walk the ordered blocks.
    replace_by_id: dict[str, str] = {}
    delete_ids: set[str] = set()
    insert_after: dict[str, list[str]] = {}
    for diff in diffs:
        block_id = diff.get("block_id")
        op = diff.get("op")
        if not block_id or not op:
            continue
        if op == "replace":
            replace_by_id[block_id] = str(diff.get("sugg") or "")
        elif op == "delete":
            delete_ids.add(block_id)
        elif op == "insert_after":
            insert_after.setdefault(block_id, []).append(str(diff.get("sugg") or ""))

    for block in blocks:
        block_id = block.get("id")
        if block_id in delete_ids:
            # Dropped — but still honour insert_after anchored on it below.
            pass
        elif block_id in replace_by_id:
            new_text = replace_by_id[block_id]
            result.append(
                {
                    "id": block_id,
                    "type": _infer_type(new_text, fallback=block.get("type")),
                    "text": new_text,
                }
            )
        else:
            result.append(dict(block))
        for new_text in insert_after.get(block_id, []):
            result.append(
                {
                    "id": _new_block_id(),
                    "type": _infer_type(new_text, fallback="paragraph"),
                    "text": new_text,
                }
            )

    return result


def _infer_type(text: str, *, fallback: str | None) -> str:
    """Heading iff the (single-line) text is a markdown ATX heading."""
    from agent_session_orchestrator.services.canvas_blocks import _HEADING_RE

    stripped = (text or "").strip()
    if "\n" not in stripped and _HEADING_RE.match(stripped):
        return "heading"
    return fallback or "paragraph"


def _new_block_id() -> str:
    from agent_session_orchestrator.services.canvas_blocks import _blk_id

    return _blk_id()
